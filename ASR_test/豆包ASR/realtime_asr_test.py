#!/usr/bin/env python3
"""
豆包语音 / 火山引擎「大模型流式语音识别」WebSocket 测试（双向流式 bigmodel）。
文档：https://www.volcengine.com/docs/6561/1354869?lang=zh
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import ssl
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import numpy as np
import websockets
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIO_PATH = PROJECT_DIR.parent / "sample.wav"
DEFAULT_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

# 协议常量（见文档「WebSocket 二进制协议」）
PROTOCOL_VERSION = 0b0001
HEADER_SIZE_UNIT = 0b0001  # header 实际长度 = 1 * 4 字节
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY_REQUEST = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_ERROR_RESPONSE = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POSITIVE_SEQ = 0b0001
FLAG_LAST_AUDIO_NO_SEQ = 0b0010
FLAG_NEGATIVE_SEQ = 0b0011

SER_JSON = 0b0001
SER_RAW = 0b0000
COMP_NONE = 0b0000
COMP_GZIP = 0b0001


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    load_dotenv(PROJECT_DIR / ".env")
    resource_id = os.getenv("DOUBAO_RESOURCE_ID", "volc.bigasr.sauc.duration").strip()
    api_key = os.getenv("DOUBAO_API_KEY", "").strip()
    if api_key:
        return {"auth": "api_key", "api_key": api_key, "resource_id": resource_id}
    app_key = os.getenv("DOUBAO_APP_ID", "").strip()
    access_key = os.getenv("DOUBAO_ACCESS_TOKEN", "").strip()
    if not app_key or not access_key:
        raise ValueError(
            "请在 .env 配置 DOUBAO_API_KEY（新版），或同时配置 DOUBAO_APP_ID + DOUBAO_ACCESS_TOKEN（旧版）"
        )
    return {
        "auth": "legacy",
        "app_key": app_key,
        "access_key": access_key,
        "resource_id": resource_id,
    }


def build_ws_headers(cfg: dict, request_id: str, connect_id: str) -> dict[str, str]:
    resource_id = cfg["resource_id"]
    extra = _env_truthy("DOUBAO_WS_EXTRA_HEADERS", "0")
    instance_id = os.getenv("DOUBAO_INSTANCE_ID", "").strip()
    send_instance = _env_truthy("DOUBAO_SEND_INSTANCE_HEADER", "0")
    dual = _env_truthy("DOUBAO_DUAL_AUTH", "0")

    def _maybe_extra(h: dict[str, str]) -> None:
        if extra:
            h["X-Api-Request-Id"] = request_id
            h["X-Api-Sequence"] = "-1"
        if send_instance and instance_id:
            h["X-Api-Instance-Id"] = instance_id

    if cfg["auth"] == "api_key":
        headers: dict[str, str] = {
            "X-Api-Key": cfg["api_key"],
            "X-Api-Resource-Id": resource_id,
            "X-Api-Connect-Id": connect_id,
        }
        _maybe_extra(headers)
        if dual:
            app_key = os.getenv("DOUBAO_APP_ID", "").strip()
            access_key = os.getenv("DOUBAO_ACCESS_TOKEN", "").strip()
            if app_key:
                headers["X-Api-App-Key"] = app_key
            if access_key:
                headers["X-Api-Access-Key"] = access_key
        return headers

    headers = {
        "X-Api-App-Key": cfg["app_key"],
        "X-Api-Access-Key": cfg["access_key"],
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": connect_id,
    }
    _maybe_extra(headers)
    return headers


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


def _header(
    message_type: int,
    message_type_flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    b0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE_UNIT
    b1 = (message_type << 4) | (message_type_flags & 0x0F)
    b2 = (serialization << 4) | (compression & 0x0F)
    b3 = 0x00
    return bytes([b0, b1, b2, b3])


def build_full_client_request(payload_dict: dict) -> bytes:
    raw = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    payload = gzip.compress(raw)
    hdr = _header(MSG_FULL_CLIENT_REQUEST, FLAG_NO_SEQ, SER_JSON, COMP_GZIP)
    return hdr + struct.pack(">I", len(payload)) + payload


def build_audio_frame(
    pcm_chunk: bytes,
    *,
    sequence: int | None,
    is_last: bool,
) -> bytes:
    if is_last:
        hdr = _header(MSG_AUDIO_ONLY_REQUEST, FLAG_LAST_AUDIO_NO_SEQ, SER_RAW, COMP_GZIP)
        payload = gzip.compress(pcm_chunk) if pcm_chunk else gzip.compress(b"")
        return hdr + struct.pack(">I", len(payload)) + payload

    if sequence is None:
        raise ValueError("非最后一包音频必须带 sequence")
    hdr = _header(MSG_AUDIO_ONLY_REQUEST, FLAG_POSITIVE_SEQ, SER_RAW, COMP_GZIP)
    payload = gzip.compress(pcm_chunk)
    return hdr + struct.pack(">I", sequence) + struct.pack(">I", len(payload)) + payload


def parse_server_frame(data: bytes) -> tuple[int, dict[str, Any]]:
    """
    解析服务端二进制帧。返回 (message_type_nibble, payload_dict 或 raw/error 信息)。
    """
    if len(data) < 4:
        return MSG_ERROR_RESPONSE, {"error": "frame_too_short", "len": len(data)}

    b0, b1, b2, _ = data[0], data[1], data[2], data[3]
    msg_type = (b1 >> 4) & 0x0F
    flags = b1 & 0x0F
    serialization = (b2 >> 4) & 0x0F
    compression = b2 & 0x0F
    offset = 4

    if msg_type == MSG_ERROR_RESPONSE:
        if len(data) < offset + 8:
            return msg_type, {"error": "error_frame_truncated"}
        code = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        msg_len = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        msg = data[offset : offset + msg_len].decode("utf-8", errors="replace")
        return msg_type, {"code": code, "message": msg}

    seq: int | None = None
    if flags in (FLAG_POSITIVE_SEQ, FLAG_NEGATIVE_SEQ):
        if len(data) < offset + 4:
            return msg_type, {"error": "missing_sequence", "flags": flags}
        if flags == FLAG_NEGATIVE_SEQ:
            seq = struct.unpack(">i", data[offset : offset + 4])[0]
        else:
            seq = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

    if len(data) < offset + 4:
        return msg_type, {"error": "missing_payload_size", "flags": flags, "seq": seq}
    payload_len = struct.unpack(">I", data[offset : offset + 4])[0]
    offset += 4
    if len(data) < offset + payload_len:
        return msg_type, {
            "error": "payload_truncated",
            "expect": payload_len,
            "have": len(data) - offset,
        }
    payload = data[offset : offset + payload_len]

    if compression == COMP_GZIP:
        try:
            payload = gzip.decompress(payload)
        except OSError as e:
            return msg_type, {"error": "gzip_failed", "detail": str(e)}

    if msg_type == MSG_FULL_SERVER_RESPONSE and serialization == SER_JSON:
        try:
            obj = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as e:
            return msg_type, {"error": "json_parse", "detail": str(e)}
        if seq is not None:
            obj["_seq"] = seq
        obj["_flags"] = flags
        return msg_type, obj

    return msg_type, {
        "raw_len": len(payload),
        "serialization": serialization,
        "compression": compression,
        "seq": seq,
    }


def _default_client_json(uid: str) -> dict:
    return {
        "user": {"uid": uid},
        "audio": {
            "format": "pcm",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
            "codec": "raw",
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
        },
    }


async def run_realtime_asr(audio_path: Path) -> dict:
    cfg = load_config()
    ws_url = os.getenv("DOUBAO_WS_URL", DEFAULT_WS_URL).strip() or DEFAULT_WS_URL
    pcm = wav_to_pcm_16k_mono(audio_path)
    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    uid = os.getenv("DOUBAO_UID", "voicebridge-test").strip()

    extra_headers = build_ws_headers(cfg, request_id, connect_id)

    chunk_ms = int(os.getenv("DOUBAO_CHUNK_MS", "200"))
    bytes_per_ms = 16000 * 2 // 1000
    chunk_bytes = max(640, chunk_ms * bytes_per_ms)
    sleep_s = chunk_ms / 1000.0

    responses: list[dict[str, Any]] = []
    last_text = ""
    started = time.time()

    ssl_context = ssl.create_default_context()
    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_context,
            additional_headers=extra_headers,
            ping_interval=None,
            max_size=None,
        ) as ws:
            first = build_full_client_request(_default_client_json(uid))
            await ws.send(first)

            seq = 2
            n = len(pcm)
            pos = 0
            while pos < n:
                take = min(chunk_bytes, n - pos)
                chunk = pcm[pos : pos + take]
                pos += take
                is_last = pos >= n
                frame = build_audio_frame(
                    chunk, sequence=None if is_last else seq, is_last=is_last
                )
                if not is_last:
                    seq += 1
                await ws.send(frame)
                await asyncio.sleep(sleep_s)

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    responses.append({"error": "recv_timeout"})
                    break
                except websockets.exceptions.ConnectionClosed:
                    break

                if isinstance(raw, str):
                    responses.append({"error": "unexpected_text_frame", "data": raw[:500]})
                    break

                mtype, body = parse_server_frame(raw)
                if mtype == MSG_ERROR_RESPONSE:
                    responses.append({"msg_kind": "error", **body})
                    print(f"[ERR] {body}")
                    break
                if mtype == MSG_FULL_SERVER_RESPONSE and isinstance(body, dict) and "result" in body:
                    responses.append(body)
                    res = body.get("result") or {}
                    text = res.get("text") or ""
                    if text and text != last_text:
                        print(f"[ASR] {text}")
                        last_text = text
                elif isinstance(body, dict) and body.get("error"):
                    responses.append(body)
                    print(f"[PARSE] {body}")
    except websockets.exceptions.InvalidStatus as e:
        resp = e.response
        body = bytes(getattr(resp, "body", b"") or b"")
        hint = (
            "非 200：403 常见为资源未开通或 ResourceId 与套餐不一致；"
            "400 常见为 ResourceId 与当前接口/模型不匹配。"
            "请在控制台确认已开通「大模型流式语音识别」并选择正确的 duration/concurrent 与 1.0/2.0 资源 ID。"
        )
        raise RuntimeError(
            f"{hint}\nHTTP {resp.status_code}\n响应体: {body[:800]!r}\n"
            f"当前 X-Api-Resource-Id: {extra_headers.get('X-Api-Resource-Id')}\n"
            f"请求头键: {list(extra_headers.keys())}"
        ) from e

    elapsed = time.time() - started
    return {
        "audio_path": str(audio_path),
        "ws_url": ws_url,
        "request_id": request_id,
        "connect_id": connect_id,
        "elapsed_seconds": round(elapsed, 3),
        "last_text": last_text,
        "responses": responses,
    }


def main():
    audio = Path(os.getenv("DOUBAO_TEST_AUDIO", str(DEFAULT_AUDIO_PATH)))
    if not audio.exists():
        raise FileNotFoundError(audio)

    result = asyncio.run(run_realtime_asr(audio))
    out = PROJECT_DIR / "asr_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完成。")
    print(f"最后整段文本: {result.get('last_text', '')}")
    print(f"耗时: {result['elapsed_seconds']}s")
    print(f"结果: {out}")


if __name__ == "__main__":
    main()
