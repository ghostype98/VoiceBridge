#!/usr/bin/env python3
"""
查询指定邀请ID的题目分类，分析为什么第10、12、19阶段都匹配到13道题
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import DatabaseManager
from app.database.config import db_config

def check_question_categories(invitation_id: str):
    """查询题目分类信息"""
    db_manager = DatabaseManager()
    
    # 查询该邀请ID下的所有题目及其分类
    query = """
    SELECT 
        iq.question_id,
        iq.question_type,
        iq.question_category,
        iq.question_order,
        iqs.content as question_text
    FROM interview_question iq
    LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
    WHERE iq.invitation_id = %s
    ORDER BY 
        CASE iq.question_type
            WHEN 'BASIC' THEN 1
            WHEN 'BASIC_INFO' THEN 1
            WHEN 'SPECIALTY' THEN 2
            WHEN 'PROFESSIONAL' THEN 2
            ELSE 3
        END,
        iq.question_order ASC
    """
    
    try:
        results = db_manager.execute_query(query, (invitation_id,))
        
        print("=" * 100)
        print(f"邀请ID: {invitation_id}")
        print("=" * 100)
        print(f"\n总共找到 {len(results)} 道题目\n")
        
        # 统计分类
        category_count = {}
        null_category_count = 0
        
        print("题目详情：")
        print("-" * 100)
        print(f"{'序号':<5} {'题目ID':<40} {'类型':<15} {'分类':<30} {'题目内容（前50字）'}")
        print("-" * 100)
        
        for idx, row in enumerate(results, 1):
            question_id = row.get('question_id', '')
            question_type = row.get('question_type', '')
            question_category = row.get('question_category') or ''  # None转为空字符串
            question_text = (row.get('question_text') or '')[:50]
            
            # 统计分类
            if question_category:
                category_count[question_category] = category_count.get(question_category, 0) + 1
            else:
                null_category_count += 1
            
            print(f"{idx:<5} {question_id:<40} {question_type:<15} {question_category:<30} {question_text}")
        
        print("\n" + "=" * 100)
        print("分类统计：")
        print("-" * 100)
        for category, count in sorted(category_count.items()):
            print(f"  {category}: {count} 道题")
        if null_category_count > 0:
            print(f"  (空/None): {null_category_count} 道题")
        
        print("\n" + "=" * 100)
        print("维度配置分析：")
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
                print(f"  → 结果: 匹配所有 {len(results)} 道题（因为配置为空列表）")
            else:
                # 计算匹配的题目数
                matched_count = 0
                for row in results:
                    question_category = row.get('question_category') or ''
                    if question_category in expected_categories:
                        matched_count += 1
                
                print(f"  → 匹配的题目数: {matched_count} 道")
                if matched_count == len(results):
                    print(f"  ⚠️  警告: 所有题目都被匹配！可能原因：")
                    print(f"     1. 所有题目的分类都是 '{expected_categories[0]}'")
                    print(f"     2. 或者所有题目的分类都是空字符串，但代码逻辑有问题")
                
                # 显示匹配的题目
                if matched_count > 0 and matched_count <= 5:
                    print(f"  → 匹配的题目:")
                    for row in results:
                        question_category = row.get('question_category') or ''
                        if question_category in expected_categories:
                            question_id = row.get('question_id', '')
                            print(f"     - {question_id} (分类: {question_category})")
        
        print("\n" + "=" * 100)
        
    except Exception as e:
        print(f"查询失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if hasattr(db_manager, '_connection_pool') and db_manager._connection_pool:
            db_manager._connection_pool.closeall()

if __name__ == "__main__":
    # 从调试文件中提取邀请ID
    invitation_id = "INV_20260209135052_BC113F8F"
    
    if len(sys.argv) > 1:
        invitation_id = sys.argv[1]
    
    check_question_categories(invitation_id)

