#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 语音交互服务重启脚本

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "VoiceBridge 服务重启"
echo "=========================================="

# 停止服务
bash "$SCRIPT_DIR/stop_service.sh"

# 等待2秒
sleep 2

# 启动服务
bash "$SCRIPT_DIR/start_service.sh"
