@echo off
REM Windows批处理脚本：下载模型

echo 开始下载模型...

REM 创建模型目录
if not exist models mkdir models
cd models

REM 下载Vosk中文模型
echo 下载Vosk中文模型...
if not exist vosk-model-cn-0.22 (
    echo 正在下载 vosk-model-cn-0.22.zip...
    powershell -Command "Invoke-WebRequest -Uri 'https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip' -OutFile 'vosk-model-cn-0.22.zip'"
    if exist vosk-model-cn-0.22.zip (
        echo 正在解压模型文件...
        powershell -Command "Expand-Archive -Path 'vosk-model-cn-0.22.zip' -DestinationPath '.' -Force"
        del vosk-model-cn-0.22.zip
        echo Vosk模型下载完成
    ) else (
        echo 下载失败，请手动下载: https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip
        echo 解压到 models/vosk-model-cn-0.22 目录
    )
) else (
    echo Vosk模型已存在，跳过下载
)

echo.
echo Whisper模型将在首次使用时自动下载
echo Coqui TTS模型将在首次使用时自动下载

echo.
echo 模型下载脚本执行完成
pause

