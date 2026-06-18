#!/bin/bash

# NATAPP 内网穿透停止脚本

cd "$(dirname "$0")"
PROJECT_DIR="$(cd "../.." && pwd)"
NATAPP_BIN="$PROJECT_DIR/tools/natapp/natapp"

echo "正在停止 NATAPP 内网穿透..."

# 只停止当前项目目录下的 natapp 进程，避免误杀其他项目
PIDS=$(pgrep -f "$NATAPP_BIN")
if [ -z "$PIDS" ]; then
    echo "NATAPP 未运行"
    exit 0
fi

kill $PIDS 2>/dev/null
sleep 1
if pgrep -f "$NATAPP_BIN" > /dev/null; then
    echo "尝试强制停止 (kill -9)..."
    kill -9 $PIDS 2>/dev/null
    sleep 1
fi

if pgrep -f "$NATAPP_BIN" > /dev/null; then
    echo "✗ 无法停止 NATAPP (PID: $(echo "$PIDS" | tr '\n' ' '))"
    echo "  可能由其他用户或 root 启动，当前用户无权限结束该进程。"
    echo "  请尝试: sudo pkill -f \"$NATAPP_BIN\""
    echo "  执行成功后，再运行 start.sh 即可由当前用户启动并写入 logs/natapp/natapp.log"
    exit 1
fi
echo "✓ NATAPP 已停止"

