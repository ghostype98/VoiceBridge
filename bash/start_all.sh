#!/bin/bash
# -*- coding: utf-8 -*-
# 一键启动 = 启动服务(主服务端口见 config/config.yaml) + 内网穿透
# 用法: bash bash/start_all.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs/service"
mkdir -p "$LOG_DIR"

# 从 config/config.yaml 读取主端口
MAIN_PORT=$(python3 -c "
import yaml, os
cfg = os.path.join('$PROJECT_DIR', 'config', 'config.yaml')
if os.path.isfile(cfg):
    with open(cfg) as f:
        c = yaml.safe_load(f) or {}
    print(c.get('services', {}).get('voicebridge', {}).get('port', 8002))
else:
    print(8002)
" 2>/dev/null) || MAIN_PORT=8002

echo "=========================================="
echo "VoiceBridge 一键启动（主服务端口: $MAIN_PORT + 内网穿透）"
echo "=========================================="
echo "项目目录: $PROJECT_DIR"
echo ""

# 1. 启动服务（单端口：API + 前端一体）
echo ">>> 1/2 启动服务 (端口 $MAIN_PORT)..."
bash "$SCRIPT_DIR/start_service.sh" || exit 1
echo ""

# 2. 启动内网穿透（NATAPP 转发到主服务端口）
echo ">>> 2/2 启动内网穿透 (NATAPP -> $MAIN_PORT)..."
NATAPP_LOG="$PROJECT_DIR/logs/natapp/natapp.log"
bash "$PROJECT_DIR/tools/natapp/start.sh" || {
    echo "警告: NATAPP 启动失败或已在运行。日志位置: $NATAPP_LOG"
}
echo ""

echo "=========================================="
echo "全部启动完成。"
echo "  - 本地访问: http://localhost:$MAIN_PORT/login"
echo "  - 内网穿透暴露 $MAIN_PORT 后，外网访问同一端口即可"
echo "  - 日志:      $PROJECT_DIR/logs/"
echo "=========================================="
echo "查看服务日志: tail -f $PROJECT_DIR/logs/service/service_*.log"
echo "查看内网穿透: tail -f $PROJECT_DIR/logs/natapp/natapp.log"
echo "一键关闭:     bash $SCRIPT_DIR/stop_all.sh"
echo "一键重启:     bash $SCRIPT_DIR/restart_all.sh"
echo ""
