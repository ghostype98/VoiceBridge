#!/usr/bin/env python3
"""
面试评估测试脚本
测试21维度面试评估功能
"""

import asyncio
import json
import sys
import os
from datetime import datetime
import uuid

import json as json_module

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.service import database_service
from agent.interview_evaluation_service import interview_evaluation_service


async def create_test_invitation():
    """创建测试面试邀请"""
    invitation_id = f"TEST_INV_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # 直接使用SQL创建邀请记录
    evaluation_id = f"EVAL_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
    try:
        affected_rows = database_service.db.execute_update("""
            INSERT INTO interview_invitation (
                invitation_id, evaluation_id, position, candidate_name, requester,
                interview_status, created_time
            ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """, (
            invitation_id,
            evaluation_id,
            "高级Java开发工程师",
            "张三",
            "测试公司",
            "进行中"
        ))

        if affected_rows > 0:
            print(f"✅ 创建测试邀请成功: {invitation_id}")
            return invitation_id
        else:
            raise Exception("插入邀请记录失败")
    except Exception as e:
        print(f"❌ 创建测试邀请失败: {e}")
        raise


async def create_test_questions(invitation_id):
    """创建测试问题"""
    questions = [
        {
            "question_id": f"Q_TEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}_001",
            "question_text": "请介绍一下你最近参与的一个项目，以及你在其中担任的角色。",
            "question_type": "PROFESSIONAL",
            "evaluation_points": [
                {"point": "项目描述的完整性", "weight": 0.3},
                {"point": "角色职责的清晰度", "weight": 0.3},
                {"point": "技术难点的识别", "weight": 0.4}
            ]
        },
        {
            "question_id": f"Q_TEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}_002",
            "question_text": "在项目开发过程中，你遇到过最大的技术挑战是什么？是如何解决的？",
            "question_type": "PROFESSIONAL",
            "evaluation_points": [
                {"point": "问题分析的深度", "weight": 0.3},
                {"point": "解决方案的合理性", "weight": 0.4},
                {"point": "学习能力的体现", "weight": 0.3}
            ]
        },
        {
            "question_id": f"Q_TEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}_003",
            "question_text": "请谈谈你对团队协作的理解，以及你在团队中是如何贡献的。",
            "question_type": "PROFESSIONAL",
            "evaluation_points": [
                {"point": "团队协作理念的理解", "weight": 0.3},
                {"point": "沟通能力的体现", "weight": 0.3},
                {"point": "贡献方式的多样性", "weight": 0.4}
            ]
        }
    ]

    created_questions = []
    for q in questions:
        try:
            affected_rows = database_service.db.execute_update("""
                INSERT INTO interview_question (
                    question_id, invitation_id, question_text,
                    question_type, evaluation_points, create_time
                ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (
                q["question_id"],
                invitation_id,
                q["question_text"],
                q["question_type"],
                json.dumps(q["evaluation_points"])
            ))

            if affected_rows > 0:
                created_questions.append(q)
                print(f"✅ 创建测试问题成功: {q['question_id']}")
            else:
                print(f"❌ 创建测试问题失败: {q['question_id']}")
        except Exception as e:
            print(f"❌ 创建测试问题失败: {q['question_id']}, error: {e}")

    return created_questions


async def create_test_answers(session_id, questions):
    """创建测试答案"""
    answers = [
        {
            "question_id": questions[0]["question_id"],
            "answer_text": "我最近参与了一个电商平台的微服务架构改造项目，担任后端开发工程师的角色。主要负责用户服务模块的重构工作。我们采用了Spring Cloud技术栈，将单体架构拆分为微服务，提高了系统的可扩展性和维护性。",
            "evaluation_result": {
                "score": 85,
                "reasoning": "候选人对项目的描述较为完整，技术栈选择合理",
                "point_scores": [
                    {"point": "项目描述的完整性", "score": 0.9, "weight": 0.3},
                    {"point": "角色职责的清晰度", "score": 0.8, "weight": 0.3},
                    {"point": "技术难点的识别", "score": 0.8, "weight": 0.4}
                ]
            }
        },
        {
            "question_id": questions[1]["question_id"],
            "answer_text": "在项目中，我遇到最大的挑战是数据库性能优化。当时系统QPS很高，经常出现慢查询。我首先通过添加索引和优化SQL语句，将查询时间从3秒降低到0.5秒。然后引入Redis缓存，进一步提升了响应速度。最后编写了监控脚本，实现了性能指标的自动化监控。",
            "evaluation_result": {
                "score": 90,
                "reasoning": "候选人展现了出色的技术分析能力和解决思路",
                "point_scores": [
                    {"point": "问题分析的深度", "score": 0.9, "weight": 0.3},
                    {"point": "解决方案的合理性", "score": 0.95, "weight": 0.4},
                    {"point": "学习能力的体现", "score": 0.85, "weight": 0.3}
                ]
            }
        },
        {
            "question_id": questions[2]["question_id"],
            "answer_text": "我认为团队协作的核心是高效沟通和相互支持。在团队中，我会主动分享技术经验，帮助同事解决技术难题。同时，我注重代码质量，会进行Code Review确保代码规范。另外，我会参与需求讨论，从技术角度提出合理建议，为项目决策贡献力量。",
            "evaluation_result": {
                "score": 88,
                "reasoning": "候选人展现了良好的团队协作意识和贡献精神",
                "point_scores": [
                    {"point": "团队协作理念的理解", "score": 0.85, "weight": 0.3},
                    {"point": "沟通能力的体现", "score": 0.9, "weight": 0.3},
                    {"point": "贡献方式的多样性", "score": 0.9, "weight": 0.4}
                ]
            }
        }
    ]

    created_answers = []
    for i, answer_data in enumerate(answers):
        # 创建答案记录
        answer_id = f"ANS_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
        try:
            affected_rows = database_service.db.execute_update("""
                INSERT INTO candidate_answers (
                    id, session_id, question_id, answer_text,
                    is_follow_up, status, create_time
                ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (
                answer_id,
                session_id,
                answer_data["question_id"],
                answer_data["answer_text"],
                False,
                'recorded'
            ))

            if affected_rows > 0:
                # 更新评估结果
                database_service.db.execute_update("""
                    UPDATE candidate_answers SET
                        evaluation_result = %s,
                        point_evaluations = %s,
                        final_score = %s,
                        status = 'evaluated',
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (
                    json.dumps(answer_data["evaluation_result"]),
                    json.dumps(answer_data["evaluation_result"]["point_scores"]),
                    answer_data["evaluation_result"]["score"],
                    answer_id
                ))

                created_answers.append({"id": answer_id})
                print(f"✅ 创建测试答案成功: 问题{i+1}, 答案ID: {answer_id}")
            else:
                print(f"❌ 创建测试答案失败: 问题{i+1}")
        except Exception as e:
            print(f"❌ 创建测试答案失败: 问题{i+1}, error: {e}")

    return created_answers


async def run_evaluation_test(session_id, invitation_id):
    """运行评估测试"""
    print(f"\n🔍 开始评估测试: session_id={session_id}, invitation_id={invitation_id}")

    try:
        # 执行评估
        evaluation_result = await interview_evaluation_service.evaluate_interview(
            session_id=session_id,
            invitation_id=invitation_id
        )

        print("✅ 评估执行成功！")
        print(f"📊 总体得分: {evaluation_result.get('overall_score', 0)}")
        print(f"🎯 是否通过: {'是' if evaluation_result.get('is_passed', 0) == 1 else '否'}")

        # 显示维度评分
        dimension_scores = evaluation_result.get('dimension_scores', {})
        print(f"\n📋 维度评分详情 ({len(dimension_scores)}个维度):")
        for dim_name, score in dimension_scores.items():
            print(f"  • {dim_name}: {score}分")

        # 显示评估总结
        evaluation_summary = evaluation_result.get('evaluation_summary', '')
        evaluation_suggestions = evaluation_result.get('evaluation_suggestions', '')

        print(f"\n📝 评估总结: {evaluation_summary[:100]}..." if len(evaluation_summary) > 100 else f"\n📝 评估总结: {evaluation_summary}")
        print(f"💡 评估建议: {evaluation_suggestions[:100]}..." if len(evaluation_suggestions) > 100 else f"💡 评估建议: {evaluation_suggestions}")

        # 验证数据库保存
        saved_record = await database_service.get_interview_evaluation_record(invitation_id)
        if saved_record:
            print("✅ 评估结果已保存到数据库")
            print(f"   记录ID: {saved_record.get('evaluation_record_id')}")
            print(f"   创建时间: {saved_record.get('create_time')}")
        else:
            print("❌ 评估结果保存失败")

        return evaluation_result

    except Exception as e:
        print(f"❌ 评估测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


async def cleanup_test_data(invitation_id, questions, answers):
    """清理测试数据"""
    print("\n🧹 开始清理测试数据...")
    try:
        # 删除评估记录
        await database_service.db.execute_query(
            "DELETE FROM interview_evaluation_record WHERE invitation_id = %s",
            (invitation_id,)
        )
        print("✅ 删除评估记录")

        # 删除答案记录
        for answer in answers:
            await database_service.db.execute_query(
                "DELETE FROM candidate_answers WHERE id = %s",
                (answer["id"],)
            )
        print(f"✅ 删除答案记录 ({len(answers)}条)")

        # 删除问题记录
        for question in questions:
            await database_service.db.execute_query(
                "DELETE FROM interview_question WHERE question_id = %s",
                (question["question_id"],)
            )
        print(f"✅ 删除问题记录 ({len(questions)}条)")

        # 删除邀请记录
        await database_service.db.execute_query(
            "DELETE FROM interview_invitation WHERE invitation_id = %s",
            (invitation_id,)
        )
        print("✅ 删除邀请记录")

        print("🎉 测试数据清理完成")

    except Exception as e:
        print(f"❌ 清理测试数据失败: {e}")


async def main():
    """主测试函数"""
    print("🚀 开始面试评估功能测试")
    print("=" * 60)

    invitation_id = None
    questions = []
    answers = []
    session_id = f"TEST_SESS_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    try:
        # 1. 创建测试邀请
        invitation_id = await create_test_invitation()

        # 2. 创建测试问题
        questions = await create_test_questions(invitation_id)

        # 3. 创建测试答案
        answers = await create_test_answers(session_id, questions)

        # 4. 运行评估测试
        evaluation_result = await run_evaluation_test(session_id, invitation_id)

        if evaluation_result:
            print("\n🎉 面试评估功能测试成功完成！")
            print(f"📊 最终评估结果: 得分 {evaluation_result.get('overall_score', 0)}, 是否通过: {'是' if evaluation_result.get('is_passed', 0) == 1 else '否'}")
        else:
            print("\n❌ 面试评估功能测试失败")
            return 1

    except Exception as e:
        print(f"\n❌ 测试过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # 清理测试数据
        if invitation_id:
            await cleanup_test_data(invitation_id, questions, answers)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)