"""
独立的LLM服务
专门用于提供大语言模型API服务，支持vLLM和其他兼容OpenAI的API
"""

import os
import asyncio
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pydantic import BaseModel
from loguru import logger

from app.services.llm_service import LLMService


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False


class ChatCompletionResponse(BaseModel):
    content: str
    model: str
    usage: Optional[Dict] = None
    finish_reason: Optional[str] = None


app = FastAPI(title="VoiceBridge LLM Service", version="1.0.0")

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局LLM服务实例
llm_service: Optional[LLMService] = None


@app.on_event("startup")
async def startup_event():
    """服务启动时初始化LLM"""
    global llm_service

    try:
        logger.info("正在初始化LLM服务...")
        logger.info(f"LLM配置: 提供商={settings.LLM_PROVIDER}, 模型={settings.LLM_MODEL}, API_BASE={settings.LLM_API_BASE}")
        llm_service = LLMService()
        logger.info("LLM服务初始化成功")
    except Exception as e:
        logger.error(f"LLM服务初始化失败: {e}")


@app.get("/health")
async def health_check():
    """健康检查接口"""
    if llm_service and llm_service.enabled:
        health_status = await llm_service.health_check()
        return health_status
    else:
        return {
            "status": "disabled",
            "message": "LLM服务未启用或未初始化"
        }


@app.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completion(request: ChatCompletionRequest):
    """对话补全接口（兼容OpenAI格式）"""
    if not llm_service or not llm_service.enabled:
        raise HTTPException(
            status_code=503,
            detail="LLM服务不可用"
        )

    try:
        logger.info(f"收到对话请求，消息数量: {len(request.messages)}")

        # 转换消息格式
        messages = [{"role": msg.role, "content": msg.content} for msg in request.messages]

        # 调用LLM服务
        result = await llm_service.chat_completion(
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=request.stream
        )

        logger.info("对话补全成功")
        return ChatCompletionResponse(
            content=result["content"],
            model=result.get("model", "unknown"),
            usage=result.get("usage"),
            finish_reason=result.get("finish_reason")
        )

    except Exception as e:
        logger.error(f"对话补全异常: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"对话补全失败: {str(e)}"
        )


@app.get("/models")
async def list_models():
    """列出可用模型"""
    if not llm_service or not llm_service.enabled:
        return {"models": []}

    try:
        # 返回配置的模型信息
        return {
            "models": [{
                "id": llm_service._get_model(),
                "object": "model",
                "created": 0,
                "owned_by": llm_service.provider
            }]
        }
    except Exception as e:
        logger.error(f"获取模型列表失败: {e}")
        return {"models": []}


@app.get("/status")
async def get_status():
    """获取LLM服务状态"""
    if not llm_service:
        return {
            "available": False,
            "message": "LLM服务未初始化"
        }

    return {
        "available": llm_service.enabled,
        "provider": llm_service.provider,
        "model": llm_service._get_model() if llm_service.enabled else None,
        "base_url": llm_service._get_base_url() if llm_service.enabled else None
    }


@app.post("/generate")
async def generate_text(request: dict):
    """简单的文本生成接口"""
    if not llm_service or not llm_service.enabled:
        raise HTTPException(
            status_code=503,
            detail="LLM服务不可用"
        )

    try:
        prompt = request.get("prompt", "")
        system_message = request.get("system_message")
        temperature = request.get("temperature")
        max_tokens = request.get("max_tokens")

        if not prompt:
            raise HTTPException(
                status_code=400,
                detail="缺少prompt参数"
            )

        result = await llm_service.generate_response(
            prompt=prompt,
            system_message=system_message,
            temperature=temperature,
            max_tokens=max_tokens
        )

        return {"generated_text": result}

    except Exception as e:
        logger.error(f"文本生成异常: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"文本生成失败: {str(e)}"
        )


if __name__ == "__main__":
    # 从环境变量获取配置
    host = os.getenv("LLM_HOST", "0.0.0.0")
    port = int(os.getenv("LLM_PORT", "8002"))

    logger.info(f"启动独立的LLM服务: {host}:{port}")
    uvicorn.run(app, host=host, port=port)