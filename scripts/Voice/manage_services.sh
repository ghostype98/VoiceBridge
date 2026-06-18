#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 服务启动器
# 专注于启动和管理语音面试相关服务

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 切换到项目目录
cd "$PROJECT_DIR" || exit 1

# 确保日志目录存在
mkdir -p logs

# 检查必要的工具
check_tools() {
    local missing_tools=""

    if ! command -v lsof >/dev/null 2>&1; then
        missing_tools="$missing_tools lsof"
    fi

    if ! command -v fuser >/dev/null 2>&1; then
        missing_tools="$missing_tools fuser"
    fi

    if [ -n "$missing_tools" ]; then
        log_warning "缺少工具:$missing_tools，端口清理功能将受限"
        HAS_TOOLS=false
    else
        HAS_TOOLS=true
    fi
}

# 初始化工具检查
check_tools

# 检查LLM连通性
check_llm_connectivity() {
    log_info "检查LLM服务连通性..."

    # 从配置文件读取LLM配置
    local llm_api_base=$(python3 -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
services = config.get('services', {})
llm = services.get('llm', {})
print(llm.get('api_base', 'http://localhost:7000'))
" 2>/dev/null || echo "http://localhost:7000")

    log_info "LLM API地址: $llm_api_base"

    # 从URL中提取主机和端口
    local host=$(echo "$llm_api_base" | sed -E 's|https?://([^:/]+).*|\1|')
    local port=$(echo "$llm_api_base" | sed -E 's|https?://[^:/]+:?([0-9]+)?.*|\1|' || echo "80")

    # 如果是localhost或127.0.0.1，使用更简单的连通性检查
    if [[ "$host" == "localhost" || "$host" == "127.0.0.1" ]]; then
        if nc -z "$host" "$port" 2>/dev/null; then
            log_success "LLM服务连通性检查通过 (本地服务)"
            return 0
        fi
    else
        # 对于远程服务，尝试简单的TCP连接测试
        if timeout 5 bash -c "</dev/tcp/$host/$port" 2>/dev/null; then
            log_success "LLM服务连通性检查通过 (远程服务)"
            return 0
        fi
    fi

    log_warning "LLM服务连通性检查失败，但将继续启动（可能使用外部服务）"
    return 0
}

# 启动TTS服务
start_tts_service() {
    log_info "启动TTS服务..."

    # 检查TTS服务是否已经在8011端口运行
    if curl -s --max-time 5 http://localhost:8011/health | grep -q '"status":\s*"healthy"'; then
        log_success "TTS服务已在8011端口运行"
        return 0
    fi

    # 如果有旧的TTS进程在运行，先停止
    if pgrep -f "tts_service_standalone" > /dev/null; then
        log_warning "发现旧的TTS进程，正在停止..."
        pkill -f "tts_service_standalone"
        sleep 2
    fi

    # 启动TTS服务（后台运行）
    log_info "启动新的TTS服务进程..."

    # 检查是否能使用tts环境，如果不能则使用当前环境
    if conda info --envs | grep -q "^tts "; then
        log_info "使用tts conda环境启动TTS服务"
        nohup conda run -n tts python app/services/tts_service_standalone.py 2>&1 &
    else
        log_warning "tts conda环境不存在，使用当前环境启动TTS服务"
        nohup python3 app/services/tts_service_standalone.py 2>&1 &
    fi
    local tts_pid=$!

    # 等待服务启动
    local retries=0
    while [ $retries -lt 15 ]; do
        if curl -s --max-time 5 http://localhost:8011/health | grep -q '"status":\s*"healthy"'; then
            log_success "TTS服务启动成功 (PID: $tts_pid)"
            return 0
        fi
        sleep 2
        ((retries++))
        log_info "等待TTS服务启动... ($retries/15)"
    done

    log_error "TTS服务启动失败"
    log_info "查看TTS服务日志: ./scripts/manage_services.sh logs tts"
    return 1
}

# 启动语音面试主服务
start_voicebridge_service() {
    log_info "启动语音面试主服务..."

    # 检查服务是否已经在运行
    if pgrep -f "uvicorn.*app.main" > /dev/null; then
        log_warning "语音面试主服务已在运行，跳过启动"
        return 0
    fi

    # 启动主服务（后台运行）
    nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload > logs/voicebridge.log 2>&1 &
    local main_pid=$!

    # 等待服务启动
    local retries=0
    while [ $retries -lt 15 ]; do
        if curl -s --max-time 5 http://localhost:8010/docs | grep -q "VoiceBridge"; then
            log_success "语音面试主服务启动成功 (PID: $main_pid)"
            return 0
        fi
        sleep 2
        ((retries++))
    done

    log_error "语音面试主服务启动失败"
    return 1
}

# 检查TTS和ASR服务可用性
check_tts_asr_availability() {
    log_info "检查TTS和ASR服务可用性..."

    # 检查TTS服务
    if curl -s --max-time 10 http://localhost:8011/health | grep -q "healthy"; then
        log_success "TTS服务可用性检查通过"
    else
        log_error "TTS服务不可用"
        return 1
    fi

    # 检查ASR服务（如果配置了的话）
    # 这里可以根据需要添加ASR服务的检查逻辑
    log_info "ASR服务检查跳过（可根据需要配置）"

    return 0
}

# 显示服务状态
show_service_status() {
    echo ""
    echo "=== VoiceBridge 服务状态 ==="

    # 检查LLM服务
    echo "--- LLM服务 ---"
    local llm_api_base=$(python3 -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
services = config.get('services', {})
llm = services.get('llm', {})
print(llm.get('api_base', 'http://localhost:8002'))
" 2>/dev/null || echo "http://localhost:8002")

    if curl -s --max-time 5 "$llm_api_base/health" | grep -q "healthy"; then
        echo "状态: 运行中 ($llm_api_base)"
    else
        echo "状态: 未知或未运行 ($llm_api_base)"
    fi

    # 显示部署模式（语音流式服务）
    echo "--- 语音流式服务 ---"
    echo "模式: 语音流式服务 (阿里云百炼WebSocket)"
    echo "WebSocket地址: ws://localhost:8765"

    # 检查主服务
    echo "--- 语音面试主服务 ---"
    if pgrep -f "uvicorn.*app.main" > /dev/null; then
        echo "状态: 运行中 (PID: $(pgrep -f "uvicorn.*app.main"))"
    else
        echo "状态: 未运行"
    fi

    echo ""
    echo "服务地址:"
    echo "  LLM服务: $llm_api_base"
    echo "  主服务:  http://localhost:8010"
    echo "  语音流式WebSocket: ws://localhost:8765"
}

# 显示服务日志
show_service_logs() {
    local service_name=${1:-""}
    local lines=${2:-50}

    echo ""
    echo "=== VoiceBridge 服务日志 ==="

    if [[ -n "$service_name" ]]; then
        case $service_name in
            "tts"|"TTS")
                if [[ -f "logs/tts_service.log" ]]; then
                    echo "--- TTS服务日志 (最近 $lines 行) ---"
                    tail -n $lines logs/tts_service.log
                else
                    log_warning "TTS服务日志文件不存在"
                fi
                ;;
            "voicebridge"|"main")
                if [[ -f "logs/voicebridge.log" ]]; then
                    echo "--- 主服务日志 (最近 $lines 行) ---"
                    tail -n $lines logs/voicebridge.log
                else
                    log_warning "主服务日志文件不存在"
                fi
                ;;
            *)
                log_error "未知服务名称: $service_name"
                echo "支持的服务: tts, voicebridge"
                ;;
        esac
    else
        # 显示所有服务的日志
        if [[ -f "logs/tts_service.log" ]]; then
            echo "--- TTS服务日志 (最近 $lines 行) ---"
            tail -n $lines logs/tts_service.log
            echo ""
        fi

        if [[ -f "logs/voicebridge.log" ]]; then
            echo "--- 主服务日志 (最近 $lines 行) ---"
            tail -n $lines logs/voicebridge.log
        fi
    fi
}

