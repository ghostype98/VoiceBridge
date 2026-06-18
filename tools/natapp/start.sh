#!/bin/bash

# NATAPP 内网穿透启动脚本
# 本地转发端口从 config/config.yaml 的 services.voicebridge.port 读取（唯一配置来源）

cd "$(dirname "$0")"
# 项目根目录（voicebridge）
PROJECT_DIR="$(cd "../.." && pwd)"
NATAPP_BIN="$PROJECT_DIR/tools/natapp/natapp"
LOG_DIR="$PROJECT_DIR/logs/natapp"
mkdir -p "$LOG_DIR"
NATAPP_LOG="$LOG_DIR/natapp.log"
NATAPP_LOG_ABS="$(cd "$LOG_DIR" && pwd)/natapp.log"  # 绝对路径，供 natapp -log 使用
touch "$NATAPP_LOG"  # 确保日志文件存在，便于 tail -f

# 从 config/config.yaml 读取主服务端口，并写入 config.ini（用户只需改 config.yaml）
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
# 强制写入 lanport，确保 config.ini 与 config.yaml 一致（无空格、无注释行干扰）
if [ -f "./config.ini" ]; then
    sed -i "s/^lanport=.*/lanport=$MAIN_PORT/" ./config.ini
    # 若没有 lanport 行则追加（仅 [default] 段后）
    grep -q '^lanport=' ./config.ini || sed -i "/^\[default\]/a lanport=$MAIN_PORT" ./config.ini
fi
echo "本机 config.ini 已设为 lanport=$MAIN_PORT（主服务端口）"

# 检查natapp可执行文件是否存在
if [ ! -f "$NATAPP_BIN" ]; then
    echo "错误: natapp 可执行文件不存在！"
    exit 1
fi

# 检查config.ini是否存在
if [ ! -f "./config.ini" ]; then
    echo "错误: config.ini 配置文件不存在！"
    exit 1
fi

# 只检查当前项目目录下的 natapp 进程，避免与其他项目冲突
# 若已在运行则视为成功（exit 0），便于 systemd 开机自启时不因“已运行”报错
if pgrep -f "$NATAPP_BIN" > /dev/null; then
    RUNNING_PID=$(pgrep -f "$NATAPP_BIN" | tr '\n' ' ')
    echo "natapp 已在运行中 (PID: $RUNNING_PID)，无需重复启动。当前日志: $NATAPP_LOG"
    echo "如需重启请先执行: bash $PROJECT_DIR/tools/natapp/stop.sh"
    exit 0
fi

# 启动natapp（后台运行）：直接重定向到日志文件，避免管道导致子进程被关闭
echo "正在启动 NATAPP 内网穿透..."
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 启动 ==========" >> "$NATAPP_LOG"
nohup "$NATAPP_BIN" -config=config.ini -log=stdout -loglevel=INFO >> "$NATAPP_LOG_ABS" 2>&1 </dev/null &

# 等待一下，让服务启动并产生日志
sleep 3

# 检查是否启动成功（只认当前项目目录下的 natapp）
if pgrep -f "$NATAPP_BIN" > /dev/null; then
    RUNNING_PID=$(pgrep -f "$NATAPP_BIN" | tr '\n' ' ')
    echo "✓ NATAPP 启动成功！"
    echo "进程 PID: $RUNNING_PID"
    echo "日志文件: $NATAPP_LOG"
    if [ -s "$NATAPP_LOG" ]; then
        echo "--- 最近几行日志 ---"
        tail -8 "$NATAPP_LOG"
        echo "--- 实时查看: tail -f $NATAPP_LOG ---"
    fi
    echo "停止: bash $PROJECT_DIR/tools/natapp/stop.sh"
    echo "请访问你的域名: http://recruitment.natapp1.cc"
else
    echo "✗ NATAPP 启动失败，请查看: cat $NATAPP_LOG"
    [ -s "$NATAPP_LOG" ] && tail -20 "$NATAPP_LOG"
    exit 1
fi

