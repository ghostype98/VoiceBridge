#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小化阿里云实时语音识别探测脚本

用途：
1) 读取 config/config.yaml 中的 AK/SK/AppKey
2) 调用 CreateToken
3) 建立 nls-gateway WebSocket
4) 发送 StartTranscription -> 一小段静音 -> StopTranscription
5) 打印关键返回码，快速定位故障点
"""

import asyncio
import json
import os
import sys
import uuid

import websockets
import yaml


def mask(value: str) -> str:
    if not value or len(value) < 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg["voice_streaming"]["asr"]


async def run_probe(token: str, appkey: str) -> int:
    url = f"wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1?token={token}"
    task_id = uuid.uuid4().hex

    start = {
        "header": {
            "message_id": uuid.uuid4().hex,
            "task_id": task_id,
            "namespace": "SpeechTranscriber",
            "name": "StartTranscription",
            "appkey": appkey,
        },
        "payload": {
            "format": "pcm",
            "sample_rate": 16000,
            "enable_intermediate_result": True,
            "enable_punctuation_prediction": True,
            "enable_inverse_text_normalization": True,
        },
    }

    stop = {
        "header": {
            "message_id": uuid.uuid4().hex,
            "task_id": task_id,
            "namespace": "SpeechTranscriber",
            "name": "StopTranscription",
            "appkey": appkey,
        }
    }

    async with websockets.connect(url, open_timeout=15) as ws:
        await ws.send(json.dumps(start))

        # 等待开始确认
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            h = msg.get("header", {})
            name = h.get("name")
            status = h.get("status")
            status_text = h.get("status_text")
            print(f"[recv] name={name} status={status} status_text={status_text}")

            if name == "TaskFailed":
                print("FAIL_DETAIL:", json.dumps(msg, ensure_ascii=False))
                return 1
            if name == "TranscriptionStarted":
                break

        # 发送约100ms静音，避免 idle timeout
        await ws.send(b"\x00\x00" * 1600)
        await asyncio.sleep(0.2)
        await ws.send(json.dumps(stop))

        # 等待结束响应
        for _ in range(20):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            h = msg.get("header", {})
            name = h.get("name")
            status = h.get("status")
            status_text = h.get("status_text")
            print(f"[recv] name={name} status={status} status_text={status_text}")

            if name == "TaskFailed":
                print("FAIL_DETAIL:", json.dumps(msg, ensure_ascii=False))
                return 1
            if name == "TranscriptionCompleted":
                return 0

    return 0


def main() -> int:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.environ.get("CONFIG_FILE", os.path.join(project_root, "config", "config.yaml"))

    sys.path.insert(0, project_root)
    from app.voice_streaming.token_manager import ASRTokenManager

    try:
        asr = load_config(config_path)
        ak = asr["access_key_id"]
        sk = asr["access_key_secret"]
        appkey = asr["appkey"]
    except Exception as e:
        print(f"❌ 配置读取失败: {e}")
        return 2

    print("=== NLS Real-time Probe ===")
    print(f"CONFIG_FILE: {config_path}")
    print(f"AK: {mask(ak)}")
    print(f"AppKey: {mask(appkey)}")

    tm = ASRTokenManager(access_key_id=ak, access_key_secret=sk, region="cn-shanghai")
    token = tm.get_token()
    if not token:
        print("❌ CreateToken 失败")
        return 3
    print(f"✅ CreateToken 成功: {mask(token)}")

    try:
        rc = asyncio.run(run_probe(token, appkey))
    except Exception as e:
        print(f"❌ WebSocket 探测异常: {e}")
        return 4

    if rc == 0:
        print("✅ 探测成功：实时识别链路可用")
    else:
        print("❌ 探测失败：请根据上方 status/status_text 排查")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
