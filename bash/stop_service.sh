#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 语音交互服务停止脚本

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PID_FILE="$PROJECT_DIR/voicebridge.pid"

echo "=========================================="
echo "VoiceBridge 服务停止"
echo "=========================================="

if [ ! -f "$PID_FILE" ]; then
    echo "✗ PID文件不存在: $PID_FILE"
    echo "服务可能未运行或已被手动停止"
    
    # 尝试查找并停止进程
    echo "尝试查找运行中的服务进程..."
    PIDS=$(ps aux | grep "[p]ython.*services/run.py" | awk '{print $2}')
    
    if [ -z "$PIDS" ]; then
        echo "✓ 未找到运行中的服务进程"
        exit 0
    else
        echo "找到以下进程:"
        ps aux | grep "[p]ython.*services/run.py"
        echo ""
        # 非交互或由 stop_all 调用时自动确认
        if [ "${VOICEBRIDGE_STOP_YES:-}" = "1" ] || [ ! -t 0 ]; then
            REPLY=y
        else
            read -p "是否停止这些进程? (y/n) " -n 1 -r
            echo
        fi
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            for PID in $PIDS; do
                echo "停止进程: $PID"
                kill $PID
            done
            sleep 2
            echo "✓ 进程已停止"
        fi
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")
echo "PID: $PID"

if ps -p $PID > /dev/null 2>&1; then
    echo "正在停止服务..."
    kill $PID
    
    # 等待进程结束
    for i in {1..10}; do
        if ! ps -p $PID > /dev/null 2>&1; then
            echo "✓ 服务已停止"
            rm -f "$PID_FILE"
            echo "=========================================="
            exit 0
        fi
        sleep 1
    done
    
    # 如果进程仍在运行，强制停止
    if ps -p $PID > /dev/null 2>&1; then
        echo "进程未响应，强制停止..."
        kill -9 $PID
        sleep 1
        if ! ps -p $PID > /dev/null 2>&1; then
            echo "✓ 服务已强制停止"
            rm -f "$PID_FILE"
        else
            echo "✗ 无法停止服务"
            exit 1
        fi
    fi
else
    echo "✗ 进程不存在 (PID: $PID)"
    echo "清理PID文件..."
    rm -f "$PID_FILE"
fi

echo "=========================================="
