#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge systemd 专用启动脚本（前台运行，供 systemd 管理）
# 不要手动执行；由 systemd 调用：ExecStart=/path/to/bash/voicebridge-systemd.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# 加载 conda
if [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "/opt/conda/etc/profile.d/conda.sh" ]; then
    source "/opt/conda/etc/profile.d/conda.sh"
else
    if command -v conda &> /dev/null; then
        eval "$(conda shell.bash hook)"
    else
        echo "错误: 未找到 conda" >&2
        exit 1
    fi
fi

conda activate datastore || { echo "错误: 无法激活 conda 环境 datastore" >&2; exit 1; }
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 强制使用项目配置文件中的阿里云凭据，避免被外部环境变量覆盖
# （systemd 或用户环境可能注入任意 ALIYUN_*，一律清除后再读 config.yaml）
while IFS= read -r line; do
    case "$line" in
        ALIYUN_*=*)
            var="${line%%=*}"
            unset "$var" 2>/dev/null || true
            ;;
    esac
done < <(env)
unset ALIYUN_ACCESS_KEY_ID
unset ALIYUN_ACCESS_KEY_SECRET
unset ALIYUN_ASR_APPKEY
unset ALIYUN_ASR_TOKEN
export CONFIG_FILE="$PROJECT_DIR/config/config.yaml"

# 前台运行，systemd 据此跟踪主进程
exec python "$PROJECT_DIR/services/run.py"
