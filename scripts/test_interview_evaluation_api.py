#!/usr/bin/env python3
"""
面试评估API测试脚本
用于测试21维度面试评估功能，使用指定的session_id和invitation_id
"""

import asyncio
import json
import sys
import os
from datetime import datetime
from typing import Dict, Any

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.service import database_service
from agent.interview_evaluation_service import interview_evaluation_service


async def test_interview_evaluation(session_id: str, invitation_id: str, save_to_db: bool = True):
    """
    测试面试评估功能
    
    Args:
        session_id: 面试会话ID
        invitation_id: 面试邀请ID
    """
    print("=" * 80)
    print("🚀 开始测试面试评估API")
    print("=" * 80)
    print(f"📋 测试参数:")
    print(f"   session_id: {session_id}")
    print(f"   invitation_id: {invitation_id}")
    print()
    
    try:
        # 1. 验证数据存在性
        print("🔍 步骤1: 验证数据存在性...")
        
        # 检查邀请是否存在
        invitation = database_service.get_invitation_by_id(invitation_id)
        if not invitation:
            print(f"❌ 错误: 未找到邀请记录 invitation_id={invitation_id}")
            return False
        print(f"✅ 邀请记录存在")
        print(f"   公司: {invitation.get('requester', '未知')}")
        print(f"   部门: {invitation.get('department', '未知')}")
        print(f"   职位: {invitation.get('position', '未知')}")
        
        # 检查答案记录是否存在
        answers = database_service.get_session_candidate_answers(session_id)
        if not answers or len(answers) == 0:
            print(f"⚠️  警告: 未找到答案记录 session_id={session_id}")
            print(f"🔍 正在查找该invitation_id下的有效session_id...")
            
            # 尝试查找该invitation_id下的有效session_id
            query = """
                SELECT DISTINCT ca.session_id, COUNT(*) as answer_count
                FROM candidate_answers ca
                WHERE ca.question_id IN (
                    SELECT question_id 
                    FROM interview_question 
                    WHERE invitation_id = %s
                )
                GROUP BY ca.session_id
                ORDER BY answer_count DESC
                LIMIT 1
            """
            result = database_service.db.execute_one(query, (invitation_id,))
            
            if result and result.get('session_id'):
                new_session_id = result['session_id']
                answer_count = result.get('answer_count', 0)
                print(f"✅ 找到有效的session_id: {new_session_id} (有 {answer_count} 条答案记录)")
                print(f"📝 将使用新的session_id进行测试")
                session_id = new_session_id
                answers = database_service.get_session_candidate_answers(session_id)
            else:
                print(f"❌ 错误: 该invitation_id下也没有找到任何答案记录")
                return False
        
        print(f"✅ 找到 {len(answers)} 条答案记录")
        
        # 显示答案记录摘要
        print(f"\n📝 答案记录摘要:")
        for i, answer in enumerate(answers[:5], 1):  # 只显示前5条
            question_id = answer.get('question_id', '未知')
            answer_text = answer.get('answer_text', '')[:50] if answer.get('answer_text') else ''
            is_follow_up = answer.get('is_follow_up', False)
            status = answer.get('status', '未知')
            print(f"   {i}. 问题ID: {question_id}, 是否追问: {is_follow_up}, 状态: {status}")
            if answer_text:
                print(f"      答案预览: {answer_text}...")
        
        if len(answers) > 5:
            print(f"   ... 还有 {len(answers) - 5} 条记录")
        
        # 检查岗位JD是否存在
        company = invitation.get('requester', '')
        department = invitation.get('department', '')
        position = invitation.get('position', '')
        
        if company and department and position:
            jd_info = database_service.get_job_description_by_company_department_position(
                company=company,
                department=department,
                position=position
            )
            if jd_info:
                core_requirements = jd_info.get('core_requirements', '')
                if core_requirements:
                    print(f"✅ 找到岗位JD，核心要求长度: {len(core_requirements)} 字符")
                    print(f"   JD预览: {core_requirements[:100]}...")
                else:
                    print(f"⚠️  岗位JD存在但core_requirements为空")
            else:
                print(f"⚠️  未找到岗位JD: company={company}, department={department}, position={position}")
        else:
            print(f"⚠️  邀请信息不完整，无法查询岗位JD")
        
        print()
        
        # 2. 执行评估
        print("🔍 步骤2: 执行21维度面试评估...")
        print("   这可能需要一些时间，请耐心等待...")
        if save_to_db:
            print("   ✅ 评估结果将自动保存到interview_evaluation_record表")
        print("   📝 评估输入内容将保存到调试文件（文件名包含时间戳）")
        print()
        
        # 使用找到的有效session_id进行评估
        evaluation_result = await interview_evaluation_service.evaluate_interview(
            session_id=session_id,  # 可能是自动找到的新session_id
            invitation_id=invitation_id
        )
        
        # 3. 显示评估结果
        print("=" * 80)
        print("✅ 评估完成！")
        print("=" * 80)
        
        # 总体得分
        overall_score = evaluation_result.get('overall_score', 0)
        is_passed = evaluation_result.get('is_passed', 0)
        print(f"\n📊 总体评估结果:")
        print(f"   总体得分: {overall_score:.2f} 分")
        print(f"   是否通过: {'✅ 是' if is_passed == 1 else '❌ 否'}")
        
        # 维度评分
        dimension_scores = evaluation_result.get('dimension_scores', {})
        if dimension_scores:
            print(f"\n📋 21维度评分详情:")
            print(f"   {'维度名称':<30} {'得分':<10} {'等级':<10}")
            print(f"   {'-' * 50}")
            
            for dim_name, score in sorted(dimension_scores.items()):
                # 处理 None 值
                if score is None:
                    grade = "未评估"
                    score_display = "N/A"
                elif isinstance(score, (int, float)):
                    if score >= 85:
                        grade = "优秀"
                    elif score >= 70:
                        grade = "良好"
                    elif score >= 60:
                        grade = "及格"
                    else:
                        grade = "待提升"
                    score_display = f"{score:.1f}"
                else:
                    grade = "无效"
                    score_display = str(score)
                print(f"   {dim_name:<30} {score_display:<10} {grade:<10}")
        
        # 评估总结
        evaluation_summary = evaluation_result.get('evaluation_summary', '')
        if evaluation_summary:
            print(f"\n📝 评估总结:")
            print(f"   {evaluation_summary}")
        
        # 评估建议
        evaluation_suggestions = evaluation_result.get('evaluation_suggestions', '')
        if evaluation_suggestions:
            print(f"\n💡 评估建议:")
            print(f"   {evaluation_suggestions}")
        
        # 元数据
        evaluation_metadata = evaluation_result.get('evaluation_metadata', {})
        if evaluation_metadata:
            print(f"\n📌 评估元数据:")
            print(f"   评估时间: {evaluation_metadata.get('evaluation_time', '未知')}")
            print(f"   LLM模型: {evaluation_metadata.get('llm_model', '未知')}")
            print(f"   维度数量: {evaluation_metadata.get('dimensions_count', 0)}")
            print(f"   评估版本: {evaluation_metadata.get('evaluation_version', '未知')}")
        
        # 提示调试文件位置
        import glob
        import os
        from config.settings import settings
        debug_files = glob.glob(os.path.join(settings.LOG_DIR_DEBUG_PROMPT, "debug_prompt_*.txt"))
        if debug_files:
            # 按修改时间排序，最新的在前
            debug_files.sort(key=os.path.getmtime, reverse=True)
            latest_file = debug_files[0]
            print(f"\n📝 调试文件:")
            print(f"   最新文件: {latest_file}")
            print(f"   （包含所有21个维度的输入内容，便于排查）")
        
        # 4. 验证数据库保存
        print(f"\n🔍 步骤3: 验证评估结果是否已保存到数据库...")
        try:
            # 查询最新的评估记录
            query = """
                SELECT evaluation_record_id, overall_score, is_passed, create_time
                FROM interview_evaluation_record
                WHERE invitation_id = %s
                ORDER BY create_time DESC
                LIMIT 1
            """
            saved_record = database_service.db.execute_one(query, (invitation_id,))
            
            if saved_record:
                print(f"✅ 评估结果已保存到数据库")
                print(f"   记录ID: {saved_record.get('evaluation_record_id')}")
                print(f"   总体得分: {saved_record.get('overall_score')}")
                print(f"   是否通过: {'是' if saved_record.get('is_passed') == 1 else '否'}")
                print(f"   创建时间: {saved_record.get('create_time')}")
            else:
                print(f"⚠️  未找到保存的评估记录")
        except Exception as e:
            print(f"⚠️  查询评估记录时出错: {e}")
        
        # 5. 不再保存JSON文件，结果已保存到数据库
        
        print()
        print("=" * 80)
        print("🎉 测试完成！")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        print(f"\n❌ 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主函数"""
    import sys
    
    # 支持命令行参数
    if len(sys.argv) > 1:
        invitation_id = sys.argv[1]
        session_id = sys.argv[2] if len(sys.argv) > 2 else None
    else:
        # 使用默认测试数据
        # session_id = "4583a033-008d-4d56-acf1-34f2aca838f9"
        # invitation_id = "INV_20260128105253_C4C97511"

        # session_id = "2d7433b3-2400-497e-8854-fecff758c30b"
        # invitation_id = "INV_20260205144214_F5D45E60"

        session_id = "e5dc3008-c8c0-4f4e-9750-02d9cc3b70dd"
        invitation_id = "INV_20260209135052_BC113F8F"
    
    # 如果没有提供session_id，自动查找
    if not session_id:
        print(f"🔍 未提供session_id，正在查找invitation_id={invitation_id}下的有效session_id...")
        query = """
            SELECT DISTINCT ca.session_id, COUNT(*) as answer_count
            FROM candidate_answers ca
            WHERE ca.question_id IN (
                SELECT question_id 
                FROM interview_question 
                WHERE invitation_id = %s
            )
            GROUP BY ca.session_id
            ORDER BY answer_count DESC
            LIMIT 1
        """
        result = database_service.db.execute_one(query, (invitation_id,))
        
        if result and result.get('session_id'):
            session_id = result['session_id']
            answer_count = result.get('answer_count', 0)
            print(f"✅ 找到有效的session_id: {session_id} (有 {answer_count} 条答案记录)")
        else:
            print(f"❌ 错误: 该invitation_id下没有找到任何答案记录")
            print(f"\n💡 提示: 请确保该invitation_id下已经有候选人完成面试并提交了答案")
            return 1
    
    # 每次测试直接调用，存入interview_evaluation_record表
    success = await test_interview_evaluation(session_id, invitation_id, save_to_db=True)
    
    if success:
        print("\n✅ 所有测试通过！")
        return 0
    else:
        print("\n❌ 测试失败！")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

