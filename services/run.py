'''
Author: gaofei sdhd_gaofei@163.com
Date: 2025-12-18 13:38:55
LastEditors: gaofei sdhd_gaofei@163.com
LastEditTime: 2026-01-20 16:39:11
FilePath: /VoiceBridge/services/run.py
Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
服务启动脚本
注意：请确保在 voice conda 环境中运行此脚本
"""
import uvicorn
from loguru import logger
import sys
import os
import socket

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config.settings import settings


def _create_reuse_socket():
    """创建带 SO_REUSEADDR 的 socket，避免重启时端口 TIME_WAIT 导致 bind 失败"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass  # Windows 等无 SO_REUSEPORT
    sock.bind((settings.HOST, settings.PORT))
    sock.listen(100)
    sock.set_inheritable(True)
    return sock

if __name__ == "__main__":
    # 检查是否在正确的环境中
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    logger.info(f"Python版本: {sys.version}")
    logger.info(f"Python路径: {sys.executable}")
    
    if "voice" not in sys.executable:
        logger.warning("警告：当前不在 voice conda 环境中！")
        logger.warning(f"请运行: conda activate voice")
        logger.warning(f"然后运行: python run.py")
    
    logger.info("正在启动语音交互服务...")
    logger.info(f"监听地址: {settings.HOST}:{settings.PORT}")
    logger.info(f"对话管理服务端口: {settings.RASA_PORT}")
    logger.info(f"调试模式: {settings.DEBUG}")
    logger.info(f"日志级别: {settings.LOG_LEVEL}")
    
    try:
        if settings.DEBUG:
            # reload 模式下不用 fd，避免子进程继承问题
            uvicorn.run(
                "app.main:app",
                host=settings.HOST,
                port=settings.PORT,
                reload=True,
                log_level=settings.LOG_LEVEL.lower()
            )
        else:
            reuse_sock = _create_reuse_socket()
            fd = reuse_sock.fileno()
            logger.info(f"已绑定端口 {settings.PORT} (SO_REUSEADDR)，fd={fd}")
            uvicorn.run(
                "app.main:app",
                fd=fd,
                log_level=settings.LOG_LEVEL.lower()
            )
    except KeyboardInterrupt:
        logger.info("服务已停止")
        sys.exit(0)
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
        sys.exit(1)

