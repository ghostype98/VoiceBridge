# ASR 对比测试

各子目录为不同厂商的**实时语音识别** WebSocket 测试脚本，与生产主链路（阿里云）独立。

## 测试音频

请将 **16kHz、16bit、单声道** 的 WAV 放到 `ASR_test/sample.wav`，或通过环境变量指定路径。

## 运行

```bash
python3 ASR_test/阿里云ASR/realtime_asr_test.py
python3 ASR_test/run_four_asr.py --audio ASR_test/sample.wav
```
