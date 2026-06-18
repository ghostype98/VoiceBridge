"""
配置管理模块
支持 YAML 格式配置文件和自动配置获取
参考 TokenManager 的自动获取机制实现配置的动态管理
"""
from typing import Optional, Dict, Any, List
import os
import yaml
import time
import threading
from pathlib import Path
from datetime import datetime
from loguru import logger


class ConfigManager:
    """自动配置管理器（参考TokenManager的自动获取机制）

    自动从多个配置源获取和刷新配置，确保配置始终有效
    支持环境变量、阿里云配置服务、本地文件等多种配置源
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置管理器

        参数:
            config_path: 配置文件路径
        """
        if config_path is None:
            config_path = os.getenv("CONFIG_FILE", "./config/config.yaml")

        self.config_path = config_path
        self._config_cache: Optional[Dict[str, Any]] = None
        self._cache_timestamp: float = 0
        self._lock = threading.Lock()

        # 从配置文件加载自动配置设置
        self._load_auto_config_settings()

        logger.info("配置管理器初始化完成")

    def _load_auto_config_settings(self):
        """从配置文件加载自动配置设置"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                # 使用默认设置
                self.auto_config_enabled = True
                self.refresh_interval = 3600
                self.cache_ttl = 1800
                self.config_sources = []
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            auto_config = config.get("auto_config", {})
            self.auto_config_enabled = auto_config.get("enabled", True)
            self.refresh_interval = auto_config.get("refresh_interval", 3600)
            self.cache_ttl = auto_config.get("cache_ttl", 1800)
            self.config_sources = auto_config.get("sources", [])

        except Exception as e:
            logger.warning(f"加载自动配置设置失败，使用默认设置: {e}")
            self.auto_config_enabled = True
            self.refresh_interval = 3600
            self.cache_ttl = 1800
            self.config_sources = []

    def get_config_value(self, key_path: str, default_value: Any = None) -> Any:
        """
        获取配置值，支持自动刷新

        参数:
            key_path: 配置路径，如 "voice_streaming.asr.access_key_id"
            default_value: 默认值

        返回:
            配置值
        """
        if not self.auto_config_enabled:
            return default_value

        current_time = time.time()

        # 检查缓存是否有效
        if self._config_cache and (current_time - self._cache_timestamp) < self.cache_ttl:
            return self._get_value_from_cache(key_path, default_value)

        # 需要刷新配置
        logger.debug("配置缓存过期，开始刷新配置...")
        with self._lock:
            # 双重检查
            if self._config_cache and (current_time - self._cache_timestamp) < self.cache_ttl:
                return self._get_value_from_cache(key_path, default_value)

            return self._refresh_and_get_config(key_path, default_value)

    def _refresh_and_get_config(self, key_path: str, default_value: Any = None) -> Any:
        """刷新配置并获取值"""
        try:
            self._config_cache = self._fetch_config_from_sources()
            self._cache_timestamp = time.time()

            logger.debug("配置刷新完成")
            return self._get_value_from_cache(key_path, default_value)

        except Exception as e:
            logger.error(f"刷新配置失败: {e}")
            # 如果刷新失败但有缓存，使用缓存
            if self._config_cache:
                logger.warning("使用过期的配置缓存")
                return self._get_value_from_cache(key_path, default_value)
            return default_value

    def _fetch_config_from_sources(self) -> Dict[str, Any]:
        """从配置源获取配置（按优先级从高到低处理）"""
        config_data = {}

        # 按照优先级排序配置源：环境变量 > 文件 > 云配置
        priority_order = {
            "environment": 1,      # 最高优先级
            "file": 2,            # 中等优先级
            "alibaba_cloud_config": 3  # 最低优先级
        }

        sorted_sources = sorted(
            [s for s in self.config_sources if s.get("enabled", False)],
            key=lambda x: priority_order.get(x.get("type"), 999)
        )

        for source in sorted_sources:
            source_type = source.get("type")
            try:
                if source_type == "environment":
                    env_config = self._fetch_from_environment(source)
                    # 环境变量直接覆盖，不合并嵌套结构
                    for key_path, value in env_config.items():
                        self._set_nested_value(config_data, key_path, value)
                elif source_type == "file":
                    file_config = self._fetch_from_file(source)
                    # 文件配置进行深度合并，但不覆盖环境变量已设置的值
                    self._deep_merge_config(config_data, file_config)
                elif source_type == "alibaba_cloud_config":
                    # 未来扩展：从阿里云配置服务获取
                    cloud_config = self._fetch_from_alibaba_cloud(source)
                    self._deep_merge_config(config_data, cloud_config)
                else:
                    logger.warning(f"不支持的配置源类型: {source_type}")

            except Exception as e:
                logger.error(f"从配置源 {source_type} 获取配置失败: {e}")
                continue

        return config_data

    def _deep_merge_config(self, target: Dict[str, Any], source: Dict[str, Any]):
        """深度合并配置字典（不覆盖已存在的环境变量配置）"""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_merge_config(target[key], value)
            else:
                # 只有当目标中没有这个key时才设置（避免覆盖环境变量）
                if key not in target:
                    target[key] = value

    def _fetch_from_environment(self, source_config: Dict[str, Any]) -> Dict[str, Any]:
        """从环境变量获取配置"""
        config_data = {}
        prefix = source_config.get("prefix", "")
        mappings = source_config.get("mappings", {})

        for env_var, config_path in mappings.items():
            env_value = os.getenv(env_var)
            if env_value:
                # 直接使用配置路径作为key，不转换为嵌套结构
                config_data[config_path] = env_value
                logger.debug(f"从环境变量 {env_var} 获取配置: {config_path} = {env_value}")

        return config_data

    def _fetch_from_file(self, source_config: Dict[str, Any]) -> Dict[str, Any]:
        """从文件获取配置"""
        file_path = source_config.get("path")
        if not file_path:
            return {}

        try:
            config_file = Path(file_path)
            if not config_file.exists():
                logger.debug(f"配置文件不存在: {file_path}")
                return {}

            with open(config_file, 'r', encoding='utf-8') as f:
                if source_config.get("format", "yaml") == "yaml":
                    return yaml.safe_load(f) or {}
                else:
                    logger.warning(f"不支持的文件格式: {source_config.get('format')}")
                    return {}

        except Exception as e:
            logger.error(f"从文件 {file_path} 读取配置失败: {e}")
            return {}

    def _fetch_from_alibaba_cloud(self, source_config: Dict[str, Any]) -> Dict[str, Any]:
        """从阿里云配置服务获取配置（预留接口）"""
        # 未来实现阿里云配置服务的集成
        logger.info("阿里云配置服务集成暂未实现")
        return {}

    def _get_value_from_cache(self, key_path: str, default_value: Any = None) -> Any:
        """从缓存获取值"""
        if not self._config_cache:
            return default_value

        # 首先尝试直接从扁平结构获取（环境变量）
        if key_path in self._config_cache:
            return self._config_cache[key_path]

        # 如果找不到，尝试从嵌套结构获取（文件配置）
        keys = key_path.split('.')
        value = self._config_cache

        try:
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return default_value
            return value
        except (KeyError, TypeError):
            return default_value

    def _set_nested_value(self, config_dict: Dict[str, Any], key_path: str, value: Any):
        """设置嵌套字典的值"""
        keys = key_path.split('.')
        current = config_dict

        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]

        current[keys[-1]] = value

    def force_refresh(self) -> bool:
        """强制刷新配置"""
        logger.info("强制刷新配置...")
        with self._lock:
            try:
                self._config_cache = self._fetch_config_from_sources()
                self._cache_timestamp = time.time()
                logger.info("配置强制刷新完成")
                return True
            except Exception as e:
                logger.error(f"强制刷新配置失败: {e}")
                return False


class Settings:
    """应用配置（从 YAML 文件加载，支持自动配置获取）"""

    def __init__(self, config_path: Optional[str] = None):
        """初始化配置，从 YAML 文件加载"""
        if config_path is None:
            config_path = os.getenv("CONFIG_FILE", "./config/config.yaml")

        self.config_path = config_path
        # 项目根目录（config 所在目录的上一级）
        self.PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

        # 初始化配置管理器
        self.config_manager = ConfigManager(config_path)

        self._load_config()
        self._setup_paths()
    
    def _load_config(self):
        """从 YAML 文件加载配置"""
        config_file = Path(self.config_path)
        
        if not config_file.exists():
            # 如果配置文件不存在，使用默认配置
            self._use_defaults()
            return
        
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            raise ValueError(f"无法加载配置文件 {self.config_path}: {e}")
        
        # 语音流式服务配置
        voice_streaming = config.get("voice_streaming", {})
        websocket = voice_streaming.get("websocket", {})
        asr = voice_streaming.get("asr", {})
        audio = voice_streaming.get("audio", {})
        evaluation = voice_streaming.get("evaluation", {})
        streaming_interview = voice_streaming.get("streaming_interview", {})

        # WebSocket配置
        self.VOICE_STREAMING_WEBSOCKET_HOST = websocket.get("host", "0.0.0.0")
        self.VOICE_STREAMING_WEBSOCKET_PORT = websocket.get("port", 8003)
        self.VOICE_STREAMING_MAX_CONNECTIONS = websocket.get("max_connections", 20)
        self.VOICE_STREAMING_PING_INTERVAL = websocket.get("ping_interval", 30)
        self.VOICE_STREAMING_PING_TIMEOUT = websocket.get("ping_timeout", 10)

        # ASR配置（支持自动配置获取）
        self.ALIYUN_ASR_APPKEY = self._get_config_with_auto(
            "voice_streaming.asr.appkey",
            asr.get("appkey", "${ALIYUN_ASR_APPKEY}")
        )
        self.ALIYUN_ASR_ACCESS_KEY_ID = self._get_config_with_auto(
            "voice_streaming.asr.access_key_id",
            asr.get("access_key_id", "${ALIYUN_ACCESS_KEY_ID}")
        )
        self.ALIYUN_ASR_ACCESS_KEY_SECRET = self._get_config_with_auto(
            "voice_streaming.asr.access_key_secret",
            asr.get("access_key_secret", "${ALIYUN_ACCESS_KEY_SECRET}")
        )
        self.ALIYUN_ASR_TOKEN_REFRESH_ENABLED = asr.get("enable_token_refresh", True)
        self.ALIYUN_ASR_TOKEN_REFRESH_INTERVAL = asr.get("token_refresh_interval", 1800)

        # 音频配置
        self.AUDIO_SAMPLE_RATE = audio.get("sample_rate", 16000)
        self.AUDIO_CHANNELS = audio.get("channels", 1)
        self.AUDIO_FORMAT = audio.get("format", "wav")
        self.AUDIO_CHUNK_SIZE = audio.get("chunk_size", 1600)

        # 评估配置
        self.EVALUATION_REAL_TIME_SCORING_ENABLED = evaluation.get("enable_real_time_scoring", True)
        self.EVALUATION_SCORING_TIMEOUT = evaluation.get("scoring_timeout", 3000)
        self.EVALUATION_MIN_ANSWER_LENGTH = evaluation.get("min_answer_length", 10)

        # 评分阈值配置
        scoring_thresholds = config.get("scoring_thresholds", {})
        self.EVALUATION_FOLLOW_UP_SCORE_THRESHOLD = scoring_thresholds.get("follow_up_score_threshold", 60)
        self.INTERVIEW_PASS_THRESHOLD = scoring_thresholds.get("interview_pass_threshold", 80)
        _pass_mode = scoring_thresholds.get("interview_pass_status_mode") or "threshold"
        self.INTERVIEW_PASS_STATUS_MODE = (
            _pass_mode.strip().lower() if isinstance(_pass_mode, str) else "threshold"
        )

        # 流式面试配置
        self.STREAMING_INTERVIEW_FOLLOW_UP_ENABLED = streaming_interview.get("enable_follow_up", True)
        self.STREAMING_INTERVIEW_MAX_FOLLOW_UPS_PER_QUESTION = streaming_interview.get("max_follow_ups_per_question", 1)
        self.STREAMING_INTERVIEW_SESSION_TIMEOUT = streaming_interview.get("session_timeout", 1800)
        
        # 语音面试前端 UI（仅展示层）
        interview_ui = config.get("interview_ui", {})
        self.INTERVIEW_UI_SHOW_ASR_TEXT = interview_ui.get("show_asr_text", True)

        # 存储配置
        storage = config.get("storage", {})
        self.STORAGE_PATH = storage.get("path", "./storage")
        self.AUDIO_STORAGE_PATH = storage.get("audio_path", "./storage/audio")
        # 完整面试录音目录，默认在 STORAGE_PATH/answer_audio 下
        from pathlib import Path as _PathForAnswerAudio
        self.ANSWER_AUDIO_STORAGE_PATH = storage.get(
            "answer_audio_path",
            str(_PathForAnswerAudio(self.STORAGE_PATH) / "answer_audio"),
        )
        
        # 多服务配置
        services = config.get("services", {})
        self.SERVICES = services


        # 主应用服务配置
        voicebridge_service = services.get("voicebridge", {})
        self.VOICEBRIDGE_HOST = voicebridge_service.get("host", "0.0.0.0")
        self.VOICEBRIDGE_PORT = voicebridge_service.get("port", 8002)
        self.VOICEBRIDGE_DEBUG = voicebridge_service.get("debug", False)
        self.VOICEBRIDGE_RELOAD = voicebridge_service.get("reload", True)

        # SSL配置
        ssl_config = voicebridge_service.get("ssl", {})
        self.SSL_ENABLED = ssl_config.get("enabled", False)
        self.SSL_CERTFILE = ssl_config.get("certfile")
        self.SSL_KEYFILE = ssl_config.get("keyfile")
        self.SSL_CA_CERTS = ssl_config.get("ca_certs")

        # Rasa服务配置
        rasa_config = config.get("rasa", {})
        self.RASA_ENDPOINT = rasa_config.get("endpoint", "http://localhost:8012")
        self.RASA_MODEL_PATH = rasa_config.get("model_path", "./models/rasa")
        self.RASA_PORT = rasa_config.get("port", 8012)
        self.RASA_ENABLED = rasa_config.get("enabled", True)

        # Rasa日志文件配置（统一放入 logs/rasa/）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_rasa_dir = os.path.join(self.PROJECT_ROOT, "logs", "rasa")
        self.RASA_LOG_FILE = os.path.join(logs_rasa_dir, f"rasa_{timestamp}.log")

        # LLM服务配置
        llm_config = config.get("llm", {})
        self.LLM_ENABLED = llm_config.get("enabled", True)
        self.LLM_PROVIDER = llm_config.get("provider", "local")

        # 本地模型服务配置
        self.LLM_API_BASE = llm_config.get("api_base", "http://localhost:8002")
        self.LLM_MODEL = llm_config.get("model", "Qwen2.5-7B")
        self.LLM_API_KEY = llm_config.get("api_key", "not-needed")

        # 模型参数
        self.LLM_TEMPERATURE = llm_config.get("temperature", 0.7)
        self.LLM_TIMEOUT = llm_config.get("timeout", 60)
        self.LLM_MAX_RETRIES = llm_config.get("max_retries", 3)
        self.LLM_STREAM = llm_config.get("stream", False)

        # 上下文长度配置
        self.LLM_MAX_CONTEXT_LENGTH = llm_config.get("max_context_length", 4096)
        self.LLM_MAX_INPUT_TOKENS = llm_config.get("max_input_tokens", 4096)
        self.LLM_MAX_TOKENS = llm_config.get("max_tokens", 2048)
        self.LLM_TRUNCATION = llm_config.get("truncation", True)

        # 向后兼容的旧配置
        # vLLM 配置（如果存在旧配置）
        if "vllm" in llm_config:
            vllm_config = llm_config["vllm"]
            if not hasattr(self, 'VLLM_BASE_URL'):
                self.VLLM_BASE_URL = vllm_config.get("base_url", self.LLM_API_BASE)
                self.VLLM_API_KEY = vllm_config.get("api_key", self.LLM_API_KEY)
                self.VLLM_MODEL = vllm_config.get("model", self.LLM_MODEL)
                self.VLLM_TEMPERATURE = vllm_config.get("temperature", self.LLM_TEMPERATURE)
                self.VLLM_MAX_TOKENS = vllm_config.get("max_tokens", self.LLM_MAX_TOKENS)
                self.VLLM_TIMEOUT = vllm_config.get("timeout", self.LLM_TIMEOUT)

        # OpenAI 配置（如果存在旧配置）
        if "openai" in llm_config:
            openai_config = llm_config["openai"]
            if not hasattr(self, 'OPENAI_BASE_URL'):
                self.OPENAI_BASE_URL = openai_config.get("base_url", "https://api.openai.com/v1")
                self.OPENAI_API_KEY = openai_config.get("api_key")
                self.OPENAI_MODEL = openai_config.get("model", "gpt-4")
                self.OPENAI_TEMPERATURE = openai_config.get("temperature", self.LLM_TEMPERATURE)
                self.OPENAI_MAX_TOKENS = openai_config.get("max_tokens", self.LLM_MAX_TOKENS)
                self.OPENAI_TIMEOUT = openai_config.get("timeout", self.LLM_TIMEOUT)

        # 向后兼容的服务配置
        server = config.get("server", {})
        self.HOST = server.get("host", self.VOICEBRIDGE_HOST)
        self.PORT = server.get("port", self.VOICEBRIDGE_PORT)
        self.DEBUG = server.get("debug", self.VOICEBRIDGE_DEBUG)

        # 向后兼容的LLM配置（如果存在旧配置且services中没有llm配置）
        if "llm" in config and not services.get("llm"):
            old_llm = config.get("llm", {})
            self.LLM_ENABLED = old_llm.get("enabled", True)
            self.LLM_PROVIDER = old_llm.get("provider", "vllm")
            # 其他LLM配置保持默认值
        
        # 部署模式（现在统一为流式服务模式）
        self.DEPLOYMENT_MODE = "streaming"
        
        # 日志配置（统一放入 logs/app/）
        logging = config.get("logging", {})
        self.LOG_LEVEL = logging.get("level", "INFO")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_app_dir = os.path.join(self.PROJECT_ROOT, "logs", "app")
        self.LOG_FILE = os.path.join(logs_app_dir, f"{timestamp}.log")
        # 评估与调试日志目录（供 agent 等模块使用）
        self.LOG_DIR_EVALUATION = os.path.join(self.PROJECT_ROOT, "logs", "evaluation")
        self.LOG_DIR_DEBUG_PROMPT = os.path.join(self.PROJECT_ROOT, "logs", "debug_prompt")

    def get_config(self, key: str) -> Dict[str, Any]:
        """
        获取配置节（兼容旧的ConfigManager接口）

        参数:
            key: 配置节名称，如 "voice_streaming"

        返回:
            配置字典
        """
        # 构建配置字典，包含所有相关的配置项
        if key == "voice_streaming":
            return {
                "websocket": {
                    "host": self.VOICE_STREAMING_WEBSOCKET_HOST,
                    "port": self.VOICE_STREAMING_WEBSOCKET_PORT,
                    "max_connections": self.VOICE_STREAMING_MAX_CONNECTIONS,
                    "ping_interval": self.VOICE_STREAMING_PING_INTERVAL,
                    "ping_timeout": self.VOICE_STREAMING_PING_TIMEOUT,
                },
                "asr": {
                    "appkey": self.ALIYUN_ASR_APPKEY,
                    "access_key_id": self.ALIYUN_ASR_ACCESS_KEY_ID,
                    "access_key_secret": self.ALIYUN_ASR_ACCESS_KEY_SECRET,
                    "enable_token_refresh": self.ALIYUN_ASR_TOKEN_REFRESH_ENABLED,
                    "token_refresh_interval": self.ALIYUN_ASR_TOKEN_REFRESH_INTERVAL,
                },
                "audio": {
                    "sample_rate": self.AUDIO_SAMPLE_RATE,
                    "channels": self.AUDIO_CHANNELS,
                    "format": self.AUDIO_FORMAT,
                    "chunk_size": self.AUDIO_CHUNK_SIZE,
                },
                "evaluation": {
                    "enable_real_time_scoring": self.EVALUATION_REAL_TIME_SCORING_ENABLED,
                    "scoring_timeout": self.EVALUATION_SCORING_TIMEOUT,
                    "min_answer_length": self.EVALUATION_MIN_ANSWER_LENGTH,
                },
                "streaming_interview": {
                    "enable_follow_up": self.STREAMING_INTERVIEW_FOLLOW_UP_ENABLED,
                    "max_follow_ups_per_question": self.STREAMING_INTERVIEW_MAX_FOLLOW_UPS_PER_QUESTION,
                    "session_timeout": self.STREAMING_INTERVIEW_SESSION_TIMEOUT,
                }
            }
        elif key == "scoring_thresholds":
            return {
                "follow_up_score_threshold": self.EVALUATION_FOLLOW_UP_SCORE_THRESHOLD,
                "interview_pass_threshold": self.INTERVIEW_PASS_THRESHOLD,
                "interview_pass_status_mode": self.INTERVIEW_PASS_STATUS_MODE,
            }
        elif key == "server":
            return {
                "host": self.HOST,
                "port": self.PORT,
                "debug": self.DEBUG,
            }
        elif key == "llm":
            return {
                "api_base": self.LLM_API_BASE,
                "api_key": self.LLM_API_KEY,
                "model": self.LLM_MODEL,
                "temperature": self.LLM_TEMPERATURE,
                "timeout": self.LLM_TIMEOUT,
                "max_tokens": self.LLM_MAX_TOKENS,
                "max_context_length": self.LLM_MAX_CONTEXT_LENGTH,
                "max_input_tokens": self.LLM_MAX_INPUT_TOKENS,
                "stream": self.LLM_STREAM,
                "truncation": self.LLM_TRUNCATION,
            }
        elif key == "rasa":
            return {
                "endpoint": self.RASA_ENDPOINT,
                "model_path": self.RASA_MODEL_PATH,
                "port": self.RASA_PORT,
                "enabled": self.RASA_ENABLED,
                "log_file": self.RASA_LOG_FILE,
            }
        elif key == "storage":
            return {
                "path": self.STORAGE_PATH,
                "audio_path": self.AUDIO_STORAGE_PATH,
            }
        elif key == "logging":
            return {
                "level": self.LOG_LEVEL,
                "log_file": self.LOG_FILE,
            }
        else:
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值（兼容字典接口）

        参数:
            key: 配置键
            default: 默认值

        返回:
            配置值
        """
        # 尝试从配置节获取
        if hasattr(self, key.upper()):
            return getattr(self, key.upper(), default)
        return default

    def _get_config_with_auto(self, config_path: str, default_value: Any) -> Any:
        """
        获取配置值，支持自动配置获取

        参数:
            config_path: 配置路径，如 "voice_streaming.asr.access_key_id"
            default_value: 默认值

        返回:
            配置值
        """
        # 先尝试从自动配置管理器获取
        auto_value = self.config_manager.get_config_value(config_path)
        if auto_value is not None:
            logger.debug(f"使用自动配置: {config_path} = {auto_value}")
            return auto_value

        # 如果自动配置获取失败，使用默认值
        # 处理环境变量替换
        if isinstance(default_value, str) and default_value.startswith("${") and default_value.endswith("}"):
            env_var = default_value[2:-1]  # 移除 ${ 和 }
            env_value = os.getenv(env_var)
            if env_value:
                logger.debug(f"使用环境变量: {env_var} = {env_value}")
                return env_value
            else:
                logger.warning(f"环境变量未设置: {env_var}，使用空字符串")
                return ""

        return default_value

    def refresh_auto_config(self) -> bool:
        """
        刷新自动配置

        返回:
            bool: 刷新是否成功
        """
        return self.config_manager.force_refresh()

    def _use_defaults(self):
        """使用默认配置"""
        # 语音流式服务配置
        self.VOICE_STREAMING_WEBSOCKET_HOST = "0.0.0.0"
        self.VOICE_STREAMING_WEBSOCKET_PORT = 8003
        self.VOICE_STREAMING_MAX_CONNECTIONS = 20
        self.VOICE_STREAMING_PING_INTERVAL = 30
        self.VOICE_STREAMING_PING_TIMEOUT = 10

        # ASR配置（支持自动配置获取）
        self.ALIYUN_ASR_APPKEY = self._get_config_with_auto("voice_streaming.asr.appkey", "${ALIYUN_ASR_APPKEY}")
        self.ALIYUN_ASR_ACCESS_KEY_ID = self._get_config_with_auto("voice_streaming.asr.access_key_id", "${ALIYUN_ACCESS_KEY_ID}")
        self.ALIYUN_ASR_ACCESS_KEY_SECRET = self._get_config_with_auto("voice_streaming.asr.access_key_secret", "${ALIYUN_ACCESS_KEY_SECRET}")
        self.ALIYUN_ASR_TOKEN_REFRESH_ENABLED = True
        self.ALIYUN_ASR_TOKEN_REFRESH_INTERVAL = 1800

        # 音频配置
        self.AUDIO_SAMPLE_RATE = 16000
        self.AUDIO_CHANNELS = 1
        self.AUDIO_FORMAT = "wav"
        self.AUDIO_CHUNK_SIZE = 1600

        # 评估配置
        self.EVALUATION_REAL_TIME_SCORING_ENABLED = True
        self.EVALUATION_FOLLOW_UP_SCORE_THRESHOLD = 60
        self.EVALUATION_SCORING_TIMEOUT = 3000
        self.EVALUATION_MIN_ANSWER_LENGTH = 10

        # 流式面试配置
        self.STREAMING_INTERVIEW_FOLLOW_UP_ENABLED = True
        self.STREAMING_INTERVIEW_MAX_FOLLOW_UPS_PER_QUESTION = 1
        self.STREAMING_INTERVIEW_SESSION_TIMEOUT = 1800

        self.INTERVIEW_UI_SHOW_ASR_TEXT = True

        # Rasa配置
        self.RASA_MODEL_PATH = "./models/rasa"
        self.RASA_PORT = 8012
        self.RASA_ENDPOINT = "http://localhost:8012"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.RASA_LOG_FILE = os.path.join(self.PROJECT_ROOT, "logs", "rasa", f"rasa_{timestamp}.log")
        self.LOG_DIR_EVALUATION = os.path.join(self.PROJECT_ROOT, "logs", "evaluation")
        self.LOG_DIR_DEBUG_PROMPT = os.path.join(self.PROJECT_ROOT, "logs", "debug_prompt")

        # 存储配置
        self.STORAGE_PATH = "./storage"
        self.AUDIO_STORAGE_PATH = "./storage/audio"
        self.ANSWER_AUDIO_STORAGE_PATH = os.path.join(self.STORAGE_PATH, "answer_audio")
        # 向后兼容的旧配置
        self.HOST = "0.0.0.0"
        self.PORT = 8002  # 与 VOICEBRIDGE_PORT 一致
        self.DEBUG = False
        self.HOT_WORDS_ENABLED = True
        self.HOT_WORDS_FILE = "./config/hot_words.json"
        self.LOG_LEVEL = "INFO"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.LOG_FILE = os.path.join(self.PROJECT_ROOT, "logs", "app", f"{timestamp}.log")
        self.TTS_LOG_FILE = os.path.join(self.PROJECT_ROOT, "logs", "app", f"{timestamp}.log")

    def _setup_paths(self):
        """确保存储目录与各日志目录存在"""
        os.makedirs(self.AUDIO_STORAGE_PATH, exist_ok=True)
        os.makedirs(os.path.dirname(self.LOG_FILE), exist_ok=True)
        for sub in ("service", "app", "rasa", "evaluation", "debug_prompt", "natapp"):
            os.makedirs(os.path.join(self.PROJECT_ROOT, "logs", sub), exist_ok=True)


# 全局配置实例
settings = Settings()

# 确保存储目录存在
os.makedirs(settings.AUDIO_STORAGE_PATH, exist_ok=True)
os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)

