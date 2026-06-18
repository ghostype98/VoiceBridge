"""
数据库连接管理模块
"""
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from typing import Optional, Generator
from loguru import logger

from app.database.config import db_config


class DatabaseManager:
    """数据库连接管理器（使用连接池）"""
    
    def __init__(self):
        self._connection_pool: Optional[pool.ThreadedConnectionPool] = None
        # 根据配置确定数据库类型（目前支持PostgreSQL）
        self.db_type = "postgresql"  # 从连接参数推断
        self._initialize_pool()
    
    def _initialize_pool(self):
        """初始化连接池"""
        try:
            params = db_config.connection_params
            self._connection_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=db_config._pool_size + db_config._max_overflow,
                host=params["host"],
                port=params["port"],
                database=params["database"],
                user=params["user"],
                password=params["password"]
            )
            logger.info(f"数据库连接池初始化成功: {params['host']}:{params['port']}/{params['database']}")
        except Exception as e:
            logger.error(f"数据库连接池初始化失败: {e}")
            raise
    
    @contextmanager
    def get_connection(self) -> Generator:
        """获取数据库连接（上下文管理器）"""
        conn = None
        try:
            conn = self._connection_pool.getconn()
            yield conn
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            if conn:
                self._connection_pool.putconn(conn)
    
    def execute_query(self, query: str, params: tuple = None) -> list:
        """执行查询并返回结果列表"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
    
    def execute_one(self, query: str, params: tuple = None) -> Optional[dict]:
        """执行查询并返回单条结果"""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()
    
    def execute_update(self, query: str, params: tuple = None) -> int:
        """执行更新/插入/删除操作，返回受影响的行数"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                conn.commit()
                return cursor.rowcount
    
    def execute_insert(self, query: str, params: tuple = None) -> Optional[str]:
        """执行插入操作，返回插入的ID"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                conn.commit()
                return cursor.fetchone()[0] if cursor.rowcount > 0 else None
    
    def execute(self, query: str, params: tuple = None) -> None:
        """执行SQL语句（用于DDL操作，如CREATE TABLE）"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                conn.commit()

    def fetch_one(self, query: str, params: tuple = None) -> Optional[dict]:
        """获取单条查询结果（兼容性方法）"""
        return self.execute_one(query, params)

    def close(self):
        """关闭连接池"""
        if self._connection_pool:
            self._connection_pool.closeall()
            logger.info("数据库连接池已关闭")


# 全局数据库管理器实例
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器实例（单例模式）"""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager

