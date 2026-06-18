"""
服务依赖注入模块
实现单例模式管理服务实例，便于测试和替换
"""
from functools import lru_cache
from typing import Optional
from loguru import logger
import uuid

# ASR和TTS服务已迁移到语音流式服务
# from app.services.asr_service import ASRService
# from app.services.tts_service import TTSService
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMService
from app.services.integration_service import IntegrationService


# 服务实例缓存（单例模式）
# _asr_service: Optional[ASRService] = None
# _tts_service: Optional[TTSService] = None
_dialogue_service: Optional[DialogueService] = None
_llm_service: Optional[LLMService] = None
_integration_service: Optional[IntegrationService] = None


# ASR服务已迁移到语音流式服务，不再需要此依赖注入
# def get_asr_service() -> ASRService:
#     """
#     获取ASR服务实例（单例模式）
#
#     Returns:
#         ASRService实例
#     """
#     global _asr_service
#     if _asr_service is None:
#         logger.info("初始化ASR服务实例")
#         _asr_service = ASRService()
#     return _asr_service


def get_tts_service() -> None:
    """
    TTS功能已屏蔽，返回None
    """
    logger.info("TTS功能已屏蔽，不进行初始化")
    return None


def get_dialogue_service() -> DialogueService:
    """
    获取对话服务实例（单例模式）
    
    Returns:
        DialogueService实例
    """
    global _dialogue_service
    if _dialogue_service is None:
        logger.info("初始化对话服务实例")
        _dialogue_service = DialogueService()
    return _dialogue_service


def get_llm_service() -> LLMService:
    """
    获取LLM服务实例（单例模式）

    Returns:
        LLMService实例
    """
    global _llm_service
    if _llm_service is None:
        logger.info("初始化LLM服务实例")
        _llm_service = LLMService()
    return _llm_service


def get_integration_service() -> IntegrationService:
    """
    获取集成服务实例（单例模式）

    Returns:
        IntegrationService实例
    """
    global _integration_service
    if _integration_service is None:
        logger.info("初始化集成服务实例")
        _integration_service = IntegrationService()
    return _integration_service




def create_user_id() -> str:
    """创建用户ID（工具函数）"""
    return str(uuid.uuid4())


def reset_services():
    """
    重置所有服务实例（主要用于测试）
    """
    global _dialogue_service, _llm_service, _integration_service
    # _asr_service = None  # ASR服务已移除
    # _tts_service = None  # TTS服务已移除
    _dialogue_service = None
    _llm_service = None
    _integration_service = None
    logger.info("所有服务实例已重置")

