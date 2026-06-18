# -*- coding: utf-8 -*-
"""
语音面试流式转写数据库表创建脚本
"""

import sys
import os
from typing import List

# 添加backend到Python路径
_current_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.join(_current_dir, '..')
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from shared.config.logging_config import get_logger
from shared.tools.database.db_manager import DatabaseManager

logger = get_logger(__name__)


def create_voice_interview_tables(db_manager: DatabaseManager) -> bool:
    """创建语音面试相关的数据库表"""
    try:
        # 语音面试会话表
        voice_interview_sessions_sql = """
        CREATE TABLE IF NOT EXISTS voice_interview_sessions (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(100) UNIQUE NOT NULL,
            invitation_id VARCHAR(100) NOT NULL,
            question_id VARCHAR(100) NOT NULL,
            status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'error')),
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """

        # 如果是PostgreSQL，使用不同的语法
        if db_manager.db_type == 'postgresql':
            voice_interview_sessions_sql = """
            CREATE TABLE IF NOT EXISTS voice_interview_sessions (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100) UNIQUE NOT NULL,
                invitation_id VARCHAR(100) NOT NULL,
                question_id VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'error')),
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                CONSTRAINT uk_session_id UNIQUE (session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_voice_sessions_invitation ON voice_interview_sessions(invitation_id);
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_question ON voice_interview_sessions(question_id);
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_status ON voice_interview_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_start_time ON voice_interview_sessions(start_time);
            """

            # 添加注释（PostgreSQL方式）
            comment_sql = """
            COMMENT ON TABLE voice_interview_sessions IS '语音面试会话表';
            COMMENT ON COLUMN voice_interview_sessions.session_id IS '会话ID';
            COMMENT ON COLUMN voice_interview_sessions.invitation_id IS '面试邀请ID';
            COMMENT ON COLUMN voice_interview_sessions.question_id IS '题目ID';
            COMMENT ON COLUMN voice_interview_sessions.status IS '会话状态';
            COMMENT ON COLUMN voice_interview_sessions.start_time IS '开始时间';
            COMMENT ON COLUMN voice_interview_sessions.end_time IS '结束时间';
            """
        else:
            # MySQL需要先创建表，然后添加注释
            pass

        # 语音评价结果表
        voice_evaluation_results_sql = """
        CREATE TABLE IF NOT EXISTS voice_evaluation_results (
            id VARCHAR(100) PRIMARY KEY,
            session_id VARCHAR(100) NOT NULL,
            question_id VARCHAR(100) NOT NULL,
            answer_text TEXT NOT NULL,
            score DECIMAL(5,2) NOT NULL,
            reason TEXT,
            evaluation_details JSONB,
            need_follow_up BOOLEAN DEFAULT FALSE,
            follow_up_question TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """

        if db_manager.db_type == 'postgresql':
            voice_evaluation_results_sql = """
            CREATE TABLE IF NOT EXISTS voice_evaluation_results (
                id VARCHAR(100) PRIMARY KEY,
                session_id VARCHAR(100) NOT NULL,
                question_id VARCHAR(100) NOT NULL,
                answer_text TEXT NOT NULL,
                score DECIMAL(5,2) NOT NULL,
                reason TEXT,
                evaluation_details JSONB,
                need_follow_up BOOLEAN DEFAULT FALSE,
                follow_up_question TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_voice_eval_session ON voice_evaluation_results(session_id);
            CREATE INDEX IF NOT EXISTS idx_voice_eval_question ON voice_evaluation_results(question_id);
            CREATE INDEX IF NOT EXISTS idx_voice_eval_score ON voice_evaluation_results(score);
            CREATE INDEX IF NOT EXISTS idx_voice_eval_follow_up ON voice_evaluation_results(need_follow_up);
            CREATE INDEX IF NOT EXISTS idx_voice_eval_created_at ON voice_evaluation_results(created_at);
            """

            comment_sql += """
            COMMENT ON TABLE voice_evaluation_results IS '语音评价结果表';
            COMMENT ON COLUMN voice_evaluation_results.session_id IS '会话ID';
            COMMENT ON COLUMN voice_evaluation_results.question_id IS '题目ID';
            COMMENT ON COLUMN voice_evaluation_results.answer_text IS '回答文本';
            COMMENT ON COLUMN voice_evaluation_results.score IS '评分（0-100）';
            COMMENT ON COLUMN voice_evaluation_results.reason IS '评分理由';
            COMMENT ON COLUMN voice_evaluation_results.evaluation_details IS '详细评价信息';
            COMMENT ON COLUMN voice_evaluation_results.need_follow_up IS '是否需要追问';
            COMMENT ON COLUMN voice_evaluation_results.follow_up_question IS '追问问题';
            """

        # 执行表创建
        logger.info("开始创建语音面试数据库表...")

        db_manager.execute(voice_interview_sessions_sql)
        logger.info("voice_interview_sessions表创建成功")

        db_manager.execute(voice_evaluation_results_sql)
        logger.info("voice_evaluation_results表创建成功")

        # 如果是PostgreSQL，执行注释
        if db_manager.db_type == 'postgresql':
            db_manager.execute(comment_sql)
            logger.info("表注释添加成功")

        # 验证表是否创建成功
        tables_to_check = ['voice_interview_sessions', 'voice_evaluation_results']
        for table_name in tables_to_check:
            try:
                # 尝试查询表中的记录数来验证表是否存在
                test_sql = f"SELECT COUNT(*) as count FROM {table_name} LIMIT 1"
                result = db_manager.fetch_one(test_sql)
                if result is None:
                    raise Exception(f"表 {table_name} 创建失败")
                logger.info(f"表 {table_name} 验证成功")
            except Exception as e:
                logger.error(f"表 {table_name} 验证失败: {str(e)}")
                raise Exception(f"表 {table_name} 创建失败")

        logger.info("语音面试数据库表创建完成")
        return True

    except Exception as e:
        logger.error(f"创建语音面试数据库表失败: {str(e)}", exc_info=True)
        return False


