#!/bin/bash

# NATAPP 内网穿透状态检查脚本

cd "$(dirname "$0")"
PROJECT_DIR="$(cd "../.." && pwd)"
NATAPP_BIN="$PROJECT_DIR/tools/natapp/natapp"
NATAPP_LOG="$PROJECT_DIR/logs/natapp/natapp.log"

echo "=== NATAPP 运行状态 ==="

# 检查当前项目目录下的 natapp 进程
PID=$(pgrep -f "$NATAPP_BIN")
if [ -n "$PID" ]; then
    echo "✓ NATAPP 正在运行 (PID: $(echo "$PID" | tr '\n' ' '))"
    echo ""
    echo "查看最新日志:"
    tail -20 "$NATAPP_LOG" 2>/dev/null || echo "日志文件不存在: $NATAPP_LOG"
else
    echo "✗ NATAPP 未运行"
    echo ""
    echo "启动服务: bash start.sh"
fi

