# 百度云实时 ASR 测试

本目录用于测试百度云实时语音识别（WebSocket API）。

## 文件说明

- `realtime_asr_test.py`：实时语音识别测试脚本
- `.env`：百度云参数与测试音频路径
- `asr_result.json`：运行后输出的识别结果（自动生成）

## 运行方式

在项目根目录执行：

```bash
python3 ASR_test/百度云ASR/realtime_asr_test.py
```

## 实现细节

- 自动将输入 WAV 转为 `16kHz / 16bit / 单声道 PCM`
- 按官方建议以约 `160ms` 一帧发送二进制音频（`5120 bytes`）
- 发送 `START` / 音频帧 / `FINISH`，并接收 `MID_TEXT`、`FIN_TEXT`
- 将完整返回写入 `asr_result.json`

## 参考文档

- [百度云实时语音识别 WebSocket API](https://cloud.baidu.com/doc/SPEECH/s/jlbxejt2i)
