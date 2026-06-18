#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试答案保存功能
验证_save_question_answer和_save_evaluation_result是否正确保存到数据库
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import DatabaseManager
from config.settings import settings
from loguru import logger

def test_answer_save():
    """测试答案保存功能"""
    try:
        # 初始化数据库管理器
        db_manager = DatabaseManager()
        
        # 测试用的invitation_id和question_id（从日志中获取）
        test_invitation_id = "INV_20260128105253_C4C97511"
        test_question_id = "Q_20260128105257_FC7935BD"
        
        logger.info(f"开始测试答案保存功能: invitation_id={test_invitation_id}, question_id={test_question_id}")
        
        # 1. 检查interview_session表中是否有记录
        check_session_sql = """
        SELECT session_id, candidate_answer, session_status, start_time, end_time
        FROM interview_session
        WHERE invitation_id = %s AND question_id = %s
        ORDER BY start_time DESC
        LIMIT 1
        """
        if db_manager.db_type != 'postgresql':
            check_session_sql = check_session_sql.replace('%s', '?')
        
        session_record = db_manager.fetch_one(check_session_sql, (test_invitation_id, test_question_id))
        if session_record:
            logger.info(f"找到interview_session记录: session_id={session_record.get('session_id')}")
            logger.info(f"  candidate_answer长度: {len(session_record.get('candidate_answer', '') or '')}")
            logger.info(f"  session_status: {session_record.get('session_status')}")
            logger.info(f"  candidate_answer内容: {session_record.get('candidate_answer', '')[:100]}...")
        else:
            logger.warning(f"未找到interview_session记录: invitation_id={test_invitation_id}, question_id={test_question_id}")
        
        # 2. 检查candidate_answers表中是否有记录
        if session_record:
            session_id = session_record.get('session_id')
            check_answer_sql = """
            SELECT id, answer_text, status, final_score, evaluation_result
            FROM candidate_answers
            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
            ORDER BY create_time DESC
            LIMIT 1
            """
            if db_manager.db_type != 'postgresql':
                check_answer_sql = check_answer_sql.replace('%s', '?')
            
            answer_record = db_manager.fetch_one(check_answer_sql, (session_id, test_question_id))
            if answer_record:
                logger.info(f"找到candidate_answers记录: answer_id={answer_record.get('id')}")
                logger.info(f"  answer_text长度: {len(answer_record.get('answer_text', '') or '')}")
                logger.info(f"  status: {answer_record.get('status')}")
                logger.info(f"  final_score: {answer_record.get('final_score')}")
                logger.info(f"  answer_text内容: {answer_record.get('answer_text', '')[:100]}...")
            else:
                logger.warning(f"未找到candidate_answers记录: session_id={session_id}, question_id={test_question_id}")
        
        # 3. 列出该invitation_id下的所有session记录
        list_sessions_sql = """
        SELECT session_id, question_id, candidate_answer, session_status, start_time, end_time
        FROM interview_session
        WHERE invitation_id = %s
        ORDER BY start_time ASC
        """
        if db_manager.db_type != 'postgresql':
            list_sessions_sql = list_sessions_sql.replace('%s', '?')
        
        all_sessions = db_manager.fetch_all(list_sessions_sql, (test_invitation_id,))
        logger.info(f"\n该invitation_id下的所有session记录（共{len(all_sessions)}条）:")
        for idx, sess in enumerate(all_sessions, 1):
            logger.info(f"  [{idx}] session_id={sess.get('session_id')}, question_id={sess.get('question_id')}")
            logger.info(f"      candidate_answer长度={len(sess.get('candidate_answer', '') or '')}, status={sess.get('session_status')}")
        
        logger.info("\n测试完成")
        
    except Exception as e:
        logger.error(f"测试失败: {str(e)}", exc_info=True)

if __name__ == "__main__":
    test_answer_save()
