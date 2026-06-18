"""
集成扩展接口
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from loguru import logger

from app.dependencies import get_integration_service
from app.services.integration_service import IntegrationService

router = APIRouter()


class AudioStorageRequest(BaseModel):
    """录音存储请求"""
    audio_id: str
    user_id: str
    storage_path: Optional[str] = None


class ScoringRequest(BaseModel):
    """评分关联请求"""
    text: str
    user_id: str
    job_title: Optional[str] = None


class InterviewStatusRequest(BaseModel):
    """面试状态请求"""
    user_id: str
    interview_id: Optional[str] = None


@router.post("/audio/storage")
async def save_audio(
    request: AudioStorageRequest,
    integration_service: IntegrationService = Depends(get_integration_service)
):
    """
    录音存储接口：保存ASR原始音频和识别文本
    """
    try:
        result = await integration_service.save_audio(
            audio_id=request.audio_id,
            user_id=request.user_id,
            storage_path=request.storage_path
        )
        
        return {
            "code": 200,
            "data": result,
            "message": "录音存储成功"
        }
    except Exception as e:
        logger.error(f"录音存储失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"录音存储失败: {str(e)}")


@router.post("/scoring/link")
async def link_scoring(
    request: ScoringRequest,
    integration_service: IntegrationService = Depends(get_integration_service)
):
    """
    评分关联接口：将识别文本同步至评分模块
    """
    try:
        result = await integration_service.link_scoring(
            text=request.text,
            user_id=request.user_id,
            job_title=request.job_title
        )
        
        return {
            "code": 200,
            "data": result,
            "message": "评分关联成功"
        }
    except Exception as e:
        logger.error(f"评分关联失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"评分关联失败: {str(e)}")


@router.post("/interview/status")
async def get_interview_status(
    request: InterviewStatusRequest,
    integration_service: IntegrationService = Depends(get_integration_service)
):
    """
    面试状态接口：返回当前面试进度、异常信息
    """
    try:
        result = await integration_service.get_interview_status(
            user_id=request.user_id,
            interview_id=request.interview_id
        )
        
        return {
            "code": 200,
            "data": result
        }
    except Exception as e:
        logger.error(f"获取面试状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取面试状态失败: {str(e)}")

