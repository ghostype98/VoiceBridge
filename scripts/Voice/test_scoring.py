#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试评分功能的脚本
"""

import asyncio
import sys
sys.path.insert(0, '/opt/voicebridge')

from app.database.connection import DatabaseManager
from app.services.llm_service import LLMService
from app.voice_streaming.realtime_scorer import RealtimeScorer
from config.settings import settings

async def test_scoring():
    """测试评分功能"""
    print("=" * 60)
    print("测试评分功能")
    print("=" * 60)
    
    # 初始化组件
    db_manager = DatabaseManager()
    llm_service = LLMService()
    
    voice_config = settings.get_config('voice_streaming')
    evaluation_config = voice_config.get('evaluation', {})
    
    scorer = RealtimeScorer(
        llm_service=llm_service,
        db_manager=db_manager,
        config=evaluation_config
    )
    
    # 测试数据
    test_session_id = "TEST_SESSION_001"
    test_question_id = "Q_20260128105257_6177CB12"
    test_answer = "我认为团队协作非常重要，需要良好的沟通和相互理解。"
    
    print(f"\n测试参数：")
    print(f"  session_id: {test_session_id}")
    print(f"  question_id: {test_question_id}")
    print(f"  answer: {test_answer}")
    
    try:
        # 执行评分
        print("\n开始评分...")
        result = await scorer.evaluate_answer(
            session_id=test_session_id,
            question_id=test_question_id,
            answer_text=test_answer
        )
        
        print("\n评分结果：")
        print(f"  score: {result.get('score', 0)}")
        print(f"  reason: {result.get('reason', '')}")
        print(f"  need_follow_up: {result.get('need_follow_up', False)}")
        print(f"  follow_up_question: {result.get('follow_up_question', '')}")
        print(f"  evaluation_details: {result.get('evaluation_details', {})}")
        
        if result.get('score', 0) > 0:
            print("\n✓ 评分功能正常！")
        else:
            print("\n✗ 评分为0，可能存在问题")
            
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_scoring())
