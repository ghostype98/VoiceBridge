#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库迁移脚本：为interview_evaluation_record表添加question_score字段
"""

import sys
import os

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from app.database.connection import DatabaseManager


def add_question_score_field():
    """为interview_evaluation_record表添加question_score字段"""
    try:
        db_manager = DatabaseManager()
        
        # 检查字段是否已存在
        check_field_sql = """
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'interview_evaluation_record' 
        AND column_name = 'question_score'
        """
        
        result = db_manager.fetch_one(check_field_sql)
        if result:
            logger.info("字段 question_score 已存在，跳过添加")
            return True
        
        # 添加字段
        if db_manager.db_type == 'postgresql':
            add_field_sql = """
            ALTER TABLE interview_evaluation_record 
            ADD COLUMN question_score FLOAT DEFAULT NULL;
            """
        else:
            # MySQL/SQLite
            add_field_sql = """
            ALTER TABLE interview_evaluation_record 
            ADD COLUMN question_score FLOAT DEFAULT NULL;
            """
        
        db_manager.execute(add_field_sql)
        logger.info("✅ 成功添加字段 question_score 到 interview_evaluation_record 表")
        
        return True
        
    except Exception as e:
        logger.error(f"添加字段失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


if __name__ == "__main__":
    logger.info("开始执行数据库迁移：添加 question_score 字段")
    success = add_question_score_field()
    if success:
        logger.info("✅ 数据库迁移完成")
    else:
        logger.error("❌ 数据库迁移失败")
        sys.exit(1)

