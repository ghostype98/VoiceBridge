#!/bin/bash
# Token自动刷新功能配置和验证脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$BACKEND_DIR/services/config.yaml"

echo "=========================================="
echo "Token自动刷新功能配置助手"
echo "=========================================="
echo ""

# 检查配置文件是否存在
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

# 检查当前配置状态
echo "1. 检查当前配置状态..."
ACCESS_KEY_ID=$(python3 -c "
import yaml
with open('$CONFIG_FILE', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
    asr = config.get('voice_interview_streaming', {}).get('asr', {})
    print(asr.get('access_key_id', ''))
" 2>/dev/null || echo "")

ACCESS_KEY_SECRET=$(python3 -c "
import yaml
with open('$CONFIG_FILE', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
    asr = config.get('voice_interview_streaming', {}).get('asr', {})
    print(asr.get('access_key_secret', ''))
" 2>/dev/null || echo "")

if [ -z "$ACCESS_KEY_ID" ] || [ "$ACCESS_KEY_ID" = "your_access_key_id" ] || \
   [ -z "$ACCESS_KEY_SECRET" ] || [ "$ACCESS_KEY_SECRET" = "your_access_key_secret" ]; then
    echo "⚠️  AccessKey未配置或使用占位符"
    echo ""
    echo "请选择配置方式："
    echo "  1) 手动编辑配置文件: $CONFIG_FILE"
    echo "  2) 通过环境变量设置（当前会话有效）"
    echo ""
    read -p "请选择 (1/2): " choice
    
    if [ "$choice" = "2" ]; then
        read -p "请输入 AccessKey ID: " ak_id
        read -sp "请输入 AccessKey Secret: " ak_secret
        echo ""
        export ALIYUN_ACCESS_KEY_ID="$ak_id"
        export ALIYUN_ACCESS_KEY_SECRET="$ak_secret"
        echo "✅ 环境变量已设置（仅当前会话有效）"
    else
        echo ""
        echo "请编辑配置文件: $CONFIG_FILE"
        echo "找到以下配置项并填写真实的AccessKey："
        echo "  access_key_id: \"your_access_key_id\""
        echo "  access_key_secret: \"your_access_key_secret\""
        echo ""
        read -p "配置完成后按回车继续..."
    fi
else
    echo "✅ AccessKey已配置"
fi

# 验证配置
echo ""
echo "2. 验证Token获取功能..."
cd "$BACKEND_DIR"

if [ -n "$ALIYUN_ACCESS_KEY_ID" ] && [ -n "$ALIYUN_ACCESS_KEY_SECRET" ]; then
    # 使用环境变量
    export ALIYUN_REGION="cn-shanghai"
    python3 voice_interview_streaming/verify_token.py
else
    # 使用配置文件
    python3 voice_interview_streaming/verify_token.py
fi

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Token获取验证通过！"
    echo ""
    echo "3. 重启服务..."
    echo ""
    
    # 查找运行中的服务
    PID=$(ps aux | grep -E "python.*uvicorn.*9005|uvicorn.*api.app.*9005" | grep -v grep | awk '{print $2}' | head -1)
    
    if [ -n "$PID" ]; then
        echo "   发现运行中的服务 (PID: $PID)"
        read -p "   是否重启服务? (y/n): " restart
        if [ "$restart" = "y" ] || [ "$restart" = "Y" ]; then
            echo "   正在停止服务..."
            kill $PID || true
            sleep 2
            
            echo "   正在启动服务..."
            cd "$BACKEND_DIR"
            nohup python3 -m uvicorn api.app:app --host 0.0.0.0 --port 9005 > /tmp/uvicorn_9005.log 2>&1 &
            NEW_PID=$!
            echo "   ✅ 服务已启动 (PID: $NEW_PID)"
            echo "   日志文件: /tmp/uvicorn_9005.log"
            echo ""
            echo "   等待服务启动..."
            sleep 3
            
            # 检查服务是否正常启动
            if ps -p $NEW_PID > /dev/null; then
                echo "   ✅ 服务运行正常"
                echo ""
                echo "=========================================="
                echo "配置完成！"
                echo "=========================================="
                echo ""
                echo "下一步："
                echo "  1. 访问语音面试页面建立WebSocket连接"
                echo "  2. 查看日志，应该看到Token自动获取的日志"
                echo "  3. 查看日志文件: tail -f /tmp/uvicorn_9005.log"
                echo ""
            else
                echo "   ⚠️ 服务启动可能失败，请查看日志: /tmp/uvicorn_9005.log"
            fi
        else
            echo "   跳过重启，请手动重启服务"
        fi
    else
        echo "   ⚠️ 未找到运行中的服务"
        echo "   请手动启动服务:"
        echo "   cd $BACKEND_DIR"
        echo "   python3 -m uvicorn api.app:app --host 0.0.0.0 --port 9005"
    fi
else
    echo ""
    echo "❌ Token获取验证失败"
    echo ""
    echo "请检查："
    echo "  1. AccessKey是否正确"
    echo "  2. AccessKey是否有NLS服务权限"
    echo "  3. 网络连接是否正常"
    echo ""
    echo "详细错误信息请查看上方输出"
    exit 1
fi
