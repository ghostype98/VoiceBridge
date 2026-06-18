# 豆包语音 / 火山引擎大模型流式语音识别测试

本目录按官方文档实现 **WebSocket 双向流式**（`bigmodel`）二进制协议测试。

## 参考文档

- [大模型流式语音识别 API](https://www.volcengine.com/docs/6561/1354869?lang=zh)

## 文件说明

- `realtime_asr_test.py`：建连鉴权、full client request（JSON+gzip）、分帧发送 gzip 后的 PCM、解析 full server response
- `.env`：应用凭证与资源 ID（勿提交；仓库根 `.gitignore` 已忽略 `.env`）
- `asr_result.json`：运行后写入的原始响应列表（自动生成）

## 运行

```bash
python3 /opt/voicebridge/ASR_test/豆包ASR/realtime_asr_test.py
```

## 鉴权（旧版控制台）

HTTP 握手 Header（与文档一致）：

- `X-Api-App-Key`：APP ID  
- `X-Api-Access-Key`：Access Token  
- `X-Api-Resource-Id`：如 `volc.bigasr.sauc.duration`（小时）或 `volc.bigasr.sauc.concurrent`（并发）  
- `X-Api-Request-Id`、`X-Api-Connect-Id`：UUID  
- `X-Api-Sequence`：固定 `-1`

若控制台为新版，仅需 `X-Api-Key` 时，请按文档改脚本中的 Header 名称。

## 音频

- 输入 WAV 转为 **16kHz / 16bit / mono PCM**（与 ASR_test 下其它脚本一致）
- 每包约 `DOUBAO_CHUNK_MS`（默认 200ms）PCM，再 **gzip** 后按二进制协议发送；最后一包使用文档中的「最后一包」标志位

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `DOUBAO_APP_ID` | APP ID |
| `DOUBAO_ACCESS_TOKEN` | Access Token |
| `DOUBAO_RESOURCE_ID` | 资源 ID，需与控制台开通一致 |
| `DOUBAO_TEST_AUDIO` | 测试音频路径 |
| `DOUBAO_CHUNK_MS` | 分包时长（毫秒），默认 200 |
| `DOUBAO_UID` | 可选，`user.uid` |
| `DOUBAO_WS_URL` | 可选，默认 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| `DOUBAO_WS_EXTRA_HEADERS` | 可选，`1` 时增加 `X-Api-Request-Id`、`X-Api-Sequence:-1`（默认 `0`） |
| `DOUBAO_API_KEY` | 可选，新版控制台「仅 API Key」时填入，将改用 `X-Api-Key` 握手 |
| `DOUBAO_DUAL_AUTH` | 可选，`1` 时在 `X-Api-Key` 之外再带 `X-Api-App-Key` / `X-Api-Access-Key`（少数控制台配置需要） |
| `DOUBAO_INSTANCE_ID` | 可选，控制台实例 ID（备查） |
| `DOUBAO_SEND_INSTANCE_HEADER` | 可选，`1` 时增加 `X-Api-Instance-Id`（非文档通用项，按需开启） |

`DOUBAO_SECRET_KEY` 仅作备查；本条 API 文档中的旧版鉴权未要求将其放入 WebSocket Header。

## 常见问题

### 握手 HTTP 403，响应体含 `requested resource not granted`

说明 **APP ID / Token 已通过**，但当前 `DOUBAO_RESOURCE_ID` 对应资源未在控制台开通或未授权给该应用。请在火山「豆包语音 / 语音识别」控制台核对已购套餐，将 `.env` 中的 `DOUBAO_RESOURCE_ID` 改为与套餐一致的一项（小时 `duration` 或并发 `concurrent`，以及模型 1.0 `bigasr` 与 2.0 `seedasr` 与文档一致）。

### 握手 HTTP 400，`resourceId ... is not allowed`

多为 **ResourceId 与当前 WebSocket 路径不匹配**（例如本脚本默认走 `bigmodel`，部分 2.0 资源需使用文档中的其它路径）。请按控制台与文档选择成对的地址与 `X-Api-Resource-Id`。
