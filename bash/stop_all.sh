#!/bin/bash
# -*- coding: utf-8 -*-
# 一键关闭：后端 + 前端 + 内网穿透（NATAPP）

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================="
echo "VoiceBridge 一键关闭（后端 + 前端 + 内网穿透）"
echo "=========================================="
echo "项目目录: $PROJECT_DIR"
echo ""

# 1. 停止后端
echo ">>> 1/3 停止后端服务..."
export VOICEBRIDGE_STOP_YES=1
bash "$SCRIPT_DIR/stop_service.sh"
echo ""

# 2. 停止前端
echo ">>> 2/3 停止前端..."
FRONTEND_PID_FILE="$PROJECT_DIR/voicebridge_frontend.pid"
if [ -f "$FRONTEND_PID_FILE" ]; then
    PID=$(cat "$FRONTEND_PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        kill "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null
        echo "前端已停止 (PID: $PID)"
    fi
    rm -f "$FRONTEND_PID_FILE"
else
    echo "未找到前端 PID 文件，跳过"
fi
echo ""

# 3. 停止内网穿透（NATAPP）
echo ">>> 3/3 停止内网穿透 (NATAPP)..."
bash "$PROJECT_DIR/tools/natapp/stop.sh"
echo ""

echo "=========================================="
echo "全部已关闭。"
echo "重新启动: bash $SCRIPT_DIR/start_all.sh"
echo "=========================================="
