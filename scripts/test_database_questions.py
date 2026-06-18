#!/usr/bin/env python3
"""
数据库题目读取测试脚本
测试题目数据的读取、关联和完整性

功能：
1. 测试题目知识库查询
2. 测试面试题目关联查询
3. 测试题目内容完整性
4. 测试评估要点获取
5. 验证数据一致性

使用方法：
python scripts/test_database_questions.py
"""

import sys
import os
from datetime import datetime
import json

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.service import database_service


def test_question_knowledge_base():
    """测试题目知识库查询"""
    print("📚 测试题目知识库查询...")
    print("=" * 50)

    try:
        # 查询题目总数
        result = database_service.db.execute_one("SELECT COUNT(*) as count FROM interview_questions")
        total_questions = result['count'] if result else 0
        print(f"📊 题目知识库总题数: {total_questions}")

        if total_questions > 0:
            # 查询前5个题目
            questions = database_service.db.execute_query("""
                SELECT id, content, question_type, difficulty, position,
                       jsonb_array_length(evaluation_points) as eval_count
                FROM interview_questions
                ORDER BY create_time DESC
                LIMIT 5
            """)

            print("\n📝 最新题目示例:")
            for i, q in enumerate(questions, 1):
                print(f"{i}. ID: {q[0]}")
                print(f"   类型: {q[2]} | 难度: {q[3]} | 职位: {q[4]}")
                print(f"   评估要点数: {q[5]}")
                print(f"   内容: {q[1][:100]}...")
                print("-" * 40)

        return True

    except Exception as e:
        print(f"❌ 题目知识库查询失败: {str(e)}")
        return False


def test_interview_questions_association():
    """测试面试题目关联查询"""
    print("\n🔗 测试面试题目关联查询...")
    print("=" * 50)

    try:
        # 查询interview_question表中的记录
        result = database_service.db.execute_one("SELECT COUNT(*) as count FROM interview_question")
        total_associations = result['count'] if result else 0
        print(f"📊 面试题目关联记录数: {total_associations}")

        if total_associations > 0:
            # 查询完整的题目信息（JOIN）
            questions = database_service.db.execute_query("""
                SELECT
                    iq.question_id,
                    iq.invitation_id,
                    iq.atomic_question_id,
                    iq.question_type,
                    iq.question_order,
                    iqs.content as question_text,
                    iqs.evaluation_points as atomic_eval_points,
                    iq.evaluation_points as interview_eval_points
                FROM interview_question iq
                LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
                ORDER BY iq.invitation_id, iq.question_order
                LIMIT 10
            """)

            print("\n🔗 关联查询结果:")
            current_invitation = None
            for q in questions:
                if current_invitation != q[1]:
                    current_invitation = q[1]
                    print(f"\n🏢 面试邀请: {current_invitation}")

                print(f"   题目ID: {q[0]} (顺序: {q[4]})")
                print(f"   类型: {q[3]}")
                print(f"   原子题目ID: {q[2]}")
                print(f"   题目内容: {q[5][:80] if q[5] else 'N/A'}...")
                print(f"   评估要点: 原子库{len(q[6]) if q[6] else 0}个, 面试题{len(q[7]) if q[7] else 0}个")
                print("-" * 40)

        return True

    except Exception as e:
        print(f"❌ 面试题目关联查询失败: {str(e)}")
        return False


def test_question_content_integrity():
    """测试题目内容完整性"""
    print("\n✅ 测试题目内容完整性...")
    print("=" * 50)

    issues = []

    try:
        # 检查是否有题目内容为空的情况
        empty_content = database_service.db.execute_query("""
            SELECT COUNT(*) as count
            FROM interview_question iq
            LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iqs.content IS NULL OR iqs.content = ''
        """)

        if empty_content and empty_content[0]['count'] > 0:
            issues.append(f"发现 {empty_content[0]['count']} 个题目内容为空")

        # 检查是否有评估要点缺失的情况
        missing_eval = database_service.db.execute_query("""
            SELECT COUNT(*) as count
            FROM interview_question iq
            WHERE iq.evaluation_points IS NULL
               OR iq.evaluation_points = '[]'
               OR iq.evaluation_points = '{}'
        """)

        if missing_eval and missing_eval[0]['count'] > 0:
            issues.append(f"发现 {missing_eval[0]['count']} 个题目缺少评估要点")

        # 检查是否有孤立的题目记录（atomic_question_id不存在）
        orphaned = database_service.db.execute_query("""
            SELECT COUNT(*) as count
            FROM interview_question iq
            LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iqs.id IS NULL
        """)

        if orphaned and orphaned[0]['count'] > 0:
            issues.append(f"发现 {orphaned[0]['count']} 个孤立的题目记录（原子题目不存在）")

        if issues:
            print("⚠️ 发现以下数据完整性问题:")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print("✅ 所有题目数据完整性检查通过")

        return len(issues) == 0

    except Exception as e:
        print(f"❌ 题目内容完整性检查失败: {str(e)}")
        return False


