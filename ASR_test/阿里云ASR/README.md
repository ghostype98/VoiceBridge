# 阿里云实时 ASR 测试

本目录测试阿里云智能语音交互「实时语音识别」，逻辑复用仓库内 `app/voice_streaming/asr_client.py` 与 `token_manager.py`。

## 文件

- `realtime_asr_test.py`：WebSocket 流式发送 PCM 并收集中间/句末结果
- `.env`（可选）：仅建议填写 `ALIYUN_TEST_AUDIO`、`ALIYUN_CHUNK_MS`；**不要**在此填写 AccessKey，应与主工程一致（见下）
- `asr_result.json`：运行后生成

## 运行

在仓库根目录：

```bash
python3 ASR_test/阿里云ASR/realtime_asr_test.py
```

## 说明

- 将输入 WAV 转为 `16kHz / 16bit / 单声道 PCM` 后按块发送（默认约 200ms 一块）
- **凭证**：与 `VoiceInterviewWebSocketServer` 相同，来自 `config/config.yaml` 的 `voice_streaming.asr`，以及 `auto_config` 里映射的环境变量 `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`（优先级见 `config/settings.py`）
- 脚本启动时会 `chdir` 到仓库根目录，保证 `Settings` 能正确读取 `./config/config.yaml`

## 文档

- [实时语音识别](https://help.aliyun.com/zh/isi/developer-reference/sdk-reference)
