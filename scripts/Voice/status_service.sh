#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 语音交互服务状态查看脚本

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="$PROJECT_DIR/voicebridge.pid"

echo "=========================================="
echo "VoiceBridge 服务状态"
echo "=========================================="

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "PID文件: $PID_FILE"
    echo "PID: $PID"
    echo ""
    
    if ps -p $PID > /dev/null 2>&1; then
        echo "状态: ✓ 运行中"
        echo ""
        echo "进程信息:"
        ps -f -p $PID
        echo ""
        echo "端口监听:"
        netstat -tlnp 2>/dev/null | grep $PID || ss -tlnp 2>/dev/null | grep $PID
        echo ""
        echo "最新日志 (最后20行):"
        LATEST_LOG=$(ls -t "$PROJECT_DIR/logs/service/service_"*.log 2>/dev/null | head -1)
        if [ -n "$LATEST_LOG" ]; then
            echo "日志文件: $LATEST_LOG"
            echo "---"
            tail -20 "$LATEST_LOG"
        fi
    else
        echo "状态: ✗ 未运行 (PID文件存在但进程不存在)"
        echo "建议: 运行 bash $SCRIPT_DIR/stop_service.sh 清理PID文件"
    fi
else
    echo "状态: ✗ 未运行 (PID文件不存在)"
    echo ""
    echo "查找可能的运行进程:"
    PIDS=$(ps aux | grep "[p]ython.*services/run.py" | awk '{print $2}')
    if [ -n "$PIDS" ]; then
        echo "找到以下进程 (可能是孤立进程):"
        ps aux | grep "[p]ython.*services/run.py"
    else
        echo "未找到运行中的服务进程"
    fi
fi

echo "=========================================="
echo ""
echo "服务管理命令:"
echo "  启动: bash $SCRIPT_DIR/start_service.sh"
echo "  停止: bash $SCRIPT_DIR/stop_service.sh"
echo "  重启: bash $SCRIPT_DIR/restart_service.sh"
echo "  状态: bash $SCRIPT_DIR/status_service.sh"
echo "=========================================="
