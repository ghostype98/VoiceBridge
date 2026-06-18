"""
LLM服务模块
支持多种LLM提供商：vLLM, OpenAI, Azure, etc.
优先使用本地部署的vLLM服务
"""

import asyncio
import json
from typing import Dict, List, Optional, Any, Union
from loguru import logger
import httpx

from config.settings import settings


class LLMService:
    """LLM服务类"""

    def __init__(self):
        self.client = None
        self.provider = settings.LLM_PROVIDER
        self.enabled = settings.LLM_ENABLED

        if not self.enabled:
            logger.info("LLM服务已禁用")
            return

        # 初始化HTTP客户端
        timeout = settings.LLM_TIMEOUT
        if self.provider == "local":
            timeout = settings.LLM_TIMEOUT
        elif self.provider in ["qwen", "openai", "zhipu", "moonshot"]:
            timeout = settings.LLM_TIMEOUT

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout)
        )

        logger.info(f"LLM服务初始化完成，当前提供商: {self.provider}, 模型: {settings.LLM_MODEL}")

        # 验证上下文长度配置
        if settings.LLM_MAX_CONTEXT_LENGTH > 4096 and self.provider == "local":
            logger.warning(f"本地模型 {settings.LLM_MODEL} 实际只支持 4096 tokens，"
                         f"配置的 max_context_length ({settings.LLM_MAX_CONTEXT_LENGTH}) 会被限制为 4096")
            self.max_context_length = 4096
        else:
            self.max_context_length = settings.LLM_MAX_CONTEXT_LENGTH

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    def _get_base_url(self) -> str:
        """获取基础URL"""
        if self.provider == "local":
            return settings.LLM_API_BASE.rstrip("/")
        elif self.provider in ["vllm", "qwen", "openai", "zhipu", "moonshot"]:
            # 对于这些提供商，使用统一的API_BASE配置
            return settings.LLM_API_BASE.rstrip("/")
        else:
            raise ValueError(f"不支持的LLM提供商: {self.provider}")

    def _get_api_key(self) -> Optional[str]:
        """获取API密钥"""
        if self.provider == "local":
            return settings.LLM_API_KEY
        elif self.provider in ["vllm", "qwen", "openai", "zhipu", "moonshot"]:
            return settings.LLM_API_KEY
        return None

    def _get_model(self) -> str:
        """获取模型名称"""
        return settings.LLM_MODEL

    def _get_temperature(self) -> float:
        """获取温度参数"""
        return settings.LLM_TEMPERATURE

    def _get_max_tokens(self) -> int:
        """获取最大token数"""
        return settings.LLM_MAX_TOKENS

    def _get_max_context_length(self) -> int:
        """获取最大上下文长度"""
        return self.max_context_length

    def _get_max_retries(self) -> int:
        """获取最大重试次数"""
        return settings.LLM_MAX_RETRIES

    def _get_stream(self) -> bool:
        """获取是否使用流式输出"""
        return settings.LLM_STREAM

    async def _call_local_api(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """调用本地LLM API（兼容OpenAI格式）"""
        # 对于本地vLLM服务，使用/v1/chat/completions路径
        url = f"{self._get_base_url()}/v1/chat/completions"

        headers = {"Content-Type": "application/json"}
        api_key = self._get_api_key()
        if api_key and api_key != "not-needed":
            headers["Authorization"] = f"Bearer {api_key}"

        data = {
            "model": self._get_model(),
            "messages": messages,
            "temperature": kwargs.get("temperature", self._get_temperature()),
            "max_tokens": kwargs.get("max_tokens", self._get_max_tokens()),
            "stream": kwargs.get("stream", self._get_stream())
        }

        # 添加上下文长度限制
        if "max_input_tokens" not in kwargs and self._get_max_context_length():
            data["max_input_tokens"] = min(self._get_max_context_length(), settings.LLM_MAX_INPUT_TOKENS)

        # 添加额外的参数
        for key, value in kwargs.items():
            if key not in ["temperature", "max_tokens", "stream", "max_input_tokens"]:
                data[key] = value

        logger.debug(f"调用本地LLM API: {url}")
        response = await self.client.post(url, headers=headers, json=data)

        if response.status_code != 200:
            error_text = response.text
            logger.error(f"本地LLM API调用失败: {response.status_code} - {error_text}")
            raise Exception(f"本地LLM API调用失败: {response.status_code}")

        result = response.json()

        # 提取回复内容
        if "choices" in result and len(result["choices"]) > 0:
            message = result["choices"][0].get("message", {})
            content = message.get("content", "")
            return {
                "content": content,
                "usage": result.get("usage", {}),
                "model": result.get("model", self._get_model()),
                "finish_reason": result["choices"][0].get("finish_reason")
            }
        else:
            raise Exception("本地LLM API返回格式错误")

    async def _call_vllm_api(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """调用vLLM API（已废弃，使用_local_api替代）"""
        logger.warning("vLLM提供商已废弃，请使用'local'提供商")
        return await self._call_local_api(messages, **kwargs)

    async def _call_openai_api(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """调用OpenAI兼容API"""
        # 使用标准的OpenAI API路径
        url = f"{self._get_base_url()}/v1/chat/completions"

        headers = {"Content-Type": "application/json"}
        api_key = self._get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        data = {
            "model": self._get_model(),
            "messages": messages,
            "temperature": kwargs.get("temperature", self._get_temperature()),
            "max_tokens": kwargs.get("max_tokens", self._get_max_tokens()),
            "stream": kwargs.get("stream", False)
        }

        # 添加额外的参数
        for key, value in kwargs.items():
            if key not in ["temperature", "max_tokens", "stream"]:
                data[key] = value

        logger.debug(f"调用OpenAI API: {url}")
        response = await self.client.post(url, headers=headers, json=data)

        if response.status_code != 200:
            error_text = response.text
            logger.error(f"OpenAI API调用失败: {response.status_code} - {error_text}")
            raise Exception(f"OpenAI API调用失败: {response.status_code}")

        result = response.json()

        # 提取回复内容
        if "choices" in result and len(result["choices"]) > 0:
            message = result["choices"][0].get("message", {})
            content = message.get("content", "")
            return {
                "content": content,
                "usage": result.get("usage", {}),
                "model": result.get("model", self._get_model()),
                "finish_reason": result["choices"][0].get("finish_reason")
            }
        else:
            raise Exception("OpenAI API返回格式错误")

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        执行对话补全

        Args:
            messages: 消息列表，格式如 [{"role": "user", "content": "hello"}]
            temperature: 温度参数
            max_tokens: 最大token数
            stream: 是否流式输出
            **kwargs: 其他参数

        Returns:
            包含回复内容的字典
        """
        if not self.enabled:
            raise Exception("LLM服务未启用")

        if not self.client:
            raise Exception("LLM客户端未初始化")

        # 设置参数
        call_kwargs = {}
        if temperature is not None:
            call_kwargs["temperature"] = temperature
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens
        if stream:
            call_kwargs["stream"] = stream

        # 添加其他参数
        call_kwargs.update(kwargs)

        try:
            if self.provider == "local":
                return await self._call_local_api(messages, **call_kwargs)
            elif self.provider == "vllm":
                # vLLM现在使用统一的本地API调用方式
                return await self._call_local_api(messages, **call_kwargs)
            elif self.provider == "openai":
                return await self._call_openai_api(messages, **call_kwargs)
            elif self.provider in ["qwen", "zhipu", "moonshot"]:
                # 这些提供商都使用OpenAI兼容的API格式
                return await self._call_openai_api(messages, **call_kwargs)
            else:
                raise Exception(f"不支持的LLM提供商: {self.provider}")

        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            # 回退策略：尝试使用OpenAI兼容的API
            if self.provider in ["local", "vllm"]:
                logger.info(f"{self.provider}调用失败，尝试回退到OpenAI兼容API")
                try:
                    return await self._call_openai_api(messages, **call_kwargs)
                except Exception as fallback_error:
                    logger.error(f"OpenAI兼容API回退也失败: {fallback_error}")
                    raise e
            else:
                raise e

    async def generate_response(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        生成单个回复

        Args:
            prompt: 用户提示
            system_message: 系统消息（可选）
            **kwargs: 其他参数

        Returns:
            生成的回复内容
        """
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        result = await self.chat_completion(messages, **kwargs)
        return result["content"]

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        if not self.enabled:
            return {"status": "disabled", "message": "LLM服务已禁用"}

        try:
            # 统一的健康检查：尝试调用模型列表API
            url = f"{self._get_base_url()}/v1/models"

            headers = {"Content-Type": "application/json"}
            api_key = self._get_api_key()
            if api_key and api_key != "not-needed":
                headers["Authorization"] = f"Bearer {api_key}"

            response = await self.client.get(url, headers=headers)

            if response.status_code == 200:
                return {
                    "status": "healthy",
                    "provider": self.provider,
                    "model": self._get_model(),
                    "base_url": self._get_base_url(),
                    "max_context_length": self.max_context_length,
                    "temperature": self._get_temperature(),
                    "max_tokens": self._get_max_tokens()
                }
            else:
                return {
                    "status": "unhealthy",
                    "provider": self.provider,
                    "error": f"HTTP {response.status_code}: {response.text[:100]}"
                }

        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": self.provider,
                "error": str(e)
            }

    async def close(self):
        """关闭服务"""
        if self.client:
            await self.client.aclose()
            self.client = None


# 全局LLM服务实例
llm_service = LLMService()