# 停止所有服务
stop_services() {
    log_info "停止 VoiceBridge 服务..."

    # 从配置文件读取端口信息
    local voicebridge_port=$(python3 -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
services = config.get('services', {})
voicebridge = services.get('voicebridge', {})
print(voicebridge.get('port', 8010))
" 2>/dev/null || echo "8010")

    local tts_port=$(python3 -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
services = config.get('services', {})
tts = services.get('tts', {})
print(tts.get('port', 8011))
" 2>/dev/null || echo "8011")

    local llm_port=$(python3 -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
services = config.get('services', {})
llm = services.get('llm', {})
api_base = llm.get('api_base', 'http://localhost:7000')
# 提取端口号
import re
match = re.search(r':(\d+)', api_base)
print(match.group(1) if match else '7000')
" 2>/dev/null || echo "7000")

    log_info "配置端口 - 主服务: $voicebridge_port, TTS: $tts_port, LLM: $llm_port"

    # 停止TTS服务
    if pgrep -f "tts_service_standalone" > /dev/null; then
        log_info "停止TTS服务..."
        pkill -f "tts_service_standalone"
        sleep 2
        if pgrep -f "tts_service_standalone" > /dev/null; then
            log_warning "TTS服务进程仍在运行，强制终止..."
            pkill -9 -f "tts_service_standalone"
            sleep 1
        fi
    fi

    # 停止主服务
    if pgrep -f "uvicorn.*app.main" > /dev/null; then
        log_info "停止语音面试主服务..."
        pkill -f "uvicorn.*app.main"
        sleep 2
        if pgrep -f "uvicorn.*app.main" > /dev/null; then
            log_warning "主服务进程仍在运行，强制终止..."
            pkill -9 -f "uvicorn.*app.main"
            sleep 1
        fi
    fi

    # 停止LLM服务（如果存在）
    if pgrep -f "llm_service_standalone" > /dev/null; then
        log_info "停止LLM服务..."
        pkill -f "llm_service_standalone"
        sleep 2
        if pgrep -f "llm_service_standalone" > /dev/null; then
            log_warning "LLM服务进程仍在运行，强制终止..."
            pkill -9 -f "llm_service_standalone"
            sleep 1
        fi
    fi

    # 强制清理所有配置的端口
    local ports_to_clean=("$voicebridge_port" "$tts_port" "$llm_port")
    for port in "${ports_to_clean[@]}"; do
        if lsof -i :$port >/dev/null 2>&1; then
            log_warning "端口 $port 仍在被占用，强制清理..."
            if [ "$HAS_TOOLS" = true ]; then
                fuser -k $port/tcp >/dev/null 2>&1 || true
            fi
            sleep 1

            # 如果仍有进程占用，尝试更激进的清理
            if lsof -i :$port >/dev/null 2>&1; then
                log_warning "尝试更激进的端口 $port 清理..."
                local pid=$(lsof -ti :$port 2>/dev/null)
                if [ -n "$pid" ]; then
                    kill -9 $pid 2>/dev/null || true
                    sleep 1
                fi
            fi

            if lsof -i :$port >/dev/null 2>&1; then
                log_error "端口 $port 清理失败，可能仍有进程占用"
            else
                log_success "端口 $port 已释放"
            fi
        else
            log_info "端口 $port 可用"
        fi
    done

    log_success "所有服务停止完成，所有配置端口已释放"
}

# 启动所有服务（核心功能）
start_all_services() {
    log_info "开始启动 VoiceBridge 服务..."

    # 1. 检查LLM连通性
    if ! check_llm_connectivity; then
        log_error "LLM连通性检查失败，终止启动"
        return 1
    fi

    # 2. 启动TTS服务
    if ! start_tts_service; then
        log_error "TTS服务启动失败，终止启动"
        return 1
    fi

    # 3. 启动语音面试主服务
    if ! start_voicebridge_service; then
        log_error "语音面试主服务启动失败，终止启动"
        return 1
    fi

    # 4. 检查TTS和ASR服务可用性
    if ! check_tts_asr_availability; then
        log_error "服务可用性检查失败"
        return 1
    fi

    log_success "🎉 所有服务启动完成！"
    echo ""
    echo "服务访问地址:"
    echo "  主服务:  http://localhost:8010"
    echo "  TTS服务: http://localhost:8011"
    echo "  前端界面: http://localhost:8010/static/interview.html"
}

# 重启所有服务
restart_services() {
    log_info "重启 VoiceBridge 服务..."

    # 1. 停止所有服务
    if ! stop_services; then
        log_error "停止服务失败"
        return 1
    fi

    # 等待端口完全释放
    sleep 3

    # 2. 启动所有服务
    if ! start_all_services; then
        log_error "重启服务失败"
        return 1
    fi

    log_success "🎉 所有服务重启完成！"
}

# 前台启动服务（显示实时日志）
start_foreground_services() {
    local service_type=${1:-""}

    case "$service_type" in
        "tts"|"TTS")
            start_tts_foreground
            ;;
        "main"|"web"|"api")
            start_main_foreground
            ;;
        "all")
            start_all_foreground
            ;;
        *)
            log_info "前台启动 VoiceBridge 服务"
            echo ""
            echo "前台启动选项:"
            echo "  tts    - 启动TTS服务（前台）"
            echo "  main   - 启动主服务（前台）"
            echo "  all    - 启动所有服务（需要多个终端）"
            echo ""
            echo "示例:"
            echo "  $0 foreground tts     # 前台启动TTS服务"
            echo "  $0 foreground main    # 前台启动主服务"
            echo "  $0 foreground all     # 显示完整前台启动指南"
            echo ""
            ;;
    esac
}

