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
import websockets
import ssl
from concurrent.futures import ThreadPoolExecutor

from shared.config.logging_config import get_logger
from shared.config.unified_config import load_unified_config
from shared.tools.database.db_manager import DatabaseManager
from shared.tools.llm.langchain_wrapper import LangChainWrapper

from .asr_client import AliyunASRClient
from .realtime_scorer import RealtimeScorer
from .token_manager import ASRTokenManager

logger = get_logger(__name__)


class VoiceInterviewWebSocketServer:
    """语音面试WebSocket服务器"""

    def __init__(self, config_manager, db_manager: DatabaseManager):
        self.config_manager = config_manager
        self.db_manager = db_manager
        
        # 从config_manager获取LLM配置
        from shared.config.unified_config import get_llm_config
        llm_config = get_llm_config()
        self.llm_wrapper = LangChainWrapper(llm_config)

        # 获取配置
        voice_config = config_manager.get_config('voice_interview_streaming')
        self.websocket_config = voice_config['websocket']
        self.audio_config = voice_config['audio']
        self.asr_config = voice_config['asr']
        self.recognition_config = voice_config.get('recognition', {})
        self.evaluation_config = voice_config['evaluation']
        self.streaming_config = voice_config.get('streaming_interview', {})

        # 合并ASR配置和识别参数配置
        asr_client_config = {
            **self.asr_config,
            'format': self.recognition_config.get('format', 'pcm'),
            'sample_rate': self.recognition_config.get('sample_rate', 16000),
            'endpoint': self.asr_config.get('endpoint', 'wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1')
        }

        # 初始化Token管理器（如果配置了AccessKey）
        token_manager = None
        access_key_id = self.asr_config.get('access_key_id')
        access_key_secret = self.asr_config.get('access_key_secret')
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
            # 使用静态Token
            static_token = self.asr_config.get('token')
            if not static_token:
                logger.warning("未配置Token或AccessKey，ASR功能可能无法使用")
            else:
                logger.info("使用静态Token，请定期手动更新")

        # 初始化组件
        self.asr_client = AliyunASRClient(
            appkey=self.asr_config['appkey'],
            token=self.asr_config.get('token'),  # 静态Token（可选）
            config=asr_client_config,
            token_manager=token_manager  # Token管理器（优先使用）
        )

        self.realtime_scorer = RealtimeScorer(
            llm_wrapper=self.llm_wrapper,
            db_manager=self.db_manager,
            config=self.evaluation_config
        )

        # 连接管理
        self.active_connections: Dict[str, Dict[str, Any]] = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        
        # 智能流式面试配置
        self.silence_threshold = self.streaming_config.get('silence_threshold', 3.0)  # 秒
        self.completion_keywords = self.streaming_config.get('completion_keywords', [
            '回答完毕', '回答完了', '我的回答完了', '下一题', '下一道题', '回答完成', '说完了'
        ])
        self.enable_auto_advance = self.streaming_config.get('enable_auto_advance', True)
        self.min_answer_length = self.streaming_config.get('min_answer_length', 10)

        # 服务器状态
        self.server = None
        self.is_running = False

        logger.info("语音面试WebSocket服务器初始化完成")

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
            'auto_advance_cancelled': False
        }

        self.active_connections[connection_id] = client_info
        logger.info(f"新FastAPI WebSocket连接建立: {connection_id}")

        try:
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
            'follow_up_question_id': None  # 当前追问问题的ID（如果有）
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
        if not client_info or not client_info.get('is_recording'):
            return

        try:
            # 更新活动时间
            client_info['last_activity'] = time.time()

            # 检查ASR会话是否存在且连接正常
            asr_session = client_info.get('asr_session')
            if not asr_session:
                logger.warning(f"ASR会话不存在，跳过音频数据: {connection_id}")
                return

            # 发送到ASR进行识别
            recognition_result = await self.asr_client.send_audio_data(
                asr_session,
                audio_data
            )

            # 如果返回None，检查是否是连接断开导致的
            if recognition_result is None:
                session_info = self.asr_client.active_sessions.get(asr_session)
                if session_info:
                    # 检查连接状态
                    if not session_info.get('is_connected', False):
                        logger.warning(f"ASR连接已断开，尝试重新连接: {connection_id}, session={asr_session}")
                        # 尝试重新连接ASR会话
                        try:
                            invitation_id = client_info.get('invitation_id')
                            if invitation_id:
                                asr_connected = await asyncio.wait_for(
                                    self.asr_client.connect_session(asr_session, timeout=10.0),
                                    timeout=15.0
                                )
                                if asr_connected:
                                    logger.info(f"ASR会话重新连接成功: {asr_session}")
                                    # 重新发送音频数据
                                    recognition_result = await self.asr_client.send_audio_data(
                                        asr_session,
                                        audio_data
                                    )
                                else:
                                    logger.error(f"ASR会话重新连接失败: {asr_session}")
                            else:
                                logger.error(f"无法重新连接ASR：缺少invitation_id: {connection_id}")
                        except Exception as reconnect_error:
                            logger.error(f"重新连接ASR会话失败: {str(reconnect_error)}", exc_info=True)
                    else:
                        # 连接正常但返回None，可能是ASR还未返回结果（正常情况）
                        logger.debug(f"ASR连接正常但暂未返回识别结果: {connection_id}, session={asr_session}")
                else:
                    logger.warning(f"ASR会话不存在: {connection_id}, session={asr_session}")

            if recognition_result:
                await self.process_recognition_result(connection_id, recognition_result)

        except Exception as e:
            logger.error(f"处理音频数据失败: {str(e)}", exc_info=True)

    async def start_recording(self, connection_id: str, data: Dict[str, Any]):
        """开始录音（智能流式模式：整个面试共用一个ASR会话）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return

        try:
            # 获取参数
            invitation_id = data.get('invitation_id')
            question_id = data.get('question_id')

            # 如果已经有ASR会话在运行，说明是切换题目，不需要重新创建ASR会话
            if client_info.get('asr_session') and client_info.get('is_recording'):
                # 切换题目：保存上一题的回答，切换到新题目
                await self._switch_question(connection_id, question_id)
                return

            # 首次启动：创建ASR会话
            logger.info(f"首次启动录音: invitation={invitation_id}, question={question_id}")
            
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
                'is_follow_up_question': False,  # 标识当前是否为追问问题
                'follow_up_question_id': None  # 当前追问问题的ID（如果有）
            })

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
        await self._switch_question(connection_id, new_question_id)
    
    async def _switch_question(self, connection_id: str, new_question_id: str):
        """切换题目（保存当前题目的回答，切换到新题目）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            old_question_id = client_info.get('current_question_id')
            
            # 防重复切换：如果新题目ID和当前题目ID相同，直接返回
            if old_question_id == new_question_id:
                logger.warning(f"切换题目：新题目ID与当前题目ID相同，忽略切换: {new_question_id}")
                return
            
            current_answer = client_info.get('current_question_text', '').strip()
            
            # 保存当前题目的回答
            if old_question_id and current_answer:
                await self._save_question_answer(
                    connection_id, 
                    old_question_id, 
                    current_answer
                )
            
            # 切换到新题目
            client_info['current_question_id'] = new_question_id
            client_info['current_question_text'] = ''  # 清空当前题目的文本
            client_info['accumulated_text'] = ''  # 清空累积文本（避免文本继续累积）
            client_info['sentence_buffer'] = []  # 清空句子缓冲区
            client_info['last_speech_time'] = time.time()
            client_info['silence_start_time'] = None
            # 切换题目时，清空追问状态
            client_info['is_follow_up_question'] = False
            client_info['follow_up_question_id'] = None
            
            logger.info(f"切换题目: {old_question_id} -> {new_question_id}, 已清空文本累积")
            
            # 注意：消息由调用者发送（handle_manual_next_question 或 _handle_answer_completion）
            # 这里只负责更新内部状态，避免重复发送消息
            
        except Exception as e:
            logger.error(f"切换题目失败: {str(e)}", exc_info=True)
    
    async def _save_question_answer(self, connection_id: str, question_id: str, answer_text: str):
        """保存题目的回答到数据库（一个问题回答完毕就记录一次答案）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            invitation_id = client_info.get('invitation_id')
            if not invitation_id:
                return
            
            # 检查是否已存在该题目的session记录
            check_sql = "SELECT session_id FROM interview_session WHERE invitation_id = %s AND question_id = %s LIMIT 1"
            if self.db_manager.db_type != 'postgresql':
                check_sql = check_sql.replace('%s', '?')
            
            existing_session = self.db_manager.fetch_one(check_sql, (invitation_id, question_id))
            
            # 获取题目内容（用于冗余存储）
            question_sql = "SELECT question_text FROM interview_question WHERE question_id = %s LIMIT 1"
            if self.db_manager.db_type != 'postgresql':
                question_sql = question_sql.replace('%s', '?')
            
            question_data = self.db_manager.fetch_one(question_sql, (question_id,))
            question_text = question_data.get('question_text', '') if question_data else ''
            
            if existing_session:
                # 更新现有记录（一个问题回答完毕就更新一次）
                session_id = existing_session['session_id']
                update_sql = """
                UPDATE interview_session
                SET candidate_answer = %s,
                    session_status = 'COMPLETED',
                    end_time = %s
                WHERE session_id = %s
                """
                if self.db_manager.db_type != 'postgresql':
                    update_sql = update_sql.replace('%s', '?')
                
                self.db_manager.execute(update_sql, (
                    answer_text,
                    datetime.now(),
                    session_id
                ))
                logger.info(f"已更新题目回答: question={question_id}, answer_length={len(answer_text)}")
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
                
                self.db_manager.execute(insert_sql, (
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
                logger.info(f"已创建题目回答记录: question={question_id}, answer_length={len(answer_text)}, follow_up_limit={follow_up_limit}")
            
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
                
                # 检查是否为追问答案
                is_follow_up = client_info.get('is_follow_up_question', False)
                follow_up_question_id = client_info.get('follow_up_question_id')
                
                if is_follow_up and follow_up_question_id:
                    # 这是追问答案，更新追问问题记录（将answer_text更新为追问答案）
                    update_follow_up_sql = """
                    UPDATE candidate_answers
                    SET answer_text = %s,
                        status = 'recorded',
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                    if self.db_manager.db_type != 'postgresql':
                        update_follow_up_sql = update_follow_up_sql.replace('%s', '?')
                    
                    self.db_manager.execute(update_follow_up_sql, (answer_text, follow_up_question_id))
                    logger.debug(f"已更新追问答案记录: follow_up_question_id={follow_up_question_id}")
                    
                    # 重置追问状态
                    client_info['is_follow_up_question'] = False
                    client_info['follow_up_question_id'] = None
                else:
                    # 这是主问题答案
                    if existing_answer:
                        # 更新现有答案记录
                        answer_id = existing_answer['id']
                        update_answer_sql = """
                        UPDATE candidate_answers
                        SET answer_text = %s,
                            status = 'recorded',
                            update_time = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """
                        if self.db_manager.db_type != 'postgresql':
                            update_answer_sql = update_answer_sql.replace('%s', '?')
                        
                        self.db_manager.execute(update_answer_sql, (answer_text, answer_id))
                        logger.debug(f"已更新candidate_answers记录: answer_id={answer_id}")
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
                        
                        self.db_manager.execute(insert_answer_sql, (
                            answer_id,
                            session_id,
                            question_id,
                            answer_text,
                            False,  # is_follow_up = False（主问题答案）
                            None,   # parent_answer_id = None（主问题没有父答案）
                            'recorded'  # status = 'recorded'（已录制，待评分）
                        ))
                        logger.debug(f"已创建candidate_answers记录: answer_id={answer_id}")
            except Exception as e:
                # candidate_answers表保存失败不影响主流程，只记录警告
                logger.warning(f"保存到candidate_answers表失败: {str(e)}", exc_info=True)
            
            # 保存到内存中
            client_info['question_answers'][question_id] = answer_text
            
        except Exception as e:
            logger.error(f"保存题目回答失败: {str(e)}", exc_info=True)

    async def stop_recording(self, connection_id: str, data: Dict[str, Any]):
        """停止录音（智能流式模式：保存当前题目回答后关闭ASR会话）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info or not client_info.get('is_recording'):
            return

        try:
            # 保存当前题目的回答
            current_question_id = client_info.get('current_question_id')
            current_answer = client_info.get('current_question_text', '').strip()
            if current_question_id and current_answer:
                await self._save_question_answer(connection_id, current_question_id, current_answer)
            
            # 计算面试总时长（audio_duration）：从开始面试到停止面试的时间
            # 注意：audio_duration字段存储的是秒（seconds），不是分钟
            # 重要：audio_duration应该存储整个面试的总时长，而不是单个题目的时长
            # 应该在stop_recording时统一更新该invitation_id下所有session的audio_duration
            interview_start_time = client_info.get('interview_start_time')
            if interview_start_time:
                audio_duration = time.time() - interview_start_time  # 秒
                
                # 更新interview_session表的audio_duration字段
                invitation_id = client_info.get('invitation_id')
                if invitation_id:
                    # 统一更新该邀请下所有session的audio_duration为相同的总时长
                    # 注意：所有session的audio_duration应该相同，表示整个面试的总时长
                    # 重要：audio_duration字段存储单位是秒（seconds），不是分钟
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
                    
                    self.db_manager.execute(update_duration_sql, (audio_duration, invitation_id))
                    # 显示格式：分钟及其后两位（如：3.51分钟）
                    duration_minutes = round(audio_duration / 60, 2)
                    logger.info(f"已更新面试总时长: invitation_id={invitation_id}, duration={duration_minutes}分钟")
                    logger.info(f"  已将该invitation_id下所有session的audio_duration统一更新为总时长")
            
            # 关闭ASR会话
            if client_info.get('asr_session'):
                await self.asr_client.close_session(client_info['asr_session'])

            # 重置状态
            client_info.update({
                'is_recording': False,
                'asr_session': None,
                'current_question_id': None,
                'current_question_text': '',
                'silence_start_time': None,
                'last_speech_time': None,
                'is_follow_up_question': False,
                'follow_up_question_id': None
            })

            logger.info(f"停止录音: connection={connection_id}")

            # 安全发送消息，检查连接状态
            await self._safe_send(
                client_info['websocket'],
                {
                    'type': 'recording_stopped',
                    'timestamp': datetime.now().isoformat()
                },
                connection_id
            )

        except Exception as e:
            logger.error(f"停止录音失败: {str(e)}", exc_info=True)
    
    async def handle_manual_next_question(self, connection_id: str, data: Dict[str, Any]):
        """处理手动切换到下一题的请求"""
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
            
            # 保存当前题目的回答并进行评分
            current_answer = client_info.get('current_question_text', '').strip()
            if current_answer:
                # 保存答案
                await self._save_question_answer(connection_id, current_question_id, current_answer)
                
                # 一个问题回答完毕就进行一次评分
                await self.perform_real_time_evaluation(connection_id)
            
            # 获取下一题
            next_question_id = await self._get_next_question(connection_id, current_question_id)
            
            if next_question_id:
                # 切换到下一题
                await self._switch_question(connection_id, next_question_id)
                
                # 通知前端
                await self._safe_send(client_info['websocket'], {
                    'type': 'next_question',
                    'current_question_id': current_question_id,
                    'next_question_id': next_question_id,
                    'auto_advanced': False,  # 手动切换
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
            else:
                # 没有下一题
                await self._safe_send(client_info['websocket'], {
                    'type': 'interview_completed',
                    'current_question_id': current_question_id,
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
                
        except Exception as e:
            logger.error(f"手动切换题目失败: {str(e)}", exc_info=True)
            await self._safe_send(client_info['websocket'], {
                'type': 'error',
                'message': f'切换题目失败: {str(e)}',
                'timestamp': datetime.now().isoformat()
            }, connection_id)

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

                    # 检查关键词触发（回答完毕、下一题等）
                    if self._check_completion_keywords(text):
                        logger.info(f"检测到完成关键词，准备切换题目: {text}")
                        await self._handle_answer_completion(connection_id)
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
                        await self.perform_real_time_evaluation(connection_id)

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
    
    def _check_completion_keywords(self, text: str) -> bool:
        """检查是否包含完成关键词"""
        text_lower = text.lower()
        for keyword in self.completion_keywords:
            if keyword in text or keyword.lower() in text_lower:
                return True
        return False
    
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
                    await self._handle_answer_completion(connection_id)
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
    
    async def _handle_answer_completion(self, connection_id: str):
        """处理回答完成（自动切换到下一题或等待手动切换）"""
        client_info = self.active_connections.get(connection_id)
        if not client_info:
            return
        
        try:
            current_question_id = client_info.get('current_question_id')
            current_answer = client_info.get('current_question_text', '').strip()
            
            if not current_question_id or not current_answer:
                return
            
            # 保存当前题目的回答并进行评分
            await self._save_question_answer(connection_id, current_question_id, current_answer)
            
            # 一个问题回答完毕就进行一次评分
            await self.perform_real_time_evaluation(connection_id)
            
            # 获取下一题
            next_question_id = await self._get_next_question(connection_id, current_question_id)
            
            if next_question_id:
                # 切换到下一题
                await self._switch_question(connection_id, next_question_id)
                
                # 通知前端切换到下一题
                await self._safe_send(client_info['websocket'], {
                    'type': 'next_question',
                    'current_question_id': current_question_id,
                    'next_question_id': next_question_id,
                    'auto_advanced': True,
                    'timestamp': datetime.now().isoformat()
                }, connection_id)
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
            follow_up_question_id = client_info.get('follow_up_question_id')
            
            if is_follow_up and follow_up_question_id:
                # 这是追问答案，使用追问问题的session_id
                # 追问问题记录在candidate_answers表中，需要找到对应的session_id
                follow_up_sql = """
                SELECT session_id FROM candidate_answers 
                WHERE id = %s
                LIMIT 1
                """
                if self.db_manager.db_type != 'postgresql':
                    follow_up_sql = follow_up_sql.replace('%s', '?')
                
                follow_up_data = self.db_manager.fetch_one(follow_up_sql, (follow_up_question_id,))
                if follow_up_data:
                    session_id = follow_up_data['session_id']
                    logger.debug(f"找到追问问题记录，进行评分: session_id={session_id}, follow_up_question_id={follow_up_question_id}")
                else:
                    logger.warning(f"未找到追问问题记录，跳过评分: follow_up_question_id={follow_up_question_id}")
                    return
            else:
                # 这是主问题答案，查找session记录
                check_sql = "SELECT session_id FROM interview_session WHERE invitation_id = %s AND question_id = %s LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    check_sql = check_sql.replace('%s', '?')
                
                existing_session = self.db_manager.fetch_one(check_sql, (invitation_id, question_id))
                if existing_session:
                    session_id = existing_session['session_id']
                    logger.debug(f"找到session记录，进行评分: session_id={session_id}, question_id={question_id}")
                else:
                    logger.warning(f"未找到session记录，跳过评分: invitation_id={invitation_id}, question_id={question_id}")
                    return  # 如果没有session记录，说明答案还未保存，跳过评分

            if not answer_text.strip():
                return

            # 异步执行评分
            loop = asyncio.get_event_loop()
            evaluation_result = await loop.run_in_executor(
                self.executor,
                self.realtime_scorer.evaluate_answer,
                session_id,
                question_id,
                answer_text
            )

            # 发送评分结果
            await self._safe_send(client_info['websocket'], {
                'type': 'evaluation_result',
                'result': evaluation_result,
                'is_follow_up': is_follow_up,  # 标识是否为追问答案的评分
                'timestamp': datetime.now().isoformat()
            }, connection_id)

            # 如果是追问答案，直接保存评分结果（不触发新的追问）
            if is_follow_up and follow_up_question_id:
                # 更新追问答案的评分结果
                try:
                    import json
                    final_score = evaluation_result.get('score', 0)
                    
                    # point_evaluations字段存储评估要点，不是评分维度
                    # 获取题目信息以获取评估要点
                    question_info_sql = """
                    SELECT 
                        iq.question_id,
                        iq.question_type,
                        iqs.evaluation_points
                    FROM interview_question iq
                    JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
                    WHERE iq.question_id = %s
                    LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        question_info_sql = question_info_sql.replace('%s', '?')
                    
                    question_info_data = self.db_manager.fetch_one(question_info_sql, (question_id,))
                    evaluation_points = None
                    if question_info_data:
                        evaluation_points = question_info_data.get('evaluation_points')
                        if evaluation_points:
                            try:
                                if isinstance(evaluation_points, str):
                                    evaluation_points = json.loads(evaluation_points)
                            except Exception as e:
                                logger.warning(f"解析评估要点失败: {str(e)}")
                                evaluation_points = None
                    
                    # 获取主答案的评分（用于计算综合评分）
                    parent_sql = """
                    SELECT parent_answer_id FROM candidate_answers WHERE id = %s LIMIT 1
                    """
                    if self.db_manager.db_type != 'postgresql':
                        parent_sql = parent_sql.replace('%s', '?')
                    
                    parent_data = self.db_manager.fetch_one(parent_sql, (follow_up_question_id,))
                    parent_answer_id = None
                    if parent_data:
                        if isinstance(parent_data, dict):
                            parent_answer_id = parent_data.get('parent_answer_id')
                        else:
                            parent_answer_id = parent_data[0]
                    
                    main_score = 0
                    if parent_answer_id:
                        main_answer_sql = """
                        SELECT final_score FROM candidate_answers WHERE id = %s LIMIT 1
                        """
                        if self.db_manager.db_type != 'postgresql':
                            main_answer_sql = main_answer_sql.replace('%s', '?')
                        
                        main_answer_data = self.db_manager.fetch_one(main_answer_sql, (parent_answer_id,))
                        if main_answer_data:
                            if isinstance(main_answer_data, dict):
                                main_score = main_answer_data.get('final_score', 0)
                            else:
                                main_score = main_answer_data[0] if len(main_answer_data) > 0 else 0
                    
                    # 计算综合评分：原题评分权重60%，追问评分权重40%
                    # 如果追问回答得好，可以提升综合评分；如果追问回答得差，综合评分会降低
                    comprehensive_score = main_score * 0.6 + final_score * 0.4
                    
                    # 保存追问评价（JSON格式，包含评分、理由、维度等）
                    follow_up_evaluation = {
                        'score': final_score,
                        'reason': evaluation_result.get('reason', ''),
                        'dimensions': evaluation_result.get('evaluation_details', {}),
                        'timestamp': datetime.now().isoformat()
                    }
                    
                    # 构建完整的评估结果（与主问题答案格式一致）
                    follow_up_evaluation_result = {
                        'score': final_score,
                        'reason': evaluation_result.get('reason', ''),
                        'dimensions': evaluation_result.get('evaluation_details', {}),
                        'timestamp': datetime.now().isoformat()
                    }
                    
                    update_follow_up_answer_sql = """
                    UPDATE candidate_answers
                    SET follow_up_answer_text = %s,
                        status = 'evaluated',
                        follow_up_evaluation_points = %s,
                        follow_up_evaluation = %s,
                        final_score = %s,
                        comprehensive_score = %s,
                        evaluation_result = %s,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                    if self.db_manager.db_type != 'postgresql':
                        update_follow_up_answer_sql = update_follow_up_answer_sql.replace('%s', '?')
                    
                    self.db_manager.execute(update_follow_up_answer_sql, (
                        answer_text,  # follow_up_answer_text: 追问后面试者的回答
                        json.dumps(evaluation_points) if evaluation_points else None,  # follow_up_evaluation_points: 追问的评估要点
                        json.dumps(follow_up_evaluation),  # follow_up_evaluation: 追问的评价（JSON格式，保留兼容性）
                        final_score,  # final_score: 追问答案的评分
                        comprehensive_score,  # comprehensive_score: 综合评分
                        json.dumps(follow_up_evaluation_result),  # evaluation_result: 完整的评估结果（与主问题答案格式一致）
                        follow_up_question_id
                    ))
                    
                    # 同时更新主答案的综合评分
                    if parent_answer_id:
                        update_main_comprehensive_sql = """
                        UPDATE candidate_answers
                        SET comprehensive_score = %s
                        WHERE id = %s
                        """
                        if self.db_manager.db_type != 'postgresql':
                            update_main_comprehensive_sql = update_main_comprehensive_sql.replace('%s', '?')
                        self.db_manager.execute(update_main_comprehensive_sql, (comprehensive_score, parent_answer_id))
                        logger.info(f"已更新主答案综合评分: parent_answer_id={parent_answer_id}, comprehensive_score={comprehensive_score}")
                    
                    logger.info(f"已更新追问答案评分: follow_up_question_id={follow_up_question_id}, follow_up_score={final_score}, comprehensive_score={comprehensive_score}")
                    
                    # 重置追问状态
                    client_info['is_follow_up_question'] = False
                    client_info['follow_up_question_id'] = None
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
                if evaluation_result.get('need_follow_up') and not is_basic_question:
                    # 检查追问次数限制（专业题最多追问1次）
                    check_sql = "SELECT follow_up_used, follow_up_limit FROM interview_session WHERE session_id = %s"
                    if self.db_manager.db_type != 'postgresql':
                        check_sql = check_sql.replace('%s', '?')

                    session_data = self.db_manager.fetch_one(check_sql, (session_id,))
                    follow_up_limit = session_data.get('follow_up_limit', 1) if session_data else 1  # 默认最多追问1次
                    follow_up_used = session_data.get('follow_up_used', 0) if session_data else 0
                    
                    if follow_up_used < follow_up_limit:
                        follow_up_question = evaluation_result.get('follow_up_question')
                        
                        # 保存追问问题到candidate_answers表（is_follow_up=true, answer_text=追问问题文本）
                        try:
                            # 获取主答案ID
                            main_answer_sql = """
                            SELECT id FROM candidate_answers 
                            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
                            LIMIT 1
                            """
                            if self.db_manager.db_type != 'postgresql':
                                main_answer_sql = main_answer_sql.replace('%s', '?')
                            
                            main_answer = self.db_manager.fetch_one(main_answer_sql, (session_id, question_id))
                            parent_answer_id = main_answer['id'] if main_answer else None
                            
                            if parent_answer_id and follow_up_question:
                                # 创建追问问题记录（answer_text存储追问问题文本）
                                follow_up_question_id = f"FUQ_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
                                # 获取题目的评估要点（用于存储到follow_up_evaluation_points）
                                question_info_sql = """
                                SELECT iqs.evaluation_points
                                FROM interview_question iq
                                JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
                                WHERE iq.question_id = %s
                                LIMIT 1
                                """
                                if self.db_manager.db_type != 'postgresql':
                                    question_info_sql = question_info_sql.replace('%s', '?')
                                
                                question_info_data = self.db_manager.fetch_one(question_info_sql, (question_id,))
                                follow_up_eval_points = None
                                if question_info_data:
                                    follow_up_eval_points = question_info_data.get('evaluation_points')
                                    if follow_up_eval_points:
                                        try:
                                            if isinstance(follow_up_eval_points, str):
                                                follow_up_eval_points = json.loads(follow_up_eval_points)
                                        except Exception as e:
                                            logger.warning(f"解析追问评估要点失败: {str(e)}")
                                            follow_up_eval_points = None
                                
                                insert_follow_up_sql = """
                                INSERT INTO candidate_answers (
                                    id, session_id, question_id, answer_text,
                                    is_follow_up, parent_answer_id, status,
                                    follow_up_question, follow_up_evaluation_points
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """
                                if self.db_manager.db_type != 'postgresql':
                                    insert_follow_up_sql = insert_follow_up_sql.replace('%s', '?')
                                
                                self.db_manager.execute(insert_follow_up_sql, (
                                    follow_up_question_id,
                                    session_id,
                                    question_id,
                                    follow_up_question,  # answer_text存储追问问题文本
                                    True,  # is_follow_up = True
                                    parent_answer_id,  # parent_answer_id指向主答案
                                    'pending',  # status = 'pending'（等待回答）
                                    follow_up_question,  # follow_up_question字段也存储（冗余，便于查询）
                                    json.dumps(follow_up_eval_points) if follow_up_eval_points else None  # follow_up_evaluation_points: 追问的评估要点
                                ))
                                
                                # 更新客户端信息，标记当前为追问问题
                                client_info['is_follow_up_question'] = True
                                client_info['follow_up_question_id'] = follow_up_question_id
                                
                                logger.info(f"已创建追问问题记录: follow_up_question_id={follow_up_question_id}, parent_answer_id={parent_answer_id}")
                        except Exception as e:
                            logger.error(f"保存追问问题失败: {str(e)}", exc_info=True)
                        
                        # 发送追问问题到前端（特别提示这是追问问题）
                        await client_info['websocket'].send(json.dumps({
                            'type': 'follow_up_trigger',
                            'question': follow_up_question,
                            'is_follow_up': True,  # 标识这是追问问题
                            'reason': '评分低于阈值，建议追问',
                            'follow_up_question_id': follow_up_question_id if 'follow_up_question_id' in locals() else None,
                            'timestamp': datetime.now().isoformat()
                        }))
                    else:
                        logger.info(f"已达到追问次数限制: used={follow_up_used}, limit={follow_up_limit}")
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
            # 关闭ASR会话（添加超时保护）
            if client_info.get('asr_session'):
                try:
                    await asyncio.wait_for(
                        self.asr_client.close_session(client_info['asr_session']),
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