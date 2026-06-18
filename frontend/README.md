# VoiceBridge 前端（前后端分离）

- **主服务端口**: 以 config/config.yaml 为准（默认 8002），API + 前端一体
- **本目录**: 生产由主服务挂载静态；开发时可 `npm run dev`（端口默认 8002）
- **内网穿透**: 暴露主服务端口即可

## 使用

```bash
# 首次运行安装依赖
npm install

# 启动前端（先确保后端已启动: bash ../bash/start_service.sh）
npm run dev
```

访问 http://localhost:8002/login 即可使用（端口以 config/config.yaml 为准）；经 natapp 暴露该端口后，外网也只需访问同一地址。

## 代理说明

| 路径     | 代理目标        |
|----------|-----------------|
| `/api/*` | http://127.0.0.1:8002 |
| `/ws/*`  | ws://127.0.0.1:8002   |
| `/health`, `/docs`, `/redoc` | 同上 |

静态页面与 `/static/*` 由 Vite 直接提供。