def test_evaluation_points_parsing():
    """测试评估要点解析"""
    print("\n🎯 测试评估要点解析...")
    print("=" * 50)

    try:
        # 查询有评估要点的题目
        questions_with_eval = database_service.db.execute_query("""
            SELECT question_id, evaluation_points
            FROM interview_question
            WHERE evaluation_points IS NOT NULL
              AND evaluation_points != '[]'
              AND evaluation_points != '{}'
            LIMIT 5
        """)

        if questions_with_eval:
            print("🔍 评估要点解析示例:")
            for q in questions_with_eval:
                print(f"\n题目ID: {q[0]}")
                try:
                    eval_points = json.loads(q[1]) if isinstance(q[1], str) else q[1]
                    if isinstance(eval_points, list):
                        for i, point in enumerate(eval_points, 1):
                            if isinstance(point, dict):
                                point_text = point.get('point', 'N/A')
                                weight = point.get('weight', 'N/A')
                                print(f"   {i}. {point_text} (权重: {weight})")
                            else:
                                print(f"   {i}. {point}")
                    else:
                        print(f"   评估要点格式异常: {type(eval_points)}")
                except json.JSONDecodeError as e:
                    print(f"   JSON解析失败: {str(e)}")
                print("-" * 40)

            print("✅ 评估要点解析测试完成")
            return True
        else:
            print("⚠️ 未找到包含评估要点的题目")
            return False

    except Exception as e:
        print(f"❌ 评估要点解析测试失败: {str(e)}")
        return False


def test_question_retrieval_api():
    """测试题目检索API"""
    print("\n🔍 测试题目检索API...")
    print("=" * 50)

    try:
        # 测试get_question_by_id方法
        questions = database_service.db.execute_query("SELECT question_id FROM interview_question LIMIT 3")
        if questions:
            for q in questions:
                question_id = q[0]
                print(f"\n查询题目ID: {question_id}")

                # 使用数据库服务的方法
                question_detail = database_service.get_question_by_id(question_id)

                if question_detail:
                    print(f"   ✅ 查询成功")
                    print(f"   邀请ID: {question_detail.get('invitation_id')}")
                    print(f"   题目类型: {question_detail.get('question_type')}")
                    print(f"   题目内容: {question_detail.get('question_text', 'N/A')[:100]}...")
                    print(f"   评估要点: {question_detail.get('evaluation_points', 'N/A')}")
                else:
                    print(f"   ❌ 查询失败")

        # 测试get_invitation_questions方法
        invitations = database_service.db.execute_query("SELECT DISTINCT invitation_id FROM interview_question LIMIT 2")
        if invitations:
            for inv in invitations:
                invitation_id = inv[0]
                print(f"\n查询邀请ID的所有题目: {invitation_id}")

                questions = database_service.get_invitation_questions(invitation_id)

                if questions:
                    print(f"   ✅ 查询成功，找到 {len(questions)} 个题目")
                    for i, q in enumerate(questions[:3], 1):  # 只显示前3个
                        print(f"     {i}. {q.get('question_text', 'N/A')[:60]}...")
                else:
                    print(f"   ❌ 查询失败")

        return True

    except Exception as e:
        print(f"❌ 题目检索API测试失败: {str(e)}")
        return False


def generate_test_report():
    """生成测试报告"""
    print("\n📊 生成测试报告...")
    print("=" * 50)

    report = {
        "test_time": datetime.now().isoformat(),
        "database_stats": {},
        "test_results": {},
        "issues": []
    }

    try:
        # 数据库统计
        question_count = database_service.db.execute_one("SELECT COUNT(*) as count FROM interview_questions")
        association_count = database_service.db.execute_one("SELECT COUNT(*) as count FROM interview_question")

        report["database_stats"] = {
            "interview_questions_count": question_count['count'] if question_count else 0,
            "interview_question_count": association_count['count'] if association_count else 0
        }

        # 运行所有测试
        tests = [
            ("题目知识库查询", test_question_knowledge_base),
            ("面试题目关联查询", test_interview_questions_association),
            ("题目内容完整性", test_question_content_integrity),
            ("评估要点解析", test_evaluation_points_parsing),
            ("题目检索API", test_question_retrieval_api)
        ]

        for test_name, test_func in tests:
            try:
                result = test_func()
                report["test_results"][test_name] = "通过" if result else "失败"
            except Exception as e:
                report["test_results"][test_name] = f"异常: {str(e)}"
                report["issues"].append(f"{test_name}: {str(e)}")

        # 保存报告
        report_file = f"question_database_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print(f"✅ 测试报告已保存到: {report_file}")

        # 打印总结
        print("\n📋 测试总结:")
        print(f"   题目知识库: {report['database_stats']['interview_questions_count']} 题")
        print(f"   面试题目关联: {report['database_stats']['interview_question_count']} 条")

        passed_tests = sum(1 for result in report["test_results"].values() if result == "通过")
        total_tests = len(report["test_results"])

        print(f"   测试通过: {passed_tests}/{total_tests}")

        if report["issues"]:
            print(f"   发现问题: {len(report['issues'])} 个")
            for issue in report["issues"][:3]:  # 只显示前3个
                print(f"     - {issue}")

        return report

    except Exception as e:
        print(f"❌ 生成测试报告失败: {str(e)}")
        return None


def main():
    """主函数"""
    print("🚀 开始数据库题目读取测试")
    print("=" * 60)

    try:
        # 生成测试报告
        report = generate_test_report()

        if report:
            print("\n🎉 数据库题目读取测试完成!")
        else:
            print("\n❌ 测试过程中出现严重错误")

    except Exception as e:
        print(f"❌ 测试执行失败: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()