# 删除表的函数已移除，现在使用现有的interview_session表


def get_table_creation_sql(db_type: str = 'mysql') -> List[str]:
    """获取表创建SQL语句列表"""
    sqls = []

    if db_type == 'postgresql':
        sqls.extend([
            """
            CREATE TABLE IF NOT EXISTS voice_interview_sessions (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100) UNIQUE NOT NULL,
                invitation_id VARCHAR(100) NOT NULL,
                question_id VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'error')),
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS voice_evaluation_results (
                id VARCHAR(100) PRIMARY KEY,
                session_id VARCHAR(100) NOT NULL,
                question_id VARCHAR(100) NOT NULL,
                answer_text TEXT NOT NULL,
                score DECIMAL(5,2) NOT NULL,
                reason TEXT,
                evaluation_details JSONB,
                need_follow_up BOOLEAN DEFAULT FALSE,
                follow_up_question TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            # 索引
            "CREATE INDEX IF NOT EXISTS idx_voice_sessions_invitation ON voice_interview_sessions(invitation_id);",
            "CREATE INDEX IF NOT EXISTS idx_voice_sessions_question ON voice_interview_sessions(question_id);",
            "CREATE INDEX IF NOT EXISTS idx_voice_sessions_status ON voice_interview_sessions(status);",
            "CREATE INDEX IF NOT EXISTS idx_voice_eval_session ON voice_evaluation_results(session_id);",
            "CREATE INDEX IF NOT EXISTS idx_voice_eval_question ON voice_evaluation_results(question_id);"
        ])
    else:  # MySQL
        sqls.extend([
            """
            CREATE TABLE IF NOT EXISTS voice_interview_sessions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(100) UNIQUE NOT NULL COMMENT '会话ID',
                invitation_id VARCHAR(100) NOT NULL COMMENT '面试邀请ID',
                question_id VARCHAR(100) NOT NULL COMMENT '题目ID',
                status ENUM('active', 'completed', 'error') DEFAULT 'active' COMMENT '会话状态',
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
                end_time TIMESTAMP NULL COMMENT '结束时间',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

                INDEX idx_invitation (invitation_id),
                INDEX idx_question (question_id),
                INDEX idx_status (status),
                INDEX idx_start_time (start_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='语音面试会话表';
            """,
            """
            CREATE TABLE IF NOT EXISTS voice_evaluation_results (
                id VARCHAR(100) PRIMARY KEY COMMENT '评价ID',
                session_id VARCHAR(100) NOT NULL COMMENT '会话ID',
                question_id VARCHAR(100) NOT NULL COMMENT '题目ID',
                answer_text TEXT NOT NULL COMMENT '回答文本',
                score DECIMAL(5,2) NOT NULL COMMENT '评分（0-100）',
                reason TEXT COMMENT '评分理由',
                evaluation_details JSON COMMENT '详细评价信息',
                need_follow_up BOOLEAN DEFAULT FALSE COMMENT '是否需要追问',
                follow_up_question TEXT COMMENT '追问问题',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                INDEX idx_session (session_id),
                INDEX idx_question_eval (question_id),
                INDEX idx_score (score),
                INDEX idx_need_follow_up (need_follow_up),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='语音评价结果表';
            """
        ])

    return sqls


if __name__ == "__main__":
    # 语音面试现在使用现有的interview_session表，无需创建新表
    print("语音面试功能现在使用现有的interview_session表")
    print("请确保interview_invitation、interview_question、interview_session表已存在")
    print("如需初始化基础数据，请运行其他数据库脚本")