# VoiceBridge

智能语音面试系统：浏览器麦克风 → WebSocket 流式上传 → **阿里云实时语音识别** → 本地 LLM 实时评分与追问。

## 功能概览

- 实时语音转写（阿里云 NLS WebSocket）
- 面试流程管理（题目切换、录音、作答落库）
- LLM 实时评分与追问
- 前后端一体（FastAPI + 静态前端，默认端口 `8002`）

## 快速开始

### 1. 环境

- Python 3.10+
- PostgreSQL
- 阿里云智能语音交互（已开通 **实时语音识别**，并创建 AppKey）

```bash
conda create -n voicebridge python=3.10 -y
conda activate voicebridge
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env   # 可选，用于 DB / AK 环境变量
```

编辑 `config/config.yaml`，填写：

```yaml
voice_streaming:
  asr:
    appkey: "your_aliyun_nls_appkey"
    access_key_id: "your_aliyun_access_key_id"
    access_key_secret: "your_aliyun_access_key_secret"
```

数据库连接通过环境变量（推荐）：

```bash
export DB_HOST=localhost
export DB_PASSWORD=your_db_password
```

### 3. 启动

```bash
bash bash/start_service.sh
# 或
python services/run.py
```

- 前端：`http://localhost:8002/login`
- API 文档：`http://localhost:8002/docs`
- WebSocket ASR：`ws://localhost:8002/ws/asr`

### 4. 验证 ASR 链路

```bash
# 准备 16kHz 单声道 wav 放到 ASR_test/sample.wav
python3 scripts/debug_nls_realtime_probe.py
```

## 目录结构

```
app/                 # FastAPI 路由、语音流式服务、数据库
frontend/            # 面试前端页面
config/              # config.example.yaml（复制为 config.yaml）
bash/                # 启停脚本
ASR_test/            # 各厂商 ASR 对比测试脚本（不含测试音频）
docs/                # 技术方案与部署说明
```

## 开源说明

本仓库为脱敏后的 GitHub 发布版：

- 不含真实 API 密钥、数据库密码、内网地址
- 不含候选人录音、转写结果、评价备份
- 不含 `logs/`、`storage/`、大型二进制（如 natapp）

部署 NATAPP 内网穿透时，请复制 `tools/natapp/config.example.ini` 为 `config.ini` 并填写 authtoken。

## License

请根据你的开源策略添加 LICENSE 文件（如 MIT / Apache-2.0）。
