#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 语音交互服务启动脚本（改进版）

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 日志目录与文件（统一放在 logs/service/）
LOG_DIR="$PROJECT_DIR/logs/service"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/service_${TIMESTAMP}.log"

# 输出日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=========================================="
log "VoiceBridge 服务启动"
log "=========================================="

# 切换到项目目录
cd "$PROJECT_DIR" || exit 1
log "项目目录: $PROJECT_DIR"

# 查找并激活 conda 环境
if [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
    log "使用 anaconda3"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
    log "使用 miniconda3"
elif [ -f "/opt/conda/etc/profile.d/conda.sh" ]; then
    source "/opt/conda/etc/profile.d/conda.sh"
    log "使用 /opt/conda"
else
    # 尝试从 PATH 中找到 conda
    if command -v conda &> /dev/null; then
        eval "$(conda shell.bash hook)"
        log "使用系统 conda"
    else
        log "错误: 无法找到 conda，请检查 conda 安装路径"
        exit 1
    fi
fi

# 激活 datastore 环境
log "激活 conda 环境: datastore"
conda activate datastore || {
    log "错误: 无法激活 datastore conda 环境"
    exit 1
}

# 设置环境变量
export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"
log "PYTHONPATH: $PYTHONPATH"

# 检查是否已有服务在运行
PID_FILE="$PROJECT_DIR/voicebridge.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        log "警告: 服务已在运行 (PID: $OLD_PID)"
        log "如需重启，请先运行: bash $PROJECT_DIR/bash/stop_service.sh"
        exit 1
    else
        log "清理旧的PID文件"
        rm -f "$PID_FILE"
    fi
fi

# 启动服务
log "启动 VoiceBridge 服务..."
log "主服务端口: 8010"
log "WebSocket端口: 8765"
log "日志文件: $LOG_FILE"

# 使用 nohup 在后台运行，避免阻塞终端
nohup python "$PROJECT_DIR/services/run.py" >> "$LOG_FILE" 2>&1 &
SERVICE_PID=$!

# 保存PID
echo $SERVICE_PID > "$PID_FILE"
log "服务已启动 (PID: $SERVICE_PID)"

# 等待服务启动
log "等待服务启动..."
sleep 3

# 检查服务是否正常运行
if ps -p $SERVICE_PID > /dev/null 2>&1; then
    log "✓ 服务启动成功！"
    log "  - 主服务: http://0.0.0.0:8010"
    log "  - WebSocket: ws://0.0.0.0:8765"
    log "  - 前端页面: http://localhost:8010/login"
    log "  - API文档: http://localhost:8010/docs"
    log "  - 日志文件: $LOG_FILE"
    log "  - PID文件: $PID_FILE"
    log ""
    log "查看实时日志: tail -f $LOG_FILE"
    log "停止服务: bash $PROJECT_DIR/bash/stop_service.sh"
    log "=========================================="
else
    log "✗ 服务启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
