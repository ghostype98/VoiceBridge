#!/usr/bin/env python3
"""
检查实际答案记录中的题目分类，分析为什么第10、12阶段匹配了13道题
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import DatabaseManager
from app.database.config import db_config

def check_answer_categories(session_id: str, invitation_id: str):
    """检查答案记录中的题目分类"""
    db_manager = DatabaseManager()
    
    # 查询该session的所有答案记录
    query = """
    SELECT 
        ca.id,
        ca.question_id,
        ca.answer_text,
        ca.is_follow_up,
        iq.question_type,
        iq.question_category,
        iqs.content as question_text
    FROM candidate_answers ca
    JOIN interview_question iq ON ca.question_id = iq.question_id
    LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
    WHERE ca.session_id = %s
    ORDER BY ca.id
    """
    
    try:
        results = db_manager.execute_query(query, (session_id,))
        
        print("=" * 100)
        print(f"Session ID: {session_id}")
        print(f"Invitation ID: {invitation_id}")
        print("=" * 100)
        print(f"\n总共找到 {len(results)} 条答案记录\n")
        
        # 过滤掉追问答案
        main_answers = [r for r in results if not r.get('is_follow_up', False)]
        print(f"主答案记录: {len(main_answers)} 条（已排除追问）\n")
        
        # 统计分类
        category_count = {}
        null_category_count = 0
        
        print("答案记录详情：")
        print("-" * 100)
        print(f"{'序号':<5} {'题目ID':<40} {'类型':<15} {'分类':<30} {'是否追问':<10} {'题目内容（前50字）'}")
        print("-" * 100)
        
        for idx, row in enumerate(main_answers, 1):
            question_id = row.get('question_id', '')
            question_type = row.get('question_type', '')
            question_category = row.get('question_category') or ''  # None转为空字符串
            is_follow_up = '是' if row.get('is_follow_up', False) else '否'
            question_text = (row.get('question_text') or '')[:50]
            
            # 统计分类
            if question_category:
                category_count[question_category] = category_count.get(question_category, 0) + 1
            else:
                null_category_count += 1
            
            print(f"{idx:<5} {question_id:<40} {question_type:<15} {question_category:<30} {is_follow_up:<10} {question_text}")
        
        print("\n" + "=" * 100)
        print("分类统计（主答案）：")
        print("-" * 100)
        for category, count in sorted(category_count.items()):
            print(f"  {category}: {count} 道题")
        if null_category_count > 0:
            print(f"  (空/None): {null_category_count} 道题")
        
        print("\n" + "=" * 100)
        print("维度配置分析（基于实际答案记录）：")
        print("-" * 100)
        
        # 分析三个维度的配置
        dimensions_config = {
            "第10阶段 - 学历与专业匹配度": ["技术栈深度"],
            "第12阶段 - 资格证书要求": ["技术栈深度"],
            "第19阶段 - 简历质量与完整性": []
        }
        
        for dim_name, expected_categories in dimensions_config.items():
            print(f"\n{dim_name}:")
            print(f"  配置的分类: {expected_categories if expected_categories else '[] (空列表，使用所有题目)'}")
            
            if not expected_categories:
                print(f"  → 结果: 匹配所有 {len(main_answers)} 道题（因为配置为空列表）")
            else:
                # 计算匹配的题目数
                matched_questions = []
                for row in main_answers:
                    question_category = row.get('question_category') or ''
                    question_id = row.get('question_id', '')
                    
                    # 检查匹配逻辑
                    if question_category in expected_categories:
                        matched_questions.append((question_id, question_category))
                    elif not question_category:
                        # 空字符串的情况
                        print(f"  ⚠️  发现空分类的题目: {question_id}")
                
                matched_count = len(matched_questions)
                print(f"  → 匹配的题目数: {matched_count} 道")
                
                if matched_count == len(main_answers):
                    print(f"  ⚠️  警告: 所有答案都被匹配！")
                    print(f"     可能原因：代码逻辑问题，空字符串被错误匹配")
                elif matched_count > len(expected_categories) * 2:
                    print(f"  ⚠️  警告: 匹配的题目数 ({matched_count}) 远大于预期分类的题目数")
                
                # 显示匹配的题目
                if matched_questions:
                    print(f"  → 匹配的题目:")
                    for qid, cat in matched_questions:
                        print(f"     - {qid} (分类: {cat})")
        
        print("\n" + "=" * 100)
        print("代码逻辑检查：")
        print("-" * 100)
        print("""
检查代码逻辑（agent/interview_evaluation_service.py 第584-589行）：

if dimension_key and relevant_categories:
    if question_category not in relevant_categories:
        continue

问题分析：
1. 如果 question_category 是空字符串 ''，而 relevant_categories 是 ['技术栈深度']
2. 那么 '' not in ['技术栈深度'] 为 True，应该被跳过
3. 但如果所有题目的分类都是空字符串，那么都会被跳过，不会匹配13道题

可能的原因：
1. 代码中 question_category 的获取方式有问题
2. 或者某些题目的分类实际上不是空字符串，而是其他值
3. 或者代码逻辑在某个地方被绕过了
        """)
        
    except Exception as e:
        print(f"查询失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if hasattr(db_manager, '_connection_pool') and db_manager._connection_pool:
            db_manager._connection_pool.closeall()

if __name__ == "__main__":
    # 从调试文件中提取session_id和invitation_id
    session_id = "e5dc3008-c8c0-4f4e-9750-02d9cc3b70dd"
    invitation_id = "INV_20260209135052_BC113F8F"
    
    if len(sys.argv) > 1:
        session_id = sys.argv[1]
    if len(sys.argv) > 2:
        invitation_id = sys.argv[2]
    
    check_answer_categories(session_id, invitation_id)

