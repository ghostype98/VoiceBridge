#!/bin/bash
# -*- coding: utf-8 -*-
# VoiceBridge 前端开发服务器（端口与 config/config.yaml 主服务一致，默认 8002）
# 生产环境为 API+前端一体，本脚本仅用于前端单独开发

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"

cd "$FRONTEND_DIR" || exit 1

if [ ! -f "package.json" ]; then
    echo "错误: 未找到 frontend/package.json，请先在该目录执行: npm install"
    exit 1
fi

if [ ! -d "node_modules" ]; then
    echo "首次运行，正在安装依赖: npm install"
    npm install || exit 1
fi

echo "=========================================="
echo "VoiceBridge 前端 (默认端口 8002，与 config 主服务一致)"
echo "  /api、/ws 将代理到后端"
echo "  请先启动后端: bash $SCRIPT_DIR/start_service.sh"
echo "=========================================="
npm run dev
