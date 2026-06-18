#!/usr/bin/env python3
"""
同一段录音依次跑四个厂商的实时 ASR 测试脚本，汇总转写结果。

各厂商仍使用各自目录下的 .env 与实现（百度云 / 科大讯飞 / 豆包 / 阿里云）。

用法:
  python3 ASR_test/run_four_asr.py /path/to/audio.wav
  python3 ASR_test/run_four_asr.py /path/to/audio.wav -o /path/to/summary.json
  python3 ASR_test/run_four_asr.py /path/to/audio.wav --only baidu,aliyun
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

PROVIDERS: dict[str, dict[str, Any]] = {
    "baidu": {
        "label": "百度云",
        "script": "ASR_test/百度云ASR/realtime_asr_test.py",
        "env_key": "BAIDU_TEST_AUDIO",
        "result_json": "ASR_test/百度云ASR/asr_result.json",
    },
    "ifly": {
        "label": "科大讯飞",
        "script": "ASR_test/科大讯飞ASR/realtime_asr_test.py",
        "env_key": "IFLY_TEST_AUDIO",
        "result_json": "ASR_test/科大讯飞ASR/asr_result.json",
    },
    "doubao": {
        "label": "豆包(火山)",
        "script": "ASR_test/豆包ASR/realtime_asr_test.py",
        "env_key": "DOUBAO_TEST_AUDIO",
        "result_json": "ASR_test/豆包ASR/asr_result.json",
    },
    "aliyun": {
        "label": "阿里云",
        "script": "ASR_test/阿里云ASR/realtime_asr_test.py",
        "env_key": "ALIYUN_TEST_AUDIO",
        "result_json": "ASR_test/阿里云ASR/asr_result.json",
    },
}

DEFAULT_ORDER = ("baidu", "ifly", "doubao", "aliyun")


def _extract_transcript(provider_id: str, data: dict[str, Any]) -> str:
    if provider_id == "doubao":
        return (data.get("last_text") or "").strip()
    return (data.get("final_text") or "").strip()


def _run_subprocess(
    audio: Path,
    provider_id: str,
    meta: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    script_path = REPO_ROOT / meta["script"]
    result_path = REPO_ROOT / meta["result_json"]
    env = os.environ.copy()
    env[meta["env_key"]] = str(audio.resolve())

    started = time.time()
    try:
        cp = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": f"超时（>{timeout}s）",
            "stdout_tail": (e.stdout or "")[-4000:] if e.stdout else "",
            "stderr_tail": (e.stderr or "")[-4000:] if e.stderr else "",
            "elapsed_seconds": round(time.time() - started, 3),
        }

    elapsed = round(time.time() - started, 3)
    out: dict[str, Any] = {
        "ok": cp.returncode == 0,
        "returncode": cp.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": (cp.stdout or "")[-4000:],
        "stderr_tail": (cp.stderr or "")[-4000:],
    }

    if cp.returncode != 0:
        out["error"] = "子进程非零退出"
        return out

    if not result_path.is_file():
        out["ok"] = False
        out["error"] = f"未生成结果文件: {result_path}"
        return out

    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out["ok"] = False
        out["error"] = f"读取结果 JSON 失败: {e}"
        return out

    out["transcript"] = _extract_transcript(provider_id, raw)
    out["result_json_path"] = str(result_path)
    out["vendor_result"] = {
        "audio_path": raw.get("audio_path"),
        "elapsed_seconds": raw.get("elapsed_seconds"),
        "final_text": raw.get("final_text"),
        "last_text": raw.get("last_text"),
        "final_sentences": raw.get("final_sentences"),
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="同一段录音跑四个 ASR 并汇总转写")
    parser.add_argument(
        "audio",
        type=Path,
        help="录音文件路径（wav，各脚本内部会转为 16k 单声道等）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="汇总 JSON 输出路径（默认: 与录音同目录，文件名 <录音名>_four_asr.json）",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="只跑指定厂商，逗号分隔: baidu,ifly,doubao,aliyun",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="单厂商子进程超时秒数（默认 900）",
    )
    args = parser.parse_args()

    audio = args.audio.expanduser().resolve()
    if not audio.is_file():
        print(f"错误: 文件不存在: {audio}", file=sys.stderr)
        sys.exit(1)

    if args.only.strip():
        order = tuple(p.strip() for p in args.only.split(",") if p.strip())
        bad = [p for p in order if p not in PROVIDERS]
        if bad:
            print(f"错误: 未知厂商 {bad}，可选: {list(PROVIDERS.keys())}", file=sys.stderr)
            sys.exit(1)
    else:
        order = DEFAULT_ORDER

    out_path = args.output
    if out_path is None:
        out_path = audio.parent / f"{audio.stem}_four_asr.json"

    summary: dict[str, Any] = {
        "audio_path": str(audio),
        "providers_order": list(order),
        "results": {},
    }

    print(f"录音: {audio}")
    print(f"将依次运行: {', '.join(PROVIDERS[p]['label'] for p in order)}\n")

    for pid in order:
        meta = PROVIDERS[pid]
        label = meta["label"]
        print(f"—— {label} ({pid}) ——")
        block = _run_subprocess(audio, pid, meta, args.timeout)
        summary["results"][pid] = {"label": label, **block}
        if block.get("ok"):
            text = block.get("transcript", "")
            preview = text[:200] + ("…" if len(text) > 200 else "")
            print(f"  成功 耗时 {block.get('elapsed_seconds')}s")
            print(f"  转写预览: {preview!r}\n")
        else:
            print(f"  失败: {block.get('error', 'unknown')}")
            if block.get("stderr_tail"):
                print(f"  stderr 尾部:\n{block['stderr_tail'][:1200]}\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总已写入: {out_path.resolve()}")

    print("\n========== 四路转写全文 ==========")
    for pid in order:
        r = summary["results"][pid]
        label = r["label"]
        if r.get("ok"):
            print(f"\n【{label}】\n{r.get('transcript', '')}")
        else:
            print(f"\n【{label}】\n<失败> {r.get('error', '')}")


if __name__ == "__main__":
    main()
