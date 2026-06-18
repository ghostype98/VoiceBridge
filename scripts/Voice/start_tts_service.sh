#!/bin/bash

# TTS服务启动脚本
# 在独立的conda环境中运行TTS服务

echo "正在启动TTS服务..."

# 检查是否在正确的conda环境中
if [[ "$CONDA_DEFAULT_ENV" != "tts" ]]; then
    echo "警告: 当前不在tts环境中，请先运行: conda activate tts"
    exit 1
fi

# 设置环境变量
export TTS_HOST=${TTS_HOST:-"0.0.0.0"}
export TTS_PORT=${TTS_PORT:-"8001"}

# 切换到项目根目录
cd "$(dirname "$0")/.."

echo "TTS服务配置:"
echo "  主机: $TTS_HOST"
echo "  端口: $TTS_PORT"
echo "  工作目录: $(pwd)"

# 启动TTS服务
python tts_service_simple.py