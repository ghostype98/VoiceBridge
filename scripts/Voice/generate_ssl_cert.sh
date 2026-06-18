#!/bin/bash

# VoiceBridge SSL证书生成脚本
# 用于生成自签名SSL证书，支持HTTPS访问

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CERT_DIR="$PROJECT_DIR/ssl"
CONFIG_FILE="$PROJECT_DIR/config/config.yaml"

echo "VoiceBridge SSL证书生成工具"
echo "=============================="

# 检查openssl是否安装
if ! command -v openssl &> /dev/null; then
    echo "错误: openssl 未安装，请先安装 openssl"
    echo "Ubuntu/Debian: sudo apt-get install openssl"
    echo "CentOS/RHEL: sudo yum install openssl"
    exit 1
fi

# 创建SSL证书目录
mkdir -p "$CERT_DIR"

# 证书参数
CERT_FILE="$CERT_DIR/voicebridge.crt"
KEY_FILE="$CERT_DIR/voicebridge.key"
DAYS=365
COUNTRY="CN"
STATE="Beijing"
CITY="Beijing"
ORGANIZATION="VoiceBridge"
UNIT="Development"
COMMON_NAME="localhost"
EMAIL="admin@voicebridge.local"

echo "生成SSL证书..."
echo "证书信息:"
echo "  国家: $COUNTRY"
echo "  省份: $STATE"
echo "  城市: $CITY"
echo "  组织: $ORGANIZATION"
echo "  部门: $UNIT"
echo "  域名: $COMMON_NAME"
echo "  邮箱: $EMAIL"
echo "  有效期: $DAYS 天"
echo ""

# 生成私钥
echo "1. 生成私钥..."
openssl genrsa -out "$KEY_FILE" 2048

# 生成证书签名请求
echo "2. 生成证书签名请求..."
openssl req -new -key "$KEY_FILE" -out "$CERT_DIR/voicebridge.csr" -subj "/C=$COUNTRY/ST=$STATE/L=$CITY/O=$ORGANIZATION/OU=$UNIT/CN=$COMMON_NAME/emailAddress=$EMAIL"

# 生成自签名证书
echo "3. 生成自签名证书..."
openssl x509 -req -days $DAYS -in "$CERT_DIR/voicebridge.csr" -signkey "$KEY_FILE" -out "$CERT_FILE"

# 清理临时文件
rm -f "$CERT_DIR/voicebridge.csr"

echo ""
echo "SSL证书生成完成！"
echo "证书文件: $CERT_FILE"
echo "私钥文件: $KEY_FILE"
echo ""

# 检查配置文件是否存在
if [ -f "$CONFIG_FILE" ]; then
    echo "检测到配置文件: $CONFIG_FILE"
    echo "请手动编辑配置文件启用SSL:"

    echo ""
    echo "在 config.yaml 中的 voicebridge.ssl 部分修改为:"
    echo "  ssl:"
    echo "    enabled: true"
    echo "    certfile: $CERT_FILE"
    echo "    keyfile: $KEY_FILE"
    echo ""

    # 尝试自动修改配置文件
    if command -v python3 &> /dev/null; then
        echo "尝试自动更新配置文件..."
        python3 -c "
import yaml
import os
config_path = '$CONFIG_FILE'
cert_file = '$CERT_FILE'
key_file = '$KEY_FILE'

try:
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    if 'services' not in config:
        config['services'] = {}
    if 'voicebridge' not in config['services']:
        config['services']['voicebridge'] = {}
    if 'ssl' not in config['services']['voicebridge']:
        config['services']['voicebridge']['ssl'] = {}

    config['services']['voicebridge']['ssl']['enabled'] = True
    config['services']['voicebridge']['ssl']['certfile'] = cert_file
    config['services']['voicebridge']['ssl']['keyfile'] = key_file

    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print('配置文件已自动更新！')
    print('SSL证书路径已配置')
except Exception as e:
    print(f'自动更新失败，请手动编辑: {e}')
"
    else
        echo "Python3未安装，请手动编辑配置文件"
    fi
else
    echo "未找到配置文件: $CONFIG_FILE"
    echo "请创建配置文件并添加SSL配置"
fi

echo ""
echo "重启VoiceBridge服务器后，将支持HTTPS访问："
echo "https://localhost:8010"
echo ""
echo "注意："
echo "1. 自签名证书会被浏览器标记为不安全"
echo "2. 在浏览器中点击'高级' -> '继续访问'即可"
echo "3. 生产环境建议使用由CA签发的正式证书"