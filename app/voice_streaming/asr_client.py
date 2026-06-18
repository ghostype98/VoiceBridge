# -*- coding: utf-8 -*-
"""
阿里云ASR客户端
实现WebSocket连接到阿里云实时语音识别服务
"""

import asyncio
import json
import uuid
import websockets
import ssl
from typing import Dict, Any, Optional, Callable
from .token_manager import ASRTokenManager
from datetime import datetime
import threading
import time

from loguru import logger
# 使用loguru logger

# 导入TokenManager（延迟导入避免循环依赖）
try:
    from .token_manager import ASRTokenManager
except ImportError:
    # 如果导入失败，使用类型提示的字符串形式
    ASRTokenManager = None
    logger.warning("TokenManager未找到，将使用静态Token")


class AliyunASRClient:
    """阿里云ASR客户端"""

    def __init__(self, appkey: str, token: Optional[str] = None, config: Optional[Dict[str, Any]] = None, 
                 token_manager: Optional[Any] = None):
        """
        初始化ASR客户端
        
        参数:
            appkey: 阿里云ASR AppKey
            token: 静态Token（可选，如果提供token_manager则优先使用token_manager）
            config: 配置字典
            token_manager: Token管理器（可选，如果提供则使用动态Token）
        """
        self.appkey = appkey
        self.config = config or {}
        
        # Token管理：优先使用TokenManager，否则使用静态Token
        if token_manager:
            self.token_manager = token_manager
            self.use_dynamic_token = True
            logger.info("使用动态Token管理器，Token将自动刷新")
        elif token:
            self.token = token
            self.token_manager = None
            self.use_dynamic_token = False
            logger.info("使用静态Token，请定期手动更新")
        else:
            raise ValueError("必须提供token或token_manager参数")

        # ASR会话管理
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.session_counter = 0

        # WebSocket连接池
        self.connection_pool = {}
        self.max_connections = 10

        logger.info("阿里云ASR客户端初始化完成")

    def create_session(self, invitation_id: str = None) -> str:
        """创建ASR会话
        
        参数:
            invitation_id: 邀请ID（可选），用于限制同一用户的并发会话数
        
        返回:
            session_id: 会话ID
        """
        # 如果提供了invitation_id，检查是否已有活跃会话
        if invitation_id:
            # 查找该用户的活跃会话
            for sid, info in list(self.active_sessions.items()):
                if info.get('invitation_id') == invitation_id and info.get('is_connected'):
                    logger.warning(f"用户 {invitation_id} 已有活跃会话 {sid}，先关闭旧会话")
                    # 异步关闭旧会话（不等待完成）
                    asyncio.create_task(self.close_session(sid))
        
        session_id = f"asr_session_{self.session_counter}_{uuid.uuid4().hex[:8]}"
        self.session_counter += 1

        self.active_sessions[session_id] = {
            'session_id': session_id,
            'websocket': None,
            'is_connected': False,
            'last_activity': time.time(),
            'accumulated_text': '',
            'task_id': None,
            'status': 'created',
            'transcription_started': False,  # 标记是否已收到StartTranscription的确认
            'invitation_id': invitation_id  # 保存invitation_id用于清理
        }

        logger.info(f"创建ASR会话: {session_id}" + (f" (用户: {invitation_id})" if invitation_id else ""))
        return session_id

    async def connect_session(self, session_id: str, timeout: float = 10.0) -> bool:
        """连接ASR会话
        
        参数:
            session_id: 会话ID
            timeout: 连接超时时间（秒），默认10秒
        """
        session_info = self.active_sessions.get(session_id)
        if not session_info:
            logger.error(f"会话不存在: {session_id}")
            return False

        try:
            # 构建WebSocket URL（添加超时保护，网络较慢时给足时间避免录音无法启动）
            try:
                ws_url = await asyncio.wait_for(
                    asyncio.to_thread(self._build_websocket_url),
                    timeout=15.0  # Token 获取最多 15 秒
                )
            except asyncio.TimeoutError:
                logger.error(f"获取Token超时: {session_id}")
                return False
            except Exception as e:
                logger.error(f"构建WebSocket URL失败: {str(e)}")
                return False

            # 创建SSL上下文
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # 建立WebSocket连接（添加超时保护）
            # websockets 15.0+ 使用 additional_headers 参数
            headers = self._get_headers()
            
            logger.debug(f"正在连接ASR服务: {ws_url.split('token=')[0]}token=***")
            try:
                websocket = await asyncio.wait_for(
                    websockets.connect(
                        ws_url,
                        ssl=ssl_context,
                        additional_headers=headers,  # websockets 15.0+ 使用此参数名
                        ping_interval=30,
                        ping_timeout=10,
                        close_timeout=5
                    ),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.error(f"连接ASR服务超时（{timeout}秒）: {session_id}")
                session_info['status'] = 'timeout'
                return False

            session_info.update({
                'websocket': websocket,
                'is_connected': True,
                'status': 'connected',
                'last_activity': time.time()
            })

            # 发送开始识别消息
            await self._send_start_message(session_id)
            
            # 启动消息监听任务（持续接收ASR返回的消息），并保存任务引用以便后续清理
            listen_task = asyncio.create_task(self._listen_asr_messages(session_id))
            session_info['listen_task'] = listen_task
            
            # 等待StartTranscription的响应（最多等待5秒）
            # 根据阿里云ASR文档，发送StartTranscription后需要等待服务端确认
            await asyncio.sleep(0.5)  # 给服务端一点时间处理
            
            # 检查是否收到错误响应
            max_wait_time = 5.0
            wait_interval = 0.1
            waited_time = 0.0
            
            # 必须等待收到TranscriptionStarted响应后才能发送音频数据
            # 这是阿里云ASR的强制要求，违反会导致40000002错误
            transcription_confirmed = False
            
            while waited_time < max_wait_time:
                if 'last_result' in session_info:
                    result = session_info.get('last_result')
                    if result:
                        result_type = result.get('type')
                        
                        if result_type == 'error':
                            error_code = result.get('error_code', 'UNKNOWN')
                            error_msg = result.get('message', '未知错误')
                            logger.error(f"StartTranscription失败: {error_code} - {error_msg}")
                            session_info['is_connected'] = False
                            session_info['status'] = 'error'
                            return False
                        
                        elif result_type == 'transcription_started':
                            # 收到TranscriptionStarted确认，可以开始发送音频
                            transcription_confirmed = True
                            session_info['transcription_started'] = True
                            logger.info(f"收到TranscriptionStarted确认，会话已准备好接收音频: {session_id}")
                            break
                        
                        elif result_type in ['intermediate_result', 'final_result']:
                            # 如果直接收到识别结果，也认为已准备好（某些情况下可能不发送TranscriptionStarted）
                            transcription_confirmed = True
                            session_info['transcription_started'] = True
                            logger.info(f"收到识别结果，会话已准备好: {session_id}")
                            break
                
                await asyncio.sleep(wait_interval)
                waited_time += wait_interval
            
            if not transcription_confirmed:
                logger.error(f"等待TranscriptionStarted响应超时（{max_wait_time}秒），无法发送音频数据: {session_id}")
                logger.error("这会导致40000002错误。请检查：")
                logger.error("1. message_id格式是否正确（32位十六进制，无中划线）")
                logger.error("2. AppKey是否正确")
                logger.error("3. Token是否有效")
                logger.error("4. format和sample_rate参数是否正确")
                session_info['is_connected'] = False
                session_info['status'] = 'error'
                return False

            logger.info(f"ASR会话连接成功: {session_id}")
            return True

        except Exception as e:
            error_msg = str(e)
            logger.error(f"连接ASR会话失败: {error_msg}")
            
            # 如果是403错误，提供更详细的错误信息
            if '403' in error_msg or 'Forbidden' in error_msg:
                logger.error("=" * 60)
                logger.error("ASR连接403错误，可能的原因：")
                logger.error("1. Token已过期或无效（Token通常1小时有效）")
                logger.error("2. AppKey配置错误或权限不足")
                logger.error("3. 阿里云ASR服务未开通或未授权")
                logger.error("4. Token生成方式不正确")
                logger.error("=" * 60)
                logger.error("解决方案：")
                logger.error("1. 登录阿里云控制台：https://nls.console.alibabacloud.com")
                logger.error("2. 检查智能语音交互服务是否已开通")
                logger.error("3. 重新生成Token（使用AccessKey ID和Secret）")
                logger.error("4. 验证AppKey是否正确")
                logger.error("=" * 60)
            
            session_info['status'] = 'error'
            return False
    
    async def _listen_asr_messages(self, session_id: str):
        """持续监听ASR返回的消息"""
        session_info = self.active_sessions.get(session_id)
        if not session_info or not session_info.get('websocket'):
            return
        
        websocket = session_info['websocket']
        
        try:
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        # 解析JSON字符串
                        result = json.loads(message)
                        
                        # 确保result是字典类型
                        if not isinstance(result, dict):
                            logger.error(f"解析后的消息不是字典类型: {type(result)}, 值: {str(result)[:200]}")
                            continue
                        
                        # 记录原始消息（用于调试）
                        logger.debug(f"收到ASR消息: {message[:200]}")
                        
                        # 传递session_id给_parse_result
                        # 注意：这里传递的result应该是字典类型
                        parsed_result = self._parse_result(result, session_id)
                        
                        # 将结果存储到会话中，供send_audio_data使用
                        if parsed_result:
                            session_info['last_result'] = parsed_result
                            logger.debug(f"收到ASR结果: {parsed_result.get('type', 'unknown')}")
                        else:
                            logger.debug(f"ASR消息解析后返回None，可能是未知消息类型")
                            
                    except json.JSONDecodeError as e:
                        logger.error(f"无法解析ASR消息JSON: {str(e)}, 消息: {message[:200]}")
                    except Exception as e:
                        logger.error(f"处理ASR消息失败: {str(e)}, 消息类型: {type(message)}, 消息: {str(message)[:200]}", exc_info=True)
                else:
                    logger.debug(f"收到ASR二进制消息: {len(message)}字节")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"ASR连接已关闭: {session_id}")
            session_info['is_connected'] = False
            session_info['status'] = 'closed'
        except Exception as e:
            logger.error(f"监听ASR消息失败: {str(e)}", exc_info=True)
            session_info['is_connected'] = False
            session_info['status'] = 'error'

    async def close_session(self, session_id: str):
        """关闭ASR会话
        
        注意：改为async方法以便正确清理异步任务
        """
        session_info = self.active_sessions.get(session_id)
        if not session_info:
            return

        try:
            # 取消监听任务（如果存在）
            listen_task = session_info.get('listen_task')
            if listen_task and not listen_task.done():
                listen_task.cancel()
                try:
                    await listen_task
                except asyncio.CancelledError:
                    logger.debug(f"监听任务已取消: {session_id}")
                except Exception as e:
                    logger.warning(f"取消监听任务时出现异常: {str(e)}")
            
            websocket = session_info.get('websocket')
            if websocket and session_info.get('is_connected'):
                # 发送停止识别消息
                try:
                    await self._send_stop_message(session_id)
                except Exception as e:
                    logger.warning(f"发送停止消息失败: {str(e)}")

                # 关闭连接
                try:
                    await websocket.close()
                except Exception as e:
                    logger.warning(f"关闭WebSocket连接失败: {str(e)}")

            # 从活跃会话中移除
            del self.active_sessions[session_id]

            logger.info(f"ASR会话已关闭: {session_id}")

        except Exception as e:
            logger.error(f"关闭ASR会话失败: {str(e)}", exc_info=True)

    async def send_audio_data(self, session_id: str, audio_data: bytes) -> Optional[Dict[str, Any]]:
        """发送音频数据
        
        注意：阿里云ASR需要PCM格式的音频数据
        如果前端发送的是webm格式，需要先转换
        """
        session_info = self.active_sessions.get(session_id)
        if not session_info or not session_info.get('is_connected'):
            logger.warning(f"ASR会话未连接: {session_id}")
            return None

        try:
            websocket = session_info['websocket']
            if not websocket:
                logger.warning(f"WebSocket连接不存在: {session_id}")
                return None

            # 必须检查是否已收到StartTranscription的确认
            # 这是阿里云ASR的强制要求，违反会导致40000002错误
            if not session_info.get('transcription_started', False):
                logger.error(f"未收到TranscriptionStarted确认，无法发送音频数据: {session_id}")
                logger.error("这会导致40000002错误。请确保在connect_session中已等待确认响应")
                return None

            # 记录音频数据信息
            audio_size = len(audio_data)
            
            # 控制音频发送速度
            # PCM 16K, 16bit, 单声道: 8192字节 ≈ 256ms音频
            # 如果发送频率过高，可能导致服务端缓冲区溢出
            # 这里不添加sleep，因为前端已经控制了发送频率（每4096样本≈256ms）
            # 但如果后端接收速度过快，可以在这里添加控制
            
            logger.debug(f"发送音频数据到ASR: 会话={session_id}, 大小={audio_size}字节")
            
            # 发送音频数据（二进制）
            # 注意：websockets 15.0+ 不再使用 .open 属性，直接发送并捕获异常
            # 如果连接已关闭，会抛出 ConnectionClosed 异常
            try:
                await websocket.send(audio_data)
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"WebSocket连接已关闭，无法发送音频数据: {session_id}")
                session_info['is_connected'] = False
                return None
            except Exception as e:
                logger.error(f"发送音频数据失败: {str(e)}")
                session_info['is_connected'] = False
                return None

            # 接收识别结果（异步，不阻塞）
            # 注意：ASR可能返回多个消息，需要持续监听
            result = await self._receive_result(session_id)
            session_info['last_activity'] = time.time()

            return result

        except Exception as e:
            logger.error(f"发送音频数据失败: {str(e)}", exc_info=True)
            session_info['status'] = 'error'
            return None

    def _build_websocket_url(self) -> str:
        """构建WebSocket URL
        
        注意：根据阿里云ASR文档，URL中只需要token参数进行鉴权
        appkey应该在WebSocket消息的Header中，而不是URL参数中
        
        如果使用动态Token，每次连接时都会获取最新的Token
        
        注意：此方法是同步的，但Token获取可能涉及网络请求
        在异步环境中调用时，应使用 asyncio.to_thread() 包装
        """
        base_url = self.config.get('endpoint', 'wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1')

        # 获取Token：如果使用动态Token管理器，则动态获取
        if self.use_dynamic_token and self.token_manager:
            logger.debug("使用动态Token管理器获取Token...")
            token = self.token_manager.get_token()
            if not token:
                raise Exception("无法获取ASR Token，请检查AccessKey配置")
            logger.debug(f"成功获取动态Token: {token[:20]}...{token[-10:]}")
        else:
            token = self.token
            if not token:
                raise Exception("Token未设置，请检查配置")
            logger.debug("使用静态Token")

        # URL中只需要token参数（用于鉴权）
        # 其他参数（如format、sample_rate等）应该在WebSocket消息中指定
        url = f"{base_url}?token={token}"
        
        logger.debug(f"构建WebSocket URL: {url.split('token=')[0]}token=***")
        return url

    def _get_headers(self) -> Dict[str, str]:
        """获取WebSocket连接请求头
        
        注意：根据阿里云ASR文档，请求头中不需要额外设置token
        token已经在URL参数中，appkey在WebSocket消息的Header中
        """
        # WebSocket连接请求头，不需要额外设置token（已在URL中）
        return {}

    async def _send_start_message(self, session_id: str):
        """发送开始识别消息"""
        session_info = self.active_sessions.get(session_id)
        if not session_info or not session_info.get('websocket'):
            return

        websocket = session_info['websocket']

        # 构建开始消息
        # 注意：message_id和task_id必须是32位十六进制字符串，不能包含中划线
        # 使用uuid.uuid4().hex会生成32位十六进制字符串（无中划线）
        start_message = {
            "header": {
                "message_id": uuid.uuid4().hex,  # 32位十六进制字符串，无中划线
                "task_id": uuid.uuid4().hex,      # 32位十六进制字符串，无中划线
                "namespace": "SpeechTranscriber",
                "name": "StartTranscription",
                "appkey": self.appkey
            },
            "payload": {
                "format": self.config.get('format', 'pcm'),  # 必须是pcm/wav/mp3等
                "sample_rate": self.config.get('sample_rate', 16000),  # 必须是16000或8000
                "enable_intermediate_result": self.config.get('enable_intermediate_result', True),
                "enable_punctuation_prediction": self.config.get('enable_punctuation_prediction', True),
                "enable_inverse_text_normalization": self.config.get('enable_inverse_text_normalization', True),
                "max_start_silence": self.config.get('max_start_silence', 5000),
                "max_end_silence": self.config.get('max_end_silence', 2000)
            }
        }
        
        # 验证关键参数
        format_value = start_message['payload']['format']
        sample_rate_value = start_message['payload']['sample_rate']
        if format_value not in ['pcm', 'wav', 'mp3', 'amr', 'aac', 'opus', 'speex']:
            logger.warning(f"音频格式可能不支持: {format_value}，建议使用pcm")
        if sample_rate_value not in [8000, 16000]:
            logger.error(f"采样率不支持: {sample_rate_value}，必须是8000或16000")
        
        logger.debug(f"StartTranscription消息 - format: {format_value}, sample_rate: {sample_rate_value}, message_id: {start_message['header']['message_id'][:8]}...")

        # 发送开始消息
        await websocket.send(json.dumps(start_message))

        # 保存task_id
        session_info['task_id'] = start_message['header']['task_id']

        logger.debug(f"发送开始识别消息: {session_id}")

    async def _send_stop_message(self, session_id: str):
        """发送停止识别消息"""
        session_info = self.active_sessions.get(session_id)
        if not session_info or not session_info.get('websocket'):
            return

        websocket = session_info['websocket']
        task_id = session_info.get('task_id')

        if not task_id:
            return

        # 构建停止消息
        # 注意：message_id必须是32位十六进制字符串，不能包含中划线
        stop_message = {
            "header": {
                "message_id": uuid.uuid4().hex,  # 32位十六进制字符串，无中划线
                "task_id": task_id,  # task_id已经在StartTranscription时设置，保持原值
                "namespace": "SpeechTranscriber",
                "name": "StopTranscription",
                "appkey": self.appkey
            },
            "payload": {}
        }

        try:
            await websocket.send(json.dumps(stop_message))
            logger.debug(f"发送停止识别消息: {session_id}")
        except Exception as e:
            logger.error(f"发送停止识别消息失败: {str(e)}")

    async def _receive_result(self, session_id: str) -> Optional[Dict[str, Any]]:
        """接收识别结果
        
        注意：由于已经启动了_listen_asr_messages持续监听，
        这里可以直接从session_info中获取最新的结果
        """
        session_info = self.active_sessions.get(session_id)
        if not session_info:
            return None

        # 等待一小段时间，让_listen_asr_messages处理消息
        await asyncio.sleep(0.1)
        
        # 从会话中获取最新的结果
        if 'last_result' in session_info:
            result = session_info['last_result']
            # 清除已处理的结果
            session_info.pop('last_result', None)
            return result
        
        return None

    def _parse_result(self, result: Dict[str, Any], session_id: str = None) -> Optional[Dict[str, Any]]:
        """解析识别结果"""
        try:
            # 如果result是字符串，先解析为字典
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                    logger.debug(f"解析JSON字符串成功，类型: {type(result)}")
                except json.JSONDecodeError as e:
                    logger.error(f"无法解析JSON字符串: {str(e)}, 内容: {result[:200]}")
                    return None
                except Exception as e:
                    logger.error(f"解析JSON字符串时发生异常: {str(e)}, 内容: {result[:200]}")
                    return None
            
            # 确保result是字典类型
            if not isinstance(result, dict):
                logger.error(f"result类型错误: {type(result)}, 期望dict, 值: {str(result)[:200]}")
                return None
            
            # 安全地获取header和payload
            header = result.get('header') if isinstance(result, dict) else {}
            payload = result.get('payload') if isinstance(result, dict) else None  # payload可能为None（如TranscriptionStarted）
            
            # 确保header是字典类型
            if not isinstance(header, dict):
                if isinstance(header, str):
                    try:
                        header = json.loads(header)
                        logger.debug(f"header是字符串，已解析为字典")
                    except:
                        logger.error(f"header类型错误: {type(header)}, 期望dict, 值: {str(header)[:200]}")
                        header = {}
                else:
                    logger.error(f"header类型错误: {type(header)}, 期望dict, 值: {str(header)[:200]}")
                    header = {}
            
            # payload可能为None（如TranscriptionStarted消息），或者为字典
            # 如果payload是None，设置为空字典以便后续处理
            if payload is None:
                payload = {}
            elif not isinstance(payload, dict):
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                        logger.debug(f"payload是字符串，已解析为字典")
                    except:
                        logger.error(f"payload类型错误: {type(payload)}, 期望dict, 值: {str(payload)[:200]}")
                        payload = {}
                else:
                    logger.error(f"payload类型错误: {type(payload)}, 期望dict, 值: {str(payload)[:200]}")
                    payload = {}
            
            # 如果没有传入session_id，尝试从active_sessions中查找
            if session_id is None:
                task_id = header.get('task_id')
                if task_id:
                    for sid, info in self.active_sessions.items():
                        if info.get('task_id') == task_id:
                            session_id = sid
                            break

            name = header.get('name', '')

            if name == 'TranscriptionStarted':
                # 识别已开始（StartTranscription的确认响应）
                # 注意：TranscriptionStarted消息的payload通常是空的，不需要解析
                logger.info(f"收到TranscriptionStarted确认 - 会话: {session_id if session_id else 'unknown'}")
                # 标记会话已准备好接收音频数据
                if session_id:
                    session_info = self.active_sessions.get(session_id)
                    if session_info:
                        session_info['transcription_started'] = True
                        logger.info(f"会话已标记为准备好接收音频数据: {session_id}")
                return {
                    'type': 'transcription_started',
                    'task_id': header.get('task_id'),
                    'timestamp': datetime.now().isoformat()
                }
            
            # 检查header中的status，如果status是20000000（成功），也认为StartTranscription成功
            # 某些情况下，服务端可能不发送TranscriptionStarted，而是直接发送成功状态
            status = header.get('status', 0)
            if status == 20000000 and name not in ['TranscriptionResultChanged', 'SentenceEnd', 'TaskFailed', 'TranscriptionStarted']:
                # 这是一个成功的响应，可能是StartTranscription的确认
                if session_id:
                    session_info = self.active_sessions.get(session_id)
                    if session_info and not session_info.get('transcription_started', False):
                        logger.info(f"收到成功响应（status=20000000），标记为transcription_started: {name} - 会话: {session_id}")
                        session_info['transcription_started'] = True
                        # 返回一个transcription_started类型的结果，以便等待逻辑能够识别
                        return {
                            'type': 'transcription_started',
                            'task_id': header.get('task_id'),
                            'timestamp': datetime.now().isoformat()
                        }

            elif name == 'TranscriptionResultChanged':
                # 识别结果变更
                # 注意：阿里云ASR返回的 payload.result 直接是字符串，不是字典
                # 根据阿里云文档，识别文本字段名是 'result'，不是 'text'
                result_text = payload.get('result', '')
                
                # 如果result是字符串，直接使用
                if isinstance(result_text, str):
                    text = result_text
                elif isinstance(result_text, dict):
                    # 某些情况下可能是字典，尝试获取text字段
                    text = result_text.get('text', '')
                else:
                    text = str(result_text) if result_text else ''
                
                # 获取其他字段（confidence, time, index等）
                confidence = payload.get('confidence', 0.0)
                time_ms = payload.get('time', 0)
                index = payload.get('index', 0)

                if text:
                    return {
                        'type': 'intermediate_result',
                        'text': text,
                        'confidence': confidence,
                        'time': time_ms,
                        'index': index,
                        'timestamp': datetime.now().isoformat()
                    }

            elif name == 'SentenceEnd':
                # 句子结束
                # 注意：阿里云ASR返回的 payload.result 直接是字符串，不是字典
                # 根据阿里云文档，识别文本字段名是 'result'，不是 'text'
                result_text = payload.get('result', '')
                
                # 如果result是字符串，直接使用
                if isinstance(result_text, str):
                    text = result_text
                elif isinstance(result_text, dict):
                    # 某些情况下可能是字典，尝试获取text字段
                    text = result_text.get('text', '')
                else:
                    text = str(result_text) if result_text else ''
                
                # 获取其他字段（confidence, time, begin_time, index等）
                confidence = payload.get('confidence', 0.0)
                time_ms = payload.get('time', 0)
                begin_time = payload.get('begin_time', 0)
                index = payload.get('index', 0)

                if text:
                    return {
                        'type': 'final_result',
                        'text': text,
                        'confidence': confidence,
                        'time': time_ms,
                        'begin_time': begin_time,
                        'index': index,
                        'timestamp': datetime.now().isoformat()
                    }

            elif name == 'TaskFailed':
                # 任务失败
                # 从header中获取状态码（阿里云ASR的错误码在header.status中）
                status_code = header.get('status', 0)
                status_text = header.get('status_text', '')
                
                # 从payload中获取错误信息
                error_code = payload.get('error_code', str(status_code))
                error_message = payload.get('error_message', payload.get('message', status_text))
                
                # 如果error_code是数字字符串，转换为字符串格式（如40000002）
                if isinstance(error_code, int):
                    error_code = str(error_code)
                elif isinstance(error_code, str) and error_code.isdigit():
                    pass  # 已经是数字字符串
                elif status_code != 0:
                    error_code = str(status_code)
                
                # 记录完整的错误信息
                logger.error("=" * 60)
                logger.error(f"ASR任务失败 - 会话: {session_id if session_id else 'unknown'}")
                logger.error(f"Header状态码: {status_code}")
                logger.error(f"Header状态文本: {status_text}")
                logger.error(f"错误码: {error_code}")
                logger.error(f"错误消息: {error_message}")
                logger.error(f"完整header: {json.dumps(header, ensure_ascii=False, indent=2)}")
                logger.error(f"完整payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
                logger.error("=" * 60)
                
                # 常见错误码说明（根据阿里云文档）
                error_codes = {
                    '40000000': '默认的客户端错误码，用户使用了不合理的参数或者调用逻辑',
                    '40000001': 'Token过期或无效',
                    '40000002': '无效或者错误的报文消息（可能是音频格式不匹配）',
                    '40000003': '用户传递的参数有误',
                    '40000004': 'WebSocket会话空闲超时（超过10秒未发送数据）',
                    '40000005': '并发请求过多',
                    '40000009': 'WAV头错误或过大',
                    '40000010': '试用期已结束或账号欠费',
                    '41010101': '不支持的采样率格式（只支持8000Hz和16000Hz）',
                    '41040201': '客户端数据发送超时',
                    '40270002': '无效的音频（从音频中没有识别出有效文本）',
                    '40270003': '音频解码失败（请根据实际音频格式设置对应的format参数）',
                }
                
                if error_code in error_codes:
                    logger.error(f"错误说明: {error_codes[error_code]}")
                elif str(status_code) in error_codes:
                    logger.error(f"错误说明: {error_codes[str(status_code)]}")

                return {
                    'type': 'error',
                    'error_code': error_code,
                    'status_code': status_code,
                    'status_text': status_text,
                    'message': error_message,
                    'full_payload': payload,
                    'full_header': header,
                    'timestamp': datetime.now().isoformat()
                }

            return None

        except Exception as e:
            logger.error(f"解析识别结果失败: {str(e)}")
            logger.error(f"result类型: {type(result)}, 值: {str(result)[:500] if result else 'None'}")
            logger.error(f"header类型: {type(header) if 'header' in locals() else 'N/A'}, 值: {str(header)[:200] if 'header' in locals() and header else 'N/A'}")
            logger.error(f"payload类型: {type(payload) if 'payload' in locals() else 'N/A'}, 值: {str(payload)[:200] if 'payload' in locals() and payload else 'N/A'}")
            logger.error(f"错误堆栈:", exc_info=True)
            return None

    def get_session_stats(self) -> Dict[str, Any]:
        """获取会话统计信息"""
        return {
            'total_sessions': len(self.active_sessions),
            'active_sessions': sum(1 for s in self.active_sessions.values() if s.get('is_connected')),
            'timestamp': datetime.now().isoformat()
        }

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            # 创建测试会话
            test_session = self.create_session()
            success = await self.connect_session(test_session)

            if success:
                await self.close_session(test_session)
                return True
            else:
                return False

        except Exception as e:
            logger.error(f"ASR健康检查失败: {str(e)}")
            return False