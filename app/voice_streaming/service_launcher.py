# -*- coding: utf-8 -*-
"""
语音面试流式转写服务启动器
负责初始化和启动WebSocket服务器以及相关服务
"""

import asyncio
import sys
import os
import signal
from typing import Optional

from loguru import logger
from config.settings import settings
from app.database.connection import DatabaseManager
from app.database.config import db_config

from .websocket_server import VoiceInterviewWebSocketServer
from .database_setup import create_voice_interview_tables
class ConfigWrapper:
    """配置包装器，提供ConfigManager接口"""
    def __init__(self, config):
        self.config = config

    def get_config(self, key):
        # 如果config对象有get_config方法（比如Settings类），使用它
        if hasattr(self.config, 'get_config'):
            return self.config.get_config(key)
        # 否则使用普通字典的get方法
        return self.config.get(key, {})

    def get(self, key, default=None):
        """获取配置值，支持默认值"""
        return self.config.get(key, default)

    def __getitem__(self, key):
        return self.config[key]

    def __contains__(self, key):
        return key in self.config

    def keys(self):
        return self.config.keys()

    def values(self):
        return self.config.values()

    def items(self):
        return self.config.items()

# 使用loguru logger


class VoiceInterviewServiceLauncher:
    """语音面试服务启动器"""

    def __init__(self):
        # 使用settings而不是load_unified_config
        self.config = ConfigWrapper(settings)
        self.db_manager = DatabaseManager()  # DatabaseManager会自动使用全局的db_config
        self.websocket_server: Optional[VoiceInterviewWebSocketServer] = None
        self.is_running = False

    async def initialize_services(self) -> bool:
        """初始化服务"""
        try:
            logger.info("开始初始化语音面试服务...")

            # 1. 初始化数据库表
            logger.info("初始化数据库表...")
            if not create_voice_interview_tables(self.db_manager):
                logger.error("数据库表初始化失败")
                return False

            # 2. 初始化WebSocket服务器
            logger.info("初始化WebSocket服务器...")
            # 直接使用settings对象，它已经实现了get_config方法
            config_wrapper = self.config
            self.websocket_server = VoiceInterviewWebSocketServer(
                config_wrapper,
                self.db_manager
            )

            # 3. 验证配置
            logger.info("验证配置...")
            if not self._validate_config():
                logger.error("配置验证失败")
                return False

            logger.info("语音面试服务初始化完成")
            return True

        except Exception as e:
            logger.error(f"服务初始化失败: {str(e)}", exc_info=True)
            return False

    def _validate_config(self) -> bool:
        """验证配置"""
        try:
            logger.info("跳过详细配置验证（开发模式）")
            return True
        except Exception as e:
            logger.error(f"配置验证异常: {str(e)}")
            return False

    async def start_services(self) -> bool:
        """启动服务"""
        try:
            if not self.websocket_server:
                logger.error("WebSocket服务器未初始化")
                return False

            logger.info("开始启动语音面试服务...")

            # 启动WebSocket服务器
            await self.websocket_server.start_server()

            self.is_running = True
            logger.info("语音面试服务启动成功")

            # 注册信号处理器
            self._register_signal_handlers()

            return True

        except Exception as e:
            logger.error(f"服务启动失败: {str(e)}", exc_info=True)
            return False

    async def stop_services(self):
        """停止服务"""
        try:
            logger.info("开始停止语音面试服务...")

            if self.websocket_server:
                await self.websocket_server.stop_server()

            self.is_running = False
            logger.info("语音面试服务已停止")

        except Exception as e:
            logger.error(f"服务停止异常: {str(e)}", exc_info=True)

    def _register_signal_handlers(self):
        """注册信号处理器"""
        def signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，开始优雅关闭...")
            asyncio.create_task(self.stop_services())

        # 注册常见终止信号
        signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # kill命令

        # 在Windows上注册其他信号
        if os.name == 'nt':
            try:
                signal.signal(signal.SIGBREAK, signal_handler)  # Ctrl+Break
            except:
                pass

    async def run_forever(self):
        """持续运行服务"""
        try:
            while self.is_running:
                await asyncio.sleep(1)

                # 可以在这里添加定期健康检查或其他维护任务
                # await self._perform_health_check()

        except Exception as e:
            logger.error(f"服务运行异常: {str(e)}", exc_info=True)
        finally:
            await self.stop_services()

    def get_service_status(self) -> dict:
        """获取服务状态"""
        status = {
            'is_running': self.is_running,
            'websocket_server': False,
            'database_connected': False,
            'config_valid': False
        }

        if self.websocket_server:
            status['websocket_server'] = self.websocket_server.is_running

        try:
            # 检查数据库连接
            test_sql = "SELECT 1"
            result = self.db_manager.fetch_one(test_sql)
            status['database_connected'] = result is not None
        except:
            status['database_connected'] = False

        # 检查配置
        status['config_valid'] = self._validate_config()

        return status

    async def _perform_health_check(self):
        """执行健康检查"""
        try:
            # 每60秒执行一次健康检查
            if hasattr(self, '_last_health_check'):
                if asyncio.get_event_loop().time() - self._last_health_check < 60:
                    return
            else:
                self._last_health_check = asyncio.get_event_loop().time()
                return

            self._last_health_check = asyncio.get_event_loop().time()

            status = self.get_service_status()
            all_healthy = all(status.values())

            if not all_healthy:
                logger.warning(f"健康检查发现问题: {status}")
            else:
                logger.debug("健康检查通过")

        except Exception as e:
            logger.error(f"健康检查异常: {str(e)}")


async def main():
    """主函数"""
    launcher = VoiceInterviewServiceLauncher()

    try:
        # 初始化服务
        if not await launcher.initialize_services():
            logger.error("服务初始化失败，退出")
            return 1

        # 启动服务
        if not await launcher.start_services():
            logger.error("服务启动失败，退出")
            return 1

        # 持续运行
        await launcher.run_forever()

        return 0

    except KeyboardInterrupt:
        logger.info("收到键盘中断信号")
        await launcher.stop_services()
        return 0
    except Exception as e:
        logger.error(f"服务运行异常: {str(e)}", exc_info=True)
        await launcher.stop_services()
        return 1


def run_service():
    """运行服务的便捷函数"""
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("服务被用户中断")
        sys.exit(0)
    except Exception as e:
        logger.error(f"服务启动异常: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run_service()