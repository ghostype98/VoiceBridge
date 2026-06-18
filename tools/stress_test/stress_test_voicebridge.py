#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceBridge 压力测试脚本（独立运行，不修改生产代码）
用于测试 8002 端口或外网地址（如 https://recruitment.natapp1.cc）的并发能力。
测试项：HTTP 健康检查、移动端页面、WebSocket /ws/asr 并发连接。
用法:
  # 本地 8002
  python stress_test_voicebridge.py --base http://localhost:8002 --users 5 --duration 10
  # 外网（natapp）
  python stress_test_voicebridge.py --base https://recruitment.natapp1.cc --users 5 --duration 10
  # 双人同时面试场景
  python stress_test_voicebridge.py --base https://recruitment.natapp1.cc --users 2 --duration 30
"""
import argparse
import asyncio
import ssl
import time
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None
try:
    import websockets
except ImportError:
    websockets = None


def parse_args():
    p = argparse.ArgumentParser(description="VoiceBridge 压力测试（不修改生产代码）")
    p.add_argument("--base", default="http://localhost:8002",
                   help="服务根地址，如 https://recruitment.natapp1.cc 或 http://localhost:8002")
    p.add_argument("--users", type=int, default=5, help="并发用户数")
    p.add_argument("--duration", type=int, default=15, help="持续秒数（仅对 HTTP 循环有效）")
    p.add_argument("--ws-only", action="store_true", help="仅测试 WebSocket 连接")
    p.add_argument("--http-only", action="store_true", help="仅测试 HTTP")
    p.add_argument("--no-verify-ssl", action="store_true", help="HTTPS 时跳过证书验证")
    return p.parse_args()


def make_ws_url(base: str) -> str:
    u = urlparse(base)
    scheme = "wss" if u.scheme == "https" else "ws"
    netloc = u.netloc or u.path
    path = u.path.rstrip("/") or ""
    return f"{scheme}://{netloc}{path}/ws/asr"


async def http_worker(base: str, duration: float, no_verify: bool, results: list):
    """持续请求 /health 和 /mobile-interview"""
    if not httpx:
        results.append(("http", 0, False, "httpx not installed"))
        return
    base = base.rstrip("/")
    timeout = httpx.Timeout(10.0)
    verify = not no_verify
    end = time.perf_counter() + duration
    ok, fail = 0, 0
    latencies = []
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        while time.perf_counter() < end:
            for path in ["/health", "/mobile-interview"]:
                t0 = time.perf_counter()
                try:
                    r = await client.get(f"{base}{path}")
                    lat = (time.perf_counter() - t0) * 1000
                    latencies.append(lat)
                    if r.status_code == 200:
                        ok += 1
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    latencies.append(-1)
            await asyncio.sleep(0.2)
    latencies_ok = [x for x in latencies if x >= 0]
    results.append(("http", ok, fail, latencies_ok))


async def ws_worker(ws_url: str, no_verify: bool, results: list, index: int):
    """建立 WebSocket 连接，收一条 connection_established 后关闭"""
    if not websockets:
        results.append(("ws", index, False, "websockets not installed"))
        return
    t0 = time.perf_counter()
    ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss") else None
    if no_verify and ssl_ctx:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            open_timeout=15,
            close_timeout=5,
        ) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            lat = (time.perf_counter() - t0) * 1000
            ok = "connection_established" in (msg if isinstance(msg, str) else msg.decode("utf-8", errors="ignore"))
            results.append(("ws", index, True, lat if ok else None))
    except Exception as e:
        results.append(("ws", index, False, str(e)))


async def run_ws_test(ws_url: str, users: int, no_verify: bool):
    results = []
    tasks = [ws_worker(ws_url, no_verify, results, i) for i in range(users)]
    await asyncio.gather(*tasks)
    return results


async def run_http_test(base: str, users: int, duration: int, no_verify: bool):
    results = []
    tasks = [http_worker(base, duration, no_verify, results) for _ in range(users)]
    await asyncio.gather(*tasks)
    return results


def main():
    args = parse_args()
    base = args.base.rstrip("/")
    ws_url = make_ws_url(base)

    print("=" * 60)
    print("VoiceBridge 压力测试（不修改生产代码）")
    print("=" * 60)
    print(f"  目标: {base}")
    print(f"  WebSocket: {ws_url}")
    print(f"  并发用户: {args.users}, 持续时间: {args.duration}s")
    print("=" * 60)

    all_http = []
    all_ws = []

    if not args.ws_only:
        if not httpx:
            print("跳过 HTTP 测试: 未安装 httpx")
        else:
            print("\n[1] HTTP 测试 (GET /health, GET /mobile-interview)...")
            all_http = asyncio.run(run_http_test(base, args.users, args.duration, args.no_verify_ssl))

    if not args.http_only:
        if not websockets:
            print("跳过 WebSocket 测试: 未安装 websockets")
        else:
            print("\n[2] WebSocket 测试 (并发连接 /ws/asr)...")
            all_ws = asyncio.run(run_ws_test(ws_url, args.users, args.no_verify_ssl))

    # 汇总
    print("\n" + "=" * 60)
    print("结果汇总")
    print("=" * 60)

    if all_http:
        total_ok = sum(r[1] for r in all_http)
        total_fail = sum(r[2] for r in all_http)
        all_lat = []
        for r in all_http:
            all_lat.extend(r[3] if isinstance(r[3], list) else [])
        all_lat = [x for x in all_lat if x >= 0]
        print(f"  HTTP: 成功 {total_ok}, 失败 {total_fail}")
        if all_lat:
            all_lat.sort()
            n = len(all_lat)
            print(f"  延迟(ms): min={min(all_lat):.0f}, max={max(all_lat):.0f}, "
                  f"p50={all_lat[n//2]:.0f}, p95={all_lat[int(n*0.95)] if n > 20 else all_lat[-1]:.0f}")

    if all_ws:
        ok = sum(1 for r in all_ws if r[2] is True)
        fail = len(all_ws) - ok
        print(f"  WebSocket: 成功 {ok}/{len(all_ws)}, 失败 {fail}")
        for r in all_ws:
            if r[2] is False:
                print(f"    连接 {r[1]}: {r[3]}")
        latencies = [r[3] for r in all_ws if r[2] is True and isinstance(r[3], (int, float))]
        if latencies:
            latencies.sort()
            n = len(latencies)
            print(f"  WebSocket 连接延迟(ms): min={min(latencies):.0f}, max={max(latencies):.0f}, p50={latencies[n//2]:.0f}")

    print("=" * 60)
    ws_ok = (not all_ws) or all(r[2] is True for r in all_ws)
    http_ok = (not all_http) or all(r[2] == 0 for r in all_http)  # r[2]=fail count
    return 0 if (ws_ok and http_ok) else 1


if __name__ == "__main__":
    exit(main())
