"""DataStoreWare 面试评价回调（主报告落库后调度百度云 ASR）。"""

from __future__ import annotations

import os

import httpx
from loguru import logger


def _dsw_api_base_url() -> str:
    return (os.environ.get("DSW_API_BASE_URL") or "http://127.0.0.1:9005").rstrip("/")


async def schedule_baidu_asr_report_after_main_eval(invitation_id: str) -> None:
    inv_id = (invitation_id or "").strip()
    if not inv_id:
        return
    url = f"{_dsw_api_base_url()}/api/v1/interview-evaluation/evaluation/schedule-baidu-asr"
    headers = {}
    internal_key = (os.environ.get("DSW_INTERNAL_SCHEDULE_KEY") or "").strip()
    if internal_key:
        headers["X-DSW-Internal-Key"] = internal_key
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json={"invitation_id": inv_id}, headers=headers)
        if response.status_code >= 400:
            logger.warning(
                "调度 DataStoreWare 百度云 ASR 失败: invitation_id={} status={} body={}",
                inv_id,
                response.status_code,
                response.text[:500],
            )
            return
        payload = response.json()
        logger.info(
            "已请求 DataStoreWare 调度百度云 ASR: invitation_id={} scheduled={}",
            inv_id,
            payload.get("scheduled"),
        )
    except Exception as e:
        logger.warning(
            "调度 DataStoreWare 百度云 ASR 异常: invitation_id={} error={}",
            inv_id,
            e,
        )
