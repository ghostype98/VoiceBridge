# 语音面试流式转写与实时评价系统

## 项目概述

这是一个全新的语音面试功能实现，采用"前端标准封装 + WebSocket流式中转 + 云端ASR + 本地大模型异步打分"的架构，完全抛弃了复杂的"FFmpeg+Vosk+HTTP切片"模式。

## 核心特性

- ✅ **实时流式转写**: WebSocket全双工通信，500ms低延迟
- ✅ **阿里云ASR集成**: 专业语音识别，准确率>95%
- ✅ **本地LLM评分**: 实时智能评价和追问决策
- ✅ **前端标准录音**: RecordRTC直接输出WAV格式
- ✅ **高并发支持**: 支持20个面试间同时运行

## 项目结构

```
voice_interview_streaming/
├── __init__.py                 # 包初始化
├── websocket_server.py        # WebSocket服务器核心
├── asr_client.py              # 阿里云ASR客户端
├── realtime_scorer.py         # 实时评分器
├── api_routes.py              # REST API路由
├── database_setup.py          # 数据库初始化
├── service_launcher.py        # 服务启动器
└── README.md                  # 项目说明
```

## 快速开始

### 1. 配置环境变量
```bash
export ALIYUN_ASR_APPKEY="your_appkey"
export ALIYUN_ASR_TOKEN="your_token"
```

### 2. 初始化数据库
```bash
python database_setup.py
```

### 3. 启动服务
```bash
python service_launcher.py
```

### 4. 前端集成
```vue
<VoiceInterviewRecorder
  :session-id="sessionId"
  :question-id="questionId"
  :websocket-url="'ws://localhost:8765'"
/>
```

## 技术架构

### 前端组件
- **录音**: RecordRTC (16K WAV格式)
- **通信**: WebSocket (500ms片段发送)
- **UI**: Vue3 + TypeScript

### 后端服务
- **WebSocket服务器**: 处理音频流和控制消息
- **ASR客户端**: 封装阿里云实时语音识别
- **评分器**: 本地LLM实时评价
- **API接口**: RESTful会话管理和统计

### 外部依赖
- **阿里云ASR**: 云端语音识别服务
- **本地LLM**: 复用现有模型服务
- **PostgreSQL**: 数据持久化存储

## API接口

### 创建会话
```
POST /api/v1/voice-interview/session
```

### 获取会话状态
```
GET /api/v1/voice-interview/session/{session_id}
```

### 健康检查
```
GET /api/v1/voice-interview/health
```

## 配置说明

详见 `config.yaml` 中的 `voice_interview_streaming` 配置段。

关键配置:
- WebSocket服务端口: 8765
- 音频采样率: 16000Hz
- 追问分数阈值: 60分

## 部署文档

- 📖 [实现说明文档](../doc/实现说明文档.md)
- 🚀 [部署指南](../doc/部署指南.md)
- ⚡ [快速开始指南](../doc/快速开始指南.md)

## 注意事项

1. **独立功能**: 此功能与现有语音功能完全独立，可安全删除
2. **资源要求**: 需要阿里云ASR服务和本地LLM服务
3. **并发限制**: 默认支持20并发连接，可根据服务器性能调整
4. **安全考虑**: 生产环境建议配置WSS和权限验证

## 开发团队

DataStoreWare Team

## 许可证

内部项目专用