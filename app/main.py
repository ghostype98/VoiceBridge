"""
FastAPI主应用入口
"""
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger
import json
import sys
import os

from config.settings import settings
from app.voice_streaming.api_routes import router as voice_streaming_router

# 其他路由
from app.routers import dialogue, integration, interview_session, interview_flow, auth

# 配置日志
logger.remove()

# 全局变量
_voice_streaming_launcher = None
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level=settings.LOG_LEVEL
)
logger.add(
    settings.LOG_FILE,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level=settings.LOG_LEVEL
)

# 创建FastAPI应用
app = FastAPI(
    title="VoiceBridge - 语音与文字双向转换服务",
    description="VoiceBridge: ASR（语音转文字）+ 对话管理 + TTS（文字转语音）三层架构，实现语音与文字之间的无缝双向转换",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 前后端一体：同一端口（见 settings.VOICEBRIDGE_PORT）提供 API + 前端静态，无需单独启动前端
# 若存在 frontend 目录则挂载静态，提供 /、/login、/interview 等

# 注册 API 与 WebSocket 路由（在前端路由之后，避免覆盖）
app.include_router(voice_streaming_router, tags=["Voice Streaming"])
app.include_router(dialogue.router, prefix="/api/v1/dialogue", tags=["对话管理"])
app.include_router(integration.router, prefix="/api/v1/integration", tags=["集成扩展"])
app.include_router(auth.router, tags=["用户认证"])
app.include_router(interview_session.router, tags=["面试会话管理"])
app.include_router(interview_flow.router, tags=["面试流程"])

@app.websocket("/ws/asr")
async def websocket_asr(websocket: WebSocket):
    """WebSocket ASR实时语音转文字端点"""
    global _voice_streaming_launcher
    if not _voice_streaming_launcher or not _voice_streaming_launcher.websocket_server:
        logger.error("语音流式服务未初始化，无法处理WebSocket连接")
        await websocket.accept()
        await websocket.close(code=1003, reason="语音流式服务未初始化")
        return
    await _voice_streaming_launcher.websocket_server.handle_fastapi_websocket(websocket)


@app.on_event("startup")
async def startup_event():
    """启动事件"""
    logger.info("=" * 60)
    logger.info("VoiceBridge 语音交互服务启动")
    logger.info("=" * 60)
    logger.info(f"主服务地址: http://{settings.VOICEBRIDGE_HOST}:{settings.VOICEBRIDGE_PORT}")
    logger.info(f"单端口 {settings.VOICEBRIDGE_PORT}：API + 前端一体，内网穿透只暴露此端口即可")

    # 启动语音流式服务
    try:
        logger.info("正在启动语音流式服务...")
        from app.voice_streaming.service_launcher import VoiceInterviewServiceLauncher

        # 创建全局服务实例
        global _voice_streaming_launcher
        _voice_streaming_launcher = VoiceInterviewServiceLauncher()

        # 初始化服务
        if not await _voice_streaming_launcher.initialize_services():
            logger.error("语音流式服务初始化失败")
            raise Exception("语音流式服务初始化失败")

        # 启动服务
        if not await _voice_streaming_launcher.start_services():
            logger.error("语音流式服务启动失败")
            raise Exception("语音流式服务启动失败")

        logger.info("语音流式服务启动成功")
        logger.info(f"WebSocket服务地址: ws://{settings.VOICEBRIDGE_HOST}:{settings.VOICE_STREAMING_WEBSOCKET_PORT}")

    except Exception as e:
        logger.error(f"语音流式服务启动异常: {e}")
        # 不阻断主服务启动，但记录错误
        pass
    logger.info(f"API文档: http://{settings.VOICEBRIDGE_HOST}:{settings.VOICEBRIDGE_PORT}/docs")
    logger.info(f"部署模式: {settings.DEPLOYMENT_MODE}")
    logger.info("语音识别: 阿里云ASR (流式服务)")
    logger.info("TTS功能已屏蔽")
    logger.info(f"对话管理服务端口: {settings.RASA_PORT}")
    logger.info(f"对话管理服务地址: {settings.RASA_ENDPOINT}")
    logger.info(f"日志级别: {settings.LOG_LEVEL}")
    logger.info(f"日志文件: {settings.LOG_FILE}")
    logger.info(f"Rasa日志文件: {settings.RASA_LOG_FILE}")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """关闭事件"""
    logger.info("VoiceBridge 服务正在关闭...")

    # 停止语音流式服务
    try:
        global _voice_streaming_launcher
        if _voice_streaming_launcher:
            await _voice_streaming_launcher.stop_services()
            logger.info("语音流式服务已停止")
    except Exception as e:
        logger.error(f"语音流式服务停止异常: {e}")

    logger.info("VoiceBridge 服务已关闭")


# API 信息（前后端分离时由前端代理访问）
@app.get("/api", include_in_schema=False)
async def api_info():
    """API信息"""
    return {
        "service": "VoiceBridge",
        "name": "语音与文字双向转换服务",
        "version": "1.0.0",
        "deployment_mode": settings.DEPLOYMENT_MODE,
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "VoiceBridge"}


# 前端静态：与主服务同端口提供页面，无需单独起前端进程
_FRONTEND_DIR = os.path.join(settings.PROJECT_ROOT, "frontend")
_FRONTEND_HTML = {
    "/": "login.html",
    "/login": "login.html",
    "/interview": "interview.html",
    "/desktop-interview": "desktop-interview.html",
    "/mobile-interview": "mobile-interview.html",
    "/mobile-debug": "mobile-debug.html",
    "/mobile-layout-debug": "mobile-layout-debug.html",
}

# 面试页需注入与 config.yaml 一致的 UI 开关，避免仅依赖 API（NATAPP/缓存/旧版后端时仍生效）
_INTERVIEW_HTML_INJECT = frozenset({"interview.html", "desktop-interview.html", "mobile-interview.html"})


def _inject_interview_ui_into_html(html: str) -> str:
    payload = json.dumps({"show_asr_text": bool(settings.INTERVIEW_UI_SHOW_ASR_TEXT)}, ensure_ascii=False)
    script = f"<script>window.__INTERVIEW_UI__={payload};</script>"
    if "</head>" in html:
        return html.replace("</head>", script + "</head>", 1)
    return script + html


if os.path.isdir(_FRONTEND_DIR):
    def _make_serve(path: str, filename: str):
        p = os.path.join(_FRONTEND_DIR, filename)
        if not os.path.isfile(p):
            return None
        p_abs = os.path.abspath(p)

        if filename in _INTERVIEW_HTML_INJECT:
            async def _serve_injected():
                with open(p_abs, "r", encoding="utf-8") as f:
                    body = _inject_interview_ui_into_html(f.read())
                return HTMLResponse(content=body, media_type="text/html; charset=utf-8")

            return _serve_injected

        async def _serve():
            return FileResponse(p_abs)

        return _serve

    # 映射页面路由（/、/login、/mobile-interview 等）
    for _path, _file in _FRONTEND_HTML.items():
        _handler = _make_serve(_path, _file)
        if _handler is not None:
            app.get(_path, include_in_schema=False)(_handler)

    # 兼容老的 /static/... 路径，比如 /static/js/api.js、/static/css/mobile-interview.css
    # 将 /static 前缀映射到 frontend 目录
    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR, html=False), name="frontend-static-legacy")

    # 同时挂载根路径静态资源（/js、/css 等）
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend-static")


if __name__ == "__main__":
    import uvicorn

    # SSL配置
    ssl_kwargs = {}
    if settings.SSL_ENABLED:
        if not settings.SSL_CERTFILE or not settings.SSL_KEYFILE:
            logger.error("SSL已启用但证书文件未配置")
            logger.error("请在config.yaml中设置ssl.certfile和ssl.keyfile")
            exit(1)

        ssl_kwargs = {
            "ssl_certfile": settings.SSL_CERTFILE,
            "ssl_keyfile": settings.SSL_KEYFILE,
        }

        if settings.SSL_CA_CERTS:
            ssl_kwargs["ssl_ca_certs"] = settings.SSL_CA_CERTS

        logger.info("SSL模式已启用")

    uvicorn.run(
        "app.main:app",
        host=settings.VOICEBRIDGE_HOST,
        port=settings.VOICEBRIDGE_PORT,
        reload=settings.VOICEBRIDGE_RELOAD,
        **ssl_kwargs
    )

