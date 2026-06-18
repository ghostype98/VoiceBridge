#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库迁移：为 interview_evaluation_record 表添加 evaluation_structured 字段（JSON 文本）。
用于存储结论、亮点、风险、复试核实项、基础/专业均分等结构化报告数据。
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from app.database.connection import DatabaseManager


def add_evaluation_structured_column():
    try:
        db_manager = DatabaseManager()

        if db_manager.db_type == "postgresql":
            check_sql = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'interview_evaluation_record'
              AND column_name = 'evaluation_structured'
            """
        else:
            check_sql = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'interview_evaluation_record'
              AND column_name = 'evaluation_structured'
            """

        result = db_manager.fetch_one(check_sql)
        if result:
            logger.info("字段 evaluation_structured 已存在，跳过添加")
            return True

        if db_manager.db_type == "postgresql":
            alter_sql = """
            ALTER TABLE interview_evaluation_record
            ADD COLUMN evaluation_structured TEXT DEFAULT NULL;
            """
        else:
            alter_sql = """
            ALTER TABLE interview_evaluation_record
            ADD COLUMN evaluation_structured TEXT DEFAULT NULL;
            """

        db_manager.execute(alter_sql)
        logger.info("✅ 已添加字段 evaluation_structured 到 interview_evaluation_record 表")
        return True
    except Exception as e:
        logger.error(f"添加字段失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


if __name__ == "__main__":
    logger.info("开始迁移：evaluation_structured")
    sys.exit(0 if add_evaluation_structured_column() else 1)
