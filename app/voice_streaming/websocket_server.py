# -*- coding: utf-8 -*-
"""
语音面试WebSocket服务器
实现前端音频流 -> 阿里云ASR -> 本地大模型实时评价的完整链路
"""

import asyncio
import json
import uuid
import threading
import time
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path
import ssl
from concurrent.futures import ThreadPoolExecutor
import wave

import numpy as np
import websockets
from loguru import logger
import audioop

from config.settings import settings
from app.database.connection import DatabaseManager
from app.services.llm_service import LLMService

from .asr_client import AliyunASRClient
from .realtime_scorer import RealtimeScorer
from .token_manager import ASRTokenManager
from app.services.interview_session_service import interview_session_service

# 使用loguru logger


class VoiceInterviewWebSocketServer:
    """语音面试WebSocket服务器"""

    def __init__(self, config_manager, db_manager: DatabaseManager):
        self.config_manager = config_manager
        self.db_manager = db_manager
        
        # 初始化LLM服务
        from app.services.llm_service import LLMService
        self.llm_service = LLMService()

        # 获取配置
        voice_config = config_manager.get_config('voice_streaming')
        self.websocket_config = voice_config.get('websocket', {})
        self.audio_config = voice_config.get('audio', {})
        self.asr_config = voice_config.get('asr', {})
        self.recognition_config = voice_config.get('recognition', {})
        self.evaluation_config = voice_config.get('evaluation', {})
        self.streaming_config = voice_config.get('streaming_interview', {})

        # 音频参数与录音文件目录（完整录音）
        self.audio_sample_rate = self.audio_config.get('sample_rate', 16000)
        self.audio_channels = self.audio_config.get('channels', 1)
        self.audio_format = (self.audio_config.get('format') or 'wav').lower()
        # 统一块大小：约200ms音频，用于对齐写盘与发送（16kHz * 2字节 * 0.2s ≈ 6400字节）
        self.aligned_block_size_bytes = int(self.audio_sample_rate * 2 * 0.2)

        # 送 ASR 前的 PCM 预处理（与 voice_streaming.audio.pcm_upstream 对齐，默认关闭 RMS 门控）
        _pu = (self.audio_config or {}).get("pcm_upstream") or {}
        self._pcm_enable_dc = bool(_pu.get("enable_dc_removal", True))
        self._pcm_enable_rms_gate = bool(_pu.get("enable_rms_gate", False))
        self._pcm_rms_threshold = max(1, int(_pu.get("rms_gate_threshold", 80)))
        self._pcm_enable_edge_smooth = bool(_pu.get("enable_edge_micro_smooth", False))
        self._pcm_preprocess_any = (
            self._pcm_enable_dc or self._pcm_enable_rms_gate or self._pcm_enable_edge_smooth
        )
        logger.info(
            f"ASR 上行 PCM: dc_removal={self._pcm_enable_dc}, rms_gate={self._pcm_enable_rms_gate} "
            f"(threshold={self._pcm_rms_threshold}), edge_smooth={self._pcm_enable_edge_smooth}"
        )

        # 支持通过 settings.ANSWER_AUDIO_STORAGE_PATH 单独配置录音目录
        base_answer_audio_path = getattr(
            settings,
            "ANSWER_AUDIO_STORAGE_PATH",
            str(Path(settings.STORAGE_PATH) / "answer_audio"),
        )
        self.answer_audio_dir = Path(base_answer_audio_path)
        try:
            self.answer_audio_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"录音存储目录: {self.answer_audio_dir.resolve()}")
        except Exception as e:
            logger.error(f"创建录音存储目录失败: {self.answer_audio_dir}, 错误: {e}")

        # 合并ASR配置和识别参数配置
        asr_client_config = {
            **self.asr_config,
            'format': self.recognition_config.get('format', 'pcm'),
            'sample_rate': self.recognition_config.get('sample_rate', 16000),
            'endpoint': self.asr_config.get('endpoint', 'wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1')
        }

        # 初始化Token管理器（如果配置了AccessKey）
        token_manager = None
        access_key_id = settings.ALIYUN_ASR_ACCESS_KEY_ID
        access_key_secret = settings.ALIYUN_ASR_ACCESS_KEY_SECRET
        region = self.asr_config.get('region', 'cn-shanghai')
        
        if access_key_id and access_key_secret:
            # 使用动态Token管理器
            token_manager = ASRTokenManager(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                region=region
            )
            logger.info("使用动态Token管理器，Token将自动刷新")
        else:
            # 使用静态Token（从环境变量或配置）
            static_token = settings.ALIYUN_ASR_TOKEN
            if not static_token or static_token.startswith('${'):
                logger.warning("未配置Token或AccessKey，ASR功能可能无法使用")
            else:
                logger.info("使用静态Token，请定期手动更新")

        # 初始化组件 - 检查ASR配置
        appkey = self.asr_config.get('appkey')
        if not appkey:
            raise ValueError("ASR配置中缺少appkey，无法初始化ASR客户端")
        elif appkey.startswith('${'):
            raise ValueError("ASR配置中的appkey未正确设置环境变量，无法初始化ASR客户端")
        else:
            try:
                self.asr_client = AliyunASRClient(
                    appkey=appkey,
                    token=self.asr_config.get('token'),  # 静态Token（可选）
                    config=asr_client_config,
                    token_manager=token_manager  # Token管理器（优先使用）
                )
                logger.info("ASR客户端初始化成功")
            except Exception as e:
                raise ValueError(f"ASR客户端初始化失败: {str(e)}")

        self.realtime_scorer = RealtimeScorer(
            llm_service=self.llm_service,
            db_manager=self.db_manager,
            config={
                **self.evaluation_config,
                'streaming_interview': self.streaming_config,
            }
        )

        # 连接管理
        self.active_connections: Dict[str, Dict[str, Any]] = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        
        # 智能流式面试配置
        self.silence_threshold = self.streaming_config.get('silence_threshold', 3.0)  # 秒
        # 口语「下一题」易混入正常作答转写导致误切题，默认不再作为语音切题触发词；需要时见 enable_voice_next_question_keywords
        _default_phrase_keywords = [
            '回答完毕', '回答完了', '我的回答完了', '回答完成', '说完了',
        ]
        _next_q_tokens = ('下一题', '下一道题')
        _cfg_kw = self.streaming_config.get('completion_keywords')
        if _cfg_kw:
            self.phrase_completion_keywords = [k for k in _cfg_kw if k not in _next_q_tokens]
            _had_next_in_cfg = any(k in _cfg_kw for k in _next_q_tokens)
        else:
            self.phrase_completion_keywords = list(_default_phrase_keywords)
            _had_next_in_cfg = False
        # 兼容旧配置：若用户在 yaml 里写了「下一题」且未显式关闭，则仍允许语音触发（不推荐）
        self.enable_voice_next_question_keywords = self.streaming_config.get(
            'enable_voice_next_question_keywords',
            _had_next_in_cfg,
        )
        self.voice_next_question_keywords = self.streaming_config.get(
            'voice_next_question_keywords',
            ['下一题', '下一道题'],
        )
        self.min_chars_before_voice_next = int(self.streaming_config.get('min_chars_before_voice_next', 40))
        self.advance_cooldown_seconds = float(self.streaming_config.get('advance_cooldown_seconds', 5.0))
        self.asr_switch_ignore_seconds = max(
            0.0,
            float(self.streaming_config.get('asr_switch_ignore_seconds', 0.8)),
        )
        self.follow_up_tts_prefix = self.streaming_config.get(
            'follow_up_tts_prefix',
            '针对刚才的回答，我想进一步了解一下：',
        )
        self.enable_auto_advance = self.streaming_config.get('enable_auto_advance', True)
        self.min_answer_length = self.streaming_config.get('min_answer_length', 10)

        # 服务器状态
        self.server = None
        self.is_running = False

        logger.info("语音面试WebSocket服务器初始化完成")

    async def _audio_writer_worker(self, connection_id: str):
        """后台录音写入任务：从队列读取音频数据并写入磁盘"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        audio_queue = client_info.get('audio_queue')
        audio_writer = client_info.get('audio_writer')

        if not audio_queue or not audio_writer:
            return

        try:
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    audio_queue.task_done()
                    break

                try:
                    # 对齐机制已在 handle_audio_data 中保证块足额且 2 字节对齐，直接写入即可
                    audio_writer.writeframes(chunk)
                except Exception as e:
                    logger.error(f"后台录音写入失败: {str(e)}")
                finally:
                    audio_queue.task_done()
        except Exception as e:
            logger.error(f"录音写入任务异常结束: {str(e)}", exc_info=True)

    async def _process_aligned_chunk(self, connection_id: str, chunk: bytes):
        """
        优化后的处理逻辑：统一 NumPy 变换，避免 bytes/audioop 多次转换带来的微观不连续与节拍感。
        1. 统一 NumPy 转换
        2. 滚动均值去直流
        3. RMS 与状态切换淡入淡出（向量化，无手动字节循环）
        4. 全时边缘微平滑
        """
        client_info = self.active_connections.get(connection_id)
        if not client_info or not client_info.get('is_recording'):
            return

        if len(chunk) < 2 or len(chunk) % 2 != 0:
            logger.warning(f"[对齐块] 块长度异常，跳过: len={len(chunk)}")
            return

        try:
            if not self._pcm_preprocess_any:
                gated_audio = chunk
            else:
                # 1. 统一转换为 NumPy Float32
                audio_np = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)

                # 2. 滚动均值去直流（可选）
                if self._pcm_enable_dc:
                    block_mean = float(np.mean(audio_np))
                    running_dc = client_info.get("dc_running_mean")
                    if running_dc is None:
                        running_dc = block_mean
                    else:
                        running_dc = 0.95 * running_dc + 0.05 * block_mean
                    client_info["dc_running_mean"] = running_dc
                    audio_np = audio_np - running_dc

                # 3. RMS 门控（可选；关闭时与阿里云控制台「原样送识别」更接近）
                if self._pcm_enable_rms_gate:
                    rms = float(np.sqrt(np.mean(audio_np**2)))
                    threshold = float(self._pcm_rms_threshold)
                    FADE_LEN = 320  # 10ms @ 16kHz

                    is_speech = rms >= threshold
                    was_speech = client_info.get('prev_was_speech', True)

                    if not is_speech:
                        if was_speech and len(audio_np) >= FADE_LEN:
                            fade_out = np.linspace(1.0, 0.0, FADE_LEN, dtype=np.float32)
                            audio_np[-FADE_LEN:] *= fade_out
                            audio_np[:-FADE_LEN] = 0.0
                        else:
                            audio_np[:] = 0.0
                    else:
                        if not was_speech and len(audio_np) >= FADE_LEN:
                            fade_in = np.linspace(0.0, 1.0, FADE_LEN, dtype=np.float32)
                            audio_np[:FADE_LEN] *= fade_in

                    client_info['prev_was_speech'] = is_speech

                # 4. 块边缘微平滑（可选）
                if self._pcm_enable_edge_smooth:
                    MICRO_SAMPLES = 64
                    if len(audio_np) > MICRO_SAMPLES * 2:
                        micro_window = np.linspace(0.0, 1.0, MICRO_SAMPLES, dtype=np.float32)
                        audio_np[:MICRO_SAMPLES] *= micro_window
                        audio_np[-MICRO_SAMPLES:] *= micro_window[::-1]

                audio_np = np.clip(audio_np, -32768.0, 32767.0)
                gated_audio = audio_np.astype(np.int16).tobytes()

            # 7. 放入队列
            for queue_name in ('audio_queue', 'asr_queue'):
                q = client_info.get(queue_name)
                if q:
                    try:
                        await q.put(gated_audio)
                    except Exception as e:
                        logger.error(f"[对齐块] 放入 {queue_name} 失败: {e}")
        except Exception as e:
            logger.warning(f"[对齐块] 处理失败（跳过本块）: {e}", exc_info=True)

    async def _flush_remaining_audio_buffer(self, connection_id: str) -> None:
        """
        将 handle_audio_data 中未满 aligned_block 的尾部 PCM 补零后送入处理链，
        避免面试结束时最后一段语音未进入 ASR 队列。
        """
        client_info = self.active_connections.get(connection_id)
        if not client_info or not client_info.get("is_recording"):
            return

        audio_buffer: bytearray = client_info.get("audio_buffer")
        if not audio_buffer or len(audio_buffer) == 0:
            return

        aligned = self.aligned_block_size_bytes
        if aligned <= 0:
            return

        try:
            while len(audio_buffer) >= aligned:
                valid_chunk = bytes(audio_buffer[:aligned])
                del audio_buffer[:aligned]
                await self._process_aligned_chunk(connection_id, valid_chunk)

            if len(audio_buffer) > 0:
                remainder_len = len(audio_buffer)
                pad_len = aligned - remainder_len
                tail = bytes(audio_buffer) + (b"\x00" * pad_len)
                audio_buffer.clear()
                await self._process_aligned_chunk(connection_id, tail)
                logger.info(
                    f"[停止录音] 已 flush 尾部音频 buffer: remainder={remainder_len}B, pad={pad_len}B, aligned={aligned}B"
                )
        except Exception as e:
            logger.warning(f"[停止录音] flush 尾部音频 buffer 失败: {e}", exc_info=True)

    async def _asr_worker(self, connection_id: str):
        """专用ASR发送任务：从队列读取音频数据并发送到ASR"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        asr_queue = client_info.get('asr_queue')

        if not asr_queue:
            return

        try:
            while True:
                chunk = await asr_queue.get()
                if chunk is None:
                    asr_queue.task_done()
                    break

                asr_session = client_info.get('asr_session')
                if not asr_session or self.asr_client is None:
                    asr_queue.task_done()
                    continue

                try:
                    recognition_result = await self.asr_client.send_audio_data(
                        asr_session,
                        chunk
                    )

                    if recognition_result is None:
                        session_info = self.asr_client.active_sessions.get(asr_session)
                        if session_info and not session_info.get('is_connected', False):
                            logger.warning(f"[ASR任务] ASR连接已断开，尝试重新连接: {connection_id}, session={asr_session}")
                            try:
                                invitation_id = client_info.get('invitation_id')
                                if invitation_id:
                                    asr_connected = await asyncio.wait_for(
                                        self.asr_client.connect_session(asr_session, timeout=10.0),
                                        timeout=15.0
                                    )
                                    if asr_connected:
                                        logger.info(f"[ASR任务] ASR会话重新连接成功: {asr_session}")
                                        recognition_result = await self.asr_client.send_audio_data(
                                            asr_session,
                                            chunk
                                        )
                                    else:
                                        logger.error(f"[ASR任务] ASR会话重新连接失败: {asr_session}")
                                else:
                                    logger.error(f"[ASR任务] 无法重新连接ASR：缺少invitation_id: {connection_id}")
                            except Exception as reconnect_error:
                                logger.error(f"[ASR任务] 重新连接ASR会话失败: {str(reconnect_error)}", exc_info=True)

                    if recognition_result:
                        await self.process_recognition_result(connection_id, recognition_result)

                except Exception as e:
                    logger.error(f"[ASR任务] 发送音频到ASR失败: {str(e)}", exc_info=True)
                finally:
                    asr_queue.task_done()

        except Exception as e:
            logger.error(f"[ASR任务] 异常结束: {str(e)}", exc_info=True)

    async def _safe_send(self, websocket, message: Dict[str, Any], connection_id: str = None) -> bool:
        """安全发送WebSocket消息（支持FastAPI和websockets库）
        
        自动检测WebSocket类型并使用相应的发送方法
        """
        try:
            # 检测WebSocket类型
            # FastAPI WebSocket有send_text/send_bytes方法
            # websockets库的WebSocket有send方法
            if hasattr(websocket, 'send_text'):
                # FastAPI WebSocket
                await websocket.send_text(json.dumps(message))
            else:
                # websockets库的WebSocket
                await websocket.send(json.dumps(message))
            logger.info(f"✅ WebSocket消息已发送: type={message.get('type')}, connection={connection_id}")
            return True
            
        except (websockets.exceptions.ConnectionClosed, Exception) as e:
            if connection_id:
                logger.debug(f"WebSocket连接已关闭，无法发送消息: {connection_id}, 错误: {str(e)}")
            return False
        except Exception as e:
            if connection_id:
                logger.error(f"发送WebSocket消息失败: {connection_id}, 错误: {str(e)}")
            else:
                logger.error(f"发送WebSocket消息失败: {str(e)}")
            return False

    async def handle_fastapi_websocket(self, websocket):
        """处理FastAPI WebSocket连接（统一端口架构）
        
        参数:
            websocket: FastAPI的WebSocket对象
        """
        import uuid
        from fastapi import WebSocketDisconnect
        
        connection_id = str(uuid.uuid4())
        client_info = {
            'connection_id': connection_id,
            'websocket': websocket,
            'session_id': None,
            'invitation_id': None,
            'question_id': None,
            'current_question_id': None,
            'asr_session': None,
            'last_activity': time.time(),
            'last_speech_time': None,
            'accumulated_text': '',
            'current_question_text': '',
            'question_answers': {},
            'sentence_buffer': [],
            'is_recording': False,
            'silence_timer': None,
            'silence_start_time': None,
            'auto_advance_cancelled': False,
            'interview_start_time': None,
            'is_follow_up_question': False,
            'follow_up_question_id': None,
            'parent_answer_id': None,
            'tts_playing': False,
            'last_auto_advance_at': 0.0,
            'last_switched_question_id': None,
            'last_switch_time': None,
            'audio_writer': None,
            'audio_file_path': None,
            'audio_filename': None,
            'answer_audio_url': None,
            'audio_queue': None,
            'audio_writer_task': None,
            'audio_buffer': bytearray(),
            'asr_queue': None,
            'asr_task': None,
            'prev_was_speech': True,
            'follow_up_ws_sent_parent_id': None,
        }

        self.active_connections[connection_id] = client_info
        logger.info(f"新FastAPI WebSocket连接建立: {connection_id}")

        try:
            # 必须先接受WebSocket连接，然后才能发送消息
            await websocket.accept()
            logger.info(f"WebSocket连接已接受: {connection_id}")
            
            # 发送连接确认
            await self._safe_send(websocket, {
                'type': 'connection_established',
                'connection_id': connection_id,
                'timestamp': datetime.now().isoformat()
            }, connection_id)

            # 持续接收消息
            while True:
                try:
                    # FastAPI WebSocket接收消息
                    data = await websocket.receive()
                    
                    if 'text' in data:
                        # 文本消息
                        message = data['text']
                        await self.handle_message(connection_id, message)
                    elif 'bytes' in data:
                        # 二进制消息（音频数据）
                        audio_data = data['bytes']
                        logger.debug(f"收到二进制音频数据: {len(audio_data)} 字节, connection={connection_id}")
                        await self.handle_audio_data(connection_id, audio_data)
                    elif 'type' in data and data['type'] == 'websocket.disconnect':
                        # 客户端断开连接
                        logger.info(f"FastAPI WebSocket客户端断开: {connection_id}")
                        break
                        
                except WebSocketDisconnect:
                    logger.info(f"FastAPI WebSocket连接断开: {connection_id}")
                    break
                except Exception as e:
                    logger.error(f"处理消息失败: {str(e)}", exc_info=True)
                    await self._safe_send(websocket, {
                        'type': 'error',
                        'message': f'处理消息失败: {str(e)}',
                        'timestamp': datetime.now().isoformat()
                    }, connection_id)

        except Exception as e:
            logger.error(f"FastAPI WebSocket连接异常: {str(e)}", exc_info=True)
        finally:
            # 清理连接
            if connection_id in self.active_connections:
                await self.cleanup_connection(connection_id)

    async def handle_connection(self, websocket):
        """处理WebSocket连接（websockets库版本）
        
        注意：websockets 15.0+ 会自动处理Connection header验证
        如果收到InvalidUpgrade错误，可能是：
        1. 前端发送了keep-alive header（浏览器重用TCP连接）
        2. 中间代理修改了header
        3. 旧连接未完全关闭
        """
        # 获取请求路径和headers（用于调试）
        path = websocket.request.path if hasattr(websocket, 'request') else '/'
        
        # 记录连接信息（用于调试Connection header问题）
        if hasattr(websocket, 'request') and hasattr(websocket.request, 'headers'):
            connection_header = websocket.request.headers.get('Connection', '')
            upgrade_header = websocket.request.headers.get('Upgrade', '')
            logger.debug(f"WebSocket握手信息 - Connection: {connection_header}, Upgrade: {upgrade_header}")
        
        connection_id = str(uuid.uuid4())
        client_info = {
            'connection_id': connection_id,
            'websocket': websocket,
            'session_id': None,
            'invitation_id': None,
            'question_id': None,
            'current_question_id': None,  # 当前正在回答的题目ID
            'asr_session': None,
            'last_activity': time.time(),
            'last_speech_time': None,  # 最后一次检测到语音的时间
            'accumulated_text': '',
            'current_question_text': '',  # 当前题目的回答文本
            'question_answers': {},  # 记录每道题的回答 {question_id: answer_text}
            'sentence_buffer': [],
            'is_recording': False,
            'silence_timer': None,  # 静音检测定时器
            'silence_start_time': None,  # 静音开始时间
            'auto_advance_cancelled': False,  # 是否取消了自动切换
            'interview_start_time': None,  # 面试开始时间（用于计算audio_duration）
            'is_follow_up_question': False,  # 标识当前是否为追问问题
            'follow_up_question_id': None,  # 当前追问问题的ID（如果有）
            'parent_answer_id': None,
            'tts_playing': False,
            'last_auto_advance_at': 0.0,
            'last_switched_question_id': None,
            'last_switch_time': None,
            'audio_writer': None,
            'audio_file_path': None,
            'audio_filename': None,
            'answer_audio_url': None,
            'audio_queue': None,
            'audio_writer_task': None,
            'audio_buffer': bytearray(),
            'asr_queue': None,
            'asr_task': None,
            'prev_was_speech': True,
            'follow_up_ws_sent_parent_id': None,
        }

        self.active_connections[connection_id] = client_info
        logger.info(f"新连接建立: {connection_id}, 路径: {path}")

        try:
            # 发送连接确认
            await self._safe_send(websocket, {
                'type': 'connection_established',
                'connection_id': connection_id,
                'timestamp': datetime.now().isoformat()
            }, connection_id)

            async for message in websocket:
                try:
                    await self.handle_message(connection_id, message)
                except Exception as e:
                    logger.error(f"处理消息失败: {str(e)}", exc_info=True)
                    await self._safe_send(websocket, {
                        'type': 'error',
                        'message': f'处理消息失败: {str(e)}',
                        'timestamp': datetime.now().isoformat()
                    }, connection_id)

        except websockets.exceptions.InvalidUpgrade as e:
            # 处理Connection header错误（keep-alive问题）
            error_msg = str(e)
            logger.error(f"WebSocket升级失败（Connection header错误）: {connection_id}")
            logger.error(f"错误详情: {error_msg}")
            logger.error("可能的原因：")
            logger.error("1. 浏览器重用TCP连接，发送了keep-alive header")
            logger.error("2. 旧连接未完全关闭，新连接使用了旧的TCP连接")
            logger.error("3. 中间代理修改了Connection header")
            logger.error("解决方案：前端应在建立新连接前关闭旧连接，并延迟100ms再建立新连接")
            # 不调用cleanup_connection，因为连接未成功建立
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"连接关闭: {connection_id}")
        except Exception as e:
            logger.error(f"连接异常: {str(e)}", exc_info=True)
        finally:
            # 只有在连接成功建立后才清理
            if connection_id in self.active_connections:
                await self.cleanup_connection(connection_id)

    async def handle_message(self, connection_id: str, message: str):
        """处理WebSocket消息"""
        try:
            if isinstance(message, str):
                # 文本消息（控制命令）
                data = json.loads(message)
                await self.handle_control_message(connection_id, data)
            else:
                # 二进制消息（音频数据）
                await self.handle_audio_data(connection_id, message)

        except json.JSONDecodeError:
            logger.error(f"无效的JSON消息: {message}")
        except Exception as e:
            logger.error(f"处理消息异常: {str(e)}", exc_info=True)

    async def handle_control_message(self, connection_id: str, data: Dict[str, Any]):
        """处理控制消息"""
        msg_type = data.get('type', '')
        client_info = self.active_connections.get(connection_id)

        if not client_info:
            return

        websocket = client_info['websocket']

        if msg_type == 'start_recording':
            await self.start_recording(connection_id, data)
        elif msg_type == 'switch_question':
            # 统一端口架构：切换题目（不重新初始化ASR，保持录音）
            await self.switch_question(connection_id, data)
        elif msg_type == 'stop_recording':
            await self.stop_recording(connection_id, data)
        elif msg_type == 'set_session':
            await self.set_session_info(connection_id, data)
        elif msg_type == 'manual_next_question':
            # 手动切换到下一题
            await self.handle_manual_next_question(connection_id, data)
        elif msg_type == 'cancel_auto_advance':
            # 取消自动切换
            await self.handle_cancel_auto_advance(connection_id, data)
        elif msg_type == 'permission_status':
            # 麦克风权限状态更新
            await self.handle_permission_status(connection_id, data)
        elif msg_type == 'tts_playback_state':
            await self.handle_tts_playback_state(connection_id, data)
        elif msg_type == 'ping':
            # ping消息使用_safe_send确保连接状态检查
            await self._safe_send(websocket, {
                'type': 'pong',
                'timestamp': datetime.now().isoformat()
            }, connection_id)
        else:
            logger.warning(f"未知消息类型: {msg_type}")

    async def handle_audio_data(self, connection_id: str, audio_data: bytes):
        """处理音频数据（支持FastAPI和websockets库）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            logger.warning(f"连接不存在，跳过音频数据: {connection_id}")
            return
        
        if not client_info.get('is_recording'):
            logger.warning(f"录音未开始，跳过音频数据: {connection_id}, is_recording={client_info.get('is_recording')}")
            return

        try:
            # 更新活动时间
            client_info['last_activity'] = time.time()
            
            # 调试日志：记录收到的音频数据大小
            logger.debug(f"收到音频数据: {len(audio_data)} 字节, connection={connection_id}")

            # 不对外部单包做奇数字节补齐，避免误补导致采样点高低字节错位；仅在 buffer 内按偶数长度切块
            audio_buffer: bytearray = client_info.get('audio_buffer')
            if audio_buffer is None:
                audio_buffer = bytearray()
                client_info['audio_buffer'] = audio_buffer

            audio_buffer.extend(audio_data)

            # 仅当 buffer 足够长时切出整块（aligned_block_size_bytes 为偶数，保证 2 字节采样对齐）
            while len(audio_buffer) >= self.aligned_block_size_bytes:
                valid_chunk = bytes(audio_buffer[:self.aligned_block_size_bytes])
                del audio_buffer[:self.aligned_block_size_bytes]
                await self._process_aligned_chunk(connection_id, valid_chunk)

        except Exception as e:
            logger.error(f"处理音频数据失败: {str(e)}", exc_info=True)

    async def start_recording(self, connection_id: str, data: Dict[str, Any]):
        """开始录音（智能流式模式：整个面试共用一个ASR会话）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            logger.error(f"start_recording: 连接不存在: {connection_id}")
            return

        try:
            # 获取参数
            invitation_id = data.get('invitation_id')
            question_id = data.get('question_id')
            logger.info(f"收到start_recording请求: connection={connection_id}, invitation={invitation_id}, question={question_id}")

            # 如果已经有ASR会话在运行，说明是切换题目，不需要重新创建ASR会话
            if client_info.get('asr_session') and client_info.get('is_recording'):
                # 已在录音：仅当题目真的变化时才切题（避免重复 start_recording 误走 _switch_question）
                if question_id and question_id == client_info.get('current_question_id'):
                    logger.debug(
                        f"start_recording: 已在录音且题目未变，忽略: connection={connection_id}, question={question_id}"
                    )
                    return
                await self._switch_question(connection_id, question_id, trigger_source='start_recording_switch')
                return

            # 首次启动：创建ASR会话
            logger.info(f"首次启动录音: invitation={invitation_id}, question={question_id}")

            # 检查ASR客户端是否可用
            if self.asr_client is None:
                logger.warning(f"ASR客户端未初始化，无法启动录音: {connection_id}")
                await self._safe_send(client_info['websocket'], {
                    'type': 'error',
                    'message': '语音识别服务未配置，请联系管理员',
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                return

            # 初始化ASR会话（传入invitation_id用于清理旧会话）
            asr_session = self.asr_client.create_session(invitation_id=invitation_id)
            
            # 连接到阿里云ASR服务（添加超时保护）
            logger.info(f"正在连接到阿里云ASR服务: {asr_session}")
            try:
                asr_connected = await asyncio.wait_for(
                    self.asr_client.connect_session(asr_session, timeout=10.0),
                    timeout=15.0  # 总超时15秒（包括连接和等待TranscriptionStarted）
                )
            except asyncio.TimeoutError:
                logger.error(f"连接ASR服务总超时（15秒）: {asr_session}")
                asr_connected = False
                # 清理失败的会话
                await self.asr_client.close_session(asr_session)
            
            if not asr_connected:
                logger.error(f"连接阿里云ASR服务失败: {asr_session}")
                await self._safe_send(client_info['websocket'], {
                    'type': 'error',
                    'message': '连接语音识别服务失败，请重试',
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                return
            
            logger.info(f"阿里云ASR服务连接成功: {asr_session}")
            
            # 初始化客户端信息
            client_info.update({
                'invitation_id': invitation_id,
                'current_question_id': question_id,
                'asr_session': asr_session,
                'is_recording': True,
                'accumulated_text': '',
                'current_question_text': '',
                'question_answers': {},
                'sentence_buffer': [],
                'last_speech_time': time.time(),
                'silence_start_time': None,
                'interview_start_time': time.time(),  # 记录面试开始时间（用于计算audio_duration）
                'tts_playing': False,
                'last_auto_advance_at': 0.0,
                'is_follow_up_question': False,  # 标识当前是否为追问问题
                'follow_up_question_id': None,  # 当前追问问题的ID（如果有）
                'audio_writer': None,
                'audio_file_path': None,
                'audio_filename': None,
                'answer_audio_url': None,
                'audio_queue': None,
                'audio_writer_task': None,
                'asr_queue': None,
                'asr_task': None,
                'prev_was_speech': True,
                'dc_running_mean': None,  # 去直流滚动均值，每次开始录音时重置
            })

            # 为本次面试创建完整录音文件（wav），并启动后台写入任务
            if invitation_id:
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                audio_filename = f"{invitation_id}_{timestamp}.wav"
                audio_path = self.answer_audio_dir / audio_filename
                try:
                    wf = wave.open(str(audio_path), 'wb')
                    wf.setnchannels(int(self.audio_channels) or 1)
                    # 目前前端发送的是16bit PCM
                    wf.setsampwidth(2)
                    wf.setframerate(int(self.audio_sample_rate) or 16000)
                    client_info['audio_writer'] = wf
                    client_info['audio_file_path'] = str(audio_path)
                    client_info['audio_filename'] = audio_filename

                    # 为该连接创建异步队列和后台写入任务
                    client_info['audio_queue'] = asyncio.Queue()
                    client_info['audio_writer_task'] = asyncio.create_task(
                        self._audio_writer_worker(connection_id)
                    )

                    logger.info(f"创建面试录音文件: {audio_path}")
                except Exception as e:
                    # 录音失败不影响ASR主流程
                    client_info['audio_writer'] = None
                    client_info['audio_file_path'] = None
                    client_info['audio_filename'] = None
                    client_info['audio_queue'] = None
                    client_info['audio_writer_task'] = None
                    logger.error(f"创建录音文件失败，不影响ASR流程: {str(e)}")

            # 创建ASR发送队列与专用任务，将ASR调用与接收循环解耦
            client_info['asr_queue'] = asyncio.Queue()
            client_info['asr_task'] = asyncio.create_task(
                self._asr_worker(connection_id)
            )

            logger.info(f"开始录音: connection={connection_id}, invitation={invitation_id}, question={question_id}")

            await self._safe_send(client_info['websocket'], {
                'type': 'recording_started',
                'invitation_id': invitation_id,
                'question_id': question_id,
                'mode': 'streaming',  # 标识为流式模式
                'timestamp': datetime.now().isoformat()
            }, connection_id)

        except Exception as e:
            logger.error(f"开始录音失败: {str(e)}", exc_info=True)
            await self._safe_send(client_info['websocket'], {
                'type': 'error',
                'message': f'开始录音失败: {str(e)}',
                'timestamp': datetime.now().isoformat()
            }, connection_id)
    
    async def switch_question(self, connection_id: str, data: Dict[str, Any]):
        """切换题目（统一端口架构：不重新初始化ASR，保持录音）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            logger.warning(f"连接不存在: {connection_id}")
            return
        
        new_question_id = data.get('question_id')
        if not new_question_id:
            logger.warning(f"缺少question_id: {connection_id}")
            await self._safe_send(client_info['websocket'], {
                'type': 'error',
                'message': '缺少question_id',
                'timestamp': datetime.now().isoformat()
            }, connection_id)
            return
        
        # 调用内部切换方法
        await self._switch_question(
            connection_id,
            new_question_id,
            trigger_source=data.get('trigger_source') or 'client_switch_question',
        )
    
    async def _switch_question(self, connection_id: str, new_question_id: str, trigger_source: str = 'unknown'):
        """切换题目（保存当前题目的回答，切换到新题目）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            old_question_id = client_info.get('current_question_id')
            
            # 防重复切换：如果新题目ID和当前题目ID相同，直接返回
            # 注意：这里不应该出现相同的情况，如果出现说明前端逻辑有问题
            if old_question_id == new_question_id:
                # 前端常在 manual_next / API 同步后再发一次 switch_question 或 start_recording；
                # 再发 question_switched 会导致 UI 重复刷新、题干重复出现，故静默忽略。
                logger.debug(
                    f"切换题目：与当前题目相同，忽略重复请求: {new_question_id}, "
                    f"trigger_source={trigger_source}"
                )
                return
            
            current_answer = client_info.get('current_question_text', '').strip()
            
            # 关键修复：在_switch_question时不应该调用_save_question_answer
            # 因为答案已经在handle_manual_next_question中保存了
            # 如果这里再次保存，可能会导致answer_text被覆盖（特别是追问答案会覆盖主问题答案）
            # 注释掉这段代码，避免重复保存
            # if old_question_id and current_answer:
            #     await self._save_question_answer(
            #         connection_id, 
            #         old_question_id, 
            #         current_answer
            #     )
            
            # 切换到新题目
            client_info['current_question_id'] = new_question_id
            client_info['follow_up_ws_sent_parent_id'] = None
            client_info['asr_ignore_until'] = time.time() + self.asr_switch_ignore_seconds
            client_info['current_question_text'] = ''  # 清空当前题目的文本
            client_info['accumulated_text'] = ''  # 清空累积文本（避免文本继续累积）
            client_info['sentence_buffer'] = []  # 清空句子缓冲区
            client_info['last_speech_time'] = time.time()
            client_info['silence_start_time'] = None
            # 已切换到不同母题：必须清空追问上下文，避免下一题的回答被误判为上一题的追问
            client_info['parent_answer_id'] = None
            client_info['is_follow_up_question'] = False
            client_info['follow_up_question_id'] = None
            
            logger.info(
                f"切换题目: {old_question_id} -> {new_question_id}, "
                f"trigger_source={trigger_source}, 已清空文本累积"
            )
            
            # 发送题目切换成功消息给前端
            await self._safe_send(client_info['websocket'], {
                'type': 'question_switched',
                'question_id': new_question_id,
                'old_question_id': old_question_id,
                'trigger_source': trigger_source,
                'message': '题目已成功切换',
                'timestamp': datetime.now().isoformat()
            }, connection_id)
            
            # 同步更新 interview_session_service 的内存状态
            try:
                invitation_id = client_info.get('invitation_id')
                if invitation_id:
                    # 从数据库获取新题目的完整信息
                    question_sql = "SELECT iq.question_id, iq.question_type as type, iq.question_text, iq.question_order as \"order\", iq.create_time FROM interview_question iq WHERE iq.question_id = %s LIMIT 1"
                    if self.db_manager.db_type != 'postgresql':
                        question_sql = question_sql.replace('%s', '?')
                    
                    question_data = self.db_manager.fetch_one(question_sql, (new_question_id,))
                    if question_data:
                        # 将数据库返回的 Row 对象转换为字典
                        question_dict = dict(question_data)
                        
                        # 查找所有与该 invitation_id 关联的 session
                        # 遍历 interview_session_service 的 active_sessions
                        updated_count = 0
                        for session_id, session in list(interview_session_service.active_sessions.items()):
                            if session.get('invitation_id') == invitation_id:
                                # 更新当前问题
                                session['current_question'] = question_dict
                                # 确保会话状态为 IN_PROGRESS，防止被标记为 COMPLETED
                                if session.get('status') != 'IN_PROGRESS':
                                    session['status'] = 'IN_PROGRESS'
                                updated_count += 1
                                logger.info(f"已同步更新 interview_session_service: session_id={session_id}, question_id={new_question_id}, status={session['status']}")
                        
                        if updated_count == 0:
                            logger.warning(f"未找到与 invitation_id={invitation_id} 关联的活跃会话，可能需要创建新会话")
                    else:
                        logger.warning(f"未找到 question_id={new_question_id} 的题目信息")
                else:
                    logger.warning(f"未找到 invitation_id，无法同步更新")
            except Exception as sync_error:
                logger.error(f"同步更新 interview_session_service 失败: {str(sync_error)}", exc_info=True)
            
            # 注意：消息由调用者发送（handle_manual_next_question 或 _handle_answer_completion）
            # 这里只负责更新内部状态，避免重复发送消息
            
        except Exception as e:
            logger.error(f"切换题目失败: {str(e)}", exc_info=True)

    @staticmethod
    def _awaiting_follow_up_response(client_info: Dict[str, Any]) -> bool:
        """主问已评分且已下发追问，尚未完成追问作答；此时禁止切下一母题。"""
        return bool(client_info.get('is_follow_up_question')) and bool(client_info.get('parent_answer_id'))
    
    async def _save_question_answer(self, connection_id: str, question_id: str, answer_text: str):
        """保存题目的回答到数据库（一个问题回答完毕就记录一次答案）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            # ========== 核心修复：最高优先级拦截 ==========
            # 关键修复：检查是否是追问答案，如果是，只更新follow_up_answer_text，不更新answer_text和interview_session
            parent_answer_id = client_info.get('parent_answer_id')
            if parent_answer_id:
                logger.info(f"[保存追问答案-步骤1] 🔒 检测到追问模式，强制更新主记录: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}")
                # 关键修复：只更新follow_up_answer_text，不更新answer_text，防止覆盖主问题回答
                # 关键修复：检查是否已经有follow_up_answer_text，如果有，说明已经保存过了，不再重复保存
                check_follow_up_sql = "SELECT follow_up_answer_text, answer_text FROM candidate_answers WHERE id = %s"
                if self.db_manager.db_type != 'postgresql':
                    check_follow_up_sql = check_follow_up_sql.replace('%s', '?')
                existing_follow_up = self.db_manager.fetch_one(check_follow_up_sql, (parent_answer_id,))
                if existing_follow_up:
                    existing_follow_up_text = existing_follow_up.get('follow_up_answer_text') if isinstance(existing_follow_up, dict) else existing_follow_up[0]
                    existing_answer_text = existing_follow_up.get('answer_text') if isinstance(existing_follow_up, dict) else existing_follow_up[1]
                    logger.info(f"[保存追问答案-步骤1] 当前数据库状态: answer_text长度={len(existing_answer_text) if existing_answer_text else 0}, follow_up_answer_text长度={len(existing_follow_up_text) if existing_follow_up_text else 0}")
                    if existing_follow_up_text and existing_follow_up_text.strip():
                        # 已经保存过了，不再重复保存
                        logger.info(f"[保存追问答案-步骤1] ⚠️ follow_up_answer_text已存在，跳过重复保存: parent_answer_id={parent_answer_id}, existing_length={len(existing_follow_up_text)}")
                        return
                
                # 直接设置，不使用追加，避免重复
                # 关键修复：只更新follow_up_answer_text，绝对不更新answer_text
                if self.db_manager.db_type == 'postgresql':
                    update_main_answer_sql = """
                    UPDATE candidate_answers
                    SET follow_up_answer_text = %s,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                else:
                    # SQLite
                    update_main_answer_sql = """
                    UPDATE candidate_answers
                    SET follow_up_answer_text = ?,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """
                
                rows_affected = self.db_manager.execute_update(update_main_answer_sql, (answer_text, parent_answer_id))
                if rows_affected == 0:
                    logger.error(f"[保存追问答案-步骤1] 更新主答案记录失败: 没有行被更新, parent_answer_id={parent_answer_id}")
                else:
                    logger.info(f"[保存追问答案-步骤1] ✅ 已更新主答案记录（仅更新follow_up_answer_text，不更新answer_text）: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}, rows_affected={rows_affected}")
                # 重点：必须Return，阻止下方任何INSERT逻辑和interview_session更新逻辑运行
                return
            
            invitation_id = client_info.get('invitation_id')
            if not invitation_id:
                return
            
            # 检查是否已存在该invitation的session记录（同一面试的所有问题共享同一个session_id）
            # 注意：使用invitation_id查找，而不是invitation_id + question_id，这样同一面试的所有问题共享同一个session_id
            check_sql = "SELECT session_id, follow_up_used, follow_up_limit FROM interview_session WHERE invitation_id = %s ORDER BY create_time DESC LIMIT 1"
            if self.db_manager.db_type != 'postgresql':
                check_sql = check_sql.replace('%s', '?')
            
            existing_session = self.db_manager.fetch_one(check_sql, (invitation_id,))
            
            # 获取题目内容（用于冗余存储）
            # 注意：题目内容应该从interview_questions表通过atomic_question_id关联获取
            question_sql = """
            SELECT COALESCE(iqs.content, iq.question_text, '') as question_text
            FROM interview_question iq
            LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iq.question_id = %s
            LIMIT 1
            """
            if self.db_manager.db_type != 'postgresql':
                question_sql = question_sql.replace('%s', '?')
            
            question_data = self.db_manager.fetch_one(question_sql, (question_id,))
            question_text = question_data.get('question_text', '') if question_data else ''
            
            if existing_session:
                # 更新现有记录（一个问题回答完毕就更新一次）
                session_id = existing_session.get('session_id') if isinstance(existing_session, dict) else existing_session[0]
                # 注意：这里不更新question_id，因为同一面试的所有问题共享同一个session_id
                # 只更新candidate_answer，保留question_id为第一个问题的ID（或者可以更新为当前问题的ID）
                update_sql = """
                UPDATE interview_session
                SET candidate_answer = %s,
                    question_id = %s,
                    question_text = %s,
                    session_status = 'IN_PROGRESS',
                    end_time = %s
                WHERE session_id = %s
                """
                if self.db_manager.db_type != 'postgresql':
                    update_sql = update_sql.replace('%s', '?')
                
                rows_affected = self.db_manager.execute_update(update_sql, (
                    answer_text,
                    question_id,
                    question_text,
                    datetime.now(),
                    session_id
                ))
                if rows_affected == 0:
                    logger.error(f"更新interview_session失败: 没有行被更新, session_id={session_id}, question_id={question_id}")
                else:
                    logger.info(f"已更新题目回答: question={question_id}, answer_length={len(answer_text)}, session_id={session_id}, rows_affected={rows_affected}")
            else:
                # 创建新记录（一个问题回答完毕就创建一次）
                session_id = f"VOICE_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
                
                insert_sql = """
                INSERT INTO interview_session (
                    session_id, invitation_id, question_id, question_text,
                    candidate_answer, session_status, start_time, end_time, follow_up_limit
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                if self.db_manager.db_type != 'postgresql':
                    insert_sql = insert_sql.replace('%s', '?')
                
                # 专业题最多追问1次，基础题不追问（follow_up_limit=0）
                # 获取题目类型
                question_type_sql = "SELECT question_type FROM interview_question WHERE question_id = %s LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    question_type_sql = question_type_sql.replace('%s', '?')
                question_type_data = self.db_manager.fetch_one(question_type_sql, (question_id,))
                question_type = question_type_data.get('question_type', 'SPECIALTY') if question_type_data else 'SPECIALTY'
                # 专业题（SPECIALTY或PROFESSIONAL）最多追问1次，基础题（BASIC或BASIC_INFO）不追问
                is_professional = question_type in ['SPECIALTY', 'PROFESSIONAL']
                follow_up_limit = 1 if is_professional else 0
                
                # 注意：interview_start_time 应该在 start_recording 时设置（用户点击"开始面试"时）
                # 这里不设置，因为用户可能还没有点击"开始面试"
                # 如果此时还没有 interview_start_time，说明用户还没有开始面试，不应该记录时长
                # 注意：audio_duration 不应该在这里设置，应该在 stop_recording 时统一更新
                
                rows_affected = self.db_manager.execute_update(insert_sql, (
                    session_id,
                    invitation_id,
                    question_id,
                    question_text,
                    answer_text,
                    'COMPLETED',
                    datetime.now(),
                    datetime.now(),
                    follow_up_limit
                ))
                if rows_affected == 0:
                    logger.error(f"插入interview_session失败: 没有行被插入, session_id={session_id}, question_id={question_id}")
                else:
                    logger.info(f"已创建题目回答记录: question={question_id}, answer_length={len(answer_text)}, session_id={session_id}, follow_up_limit={follow_up_limit}, rows_affected={rows_affected}")
            
            # 同时保存到candidate_answers表（主问题答案，status='recorded'，评分后会更新为'evaluated'）
            try:
                # 检查是否已存在该答案记录
                check_answer_sql = """
                SELECT id FROM candidate_answers 
                WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
                LIMIT 1
                """
                if self.db_manager.db_type != 'postgresql':
                    check_answer_sql = check_answer_sql.replace('%s', '?')
                
                existing_answer = self.db_manager.fetch_one(check_answer_sql, (session_id, question_id))
                
                # 检查是否为追问答案（使用parent_answer_id而不是follow_up_question_id）
                # 关键修复：只要内存里有parent_answer_id，无论is_follow_up状态如何，都认为是更新操作
                parent_answer_id = client_info.get('parent_answer_id')
                
                # 如果parent_answer_id存在，直接更新主答案记录，不创建新记录
                if parent_answer_id:
                    logger.info(f"[保存追问答案] ✅ 识别到parent_answer_id，执行UPDATE: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}")
                    update_main_answer_sql = """
                    UPDATE candidate_answers
                    SET follow_up_answer_text = %s,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                    if self.db_manager.db_type != 'postgresql':
                        update_main_answer_sql = update_main_answer_sql.replace('%s', '?')
                    
                    rows_affected = self.db_manager.execute_update(update_main_answer_sql, (answer_text, parent_answer_id))
                    if rows_affected == 0:
                        logger.error(f"[保存追问答案] 更新主答案记录失败: 没有行被更新, parent_answer_id={parent_answer_id}")
                    else:
                        logger.info(f"[保存追问答案] 已更新主答案记录: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}, rows_affected={rows_affected}")
                    # 必须直接return，防止滑入下方的INSERT逻辑
                    return
                
                # 备用检查：如果parent_answer_id丢失，通过数据库查找
                # 改进：不依赖question_id，通过session_id查找最近的有follow_up_question但没有follow_up_answer_text的记录
                if answer_text and answer_text.strip() and not parent_answer_id:
                    check_follow_up_sql = """
                    SELECT id, follow_up_question, follow_up_answer_text 
                    FROM candidate_answers 
                    WHERE session_id = %s
                      AND is_follow_up = FALSE
                      AND follow_up_question IS NOT NULL
                      AND follow_up_question != ''
                      AND (follow_up_answer_text IS NULL OR follow_up_answer_text = '')
                    ORDER BY create_time DESC
                    LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        check_follow_up_sql = check_follow_up_sql.replace('%s', '?')
                    
                    follow_up_check = self.db_manager.fetch_one(check_follow_up_sql, (session_id,))
                    if follow_up_check:
                        if isinstance(follow_up_check, dict):
                            parent_answer_id = follow_up_check.get('id')
                        else:
                            parent_answer_id = follow_up_check[0]
                        client_info['is_follow_up_question'] = True
                        client_info['parent_answer_id'] = parent_answer_id
                        logger.info(f"[保存追问答案] ✅ 通过数据库检查发现是追问答案: parent_answer_id={parent_answer_id}, session_id={session_id}, question_id={question_id}")
                        
                        # 更新主答案记录
                        update_main_answer_sql = """
                        UPDATE candidate_answers
                        SET follow_up_answer_text = %s,
                            update_time = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """
                        if self.db_manager.db_type != 'postgresql':
                            update_main_answer_sql = update_main_answer_sql.replace('%s', '?')
                        
                        rows_affected = self.db_manager.execute_update(update_main_answer_sql, (answer_text, parent_answer_id))
                        if rows_affected == 0:
                            logger.error(f"[保存追问答案] 更新主答案记录失败: 没有行被更新, parent_answer_id={parent_answer_id}")
                        else:
                            logger.info(f"[保存追问答案] 已更新主答案记录: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}, rows_affected={rows_affected}")
                        # 必须直接return，防止滑入下方的INSERT逻辑
                        return
                else:
                    # 这是主问题答案
                    if existing_answer:
                        # 更新现有答案记录
                        answer_id = existing_answer['id']
                        # 检查是否已经有final_score，如果有，说明已经评分过，不应该覆盖status
                        check_score_sql = "SELECT final_score, status FROM candidate_answers WHERE id = %s"
                        if self.db_manager.db_type != 'postgresql':
                            check_score_sql = check_score_sql.replace('%s', '?')
                        score_check = self.db_manager.fetch_one(check_score_sql, (answer_id,))
                        has_score = False
                        if score_check:
                            final_score_value = score_check.get('final_score') if isinstance(score_check, dict) else score_check[0]
                            has_score = final_score_value is not None
                        
                        # 关键修复：检查是否是追问答案，如果是，不应该更新answer_text
                        parent_answer_id_check = client_info.get('parent_answer_id')
                        if parent_answer_id_check:
                            # 这是追问答案，不应该更新主问题的answer_text
                            logger.warning(f"[保存追问答案] ⚠️ 检测到parent_answer_id，跳过answer_text更新，避免覆盖主问题回答: parent_answer_id={parent_answer_id_check}, answer_id={answer_id}")
                            return
                        
                        if has_score:
                            # 已经有评分，只更新answer_text，不更新status（保持evaluated状态）
                            # 但必须确保这不是追问答案
                            update_answer_sql = """
                            UPDATE candidate_answers
                            SET answer_text = %s,
                                update_time = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """
                            if self.db_manager.db_type != 'postgresql':
                                update_answer_sql = update_answer_sql.replace('%s', '?')
                            
                            rows_affected = self.db_manager.execute_update(update_answer_sql, (answer_text, answer_id))
                            if rows_affected == 0:
                                logger.error(f"更新candidate_answers失败: 没有行被更新, answer_id={answer_id}, session_id={session_id}, question_id={question_id}")
                            else:
                                logger.info(f"已更新candidate_answers记录（保持evaluated状态）: answer_id={answer_id}, session_id={session_id}, question_id={question_id}, rows_affected={rows_affected}")
                        else:
                            # 还没有评分，更新answer_text和status
                            update_answer_sql = """
                            UPDATE candidate_answers
                            SET answer_text = %s,
                                status = 'recorded',
                                update_time = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """
                            if self.db_manager.db_type != 'postgresql':
                                update_answer_sql = update_answer_sql.replace('%s', '?')
                            
                            rows_affected = self.db_manager.execute_update(update_answer_sql, (answer_text, answer_id))
                            if rows_affected == 0:
                                logger.error(f"更新candidate_answers失败: 没有行被更新, answer_id={answer_id}, session_id={session_id}, question_id={question_id}")
                            else:
                                logger.info(f"已更新candidate_answers记录: answer_id={answer_id}, session_id={session_id}, question_id={question_id}, rows_affected={rows_affected}")
                    else:
                        # 创建新答案记录
                        answer_id = f"ANS_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
                        insert_answer_sql = """
                        INSERT INTO candidate_answers (
                            id, session_id, question_id, answer_text,
                            is_follow_up, parent_answer_id, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """
                        if self.db_manager.db_type != 'postgresql':
                            insert_answer_sql = insert_answer_sql.replace('%s', '?')
                        
                        rows_affected = self.db_manager.execute_update(insert_answer_sql, (
                            answer_id,
                            session_id,
                            question_id,
                            answer_text,
                            False,  # is_follow_up = False（主问题答案）
                            None,   # parent_answer_id = None（主问题没有父答案）
                            'recorded'  # status = 'recorded'（已录制，待评分）
                        ))
                        if rows_affected == 0:
                            logger.error(f"插入candidate_answers失败: 没有行被插入, answer_id={answer_id}, session_id={session_id}, question_id={question_id}")
                        else:
                            logger.info(f"已创建candidate_answers记录: answer_id={answer_id}, session_id={session_id}, question_id={question_id}, answer_length={len(answer_text)}, rows_affected={rows_affected}")
            except Exception as e:
                # candidate_answers表保存失败不影响主流程，只记录警告
                logger.warning(f"保存到candidate_answers表失败: {str(e)}", exc_info=True)
            
            # 保存到内存中
            client_info['question_answers'][question_id] = answer_text
            
        except Exception as e:
            logger.error(f"保存题目回答失败: {str(e)}", exc_info=True)

    async def stop_recording(self, connection_id: str, data: Dict[str, Any]):
        """停止录音：先排空 ASR 队列与尾部音频，再落库当前题完整转写，最后收尾录音文件与状态。"""
        client_info = self.active_connections.get(connection_id)
        if not client_info or not client_info.get('is_recording'):
            return

        try:
            # 1) 尾部未满对齐块的 PCM 送入 ASR / 录音队列，避免尾段丢失
            await self._flush_remaining_audio_buffer(connection_id)

            interview_start_time = client_info.get('interview_start_time')
            invitation_id = client_info.get('invitation_id')
            asr_session_id = client_info.get('asr_session')

            # 2) 先排空 ASR 队列（必须在 close_session 之前，确保 final_result 已累加到 current_question_text）
            asr_queue = client_info.get('asr_queue')
            asr_task = client_info.get('asr_task')
            if asr_queue:
                try:
                    await asr_queue.put(None)
                    await asr_queue.join()
                except Exception as e:
                    logger.warning(f"等待ASR队列处理完成时出错: {str(e)}")

            if asr_task:
                try:
                    await asyncio.wait_for(asr_task, timeout=30.0)
                except Exception as e:
                    logger.warning(f"等待ASR任务结束时出错: {str(e)}")

            # 3) 关闭 ASR 会话（所有已排队音频已发送后再关）
            if asr_session_id and self.asr_client:
                try:
                    await self.asr_client.close_session(asr_session_id)
                except Exception as e:
                    logger.warning(f"关闭ASR会话失败: {str(e)}")
                client_info['asr_session'] = None

            # 4) 落库当前题目完整转写（利用既有 UPDATE 逻辑覆盖不完整的首写）
            current_question_id = client_info.get('current_question_id')
            current_answer = client_info.get('current_question_text', '').strip()
            if current_question_id:
                await self._save_question_answer(connection_id, current_question_id, current_answer)
                logger.info(
                    f"[停止录音] 最终答案已保存: question_id={current_question_id}, len={len(current_answer)}"
                )
                if current_answer:
                    try:
                        session_id = client_info.get('session_id')
                        if not session_id and invitation_id:
                            session_sql = """
                            SELECT session_id
                            FROM interview_session
                            WHERE invitation_id = %s
                            ORDER BY create_time DESC
                            LIMIT 1
                            """
                            if self.db_manager.db_type != 'postgresql':
                                session_sql = session_sql.replace('%s', '?')
                            session_row = self.db_manager.fetch_one(session_sql, (invitation_id,))
                            if isinstance(session_row, dict):
                                session_id = session_row.get('session_id')
                            elif session_row:
                                session_id = session_row[0]

                        if session_id:
                            final_eval = await self.realtime_scorer.evaluate_answer(
                                session_id,
                                current_question_id,
                                current_answer,
                                rescore_mode=True,
                            )
                            logger.info(
                                "[停止录音] 最终题目评分已补齐到candidate_answers: "
                                f"session_id={session_id}, question_id={current_question_id}, "
                                f"score={final_eval.get('score') if isinstance(final_eval, dict) else None}"
                            )
                        else:
                            logger.warning(
                                f"[停止录音] 未找到session_id，无法补齐最终题目评分: question_id={current_question_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[停止录音] 补齐最终题目candidate_answers失败（不影响停止录音）: {str(e)}",
                            exc_info=True,
                        )

            # 计算面试总时长（audio_duration）：从开始面试到停止面试的时间
            # 重要：audio_duration应该存储整个面试的总时长，而不是单个题目的时长
            # 注意：audio_duration字段存储单位是分钟（minutes），与面试结束后显示的时间保持一致
            # 应该在stop_recording时统一更新该invitation_id下所有session的audio_duration
            if interview_start_time:
                audio_duration_seconds = time.time() - interview_start_time  # 秒
                # 转换为分钟并保留两位小数
                audio_duration_minutes = round(audio_duration_seconds / 60, 2)
                
                # 更新interview_session表的audio_duration字段
                if invitation_id:
                    # 统一更新该邀请下所有session的audio_duration为相同的总时长
                    # 注意：所有session的audio_duration应该相同，表示整个面试的总时长
                    # 重要：audio_duration字段存储单位是分钟（minutes），与面试结束后显示的时间保持一致
                    if self.db_manager.db_type == 'postgresql':
                        update_duration_sql = """
                        UPDATE interview_session
                        SET audio_duration = %s
                        WHERE invitation_id = %s
                        """
                    else:
                        # SQLite也使用相同的更新方式
                        update_duration_sql = """
                        UPDATE interview_session
                        SET audio_duration = %s
                        WHERE invitation_id = %s
                        """
                    
                    rows_affected = self.db_manager.execute_update(update_duration_sql, (audio_duration_minutes, invitation_id))
                    if rows_affected == 0:
                        logger.warning(f"更新面试总时长失败: 没有行被更新, invitation_id={invitation_id}")
                    else:
                        logger.info(f"已更新面试总时长: invitation_id={invitation_id}, duration={audio_duration_minutes}分钟, rows_affected={rows_affected}")
                        logger.info(f"  已将该invitation_id下所有session的audio_duration统一更新为总时长（分钟）")

            # 通知后台写入任务结束，并等待队列写完后再关闭录音文件
            audio_queue = client_info.get('audio_queue')
            audio_writer = client_info.get('audio_writer')
            if audio_queue and audio_writer:
                try:
                    # 发送结束信号
                    await audio_queue.put(None)
                    # 等待所有待写入的数据处理完成
                    await audio_queue.join()
                except Exception as e:
                    logger.warning(f"等待录音队列写入完成时出错（不影响主流程）: {str(e)}")

            # 关闭录音文件并记录完整录音URL到数据库
            if audio_writer:
                try:
                    audio_writer.close()
                    logger.info(f"录音文件已关闭: {client_info.get('audio_file_path')}")
                except Exception as e:
                    logger.error(f"关闭录音文件失败: {str(e)}")
                finally:
                    client_info['audio_writer'] = None
                    client_info['audio_queue'] = None
                    client_info['audio_writer_task'] = None

            audio_filename = client_info.get('audio_filename')
            if invitation_id and audio_filename:
                answer_audio_url = f"/api/v1/interview/answer-audio/{audio_filename}"
                client_info['answer_audio_url'] = answer_audio_url

                try:
                    if self.db_manager.db_type == 'postgresql':
                        update_audio_sql = """
                        UPDATE interview_session
                        SET session_content = COALESCE(session_content, '') || %s
                        WHERE invitation_id = %s
                        """
                    else:
                        update_audio_sql = """
                        UPDATE interview_session
                        SET session_content = COALESCE(session_content, '') || ?
                        WHERE invitation_id = ?
                        """

                    append_text = f"\n[FULL_AUDIO_URL] {answer_audio_url}"
                    rows_affected = self.db_manager.execute_update(update_audio_sql, (append_text, invitation_id))
                    if rows_affected == 0:
                        logger.warning(f"更新完整录音URL失败: 没有行被更新, invitation_id={invitation_id}")
                    else:
                        logger.info(f"已写入完整录音URL到session_content: invitation_id={invitation_id}, url={answer_audio_url}, rows_affected={rows_affected}")
                except Exception as e:
                    logger.warning(f"写入完整录音URL到session_content失败（不影响主流程）: {str(e)}", exc_info=True)

            # 重置状态
            client_info.update({
                'is_recording': False,
                'asr_session': None,
                'current_question_id': None,
                'current_question_text': '',
                'silence_start_time': None,
                'last_speech_time': None,
                'is_follow_up_question': False,
                'follow_up_question_id': None,
                'asr_queue': None,
                'asr_task': None,
                'prev_was_speech': True,
                'dc_running_mean': None,
            })

            logger.info(f"停止录音: connection={connection_id}")

            # 安全发送消息，检查连接状态
            await self._safe_send(
                client_info['websocket'],
                {
                    'type': 'recording_stopped',
                    'timestamp': datetime.now().isoformat(),
                    'answer_audio_url': client_info.get('answer_audio_url'),
                },
                connection_id
            )

        except Exception as e:
            logger.error(f"停止录音失败: {str(e)}", exc_info=True)
    
    async def handle_manual_next_question(self, connection_id: str, data: Dict[str, Any]):
        """
        处理手动切换到下一题的请求

        回答完毕下一题操作流程：
        步骤一：前端操作
        - 用户点击"回答完毕，下一题"按钮
        - 前端发送manual_next_question消息

        步骤二：后端操作
        - 停止对当前题的音频流采集（仅流分段，非连接关闭）
        - 保存当前题目的回答到数据库
        - 调用LLM完成该题的详细评分+追问判断

        步骤三：数据库操作
        - 更新interview_session表的状态
        - 记录评分结果和追问信息

        步骤四：切换到下一题
        - 获取下一题信息
        - 通知前端切换题目
        """
        data = data or {}
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            current_question_id = client_info.get('current_question_id')
            if not current_question_id:
                await self._safe_send(client_info['websocket'], {
                    'type': 'error',
                    'message': '当前没有正在回答的题目',
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                return

            logger.info(
                f"[回答完毕下一题] 开始处理: connection={connection_id}, question={current_question_id}, "
                f"trigger_source={data.get('trigger_source') or 'manual_next_question'}"
            )

            # 步骤二.1：停止对当前题的音频流采集（仅流分段，非连接关闭）
            # 注意：这里不调用stop_recording()，因为那会关闭整个ASR会话
            # 我们只是停止当前题的音频流，但保持ASR连接可用
            await self._stop_current_question_audio_stream(connection_id)

            # 步骤二.2：保存当前题目的回答到数据库
            current_answer = client_info.get('current_question_text', '').strip()
            # 即使答案为空，也要保存（至少创建记录），确保转文字结果存入表中
            if current_answer:
                logger.info(f"[回答完毕下一题] 保存答案: length={len(current_answer)}")
            else:
                logger.warning("[回答完毕下一题] 答案为空，但仍会保存记录")
            
            # 关键修复：先保存答案，再调用LLM评分，确保session_id存在
            # 步骤二.2：先保存答案到数据库，确保session_id和candidate_answers记录存在
            if current_answer:
                logger.info(f"[回答完毕下一题] 保存答案: length={len(current_answer)}")
            else:
                logger.warning("[回答完毕下一题] 答案为空，但仍会保存记录")
            
            # 始终保存答案（即使为空），确保转文字结果存入表中，并创建session_id
            await self._save_question_answer(connection_id, current_question_id, current_answer)
            
            # 步骤二.3：调用LLM完成该题的详细评分+追问判断（仅当有答案时）
            if current_answer:
                logger.info("[回答完毕下一题] 开始LLM评分和追问判断")
                await self.perform_real_time_evaluation(connection_id)
            else:
                logger.warning("[回答完毕下一题] 答案为空，跳过评分")

            if self._awaiting_follow_up_response(client_info):
                logger.info(
                    "[回答完毕下一题] 本题已触发追问，请先完成追问作答后再点「下一题」；本次不切题"
                )
                await self._safe_send(client_info['websocket'], {
                    'type': 'follow_up_pending',
                    'question_id': current_question_id,
                    'parent_answer_id': client_info.get('parent_answer_id'),
                    'message': '请先回答追问后再进入下一题',
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                return

            # 步骤三：获取下一题
            logger.info("[回答完毕下一题] 获取下一题")
            next_question_id = await self._get_next_question(connection_id, current_question_id)

            if next_question_id:
                # 步骤四：切换到下一题
                # 关键修复：在_switch_question之前，如果还没有保存答案，先保存（避免在_switch_question时重复保存）
                # 但此时parent_answer_id可能已经设置，所以_save_question_answer会正确处理
                if current_answer:
                    # 答案已经在perform_real_time_evaluation中保存了，这里不需要再次保存
                    # 但如果perform_real_time_evaluation没有保存（比如评分失败），这里需要保存
                    pass
                logger.info(f"[回答完毕下一题] 切换到下一题: {next_question_id}")
                await self._switch_question(
                    connection_id,
                    next_question_id,
                    trigger_source=(data or {}).get('trigger_source') or 'manual_next_question',
                )
                # 防重复：记录刚切换到的题目与时间，避免前端重复发 manual_next_question 时误判为面试完成
                client_info['last_switched_question_id'] = next_question_id
                client_info['last_switch_time'] = time.time()

                # 通知前端
                await self._safe_send(client_info['websocket'], {
                    'type': 'next_question',
                    'current_question_id': current_question_id,
                    'next_question_id': next_question_id,
                    'auto_advanced': False,  # 手动切换
                    'timestamp': datetime.now().isoformat()
                }, connection_id)

                logger.info(f"[回答完毕下一题] 完成: {current_question_id} -> {next_question_id}")
            else:
                # 没有下一题：若当前题是刚切换到的（无答案且短时间内），视为重复请求，只回传当前题不结束面试
                now = time.time()
                last_id = client_info.get('last_switched_question_id')
                last_t = client_info.get('last_switch_time') or 0
                no_answer = not (client_info.get('current_question_text') or '').strip()
                if no_answer and current_question_id == last_id and (now - last_t) < 3.0:
                    logger.info(f"[回答完毕下一题] 忽略重复请求，保持当前题: {current_question_id}")
                    await self._safe_send(client_info['websocket'], {
                        'type': 'next_question',
                        'current_question_id': current_question_id,
                        'next_question_id': current_question_id,
                        'auto_advanced': False,
                        'timestamp': datetime.now().isoformat()
                    }, connection_id)
                    return
                # 真正没有下一题，面试完成
                logger.info("[回答完毕下一题] 面试已完成")
                await self._safe_send(client_info['websocket'], {
                    'type': 'interview_completed',
                    'current_question_id': current_question_id,
                    'timestamp': datetime.now().isoformat()
                }, connection_id)

        except Exception as e:
            logger.error(f"[回答完毕下一题] 处理失败: {str(e)}", exc_info=True)
            await self._safe_send(client_info['websocket'], {
                'type': 'error',
                'message': f'切换题目失败: {str(e)}',
                'timestamp': datetime.now().isoformat()
            }, connection_id)

    async def _stop_current_question_audio_stream(self, connection_id: str):
        """
        停止对当前题的音频流采集（仅流分段，非连接关闭）

        这是单题收尾处理的核心操作：
        - 停止当前题目的音频流采集
        - 但不关闭ASR WebSocket连接
        - 不停止服务，只做单题处理
        """
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            logger.debug(f"[音频流分段] 停止当前题音频流: connection={connection_id}")

            # 清理当前题目的音频数据缓冲区
            # 注意：这里不关闭ASR连接，只是停止当前题目的音频流采集
            if 'current_question_audio_chunks' in client_info:
                client_info['current_question_audio_chunks'] = []

            # 重置当前题目的音频相关状态
            # 但保持ASR连接可用，以便下一题继续使用
            client_info['current_question_audio_start_time'] = None

            logger.debug(f"[音频流分段] 当前题音频流已停止，ASR连接保持可用")

        except Exception as e:
            logger.error(f"[音频流分段] 停止失败: {str(e)}", exc_info=True)

    async def set_session_info(self, connection_id: str, data: Dict[str, Any]):
        """设置会话信息"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        invitation_id = data.get('invitation_id')
        question_id = data.get('question_id')

        client_info.update({
            'invitation_id': invitation_id,
            'question_id': question_id
        })

        logger.info(f"设置会话信息: connection={connection_id}, invitation={invitation_id}, question={question_id}")

        await self._safe_send(client_info['websocket'], {
            'type': 'session_set',
            'invitation_id': invitation_id,
            'question_id': question_id,
            'timestamp': datetime.now().isoformat()
        }, connection_id)

    async def process_recognition_result(self, connection_id: str, result: Dict[str, Any]):
        """处理识别结果（智能流式模式：支持静音检测和关键词检测）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            result_type = result.get('type', '')
            websocket = client_info['websocket']

            if (
                result_type in ('intermediate_result', 'final_result')
                and time.time() < float(client_info.get('asr_ignore_until') or 0.0)
            ):
                logger.debug(
                    f"[ASR分段] 丢弃切题后迟到识别结果: type={result_type}, "
                    f"question={client_info.get('current_question_id')}, "
                    f"text_preview={(result.get('text') or '')[:50]!r}"
                )
                return

            if result_type == 'intermediate_result':
                # 中间结果
                text = result.get('text', '')
                if text:
                    # 更新语音活动时间
                    client_info['last_speech_time'] = time.time()
                    client_info['silence_start_time'] = None
                    
                    await self._safe_send(websocket, {
                        'type': 'intermediate_text',
                        'text': text,
                        'question_id': client_info.get('current_question_id'),
                        'timestamp': datetime.now().isoformat()
                    }, connection_id)

            elif result_type == 'final_result':
                # 最终结果
                text = result.get('text', '')
                if text:
                    # 更新语音活动时间
                    client_info['last_speech_time'] = time.time()
                    client_info['silence_start_time'] = None
                    
                    # 累积文本（全局和当前题目）
                    client_info['accumulated_text'] += text
                    client_info['current_question_text'] += text
                    client_info['sentence_buffer'].append(text)

                    vc = self._classify_voice_completion_trigger(text, client_info)
                    if vc:
                        logger.info(f"检测到语音切题触发: source={vc}, text_preview={text[:120]!r}")
                        await self._handle_answer_completion(connection_id, completion_source=vc)
                        return

                    # 发送最终文本
                    await self._safe_send(websocket, {
                        'type': 'final_text',
                        'text': text,
                        'accumulated_text': client_info['accumulated_text'],
                        'current_question_text': client_info['current_question_text'],
                        'question_id': client_info.get('current_question_id'),
                        'timestamp': datetime.now().isoformat()
                    }, connection_id)

                    # 检查是否形成完整句子并进行评分
                    if self._is_sentence_end(text):
                        # 修复：移除句子结束时的实时评分，避免重复评分
                        # 评分统一在用户点击"下一题"或自动切换时进行
                        # await self.perform_real_time_evaluation(connection_id)
                        pass  # 占位符，避免语法错误

            elif result_type == 'error':
                # 识别错误
                error_code = result.get('error_code', result.get('status_code', 'UNKNOWN'))
                error_msg = result.get('message', result.get('status_text', '未知错误'))
                full_payload = result.get('full_payload', {})
                full_header = result.get('full_header', {})
                
                logger.error("=" * 60)
                logger.error(f"ASR识别错误 - 连接: {connection_id}")
                logger.error(f"错误码: {error_code}")
                logger.error(f"状态码: {result.get('status_code', 'N/A')}")
                logger.error(f"状态文本: {result.get('status_text', 'N/A')}")
                logger.error(f"错误消息: {error_msg}")
                if full_header:
                    logger.error(f"完整header: {json.dumps(full_header, ensure_ascii=False, indent=2)}")
                if full_payload:
                    logger.error(f"完整payload: {json.dumps(full_payload, ensure_ascii=False, indent=2)}")
                logger.error("=" * 60)
                
                # 常见错误码说明
                error_explanations = {
                    '40000002': '参数错误 - 可能是音频格式不匹配（需要PCM格式，16K采样率，16bit，单声道）',
                    '40000003': '音频数据格式错误',
                    '40000004': '采样率不匹配（需要8000或16000Hz）',
                    '40000005': '音频数据为空',
                }
                
                if error_code in error_explanations:
                    logger.error(f"错误说明: {error_explanations[error_code]}")

                await websocket.send(json.dumps({
                    'type': 'recognition_error',
                    'error_code': error_code,
                    'message': error_msg,
                    'explanation': error_explanations.get(error_code, ''),
                    'timestamp': datetime.now().isoformat()
                }))
            
            # 检查静音（在每次识别结果处理后，但错误情况除外）
            if result_type != 'error':
                await self._check_silence(connection_id)

        except Exception as e:
            logger.error(f"处理识别结果失败: {str(e)}", exc_info=True)

    def _is_sentence_end(self, text: str) -> bool:
        """判断是否为句子结束"""
        # 简单的句子结束检测逻辑
        end_markers = ['。', '！', '？', '!', '?', '.', '\n']
        return any(text.endswith(marker) for marker in end_markers)
    
    async def _check_silence(self, connection_id: str):
        """检查静音状态（VAD检测）"""
        if not self.enable_auto_advance:
            return
        
        client_info = self.active_connections.get(connection_id)
        if not client_info or not client_info.get('is_recording'):
            return
        
        # 如果已取消自动切换，不执行
        if client_info.get('auto_advance_cancelled', False):
            return
        
        current_time = time.time()
        last_speech_time = client_info.get('last_speech_time')
        
        if last_speech_time is None:
            return
        
        silence_duration = current_time - last_speech_time
        
        # 如果静音时间超过阈值
        if silence_duration >= self.silence_threshold:
            # 检查是否有有效回答（至少有一些文本）
            current_answer = client_info.get('current_question_text', '').strip()
            if len(current_answer) >= self.min_answer_length:  # 至少min_answer_length个字符才认为是有效回答
                # 设置静音开始时间（如果还没设置）
                if client_info.get('silence_start_time') is None:
                    client_info['silence_start_time'] = last_speech_time
                    logger.info(f"检测到静音开始: question={client_info.get('current_question_id')}, silence_duration={silence_duration:.2f}s")
                    
                    # 发送倒计时通知（3秒倒计时）
                    await self._send_silence_countdown(connection_id, 3)
                
                # 如果静音持续时间超过阈值，触发完成
                elapsed_silence = current_time - client_info['silence_start_time']
                if elapsed_silence >= self.silence_threshold:
                    logger.info(f"静音时间超过阈值，准备切换题目: duration={elapsed_silence:.2f}s")
                    await self._handle_answer_completion(connection_id, completion_source='silence_timeout')
            else:
                # 如果回答太短，重置静音计时
                client_info['silence_start_time'] = None
        else:
            # 有语音活动，重置静音计时
            client_info['silence_start_time'] = None
            client_info['auto_advance_cancelled'] = False
    
    async def _send_silence_countdown(self, connection_id: str, countdown: int):
        """发送静音倒计时通知"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        await self._safe_send(client_info['websocket'], {
            'type': 'silence_countdown',
            'countdown': countdown,
            'timestamp': datetime.now().isoformat()
        }, connection_id)
    
    async def handle_cancel_auto_advance(self, connection_id: str, data: Dict[str, Any]):
        """处理取消自动切换请求"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            client_info['auto_advance_cancelled'] = True
            client_info['silence_start_time'] = None
            
            logger.info(f"用户取消了自动切换: connection={connection_id}")
            
            await self._safe_send(client_info['websocket'], {
                'type': 'silence_cancelled',
                'timestamp': datetime.now().isoformat()
            }, connection_id)
            
        except Exception as e:
            logger.error(f"取消自动切换失败: {str(e)}", exc_info=True)

    async def handle_permission_status(self, connection_id: str, data: Dict[str, Any]):
        """处理麦克风权限状态更新"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            permission_state = data.get('state', 'unknown')
            permission_message = data.get('message', '')

            # 更新客户端信息中的权限状态
            client_info['microphone_permission'] = {
                'state': permission_state,
                'message': permission_message,
                'timestamp': datetime.now().isoformat()
            }

            # 记录权限状态到日志
            logger.info(f"🎙️ 客户端 {connection_id} 麦克风权限状态更新: {permission_state} - {permission_message}")

            # 可以在这里添加更多的权限状态处理逻辑
            # 比如根据权限状态调整服务行为等

        except Exception as e:
            logger.error(f"处理权限状态失败: {str(e)}", exc_info=True)

    async def handle_tts_playback_state(self, connection_id: str, data: Dict[str, Any]):
        """前端播报 TTS 状态：播放中时应忽略 ASR 里的「切题」类关键词，减少抢跳。"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        try:
            playing = bool(data.get('playing', data.get('is_playing', False)))
            client_info['tts_playing'] = playing
            client_info['tts_playback_updated_at'] = time.time()
            logger.info(f"TTS 播放状态: connection={connection_id}, playing={playing}")
        except Exception as e:
            logger.error(f"处理 tts_playback_state 失败: {str(e)}", exc_info=True)

    def _auto_advance_cooldown_remaining(self, client_info: Dict[str, Any]) -> float:
        last = float(client_info.get('last_auto_advance_at') or 0.0)
        elapsed = time.time() - last
        return max(0.0, self.advance_cooldown_seconds - elapsed)

    def _classify_voice_completion_trigger(self, text: str, client_info: Dict[str, Any]) -> Optional[str]:
        """
        若本句应触发「自动当作答毕并切题」，返回原因标签；否则返回 None。
        注意：不在 TTS 播放中响应；「下一题」默认不触发除非 enable_voice_next_question_keywords。
        """
        if client_info.get('tts_playing'):
            return None
        if not text or not text.strip():
            return None
        text_lower = text.lower()
        for keyword in self.phrase_completion_keywords:
            if keyword in text or keyword.lower() in text_lower:
                return 'keyword_phrase'
        if self.enable_voice_next_question_keywords:
            buf_len = len((client_info.get('current_question_text') or '').strip())
            for keyword in self.voice_next_question_keywords:
                if not keyword:
                    continue
                if keyword in text or keyword.lower() in text_lower:
                    if buf_len >= self.min_chars_before_voice_next:
                        return 'keyword_next'
        return None

    async def _handle_answer_completion(self, connection_id: str, completion_source: str = 'unknown'):
        """处理回答完成（自动切换到下一题或等待手动切换）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            if completion_source in ('keyword_phrase', 'keyword_next', 'silence_timeout'):
                rem = self._auto_advance_cooldown_remaining(client_info)
                if rem > 0:
                    logger.info(
                        f"[自动切题] 冷却中忽略: source={completion_source}, "
                        f"remaining={rem:.2f}s, question={client_info.get('current_question_id')}"
                    )
                    return

            current_question_id = client_info.get('current_question_id')
            current_answer = client_info.get('current_question_text', '').strip()
            
            if not current_question_id:
                return
            
            # 保存当前题目的回答（即使答案为空也要保存，确保转文字结果存入表中）
            await self._save_question_answer(connection_id, current_question_id, current_answer)
            
            # 一个问题回答完毕就进行一次评分（仅当有答案时）
            if current_answer:
                await self.perform_real_time_evaluation(connection_id)

            if self._awaiting_follow_up_response(client_info):
                logger.info(
                    f"[自动切题] 已触发追问，等待追问作答后再切题，忽略: source={completion_source}, "
                    f"question={current_question_id}"
                )
                return

            # 获取下一题
            next_question_id = await self._get_next_question(connection_id, current_question_id)
            
            if next_question_id:
                # 切换到下一题
                await self._switch_question(connection_id, next_question_id, trigger_source=completion_source)
                
                # 通知前端切换到下一题
                await self._safe_send(client_info['websocket'], {
                    'type': 'next_question',
                    'current_question_id': current_question_id,
                    'next_question_id': next_question_id,
                    'auto_advanced': True,
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                if completion_source in ('keyword_phrase', 'keyword_next', 'silence_timeout'):
                    client_info['last_auto_advance_at'] = time.time()
            else:
                # 没有下一题，面试结束
                # 计算面试总时长
                interview_start_time = client_info.get('interview_start_time')
                audio_duration = None
                if interview_start_time:
                    audio_duration = time.time() - interview_start_time  # 秒
                    duration_minutes = round(audio_duration / 60, 2)  # 分钟及其后两位
                
                await self._safe_send(client_info['websocket'], {
                    'type': 'interview_completed',
                    'current_question_id': current_question_id,
                    'duration_minutes': duration_minutes if audio_duration else None,
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                
        except Exception as e:
            logger.error(f"处理回答完成失败: {str(e)}", exc_info=True)
    
    async def _get_next_question(self, connection_id: str, current_question_id: str) -> Optional[str]:
        """获取下一题（按question_order排序，先基础题再专业题）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return None
        
        try:
            invitation_id = client_info.get('invitation_id')
            if not invitation_id:
                return None
            
            # 查询当前题目的顺序和类型
            current_sql = """
            SELECT question_order, question_type
            FROM interview_question
            WHERE question_id = %s AND invitation_id = %s
            LIMIT 1
            """
            if self.db_manager.db_type != 'postgresql':
                current_sql = current_sql.replace('%s', '?')
            
            current_question = self.db_manager.fetch_one(current_sql, (current_question_id, invitation_id))
            if not current_question:
                logger.warning(f"未找到当前题目: question_id={current_question_id}, invitation_id={invitation_id}")
                return None
            
            current_order = current_question.get('question_order', 0)
            current_type = current_question.get('question_type', '')
            
            logger.info(f"当前题目: question_id={current_question_id}, order={current_order}, type={current_type}")
            
            # 修复：先查找同一类型内order更大的题目，如果没有，再查找下一个类型的第一题
            # 这样可以确保：先基础题，再专业题，按order顺序
            # 注意：数据库中的类型是BASIC和SPECIALTY
            if self.db_manager.db_type == 'postgresql':
                # 先查找同一类型内order更大的题目
                same_type_sql = """
                SELECT question_id
                FROM interview_question
                WHERE invitation_id = %s
                AND question_type = %s
                AND question_order > %s
                ORDER BY question_order ASC
                LIMIT 1
                """
                next_question = self.db_manager.fetch_one(same_type_sql, (invitation_id, current_type, current_order))
                
                if not next_question:
                    # 如果没有同一类型的下一题，查找下一个类型的第一题
                    # 注意：BASIC/BASIC_INFO优先于SPECIALTY/PROFESSIONAL
                    next_type_sql = """
                    SELECT question_id
                    FROM interview_question
                    WHERE invitation_id = %s
                    AND (
                        CASE question_type
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END >
                        CASE %s
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END
                    )
                    ORDER BY 
                        CASE question_type
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END,
                        question_order ASC
                    LIMIT 1
                    """
                    next_question = self.db_manager.fetch_one(next_type_sql, (invitation_id, current_type))
            else:
                # SQLite版本
                same_type_sql = """
                SELECT question_id
                FROM interview_question
                WHERE invitation_id = ?
                AND question_type = ?
                AND question_order > ?
                ORDER BY question_order ASC
                LIMIT 1
                """
                next_question = self.db_manager.fetch_one(same_type_sql, (invitation_id, current_type, current_order))
                
                if not next_question:
                    # 如果没有同一类型的下一题，查找下一个类型的第一题
                    # 注意：BASIC/BASIC_INFO优先于SPECIALTY/PROFESSIONAL
                    next_type_sql = """
                    SELECT question_id
                    FROM interview_question
                    WHERE invitation_id = ?
                    AND (
                        CASE question_type
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END >
                        CASE ? 
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END
                    )
                    ORDER BY 
                        CASE question_type
                            WHEN 'BASIC' THEN 1
                            WHEN 'BASIC_INFO' THEN 1
                            WHEN 'SPECIALTY' THEN 2
                            WHEN 'PROFESSIONAL' THEN 2
                            ELSE 3
                        END,
                        question_order ASC
                    LIMIT 1
                    """
                    next_question = self.db_manager.fetch_one(next_type_sql, (invitation_id, current_type))
            
            if next_question:
                next_question_id = next_question.get('question_id') if isinstance(next_question, dict) else next_question[0]
                # 查询下一题的order和type，用于日志
                next_info_sql = "SELECT question_order, question_type FROM interview_question WHERE question_id = %s LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    next_info_sql = next_info_sql.replace('%s', '?')
                next_info = self.db_manager.fetch_one(next_info_sql, (next_question_id,))
                next_order = next_info.get('question_order', 0) if next_info else 0
                next_type = next_info.get('question_type', '') if next_info else ''
                logger.info(f"找到下一题: {current_question_id}(order={current_order}, type={current_type}) -> {next_question_id}(order={next_order}, type={next_type})")
                return next_question_id
            
            logger.info(f"没有下一题，面试已完成: current_question_id={current_question_id}")
            return None
            
        except Exception as e:
            logger.error(f"获取下一题失败: {str(e)}", exc_info=True)
            return None

    async def perform_real_time_evaluation(self, connection_id: str):
        """执行实时评价（一个问题回答完毕就进行一次评分）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            # 使用当前题目的回答文本
            question_id = client_info.get('current_question_id')
            answer_text = client_info.get('current_question_text', '').strip()
            
            if not question_id or not answer_text:
                logger.debug(f"跳过评分：question_id={question_id}, answer_length={len(answer_text)}")
                return
            
            # 获取或创建session_id（用于评分记录）
            # 注意：session_id应该在_save_question_answer中已经创建，这里直接查找
            invitation_id = client_info.get('invitation_id')
            if not invitation_id:
                return
            
            # 检查是否为追问答案
            is_follow_up = client_info.get('is_follow_up_question', False)
            parent_answer_id = client_info.get('parent_answer_id')  # 使用parent_answer_id而不是follow_up_question_id
            
            # 如果parent_answer_id不存在，尝试通过检查主答案记录来判断是否是追问回答
            # 检查主答案记录是否有follow_up_question但没有follow_up_answer_text，说明这是追问回答
            if not is_follow_up or not parent_answer_id:
                # 与 _save_question_answer 一致：按邀请只取最新一条 interview_session（question_id 会随答题更新，不能用来定位会话）
                session_check_sql = "SELECT session_id FROM interview_session WHERE invitation_id = %s ORDER BY create_time DESC LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    session_check_sql = session_check_sql.replace('%s', '?')
                session_check = self.db_manager.fetch_one(session_check_sql, (invitation_id,))
                
                if session_check:
                    session_id_for_check = session_check.get('session_id') if isinstance(session_check, dict) else session_check[0]
                    
                    # 查找该session_id和question_id的主答案记录，如果有follow_up_question但没有follow_up_answer_text，说明这是追问回答
                    check_follow_up_sql = """
                    SELECT id, follow_up_question, follow_up_answer_text 
                    FROM candidate_answers 
                    WHERE session_id = %s
                      AND question_id = %s 
                      AND is_follow_up = FALSE
                      AND follow_up_question IS NOT NULL
                      AND follow_up_question != ''
                      AND (follow_up_answer_text IS NULL OR follow_up_answer_text = '')
                    ORDER BY create_time DESC
                    LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        check_follow_up_sql = check_follow_up_sql.replace('%s', '?')
                    
                    follow_up_check = self.db_manager.fetch_one(check_follow_up_sql, (session_id_for_check, question_id))
                    if follow_up_check:
                        if isinstance(follow_up_check, dict):
                            parent_answer_id = follow_up_check.get('id')
                        else:
                            parent_answer_id = follow_up_check[0]
                        is_follow_up = True
                        client_info['is_follow_up_question'] = True
                        client_info['parent_answer_id'] = parent_answer_id
                        logger.info(f"[追问检查] ✅ 通过数据库检查发现是追问答案: parent_answer_id={parent_answer_id}, session_id={session_id_for_check}, question_id={question_id}")
                else:
                    logger.warning(f"[追问检查] ⚠️ 未找到 interview_session: invitation_id={invitation_id}")
            
            logger.info(f"[追问检查] is_follow_up={is_follow_up}, parent_answer_id={parent_answer_id}, question_id={question_id}, answer_length={len(answer_text)}")
            
            if is_follow_up and parent_answer_id:
                # 这是追问答案，直接使用主答案的session_id和question_id
                # 从主答案记录中获取session_id和question_id
                main_answer_sql = """
                SELECT session_id, question_id FROM candidate_answers 
                WHERE id = %s
                LIMIT 1
                """
                if self.db_manager.db_type != 'postgresql':
                    main_answer_sql = main_answer_sql.replace('%s', '?')
                
                main_answer_data = self.db_manager.fetch_one(main_answer_sql, (parent_answer_id,))
                if main_answer_data:
                    session_id = main_answer_data['session_id'] if isinstance(main_answer_data, dict) else main_answer_data[0]
                    question_id = main_answer_data['question_id'] if isinstance(main_answer_data, dict) else main_answer_data[1]
                    logger.debug(f"找到主答案记录，进行追问评分: session_id={session_id}, question_id={question_id}, parent_answer_id={parent_answer_id}")
                else:
                    logger.warning(f"未找到主答案记录，跳过追问评分: parent_answer_id={parent_answer_id}")
                    return
            else:
                # 这是主问题答案，查找session记录
                # 关键修复：先尝试从candidate_answers获取session_id（因为_save_question_answer已经创建了记录）
                # 先尝试只通过question_id查找candidate_answers（因为candidate_answers表可能没有invitation_id字段）
                candidate_answer_sql = "SELECT session_id FROM candidate_answers WHERE question_id = %s ORDER BY create_time DESC LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    candidate_answer_sql = candidate_answer_sql.replace('%s', '?')
                
                candidate_answer_check = self.db_manager.fetch_one(candidate_answer_sql, (question_id,))
                if candidate_answer_check:
                    session_id = candidate_answer_check.get('session_id') if isinstance(candidate_answer_check, dict) else candidate_answer_check[0]
                    logger.info(f"从candidate_answers获取session_id: session_id={session_id}, question_id={question_id}")
                else:
                    # 如果candidate_answers中没有记录，尝试通过invitation_id查找最新的session记录
                    check_sql = "SELECT session_id FROM interview_session WHERE invitation_id = %s ORDER BY create_time DESC LIMIT 1"
                    if self.db_manager.db_type != 'postgresql':
                        check_sql = check_sql.replace('%s', '?')
                    
                    existing_session = self.db_manager.fetch_one(check_sql, (invitation_id,))
                    if existing_session:
                        session_id = existing_session.get('session_id') if isinstance(existing_session, dict) else existing_session[0]
                        logger.debug(f"从interview_session找到session记录，进行评分: session_id={session_id}, question_id={question_id}")
                    else:
                        logger.warning(f"未找到session记录，跳过评分: invitation_id={invitation_id}, question_id={question_id}")
                        return  # 如果没有session记录，说明答案还未保存，跳过评分

            if not answer_text.strip():
                return

            # 如果是追问答案，需要获取追问问题的信息和评估要点（从主答案记录中获取）
            custom_question_content = None
            custom_evaluation_points = None
            if is_follow_up and parent_answer_id:
                # 从主答案记录中获取追问问题的信息和评估要点
                follow_up_info_sql = """
                SELECT follow_up_question, follow_up_evaluation_points 
                FROM candidate_answers 
                WHERE id = %s
                LIMIT 1
                """
                if self.db_manager.db_type != 'postgresql':
                    follow_up_info_sql = follow_up_info_sql.replace('%s', '?')
                
                follow_up_info = self.db_manager.fetch_one(follow_up_info_sql, (parent_answer_id,))
                if follow_up_info:
                    # follow_up_question字段存储的是追问问题文本
                    custom_question_content = follow_up_info.get('follow_up_question') if isinstance(follow_up_info, dict) else follow_up_info[0]
                    
                    # follow_up_evaluation_points存储的是追问的评估要点
                    follow_up_eval_points_raw = follow_up_info.get('follow_up_evaluation_points') if isinstance(follow_up_info, dict) else follow_up_info[1]
                    if follow_up_eval_points_raw:
                        try:
                            import json
                            if isinstance(follow_up_eval_points_raw, str):
                                custom_evaluation_points = json.loads(follow_up_eval_points_raw)
                            else:
                                custom_evaluation_points = follow_up_eval_points_raw
                            logger.info(f"获取到追问评估要点: {len(custom_evaluation_points)} 个")
                        except Exception as e:
                            logger.warning(f"解析追问评估要点失败: {str(e)}")
                            custom_evaluation_points = None
                    
                    logger.info(f"使用追问问题内容进行评分: question_content={custom_question_content[:50] if custom_question_content else None}...")

            # 异步执行评分（如果是追问，传递自定义问题内容和评估要点）
            evaluation_result = await self.realtime_scorer.evaluate_answer(
                session_id,
                question_id,
                answer_text,
                custom_question_content=custom_question_content,
                custom_evaluation_points=custom_evaluation_points
            )

            # 发送评分结果
            await self._safe_send(client_info['websocket'], {
                'type': 'evaluation_result',
                'result': evaluation_result,
                'is_follow_up': is_follow_up,  # 标识是否为追问答案的评分
                'timestamp': datetime.now().isoformat()
            }, connection_id)

            # 如果是追问答案，直接更新主答案记录的追问相关字段（不触发新的追问）
            if is_follow_up and parent_answer_id:
                logger.info(f"[追问评分-步骤1] 开始更新主答案记录: parent_answer_id={parent_answer_id}, answer_length={len(answer_text)}")
                # 更新主答案记录的追问回答和评分结果
                try:
                    import json
                    final_score = evaluation_result.get('score', 0)
                    logger.info(f"[追问评分-步骤2] 追问评分结果: score={final_score}")
                    
                    # 获取主答案的评分（用于计算综合评分）
                    main_answer_sql = """
                    SELECT final_score, follow_up_evaluation_points 
                    FROM candidate_answers 
                    WHERE id = %s
                    LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        main_answer_sql = main_answer_sql.replace('%s', '?')
                    
                    main_answer_data = self.db_manager.fetch_one(main_answer_sql, (parent_answer_id,))
                    main_score = 0
                    evaluation_points = None
                    if main_answer_data:
                        if isinstance(main_answer_data, dict):
                            main_score = main_answer_data.get('final_score', 0)
                            evaluation_points_raw = main_answer_data.get('follow_up_evaluation_points')
                        else:
                            main_score = main_answer_data[0] if len(main_answer_data) > 0 else 0
                            evaluation_points_raw = main_answer_data[1] if len(main_answer_data) > 1 else None
                        
                        # 解析评估要点
                        if evaluation_points_raw:
                            try:
                                if isinstance(evaluation_points_raw, str):
                                    evaluation_points = json.loads(evaluation_points_raw)
                                else:
                                    evaluation_points = evaluation_points_raw
                            except Exception as e:
                                logger.warning(f"解析追问评估要点失败: {str(e)}")
                                evaluation_points = None
                    
                    # 计算综合评分：原题评分权重60%，追问评分权重40%
                    # 如果追问回答得好，可以提升综合评分；如果追问回答得差，综合评分会降低
                    comprehensive_score = main_score * 0.6 + final_score * 0.4
                    logger.info(f"[追问评分-步骤3] 计算综合评分: main_score={main_score}, follow_up_score={final_score}, comprehensive_score={comprehensive_score}")
                    
                    # 构建完整的评估结果（与主问题答案格式一致，参考evaluation_result字段格式）
                    # 获取追问评估要点（优先从custom_evaluation_points获取，其次从主答案记录获取）
                    follow_up_points_for_update = None
                    if custom_evaluation_points:
                        follow_up_points_for_update = custom_evaluation_points
                        logger.info(f"[追问评分-步骤4] 使用custom_evaluation_points: points_count={len(custom_evaluation_points) if custom_evaluation_points else 0}")
                    else:
                        # 尝试从主答案记录中获取
                        main_answer_points_sql = "SELECT follow_up_evaluation_points FROM candidate_answers WHERE id = %s"
                        if self.db_manager.db_type != 'postgresql':
                            main_answer_points_sql = main_answer_points_sql.replace('%s', '?')
                        main_points_data = self.db_manager.fetch_one(main_answer_points_sql, (parent_answer_id,))
                        if main_points_data:
                            points_raw = main_points_data.get('follow_up_evaluation_points') if isinstance(main_points_data, dict) else main_points_data[0]
                            if points_raw:
                                try:
                                    if isinstance(points_raw, str):
                                        follow_up_points_for_update = json.loads(points_raw)
                                    else:
                                        follow_up_points_for_update = points_raw
                                    logger.info(f"[追问评分-步骤4] 从主答案记录获取评估要点: points_count={len(follow_up_points_for_update) if follow_up_points_for_update else 0}")
                                except Exception as e:
                                    logger.warning(f"[追问评分] 解析评估要点失败: {str(e)}")
                    
                    # 构建完整的评估结果（与主问题答案格式一致，参考evaluation_result字段格式）
                    # 关键修复：使用follow_up_evaluation_points，而不是主问题的point_evaluations
                    follow_up_evaluation_result = {
                        'score': final_score,
                        'reason': evaluation_result.get('reason', ''),
                        'dimensions': evaluation_result.get('evaluation_details', {}),
                        'point_evaluations': follow_up_points_for_update,  # 关键修复：使用追问的评估要点，而不是主问题的point_evaluations
                        'question_type': evaluation_result.get('question_type', 'SPECIALTY'),
                        'timestamp': datetime.now().isoformat()
                    }
                    
                    # follow_up_evaluation保存完整的评估结果（与evaluation_result格式一致）
                    follow_up_evaluation = follow_up_evaluation_result
                    logger.info(f"[追问评分-步骤5] 准备更新数据库: parent_answer_id={parent_answer_id}, answer_text_length={len(answer_text)}, comprehensive_score={comprehensive_score}")
                    
                    # 关键修复：强制更新所有字段，不使用COALESCE（因为它会保留旧的NULL值）
                    # 追问评分完成后，才是真正的evaluated状态
                    follow_up_evaluation_json = json.dumps(follow_up_evaluation, ensure_ascii=False)
                    logger.info(f"[追问评分-步骤5] 🔄 准备更新数据库: parent_answer_id={parent_answer_id}, answer_text={answer_text[:50]}..., follow_up_evaluation长度={len(follow_up_evaluation_json)}, comprehensive_score={comprehensive_score}")
                    logger.debug(f"[追问评分-步骤5] follow_up_evaluation内容: {follow_up_evaluation_json[:200]}...")
                    
                    points_json_for_update = json.dumps(follow_up_points_for_update, ensure_ascii=False) if follow_up_points_for_update else None
                    
                    # 强制更新所有字段，不使用COALESCE
                    update_main_answer_sql = """
                    UPDATE candidate_answers
                    SET follow_up_answer_text = %s,
                        follow_up_evaluation = %s,
                        comprehensive_score = %s,
                        follow_up_evaluation_points = %s,
                        status = 'evaluated',
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                    if self.db_manager.db_type != 'postgresql':
                        update_main_answer_sql = update_main_answer_sql.replace('%s', '?')
                    
                    rows_affected = self.db_manager.execute_update(update_main_answer_sql, (
                        answer_text,  # follow_up_answer_text: 追问后面试者的回答（强制更新）
                        follow_up_evaluation_json,  # follow_up_evaluation: 追问的评价（完整格式）
                        comprehensive_score,  # comprehensive_score: 综合评分
                        points_json_for_update,  # follow_up_evaluation_points: 追问评估要点（强制更新）
                        parent_answer_id  # 直接更新主答案记录
                    ))
                    if rows_affected == 0:
                        logger.error(f"[追问评分] ❌ 更新主答案追问信息失败: 没有行被更新, parent_answer_id={parent_answer_id}, 请检查parent_answer_id是否正确")
                    else:
                        logger.info(f"[追问评分] ✅ 已更新主答案追问信息: parent_answer_id={parent_answer_id}, follow_up_score={final_score}, comprehensive_score={comprehensive_score}, rows_affected={rows_affected}")
                        
                        # 关键修复：追问次数应该在触发追问时增加，而不是在追问评分时
                        # 这里不再增加follow_up_used，因为已经在触发追问时增加了
                        logger.info(f"[追问评分] 追问评分完成，追问次数已在触发时更新: session_id={session_id}")
                        client_info['is_follow_up_question'] = False
                        client_info['parent_answer_id'] = None
                except Exception as e:
                    logger.error(f"更新追问答案评分失败: {str(e)}", exc_info=True)
            else:
                # 这是主问题答案，检查是否需要触发追问
                # 必须从interview_question表的question_type字段判断题目类型
                question_type_check_sql = "SELECT question_type FROM interview_question WHERE question_id = %s LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    question_type_check_sql = question_type_check_sql.replace('%s', '?')
                
                question_type_data = self.db_manager.fetch_one(question_type_check_sql, (question_id,))
                question_type = question_type_data.get('question_type', 'SPECIALTY') if question_type_data else 'SPECIALTY'
                # 判断是否为基础题：BASIC或BASIC_INFO都视为基础题
                is_basic_question = question_type in ['BASIC', 'BASIC_INFO']
                
                # 只有专业题才触发追问，基础题不触发追问
                # 追问次数按「每道专业题一次」控制：由 candidate_answers 与评分器保证，不再用会话级 follow_up_used 卡死整场面谈
                if evaluation_result.get('need_follow_up') and not is_basic_question:
                    check_sql = "SELECT follow_up_limit FROM interview_session WHERE session_id = %s"
                    if self.db_manager.db_type != 'postgresql':
                        check_sql = check_sql.replace('%s', '?')

                    session_data = self.db_manager.fetch_one(check_sql, (session_id,))
                    db_follow_up_limit = session_data.get('follow_up_limit', 1) if session_data else 1
                    if not is_basic_question and db_follow_up_limit != 1:
                        logger.warning(f"⚠️ 数据库中的follow_up_limit={db_follow_up_limit}，但专业题最多追问1次，强制设置为1: question_id={question_id}, question_type={question_type}")
                        update_limit_sql = "UPDATE interview_session SET follow_up_limit = 1 WHERE session_id = %s"
                        if self.db_manager.db_type != 'postgresql':
                            update_limit_sql = update_limit_sql.replace('%s', '?')
                        self.db_manager.execute_update(update_limit_sql, (session_id,))
                        logger.info(f"✅ 已更新follow_up_limit: session_id={session_id}, follow_up_limit=1")

                    # 取本题最新主答案行：是否已答追问
                    check_already_answered_sql = """
                    SELECT follow_up_answer_text FROM candidate_answers 
                    WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
                    ORDER BY create_time DESC
                    LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        check_already_answered_sql = check_already_answered_sql.replace('%s', '?')
                    
                    already_answered = self.db_manager.fetch_one(check_already_answered_sql, (session_id, question_id))
                    has_follow_up_answer = False
                    if already_answered:
                        follow_up_answer_text = already_answered.get('follow_up_answer_text') if isinstance(already_answered, dict) else already_answered[0]
                        has_follow_up_answer = follow_up_answer_text and follow_up_answer_text.strip()
                    
                    follow_up_question = evaluation_result.get('follow_up_question')
                    follow_up_evaluation_points = evaluation_result.get('follow_up_evaluation_points')
                    
                    if follow_up_question and not has_follow_up_answer:
                        # 追问内容已由 realtime_scorer 写入 DB；此处只负责推送 TTS / 同步客户端状态（每主答案行最多推送一次）
                        try:
                            main_answer_sql = """
                            SELECT id, follow_up_evaluation_points FROM candidate_answers 
                            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
                            ORDER BY create_time DESC
                            LIMIT 1
                            """
                            if self.db_manager.db_type != 'postgresql':
                                main_answer_sql = main_answer_sql.replace('%s', '?')
                            
                            main_answer = self.db_manager.fetch_one(main_answer_sql, (session_id, question_id))
                            if isinstance(main_answer, dict):
                                parent_answer_id = main_answer.get('id')
                                existing_points = main_answer.get('follow_up_evaluation_points')
                            elif main_answer:
                                parent_answer_id = main_answer[0]
                                existing_points = main_answer[1] if len(main_answer) > 1 else None
                            else:
                                parent_answer_id = None
                                existing_points = None

                            if client_info.get('follow_up_ws_sent_parent_id') == parent_answer_id and parent_answer_id:
                                logger.info(
                                    f"追问 TTS 已对本主答案推送过，跳过重复: parent_answer_id={parent_answer_id}, question_id={question_id}"
                                )
                            elif parent_answer_id and follow_up_question:
                                # 确保评估要点被正确保存（如果_save_evaluation_result没有保存，这里补充保存）
                                import json
                                
                                # 关键修复：确保follow_up_evaluation_points是列表或字典，不是字符串
                                follow_up_evaluation_points_safe = follow_up_evaluation_points
                                if isinstance(follow_up_evaluation_points, str):
                                    try:
                                        follow_up_evaluation_points_safe = json.loads(follow_up_evaluation_points)
                                    except Exception as e:
                                        logger.warning(f"⚠️ 解析follow_up_evaluation_points失败: {str(e)}")
                                        follow_up_evaluation_points_safe = None
                                
                                # 如果主答案记录中没有评估要点，且evaluation_result中有，则补充保存
                                if not existing_points and follow_up_evaluation_points_safe:
                                    update_points_sql = """
                                    UPDATE candidate_answers 
                                    SET follow_up_evaluation_points = %s 
                                    WHERE id = %s
                                    """
                                    if self.db_manager.db_type != 'postgresql':
                                        update_points_sql = update_points_sql.replace('%s', '?')
                                    
                                    points_json = json.dumps(follow_up_evaluation_points_safe, ensure_ascii=False) if follow_up_evaluation_points_safe else None
                                    rows_affected = self.db_manager.execute_update(update_points_sql, (points_json, parent_answer_id))
                                    if rows_affected > 0:
                                        points_count = len(follow_up_evaluation_points_safe) if isinstance(follow_up_evaluation_points_safe, (list, dict)) else 0
                                        logger.info(f"✅ 已补充保存追问评估要点: parent_answer_id={parent_answer_id}, points_count={points_count}")
                                    else:
                                        logger.error(f"❌ 补充保存追问评估要点失败: parent_answer_id={parent_answer_id}")
                                elif existing_points:
                                    logger.debug(f"追问评估要点已存在，无需补充保存: parent_answer_id={parent_answer_id}")
                                elif not follow_up_evaluation_points_safe:
                                    logger.warning(f"⚠️ evaluation_result中没有follow_up_evaluation_points: parent_answer_id={parent_answer_id}")
                                
                                # 更新客户端信息，标记当前为追问问题（使用parent_answer_id而不是创建新记录）
                                client_info['is_follow_up_question'] = True
                                client_info['parent_answer_id'] = parent_answer_id  # 使用parent_answer_id标记主答案
                                
                                # 关键修复：确保follow_up_question是字符串，不是字典
                                follow_up_question_str = follow_up_question if isinstance(follow_up_question, str) else str(follow_up_question)
                                # 关键修复：防止string indices must be integers错误 - 确保follow_up_evaluation_points_safe是列表或字典
                                evaluation_points_count = len(follow_up_evaluation_points_safe) if isinstance(follow_up_evaluation_points_safe, (list, dict)) else 0
                                # 关键修复：防止string indices must be integers错误
                                follow_up_question_preview = follow_up_question_str[:50] if isinstance(follow_up_question_str, str) and len(follow_up_question_str) > 0 else (str(follow_up_question_str) if follow_up_question_str else "None")
                                logger.info(f"已标记追问状态: parent_answer_id={parent_answer_id}, follow_up_question={follow_up_question_preview}..., evaluation_points_count={evaluation_points_count}")
                                
                                # 发送追问问题到前端（特别提示这是追问问题）
                                # 关键修复：确保follow_up_question是字符串
                                follow_up_question_str = follow_up_question if isinstance(follow_up_question, str) else str(follow_up_question)
                                prefix = (self.follow_up_tts_prefix or '').strip()
                                question_for_tts = (
                                    f"{prefix}{follow_up_question_str}"
                                    if prefix
                                    else follow_up_question_str
                                )
                                await self._safe_send(client_info['websocket'], {
                                    'type': 'follow_up_trigger',
                                    'question': follow_up_question_str,
                                    'question_for_tts': question_for_tts,
                                    'question_role': 'follow_up',
                                    'is_follow_up': True,  # 标识这是追问问题
                                    'reason': '评分低于阈值，建议追问',
                                    'parent_answer_id': parent_answer_id,
                                    'timestamp': datetime.now().isoformat()
                                }, connection_id)
                                client_info['follow_up_ws_sent_parent_id'] = parent_answer_id

                                # 会话级 follow_up_used 仅作统计，不再用于「整场面谈只能追问一次」的卡控
                                update_follow_up_used_sql = """
                                UPDATE interview_session
                                SET follow_up_used = COALESCE(follow_up_used, 0) + 1
                                WHERE session_id = %s
                                """
                                if self.db_manager.db_type != 'postgresql':
                                    update_follow_up_used_sql = update_follow_up_used_sql.replace('%s', '?')

                                follow_up_used_rows = self.db_manager.execute_update(update_follow_up_used_sql, (session_id,))
                                if follow_up_used_rows > 0:
                                    logger.info(f"✅ [触发追问] 已更新追问统计次数 follow_up_used: session_id={session_id}")
                                else:
                                    logger.warning(f"⚠️ [触发追问] 更新 follow_up_used 失败（可能无 interview_session 行）: session_id={session_id}")
                            else:
                                logger.warning(
                                    f"有追问题干但未找到本题主答案行，无法推送追问: session_id={session_id}, question_id={question_id}"
                                )
                        except Exception as e:
                            logger.error(f"标记追问状态失败: {str(e)}", exc_info=True)
                    elif follow_up_question and has_follow_up_answer:
                        logger.info(
                            f"本题追问已作答，不再推送追问 TTS: question_id={question_id}, session_id={session_id}"
                        )
                    elif evaluation_result.get('need_follow_up'):
                        logger.warning(
                            f"评分建议追问但未生成追问题干，跳过推送: question_id={question_id}, session_id={session_id}"
                        )
                elif is_basic_question and evaluation_result.get('need_follow_up'):
                    # 基础题不进行追问
                    logger.info(f"基础题不进行追问: question_id={question_id}, question_type={question_type}")

            # 清空句子缓冲区（但保留current_question_text，因为可能还有后续回答）
            client_info['sentence_buffer'] = []

        except Exception as e:
            logger.error(f"实时评价失败: {str(e)}", exc_info=True)

    async def cleanup_connection(self, connection_id: str):
        """清理连接（添加超时保护）"""
        client_info = self.active_connections.get(connection_id)
        if client_info:
            # 通知录音后台任务退出，并尝试关闭录音文件
            audio_queue = client_info.get('audio_queue')
            audio_writer = client_info.get('audio_writer')
            if audio_queue and audio_writer:
                try:
                    await audio_queue.put(None)
                except Exception as e:
                    logger.warning(f"清理连接时发送录音结束信号失败: {str(e)}")

            if audio_writer:
                try:
                    audio_writer.close()
                    logger.info(f"清理连接时关闭录音文件: {client_info.get('audio_file_path')}")
                except Exception as e:
                    logger.error(f"清理连接时关闭录音文件失败: {str(e)}")
                finally:
                    client_info['audio_writer'] = None

            # 通知ASR任务退出
            asr_queue = client_info.get('asr_queue')
            asr_task = client_info.get('asr_task')
            if asr_queue:
                try:
                    await asr_queue.put(None)
                except Exception as e:
                    logger.warning(f"清理连接时发送ASR结束信号失败: {str(e)}")
            if asr_task:
                try:
                    await asyncio.wait_for(asr_task, timeout=5.0)
                except Exception as e:
                    logger.warning(f"清理连接时等待ASR任务结束失败: {str(e)}")

            # 关闭ASR会话（添加超时保护）
            if client_info.get('asr_session'):
                try:
                    await asyncio.wait_for(
                        self.asr_client.close_session(client_info['asr_session']) if self.asr_client else None,
                        timeout=5.0  # 最多等待5秒
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"关闭ASR会话超时: {client_info['asr_session']}")
                except Exception as e:
                    logger.error(f"关闭ASR会话失败: {str(e)}")

            # 从活跃连接中移除
            del self.active_connections[connection_id]

            logger.info(f"连接清理完成: {connection_id}")

    async def start_server(self):
        """启动服务器"""
        try:
            host = self.websocket_config['host']
            port = self.websocket_config['port']

            # 创建SSL上下文（如果需要）
            ssl_context = None
            if self.websocket_config.get('ssl_enabled'):
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                # 配置SSL证书...

            self.server = await websockets.serve(
                self.handle_connection,
                host,
                port,
                ssl=ssl_context,
                ping_interval=self.websocket_config['ping_interval'],
                ping_timeout=self.websocket_config['ping_timeout'],
                max_size=10 * 1024 * 1024,  # 10MB
                max_queue=32
            )

            self.is_running = True
            logger.info(f"语音面试WebSocket服务器启动成功: {host}:{port}")

            # 启动监控任务
            asyncio.create_task(self.monitor_connections())

        except Exception as e:
            logger.error(f"启动WebSocket服务器失败: {str(e)}", exc_info=True)
            raise

    async def stop_server(self):
        """停止服务器"""
        try:
            self.is_running = False

            if self.server:
                self.server.close()
                await self.server.wait_closed()

            # 清理所有连接
            for connection_id in list(self.active_connections.keys()):
                await self.cleanup_connection(connection_id)

            # 关闭线程池
            self.executor.shutdown(wait=True)

            logger.info("语音面试WebSocket服务器已停止")

        except Exception as e:
            logger.error(f"停止WebSocket服务器失败: {str(e)}", exc_info=True)

    async def monitor_connections(self):
        """监控连接状态"""
        while self.is_running:
            try:
                current_time = time.time()
                timeout = self.websocket_config['ping_timeout'] * 2

                # 检查超时连接
                expired_connections = []
                for connection_id, client_info in self.active_connections.items():
                    if current_time - client_info['last_activity'] > timeout:
                        expired_connections.append(connection_id)

                # 清理超时连接
                for connection_id in expired_connections:
                    logger.warning(f"连接超时，清理连接: {connection_id}")
                    await self.cleanup_connection(connection_id)

                # 记录连接统计信息
                active_count = len(self.active_connections)
                if active_count > 0:
                    logger.info(f"活跃连接数: {active_count}")

            except Exception as e:
                logger.error(f"监控连接状态失败: {str(e)}")

            await asyncio.sleep(30)  # 每30秒检查一次

    def get_connection_stats(self) -> Dict[str, Any]:
        """获取连接统计信息"""
        return {
            'total_connections': len(self.active_connections),
            'active_connections': sum(1 for c in self.active_connections.values() if c.get('is_recording')),
            'server_running': self.is_running,
            'timestamp': datetime.now().isoformat()
        }