# 前台启动TTS服务
start_tts_foreground() {
    log_info "前台启动TTS服务..."

    # 检查是否已有后台服务运行
    if pgrep -f "tts_service_standalone" > /dev/null; then
        log_warning "检测到TTS服务正在后台运行，请先执行: $0 stop"
        return 1
    fi

    # 检查是否能使用tts环境
    if conda info --envs | grep -q "^tts "; then
        log_info "使用tts conda环境启动TTS服务（前台模式）"
        log_info "按 Ctrl+C 停止服务"
        echo ""
        conda run -n tts python app/services/tts_service_standalone.py
    else
        log_warning "tts conda环境不存在，使用当前环境启动TTS服务（前台模式）"
        log_info "按 Ctrl+C 停止服务"
        echo ""
        python3 app/services/tts_service_standalone.py
    fi
}

# 前台启动主服务
start_main_foreground() {
    log_info "前台启动语音面试主服务..."

    # 检查是否已有后台服务运行
    if pgrep -f "uvicorn.*app.main" > /dev/null; then
        log_warning "检测到主服务正在后台运行，请先执行: $0 stop"
        return 1
    fi

    log_info "使用uvicorn启动主服务（前台模式，支持热重载）"
    log_info "按 Ctrl+C 停止服务"
    echo ""
    python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
}

