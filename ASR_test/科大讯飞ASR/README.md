# 科大讯飞实时语音转写大模型（WebSocket）测试

本目录用于按官方文档调用「实时语音转写大模型」接口，对本地 WAV 做流式识别。

## 参考文档

- [实时语音转写大模型（rtasr_llm）](https://www.xfyun.cn/doc/spark/asr_llm/rtasr_llm.html)

## 文件说明

- `realtime_asr_test.py`：握手 URL 签名、二进制分帧发送、结束帧、结果解析
- `.env`：应用 `APPID` / `APIKey` / `APISecret` 等（勿提交仓库；根目录 `.gitignore` 已忽略 `.env`）
- `asr_result.json`：运行后完整消息与日志（自动生成）

## 运行

```bash
python3 /opt/voicebridge/ASR_test/科大讯飞ASR/realtime_asr_test.py
```

## 实现要点（与文档对齐）

- 地址：`wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1?...`
- 鉴权：`accessKeyId` + `appId` + `utc` + `uuid` + `lang` + `audio_encode` + `samplerate` 等参与升序拼接得到 `baseString`，`HmacSHA1(accessKeySecret, baseString)` 再 Base64 得到 `signature`
- 音频：`pcm_s16le`，`samplerate=16000`，单声道；脚本将输入 WAV 转为 16k/mono/int16（与百度云测试目录中做法一致）
- 发送：建议 **每 40ms 发送 1280 字节** 二进制音频；结束后发送文本 JSON：`{"end": true, "sessionId": "<同一 session>"}`
- 返回：解析 `msg_type=result` 且 `res_type=asr` 的 `data.cn.st.rt` 下各词的 `w` 字段拼接为文本

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `IFLY_APP_ID` | 控制台应用 AppID |
| `IFLY_API_KEY` | 对应文档握手参数中的 `accessKeyId` |
| `IFLY_API_SECRET` | 对应文档中的 `accessKeySecret`（用于 HMAC） |
| `IFLY_LANG` | 默认 `autodialect` |
| `IFLY_TEST_AUDIO` | 测试 WAV 路径 |
| `IFLY_UUID` | 可选，不传则每次随机 |
| `IFLY_API_SECRET_IS_B64` | 若签名校验失败，可设为 `1` 尝试对 Secret 先 Base64 解码再签名 |
| `IFLY_PRINT_ALL_PACKETS` | 设为 `1` 时在终端打印每条流式包（含中间结果 type=1）；默认 `0` 只打印确定性 type=0 |

## 为何不能把每条返回都拼成「全文」

流式 `result` 里，`data.cn.st.rt` 常见语义是**当前片段的整句快照**：同一段话会从短变长（`type=1` 中间结果），**不是**「每条只多几个字」的增量。若把每条文本直接首尾相接，就会出现「是通过是通过计算机是通过…」这种重复。

脚本里 **`final_text` 只合并 `data.cn.st.type == 0` 的包**（文档：0=确定性结果，1=中间结果），与对外展示一致。完整原始包仍保存在 `asr_result.json` 的 `messages` 中便于排查。
