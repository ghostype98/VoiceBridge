#!/bin/bash
# 下载模型脚本（本地部署）

echo "开始下载模型..."

# 创建模型目录
mkdir -p models
cd models

# 下载Vosk中文模型
echo "下载Vosk中文模型..."
if [ ! -d "vosk-model-cn-0.22" ]; then
    wget https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip
    unzip vosk-model-cn-0.22.zip
    rm vosk-model-cn-0.22.zip
    echo "Vosk模型下载完成"
else
    echo "Vosk模型已存在，跳过下载"
fi

# Whisper模型会在首次使用时自动下载
echo "Whisper模型将在首次使用时自动下载"

# Coqui TTS模型会在首次使用时自动下载
echo "Coqui TTS模型将在首次使用时自动下载"

echo "模型下载脚本执行完成"

