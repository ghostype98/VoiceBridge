# VoiceBridge 脚本工具集

本目录包含 VoiceBridge 语音面试系统的所有实用脚本和工具。

## 📁 目录结构

```
scripts/Voice/
├── README.md                    # 本说明文档
├── backup/                      # 备份文件目录
│   └── *.backup*               # 各种备份文件
├── 服务管理脚本/
│   ├── start_service.sh         # 启动主服务
│   ├── stop_service.sh          # 停止主服务
│   ├── restart_service.sh       # 重启主服务
│   ├── status_service.sh        # 查看服务状态
│   └── manage_services.sh       # 综合服务管理脚本
├── TTS服务脚本/
│   ├── start_tts_service.sh     # 启动TTS服务
│   └── start_tts_systemd.sh    # systemd方式启动TTS
├── 工具脚本/
│   ├── download_models.sh       # 下载语音模型
│   └── generate_ssl_cert.sh     # 生成SSL证书
└── 测试脚本/
    ├── test_service_start.py     # 测试服务启动
    ├── test_scoring.py          # 测试评分功能
    ├── test_llm_parsing.py      # 测试LLM解析
    └── test_llm_database_storage.py  # 测试数据库存储
```

## 🚀 服务管理脚本

### start_service.sh
启动 VoiceBridge 主服务（后台运行）

**使用方法：**
```bash
bash scripts/Voice/start_service.sh
```

**功能：**
- 自动激活 conda 环境（datastore）
- 后台运行服务，不阻塞终端
- 自动创建 PID 文件，防止重复启动
- 日志保存到 `logs/service_YYYYMMDD_HHMMSS.log`

**注意事项：**
- 服务在后台运行，使用 nohup
- 不会影响 Cursor SSH 连接
- 前端页面立即可访问：http://localhost:8010/login

### stop_service.sh
停止 VoiceBridge 主服务

**使用方法：**
```bash
bash scripts/Voice/stop_service.sh
```

**功能：**
- 优雅停止服务（先发送 SIGTERM）
- 10秒后仍未停止则强制停止（SIGKILL）
- 自动清理 PID 文件

### restart_service.sh
重启 VoiceBridge 主服务

**使用方法：**
```bash
bash scripts/Voice/restart_service.sh
```

**功能：**
- 先停止服务，再启动服务
- 自动检查服务状态

### status_service.sh
查看服务运行状态

**使用方法：**
```bash
bash scripts/Voice/status_service.sh
```

**功能：**
- 显示服务进程信息
- 显示端口监听状态（8010, 8765）
- 显示最新日志内容

### manage_services.sh
综合服务管理脚本（推荐使用）

**使用方法：**
```bash
# 启动所有服务
bash scripts/Voice/manage_services.sh start

# 停止所有服务
bash scripts/Voice/manage_services.sh stop

# 重启所有服务
bash scripts/Voice/manage_services.sh restart

# 查看服务状态
bash scripts/Voice/manage_services.sh status

# 查看服务日志
bash scripts/Voice/manage_services.sh logs
bash scripts/Voice/manage_services.sh logs tts
bash scripts/Voice/manage_services.sh logs voicebridge

# 前台运行（开发模式）
bash scripts/Voice/manage_services.sh dev

# 显示帮助
bash scripts/Voice/manage_services.sh help
```

**功能：**
- 统一管理所有服务（LLM、TTS、主服务）
- 自动检查服务依赖关系
- 检查 LLM 服务连通性
- 启动 TTS 服务（端口 8011）
- 启动主应用服务（端口 8010）
- 验证服务健康状态
- 完整的日志管理

## 🎤 TTS 服务脚本

### start_tts_service.sh
启动 TTS（文本转语音）服务

**使用方法：**
```bash
# 先激活 tts conda 环境
conda activate tts

# 启动服务
bash scripts/Voice/start_tts_service.sh
```

**功能：**
- 在独立的 conda 环境（tts）中运行
- 默认端口：8001
- 支持环境变量配置：TTS_HOST, TTS_PORT

### start_tts_systemd.sh
使用 systemd 方式启动 TTS 服务

**使用方法：**
```bash
bash scripts/Voice/start_tts_systemd.sh
```

**功能：**
- 配置 systemd 服务单元
- 支持开机自启动
- 自动重启管理

## 🛠️ 工具脚本

### download_models.sh
下载语音识别和合成所需的模型文件

**使用方法：**
```bash
bash scripts/Voice/download_models.sh
```

**功能：**
- 下载 Vosk 中文语音识别模型
- 自动创建 models 目录
- Whisper 和 Coqui TTS 模型会在首次使用时自动下载

**下载的模型：**
- Vosk 中文模型：`vosk-model-cn-0.22`
- 保存位置：`models/vosk-model-cn-0.22/`

