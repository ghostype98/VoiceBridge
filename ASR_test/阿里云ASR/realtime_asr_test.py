#!/usr/bin/env python3
"""
阿里云智能语音交互「实时语音识别」WebSocket 测试。

凭证与客户端构造与 app.voice_streaming.websocket_server.VoiceInterviewWebSocketServer
一致：使用 config.settings（config/config.yaml + auto_config 环境变量映射）。
请勿在本目录 .env 中填写 ALIYUN_ACCESS_KEY_*，以免与主工程配置不一致或覆盖错误。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent.parent
os.chdir(REPO_ROOT)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.voice_streaming.asr_client import AliyunASRClient  # noqa: E402
from app.voice_streaming.token_manager import ASRTokenManager  # noqa: E402
from config.settings import settings  # noqa: E402

DEFAULT_AUDIO_PATH = REPO_ROOT / "ASR_test" / "sample.wav"


def _load_dotenv_test_only() -> None:
    """仅补充测试用变量；override=False 不覆盖已在环境中设置的 ALIYUN_*。"""
    env_path = PROJECT_DIR / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def build_asr_client_like_websocket() -> AliyunASRClient:
    """与 VoiceInterviewWebSocketServer 初始化 ASR 客户端逻辑对齐。"""
    voice_config = settings.get_config("voice_streaming")
    asr_config: dict[str, Any] = dict(voice_config.get("asr") or {})
    recognition_config = voice_config.get("recognition") or {}

    asr_client_config: dict[str, Any] = {
        **asr_config,
        "format": recognition_config.get("format", "pcm"),
        "sample_rate": recognition_config.get("sample_rate", 16000),
        "endpoint": asr_config.get(
            "endpoint", "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"
        ),
    }

    token_manager = None
    access_key_id = (settings.ALIYUN_ASR_ACCESS_KEY_ID or "").strip()
    access_key_secret = (settings.ALIYUN_ASR_ACCESS_KEY_SECRET or "").strip()
    region = (asr_config.get("region") or "cn-shanghai").strip()

    if access_key_id and access_key_secret:
        token_manager = ASRTokenManager(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region=region,
        )

    appkey = (asr_config.get("appkey") or "").strip()
    if not appkey or appkey.startswith("${"):
        raise ValueError(
            "voice_streaming.asr.appkey 未在 config/config.yaml 中正确配置（或仍为 ${...} 占位符）"
        )

    static_token = asr_config.get("token")
    if isinstance(static_token, str):
        static_token = static_token.strip()
        if static_token.startswith("${"):
            static_token = None
    else:
        static_token = None

    return AliyunASRClient(
        appkey=appkey,
        token=static_token,
        config=asr_client_config,
        token_manager=token_manager,
    )


def load_chunk_ms() -> int:
    _load_dotenv_test_only()
    return max(50, int(os.getenv("ALIYUN_CHUNK_MS", "200")))


def wav_to_pcm_16k_mono(audio_path: Path) -> bytes:
    with wave.open(str(audio_path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth != 2:
        raise ValueError(f"仅支持 16bit PCM WAV，当前 sampwidth={sampwidth}")
    if channels not in (1, 2):
        raise ValueError(f"仅支持单声道/双声道 WAV，当前 channels={channels}")

    pcm = np.frombuffer(frames, dtype=np.int16)
    if channels == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)

    if framerate != 16000:
        src_len = len(pcm)
        dst_len = int(src_len * 16000 / framerate)
        if dst_len <= 0:
            raise ValueError("音频长度异常，无法重采样")
        src_x = np.linspace(0, src_len - 1, num=src_len, dtype=np.float64)
        dst_x = np.linspace(0, src_len - 1, num=dst_len, dtype=np.float64)
        pcm = np.interp(dst_x, src_x, pcm.astype(np.float64))
        pcm = np.clip(pcm, -32768, 32767).astype(np.int16)

    return pcm.tobytes()


def _append_parsed(
    events: list[dict[str, Any]], r: dict[str, Any] | None
) -> None:
    if not r:
        return
    events.append(r)
    t = r.get("type")
    if t == "intermediate_result":
        print(f"[MID] {r.get('text', '')}")
    elif t == "final_result":
        print(f"[FIN] {r.get('text', '')}")
    elif t == "error":
        print(f"[ERR] {r.get('error_code')} {r.get('message')}")


async def _drain_last_results(
    client: AliyunASRClient, session_id: str, *, rounds: int = 50, interval: float = 0.1
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _ in range(rounds):
        info = client.active_sessions.get(session_id)
        if not info:
            break
        lr = info.get("last_result")
        if lr:
            info.pop("last_result", None)
            out.append(lr)
        else:
            await asyncio.sleep(interval)
    return out


async def run_realtime_asr(audio_path: Path) -> dict[str, Any]:
    client = build_asr_client_like_websocket()
    endpoint = client.config.get(
        "endpoint", "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"
    )
    pcm = wav_to_pcm_16k_mono(audio_path)

    chunk_ms = load_chunk_ms()
    bytes_per_ms = 16000 * 2 // 1000
    chunk_bytes = max(640, chunk_ms * bytes_per_ms)
    sleep_s = chunk_ms / 1000.0

    session_id = client.create_session()
    connected = await client.connect_session(session_id, timeout=20.0)
    if not connected:
        await client.close_session(session_id)
        raise RuntimeError(
            "连接阿里云 ASR 失败。凭证与线上一致，请核对 config/config.yaml 中 "
            "access_key_secret 是否与 RAM 控制台一致，或环境变量 ALIYUN_ACCESS_KEY_* 是否被错误覆盖。"
        )

    events: list[dict[str, Any]] = []
    n = len(pcm)
    pos = 0
    t0 = time.time()

    try:
        while pos < n:
            take = min(chunk_bytes, n - pos)
            chunk = pcm[pos : pos + take]
            pos += take
            r = await client.send_audio_data(session_id, chunk)
            _append_parsed(events, r)
            for extra in await _drain_last_results(
                client, session_id, rounds=5, interval=0.05
            ):
                _append_parsed(events, extra)
            await asyncio.sleep(sleep_s)

        for extra in await _drain_last_results(client, session_id, rounds=60):
            _append_parsed(events, extra)

        await asyncio.sleep(0.5)
        for extra in await _drain_last_results(client, session_id, rounds=20):
            _append_parsed(events, extra)
    finally:
        await client.close_session(session_id)

    elapsed = time.time() - t0
    finals = [e.get("text", "") for e in events if e.get("type") == "final_result"]
    last_mid = ""
    for e in events:
        if e.get("type") == "intermediate_result":
            last_mid = e.get("text") or last_mid

    return {
        "audio_path": str(audio_path),
        "endpoint": endpoint,
        "elapsed_seconds": round(elapsed, 3),
        "final_sentences": finals,
        "final_text": "".join(finals),
        "last_intermediate": last_mid,
        "events": events,
    }


def main() -> None:
    _load_dotenv_test_only()
    if len(sys.argv) > 2:
        raise SystemExit(f"用法: python3 {Path(sys.argv[0]).name} [录音文件路径]")

    audio = (
        Path(sys.argv[1])
        if len(sys.argv) == 2
        else Path(os.getenv("ALIYUN_TEST_AUDIO", str(DEFAULT_AUDIO_PATH)))
    )
    if not audio.exists():
        raise FileNotFoundError(audio)

    result = asyncio.run(run_realtime_asr(audio))
    out = PROJECT_DIR / "asr_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完成。")
    print(f"整句拼接: {result.get('final_text', '')}")
    if result.get("last_intermediate"):
        print(f"末次中间结果: {result['last_intermediate']}")
    print(f"耗时: {result['elapsed_seconds']}s")
    print(f"结果: {out}")


if __name__ == "__main__":
    main()
