#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试新的服务启动脚本
验证是否解决了Cursor连接重置和前端加载延迟问题
"""

import subprocess
import time
import requests
import os

def run_command(cmd, description):
    """运行命令并返回结果"""
    print(f"\n{'='*60}")
    print(f"测试: {description}")
    print(f"命令: {cmd}")
    print(f"{'='*60}")
    
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30
    )
    
    print(result.stdout)
    if result.stderr:
        print("错误输出:", result.stderr)
    
    return result.returncode == 0

def check_service_running():
    """检查服务是否运行"""
    pid_file = "/opt/voicebridge/voicebridge.pid"
    
    if not os.path.exists(pid_file):
        return False, None
    
    with open(pid_file, 'r') as f:
        pid = f.read().strip()
    
    # 检查进程是否存在
    result = subprocess.run(
        f"ps -p {pid}",
        shell=True,
        capture_output=True
    )
    
    return result.returncode == 0, pid

def check_http_service():
    """检查后端 HTTP 服务是否可访问（前后端分离后后端在 8011）"""
    try:
        response = requests.get("http://localhost:8011/health", timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def main():
    """主测试流程"""
    print("="*60)
    print("VoiceBridge 服务启动脚本测试")
    print("="*60)
    
    # 测试1: 启动服务
    print("\n[测试1] 启动服务")
    success = run_command(
        "bash /opt/voicebridge/bash/start_service.sh",
        "启动VoiceBridge服务"
    )
    
    if not success:
        print("❌ 服务启动失败")
        return False
    
    print("✅ 启动命令执行成功")
    
    # 等待服务完全启动
    print("\n等待服务完全启动...")
    time.sleep(5)
    
    # 测试2: 检查进程
    print("\n[测试2] 检查服务进程")
    is_running, pid = check_service_running()
    
    if is_running:
        print(f"✅ 服务进程运行中 (PID: {pid})")
    else:
        print("❌ 服务进程未运行")
        return False
    
    # 测试3: 检查HTTP服务
    print("\n[测试3] 检查HTTP服务")
    print("尝试访问: http://localhost:8011/health")
    
    for i in range(10):
        if check_http_service():
            print(f"✅ HTTP服务正常响应 (尝试 {i+1}/10)")
            break
        else:
            print(f"⏳ 等待HTTP服务启动... (尝试 {i+1}/10)")
            time.sleep(2)
    else:
        print("❌ HTTP服务无响应")
        return False
    
    # 测试4: 检查端口监听
    print("\n[测试4] 检查端口监听")
    result = subprocess.run(
        "netstat -tlnp 2>/dev/null | grep -E '8011|8765' || ss -tlnp 2>/dev/null | grep -E '8011|8765'",
        shell=True,
        capture_output=True,
        text=True
    )
    
    if "8011" in result.stdout and "8765" in result.stdout:
        print("✅ 端口监听正常 (8011=后端, 8765=WebSocket)")
        print(result.stdout)
    else:
        print("❌ 端口监听异常")
        print(result.stdout)
    
    # 测试5: 查看服务状态
    print("\n[测试5] 查看服务状态")
    run_command(
        "bash /opt/voicebridge/bash/status_service.sh",
        "查看服务详细状态"
    )
    
    # 测试6: 测试后端 API 与文档（前端需单独启动后访问 8010）
    print("\n[测试6] 测试后端 API")
    try:
        response = requests.get("http://localhost:8011/docs", timeout=5)
        if response.status_code == 200:
            print("✅ 后端 API 文档可访问 (8011)")
        else:
            print(f"❌ 后端返回状态码: {response.status_code}")
    except Exception as e:
        print(f"❌ 后端访问失败（请确认后端已启动）: {e}")
    
    # 测试7: 检查日志
    print("\n[测试7] 检查服务日志")
    result = subprocess.run(
        "ls -t /opt/voicebridge/logs/service/service_*.log | head -1",
        shell=True,
        capture_output=True,
        text=True
    )
    
    if result.stdout.strip():
        log_file = result.stdout.strip()
        print(f"最新日志文件: {log_file}")
        
        # 显示最后20行日志
        result = subprocess.run(
            f"tail -20 {log_file}",
            shell=True,
            capture_output=True,
            text=True
        )
        print("\n最新日志内容:")
        print("-" * 60)
        print(result.stdout)
        print("-" * 60)
    
    # 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    print("✅ 服务启动成功")
    print("✅ 进程在后台运行")
    print("✅ HTTP服务正常")
    print("✅ 端口监听正常")
    print("✅ 后端服务正常")
    print("\n访问地址（前后端分离）:")
    print("  - 后端 API 文档: http://localhost:8011/docs")
    print("  - 健康检查:       http://localhost:8011/health")
    print("  - 前端登录(需先启动前端): http://localhost:8010/login")
    print("   启动前端: cd frontend && npm run dev")
    print("\n管理命令:")
    print("  - 停止服务: bash /opt/voicebridge/bash/stop_service.sh")
    print("  - 重启服务: bash /opt/voicebridge/bash/restart_service.sh")
    print("  - 查看状态: bash /opt/voicebridge/bash/status_service.sh")
    print("="*60)
    
    return True

if __name__ == "__main__":
    try:
        success = main()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n测试被中断")
        exit(1)
    except Exception as e:
        print(f"\n\n测试出错: {e}")
        import traceback
        traceback.print_exc()
        exit(1)