### generate_ssl_cert.sh
生成自签名 SSL 证书，支持 HTTPS 访问

**使用方法：**
```bash
bash scripts/Voice/generate_ssl_cert.sh
```

**功能：**
- 生成自签名 SSL 证书（有效期 365 天）
- 自动更新配置文件
- 证书保存到 `ssl/` 目录

**生成的文件：**
- `ssl/voicebridge.crt` - 证书文件
- `ssl/voicebridge.key` - 私钥文件

**注意事项：**
- 自签名证书仅用于开发测试
- 生产环境请使用正式 CA 签发的证书

## 🧪 测试脚本

### test_service_start.py
测试服务启动功能

**使用方法：**
```bash
python scripts/Voice/test_service_start.py
```

**功能：**
- 验证服务启动脚本是否正常工作
- 检查服务进程状态
- 测试 HTTP 服务响应
- 检查端口监听状态

### test_scoring.py
测试评分功能

**使用方法：**
```bash
python scripts/Voice/test_scoring.py
```

**功能：**
- 测试实时评分器功能
- 验证评分逻辑
- 测试数据库存储

### test_llm_parsing.py
测试 LLM 响应解析

**使用方法：**
```bash
python scripts/Voice/test_llm_parsing.py
```

**功能：**
- 测试 LLM 响应解析逻辑
- 验证 JSON 格式提取
- 测试错误处理

### test_llm_database_storage.py
测试 LLM 输出数据库存储

**使用方法：**
```bash
python scripts/Voice/test_llm_database_storage.py
```

**功能：**
- 测试单题评分数据库存储
- 测试 21 维度评估数据库存储
- 验证数据完整性

## 📦 备份文件

所有备份文件已统一移动到 `backup/` 目录，包括：

- 源代码备份文件（`.backup`, `.backup_*`）
- 配置文件备份
- 历史版本备份

**备份文件命名规则：**
- `*.backup` - 通用备份
- `*.backup_YYYYMMDD_HHMMSS` - 带时间戳的备份
- `*.backup_safe` - 安全备份
- `*.backup_complete` - 完整备份

## 🔧 环境要求

### 必需环境
- Python 3.10+
- conda/miniconda
- conda 环境：`datastore`（主服务）、`tts`（TTS服务）

### 系统工具
- bash
- openssl（用于生成 SSL 证书）
- lsof/fuser（用于端口检查，可选）

## 📝 使用示例

### 完整启动流程

```bash
# 1. 进入项目目录
cd /opt/voicebridge

# 2. 启动所有服务（推荐）
bash scripts/Voice/manage_services.sh start

# 3. 检查服务状态
bash scripts/Voice/manage_services.sh status

# 4. 查看日志
bash scripts/Voice/manage_services.sh logs
```

### 单独启动主服务

```bash
# 启动
bash scripts/Voice/start_service.sh

# 查看状态
bash scripts/Voice/status_service.sh

# 停止
bash scripts/Voice/stop_service.sh
```

### 开发调试

```bash
# 前台运行（开发模式）
bash scripts/Voice/manage_services.sh dev

# 运行测试
python scripts/Voice/test_service_start.py
python scripts/Voice/test_scoring.py
```

## 🌐 服务访问地址

启动服务后，可通过以下地址访问：

- **前端登录页面**: http://localhost:8010/login
- **面试页面**: http://localhost:8010/interview
- **API 文档**: http://localhost:8010/docs
- **ReDoc 文档**: http://localhost:8010/redoc
- **健康检查**: http://localhost:8010/health
- **TTS 服务**: http://localhost:8011/health
- **WebSocket**: ws://localhost:8765

## ⚠️ 注意事项

1. **服务端口**
   - 主服务：8010（HTTP）、8765（WebSocket）
   - TTS 服务：8011
   - 确保端口未被占用

2. **环境变量**
   - 确保 conda 环境已正确配置
   - 检查 LLM 服务配置和连通性

3. **日志文件**
   - 日志保存在 `logs/` 目录
   - 日志文件会持续增长，建议定期清理

4. **PID 文件**
   - 服务启动后会在项目根目录创建 `voicebridge.pid`
   - 不要手动删除 PID 文件

5. **备份文件**
   - 备份文件已统一管理在 `backup/` 目录
   - 可根据需要清理旧备份

## 📚 相关文档

- 服务启动指南：`docs/部署/服务启动指南.md`
- systemd 部署指南：`docs/部署/systemd部署指南.md`
- 服务管理文档：`SERVICE_MANAGEMENT.md`

## 🔄 更新历史

- 2026-02-04: 整理脚本目录，统一管理到 `scripts/Voice/`
- 2026-02-03: 改进服务启动脚本，支持后台运行和 PID 管理

---

**维护者**: VoiceBridge 开发团队  
**最后更新**: 2026-02-04

