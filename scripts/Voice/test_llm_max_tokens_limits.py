#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 LLM max_tokens 参数：
1) 验证 1500 是否可用、是否出现截断
2) 探测当前接口可接受的最大 max_tokens（请求层面）
"""

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

from loguru import logger


PROJECT_ROOT = "/opt/voicebridge"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.llm_service import LLMService  # noqa: E402
from config.settings import settings  # noqa: E402


PROMPT = """你是严格的 JSON 输出器。请仅输出 JSON，不要 markdown 代码块，不要额外解释。
请返回以下结构：
{
  "score": 0-100 的整数,
  "reason": "至少 120 字中文评价",
  "dimensions": {
    "content_completeness": 0-25 的整数,
    "logical_clarity": 0-25 的整数,
    "professional_level": 0-25 的整数,
    "expression_ability": 0-25 的整数
  }
}
"""


def _extract_content(resp: Any) -> str:
    if isinstance(resp, dict):
        content = resp.get("content", "") or resp.get("text", "")
    else:
        content = str(resp)
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def _is_valid_json_text(text: str) -> Tuple[bool, Optional[str]]:
    try:
        json.loads(text)
        return True, None
    except Exception as e:
        return False, str(e)


async def _single_call(llm: LLMService, max_tokens: int) -> Dict[str, Any]:
    try:
        resp = await llm.chat_completion(
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        content = _extract_content(resp)
        ok_json, json_err = _is_valid_json_text(content)
        finish_reason = resp.get("finish_reason") if isinstance(resp, dict) else None
        return {
            "ok": True,
            "max_tokens": max_tokens,
            "finish_reason": finish_reason,
            "content_len": len(content),
            "valid_json": ok_json,
            "json_error": json_err,
            "preview": content[:200],
        }
    except Exception as e:
        return {
            "ok": False,
            "max_tokens": max_tokens,
            "error": str(e),
        }


async def test_1500(llm: LLMService) -> Dict[str, Any]:
    logger.info("开始验证 max_tokens=1500 ...")
    return await _single_call(llm, 1500)


async def probe_max_tokens_limit(llm: LLMService) -> Dict[str, Any]:
    # 基于当前配置给出探测上界，避免无意义超大值
    # local provider 在本项目里上下文通常 4096，输出 token 再大一般会被服务拒绝或截断。
    upper_cap = int(os.getenv("LLM_PROBE_UPPER_CAP", "8192"))
    lo, hi = 1, upper_cap
    best_ok = 0
    last_fail: Optional[Dict[str, Any]] = None

    # 二分查“请求可接受最大值”（HTTP/服务层面）
    while lo <= hi:
        mid = (lo + hi) // 2
        result = await _single_call(llm, mid)
        if result.get("ok"):
            best_ok = mid
            lo = mid + 1
        else:
            last_fail = result
            hi = mid - 1

    # 对 best_ok 做一次可解析性校验（不是接口上限，只是业务可用性参考）
    usable_check = await _single_call(llm, best_ok) if best_ok > 0 else None
    return {
        "best_request_accepted_max_tokens": best_ok,
        "usable_check": usable_check,
        "last_fail": last_fail,
        "probe_upper_cap": upper_cap,
    }


async def main() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="{time:HH:mm:ss} | {level: <8} | {message}",
        level="INFO",
    )

    logger.info(
        "LLM配置: provider={}, model={}, default_max_tokens={}, max_context_length={}, max_input_tokens={}",
        settings.LLM_PROVIDER,
        settings.LLM_MODEL,
        settings.LLM_MAX_TOKENS,
        settings.LLM_MAX_CONTEXT_LENGTH,
        settings.LLM_MAX_INPUT_TOKENS,
    )

    llm = LLMService()
    result_1500 = await test_1500(llm)
    logger.info("1500测试结果: {}", result_1500)

    limit_result = await probe_max_tokens_limit(llm)
    logger.info("max_tokens上限探测结果: {}", limit_result)

    print("\n=== RESULT_JSON_START ===")
    print(
        json.dumps(
            {
                "test_1500": result_1500,
                "limit_probe": limit_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("=== RESULT_JSON_END ===")


if __name__ == "__main__":
    asyncio.run(main())