# 显示前台启动指南
start_all_foreground() {
    log_info "前台启动所有服务指南"
    echo ""
    echo "由于前台启动会占用终端，建议使用多个终端窗口："
    echo ""
    echo "终端窗口1 - 启动TTS服务:"
    echo "  $0 foreground tts"
    echo ""
    echo "终端窗口2 - 启动主服务:"
    echo "  $0 foreground main"
    echo ""
    echo "或者直接使用以下命令:"
    echo ""
    echo "启动TTS服务:"
    if conda info --envs | grep -q "^tts "; then
        echo "  conda run -n tts python app/services/tts_service_standalone.py"
    else
        echo "  python3 app/services/tts_service_standalone.py"
    fi
    echo ""
    echo "启动主服务:"
    echo "  uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload"
    echo ""
    echo "查看实时日志:"
    echo "  tail -f logs/tts_service.log"
    echo "  tail -f logs/voicebridge.log"
    echo ""
}

# 显示帮助信息
show_help() {
    echo "VoiceBridge 服务启动器"
    echo ""
    echo "用法: $0 <命令> [子命令]"
    echo ""
    echo "命令:"
    echo "  start           启动所有服务（后台运行）"
    echo "  stop            停止所有服务并释放所有配置端口"
    echo "  restart         重启所有服务"
    echo "  foreground      前台启动服务（显示实时日志）"
    echo "    tts           前台启动TTS服务"
    echo "    main          前台启动主服务"
    echo "    all           显示前台启动指南"
    echo "  status          显示所有服务状态"
    echo "  logs [服务名]   显示服务日志 (默认最近50行)"
    echo "  help            显示此帮助信息"
    echo ""
    echo "服务说明:"
    echo "  - LLM服务:     大语言模型服务（外部服务，不由本脚本管理）"
    echo "  - TTS服务:     语音合成服务 (端口 8011)"
    echo "  - 主服务:      语音面试主服务 (端口 8010)"
    echo ""
    echo "示例:"
    echo "  $0 start                # 后台启动所有服务"
    echo "  $0 foreground tts       # 前台启动TTS服务"
    echo "  $0 foreground main      # 前台启动主服务"
    echo "  $0 foreground all       # 显示前台启动指南"
    echo "  $0 stop                  # 停止所有服务"
    echo "  $0 status                # 查看服务状态"
    echo "  $0 logs tts             # 查看TTS服务日志"
}

# 主函数
main() {
    local command=$1
    local subcommand=$2
    shift 2

    case "$command" in
        start)
            start_all_services
            ;;
        stop)
            stop_services
            ;;
        restart)
            restart_services
            ;;
        foreground|dev)
            start_foreground_services "$subcommand"
            ;;
        status)
            show_service_status "$subcommand" "$@"
            ;;
        logs)
            show_service_logs "$subcommand" "$@"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            log_error "未知命令: $command"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

# 如果没有参数，显示帮助
if [[ $# -eq 0 ]]; then
    show_help
    exit 0
fi

# 执行主函数
main "$@"