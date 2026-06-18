#!/usr/bin/env python3
import asyncio
import json
import os
import ssl
import time
import uuid
import wave
from pathlib import Path

import numpy as np
import websockets
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIO_PATH = PROJECT_DIR.parent / "sample.wav"


def load_config() -> dict:
    load_dotenv(PROJECT_DIR / ".env")
    appid = os.getenv("BAIDU_APP_ID", "").strip()
    appkey = os.getenv("BAIDU_API_KEY", "").strip()
    dev_pid = int(os.getenv("BAIDU_DEV_PID", "15372"))
    cuid = os.getenv("BAIDU_CUID", "voicebridge-local-test").strip()
    sn = os.getenv("BAIDU_SN", str(uuid.uuid4())).strip()

    if not appid or not appkey:
        raise ValueError("缺少 BAIDU_APP_ID 或 BAIDU_API_KEY，请检查 .env")

    return {
        "appid": int(appid),
        "appkey": appkey,
        "dev_pid": dev_pid,
        "cuid": cuid,
        "sn": sn,
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


async def run_realtime_asr(audio_path: Path) -> dict:
    cfg = load_config()
    pcm_data = wav_to_pcm_16k_mono(audio_path)
    chunk_bytes = 5120  # 160ms @ 16kHz, 16bit, mono
    ws_url = f"wss://vop.baidu.com/realtime_asr?sn={cfg['sn']}"

    start_frame = {
        "type": "START",
        "data": {
            "appid": cfg["appid"],
            "appkey": cfg["appkey"],
            "dev_pid": cfg["dev_pid"],
            "cuid": cfg["cuid"],
            "format": "pcm",
            "sample": 16000,
        },
    }

    responses = []
    final_texts = []
    started_at = time.time()
    stop_sending = asyncio.Event()

    ssl_context = ssl.create_default_context()
    async with websockets.connect(ws_url, ssl=ssl_context, ping_interval=None) as ws:
        await ws.send(json.dumps(start_frame, ensure_ascii=False))

        async def sender():
            for i in range(0, len(pcm_data), chunk_bytes):
                if stop_sending.is_set():
                    break
                chunk = pcm_data[i : i + chunk_bytes]
                try:
                    await ws.send(chunk)
                except websockets.exceptions.ConnectionClosed:
                    break
                await asyncio.sleep(0.16)
            try:
                await ws.send(json.dumps({"type": "FINISH"}))
            except websockets.exceptions.ConnectionClosed:
                pass

        async def receiver():
            while True:
                try:
                    msg = await ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    stop_sending.set()
                    break

                if isinstance(msg, bytes):
                    continue

                data = json.loads(msg)
                responses.append(data)
                typ = data.get("type")
                err_no = data.get("err_no", 0)
                if typ == "MID_TEXT":
                    print(f"[MID] {data.get('result', '')}")
                elif typ == "FIN_TEXT":
                    result = data.get("result", "")
                    if err_no == 0 and result:
                        final_texts.append(result)
                        print(f"[FIN] {result}")
                    else:
                        print(f"[FIN-ERR] err_no={err_no}, err_msg={data.get('err_msg')}")
                        stop_sending.set()
                elif typ == "HEARTBEAT":
                    pass

        await asyncio.gather(sender(), receiver())

    elapsed = time.time() - started_at
    return {
        "audio_path": str(audio_path),
        "elapsed_seconds": round(elapsed, 3),
        "final_text": "".join(final_texts),
        "final_sentences": final_texts,
        "raw_responses": responses,
    }


def main():
    audio_file = Path(os.getenv("BAIDU_TEST_AUDIO", str(DEFAULT_AUDIO_PATH)))
    if not audio_file.exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_file}")

    result = asyncio.run(run_realtime_asr(audio_file))

    out_path = PROJECT_DIR / "asr_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n识别完成。")
    print(f"最终文本: {result['final_text']}")
    print(f"耗时: {result['elapsed_seconds']}s")
    print(f"结果文件: {out_path}")


if __name__ == "__main__":
    main()
