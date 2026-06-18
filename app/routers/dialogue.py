"""
对话管理衔接接口
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
from loguru import logger

from fastapi import Depends
from app.dependencies import get_dialogue_service
from app.services.dialogue_service import DialogueService

router = APIRouter()


class ConversationState(BaseModel):
    """对话状态模型"""
    slot: Dict[str, Any] = {}


class DialogueRequest(BaseModel):
    """对话请求模型"""
    user_id: str
    asr_text: str
    conversation_state: Optional[ConversationState] = None
    intent: Optional[str] = None


class DialogueResponse(BaseModel):
    """对话响应模型"""
    code: int
    data: dict
    message: Optional[str] = None


@router.post("/process", response_model=DialogueResponse)
async def process_dialogue(
    request: DialogueRequest,
    dialogue_service: DialogueService = Depends(get_dialogue_service)
):
    """
    对话管理衔接接口：处理多轮对话逻辑
    
    - **user_id**: 候选人唯一标识
    - **asr_text**: ASR识别结果
    - **conversation_state**: 对话状态（槽位信息）
    - **intent**: 识别意图（可选）
    """
    try:
        if not request.user_id or not request.asr_text:
            raise HTTPException(status_code=400, detail="user_id和asr_text不能为空")
        
        logger.info(f"收到对话请求: user_id={request.user_id}, text={request.asr_text[:50]}...")
        
        # 调用对话服务
        result = await dialogue_service.process(
            user_id=request.user_id,
            asr_text=request.asr_text,
            conversation_state=request.conversation_state.dict() if request.conversation_state else {},
            intent=request.intent
        )
        
        # 构建响应
        response_data = {
            "tts_text": result.get("tts_text", ""),
            "conversation_state": result.get("conversation_state", {}),
            "fallback": result.get("fallback", False)
        }
        
        logger.info(f"对话处理成功: user_id={request.user_id}, tts_text={result.get('tts_text', '')[:50]}...")
        
        return DialogueResponse(
            code=200,
            data=response_data
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对话处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"对话处理失败: {str(e)}")


@router.get("/status/{user_id}")
async def get_conversation_status(
    user_id: str,
    dialogue_service: DialogueService = Depends(get_dialogue_service)
):
    """获取指定用户的对话状态"""
    try:
        status = await dialogue_service.get_status(user_id)
        return {
            "code": 200,
            "data": status
        }
    except Exception as e:
        logger.error(f"获取对话状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取对话状态失败: {str(e)}")

