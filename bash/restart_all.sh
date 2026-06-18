#!/bin/bash
# -*- coding: utf-8 -*-
# 一键重启：先停止语音服务 + 内网穿透，再执行一键启动（改完代码后使用）
# 若服务器 IP 变更（如 localhost -> localhost），务必执行本脚本以重新建立 natapp 隧道，否则外网访问可能 404
# 用法: bash /opt/voicebridge/bash/restart_all.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================="
echo "VoiceBridge 一键重启（语音服务 + 内网穿透）"
echo "=========================================="
echo ""

# 1. 停止语音服务
echo ">>> 停止语音服务..."
bash "$SCRIPT_DIR/stop_service.sh"
echo ""

# 2. 停止内网穿透
echo ">>> 停止内网穿透 (NATAPP)..."
bash "$PROJECT_DIR/tools/natapp/stop.sh"
echo ""

echo "等待 2 秒后重新启动..."
sleep 2
echo ""

# 3. 一键启动
bash "$SCRIPT_DIR/start_all.sh"
