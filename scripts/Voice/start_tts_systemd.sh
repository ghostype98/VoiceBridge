#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge TTS 服务启动脚本（用于 systemd）

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 切换到项目目录
cd "$PROJECT_DIR" || exit 1

# 激活 conda 环境
# 注意：请根据实际情况修改 conda 路径
if [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "/opt/conda/etc/profile.d/conda.sh" ]; then
    source "/opt/conda/etc/profile.d/conda.sh"
else
    # 尝试从 PATH 中找到 conda
    if command -v conda &> /dev/null; then
        eval "$(conda shell.bash hook)"
    else
        echo "错误: 无法找到 conda，请检查 conda 安装路径"
        exit 1
    fi
fi

# 激活 tts 环境
conda activate tts || {
    echo "错误: 无法激活 tts conda 环境"
    exit 1
}

# 设置环境变量
export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
export TTS_HOST="${TTS_HOST:-0.0.0.0}"
export TTS_PORT="${TTS_PORT:-8011}"

# 启动 TTS 服务
echo "启动 TTS 服务: $TTS_HOST:$TTS_PORT"
exec python "$PROJECT_DIR/tts_service_simple.py"