#!/usr/bin/env python3
"""
科大讯飞「实时语音转写大模型」WebSocket 测试。
文档：https://www.xfyun.cn/doc/spark/asr_llm/rtasr_llm.html
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import uuid
import wave
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
import websockets
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIO_PATH = PROJECT_DIR.parent / "sample.wav"
WS_HOST_PATH = "/ast/communicate/v1"


def load_config() -> dict:
    load_dotenv(PROJECT_DIR / ".env")
    app_id = os.getenv("IFLY_APP_ID", "").strip()
    api_key = os.getenv("IFLY_API_KEY", "").strip()
    api_secret = os.getenv("IFLY_API_SECRET", "").strip()
    lang = os.getenv("IFLY_LANG", "autodialect").strip()
    uuid_str = os.getenv("IFLY_UUID", "").strip() or uuid.uuid4().hex

    if not app_id or not api_key or not api_secret:
        raise ValueError("缺少 IFLY_APP_ID / IFLY_API_KEY / IFLY_API_SECRET，请检查 .env")

    if os.getenv("IFLY_API_SECRET_IS_B64", "").strip().lower() in ("1", "true", "yes"):
        try:
            api_secret = base64.b64decode(api_secret).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise ValueError("IFLY_API_SECRET_IS_B64=1 但 Base64 解码失败") from e

    return {
        "app_id": app_id,
        "access_key_id": api_key,
        "access_key_secret": api_secret,
        "lang": lang,
        "uuid": uuid_str,
    }


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


def _utc_plus0800() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S%z")


def _sign_base_string(params: dict[str, str]) -> str:
    """不含 signature；键名升序；键与值均 URL 编码后拼接。"""
    keys = sorted(params.keys())
    parts: list[str] = []
    for k in keys:
        ek = quote(k, safe="")
        ev = quote(str(params[k]), safe="")
        parts.append(f"{ek}={ev}")
    return "&".join(parts)


def _hmac_sha1_base64(secret: str, base_string: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_ws_url(cfg: dict) -> str:
    params: dict[str, str] = {
        "accessKeyId": cfg["access_key_id"],
        "appId": cfg["app_id"],
        "audio_encode": "pcm_s16le",
        "lang": cfg["lang"],
        "samplerate": "16000",
        "utc": _utc_plus0800(),
        "uuid": cfg["uuid"],
    }
    base_string = _sign_base_string(params)
    signature = _hmac_sha1_base64(cfg["access_key_secret"], base_string)
    params["signature"] = signature

    query_parts: list[str] = []
    for k in sorted(params.keys()):
        ek = quote(k, safe="")
        ev = quote(str(params[k]), safe="")
        query_parts.append(f"{ek}={ev}")
    query = "&".join(query_parts)
    return f"wss://office-api-ast-dx.iflyaisol.com{WS_HOST_PATH}?{query}"


def extract_text_from_data(data: dict) -> str:
    """从 data.cn.st.rt 结构中顺序拼接词 w。"""
    out: list[str] = []
    cn = data.get("cn") or {}
    st = cn.get("st") or {}
    for rt in st.get("rt") or []:
        for ws in rt.get("ws") or []:
            for cw in ws.get("cw") or []:
                w = cw.get("w")
                if isinstance(w, str) and w:
                    out.append(w)
    return "".join(out)


def _st_type(data: dict) -> str:
    st = (data.get("cn") or {}).get("st") or {}
    return str(st.get("type", ""))


def build_transcript_definite_only(messages: list[dict]) -> str:
    """
    仅合并「确定性」结果包（data.cn.st.type == 0）。
    流式过程中 type=1 的中间包往往是对同一句的加长快照（如「是通过」→「是通过计算机」），
    逐包拼接会产生大量重复；对外展示应以 type=0 为准。
    """
    parts: list[str] = []
    for m in messages:
        if m.get("msg_type") != "result" or m.get("res_type") != "asr":
            continue
        data = m.get("data") or {}
        if _st_type(data) != "0":
            continue
        t = extract_text_from_data(data).strip()
        if t:
            parts.append(t)
    return "".join(parts)


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


async def run_realtime_asr(audio_path: Path) -> dict:
    cfg = load_config()
    pcm_data = wav_to_pcm_16k_mono(audio_path)
    session_id = str(uuid.uuid4())
    chunk_bytes = 1280  # 文档建议约 40ms：16000 * 2 * 0.04
    chunk_sleep = 0.04

    ws_url = build_ws_url(cfg)
    messages: list[dict] = []
    lines: list[str] = []
    started_at = time.time()
    print_all = _env_truthy("IFLY_PRINT_ALL_PACKETS", "0")

    ssl_context = ssl.create_default_context()

    async with websockets.connect(ws_url, ssl=ssl_context, ping_interval=None) as ws:

        async def sender():
            for i in range(0, len(pcm_data), chunk_bytes):
                chunk = pcm_data[i : i + chunk_bytes]
                await ws.send(chunk)
                await asyncio.sleep(chunk_sleep)
            end_msg = json.dumps({"end": True, "sessionId": session_id}, ensure_ascii=False)
            await ws.send(end_msg)

        async def receiver():
            while True:
                try:
                    msg = await ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    break
                if isinstance(msg, bytes):
                    continue
                try:
                    obj = json.loads(msg)
                except json.JSONDecodeError:
                    messages.append({"raw": msg})
                    continue
                messages.append(obj)

                if obj.get("action") == "started":
                    continue

                msg_type = obj.get("msg_type")
                res_type = obj.get("res_type")
                if msg_type == "result" and res_type == "asr":
                    data = obj.get("data") or {}
                    text = extract_text_from_data(data)
                    seg = data.get("seg_id")
                    ls = data.get("ls")
                    typ = _st_type(data)
                    if text and (print_all or typ == "0"):
                        tag = f"seg={seg} type={typ} ls={ls}"
                        line = f"[ASR] {tag} {text}"
                        print(line)
                        lines.append(line)
                elif msg_type == "result" and res_type == "frc":
                    print(f"[ERR] {json.dumps(obj, ensure_ascii=False)}")
                elif obj.get("action") == "error" or res_type == "error":
                    print(f"[ERR] {json.dumps(obj, ensure_ascii=False)}")

        await asyncio.gather(sender(), receiver())

    elapsed = time.time() - started_at
    final_text = build_transcript_definite_only(messages)

    return {
        "audio_path": str(audio_path),
        "session_id": session_id,
        "elapsed_seconds": round(elapsed, 3),
        "final_text": final_text,
        "note": (
            "流式返回里多数包是当前片段的「整句快照」，type=1 为中间结果会反复变长；"
            "不要把每条 result 的文本直接首尾拼接。"
            "对外可读稿请用本字段 final_text（仅合并 type=0 确定性包）。"
            "调试全部包可设环境变量 IFLY_PRINT_ALL_PACKETS=1。"
        ),
        "log_lines": lines,
        "messages": messages,
    }


def main():
    audio_file = Path(os.getenv("IFLY_TEST_AUDIO", str(DEFAULT_AUDIO_PATH)))
    if not audio_file.exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_file}")

    result = asyncio.run(run_realtime_asr(audio_file))
    out_path = PROJECT_DIR / "asr_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完成。")
    print(f"最终文本（仅确定性 type=0）: {result['final_text']}")
    print(f"耗时: {result['elapsed_seconds']}s")
    print(f"结果文件: {out_path}")


if __name__ == "__main__":
    main()
