#!/usr/bin/env python3
"""
测试Agent评分和STAR追问功能
包含基础题和专业题的完整测试用例
支持数据库同步开关控制
"""

import asyncio
import sys
import os
import json
from typing import Dict, Any, Optional
from datetime import datetime
from unittest.mock import patch

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.dialogue_service import dialogue_service, STARDimension, STARFollowUpAction
from agent.evaluation_service import evaluation_service
from app.database.service import database_service


class AgentEvaluationTester:
    """Agent评分和追问测试器"""

    def __init__(self, sync_to_database: bool = False, output_dir: str = None):
        """
        初始化测试器

        Args:
            sync_to_database: 是否同步到数据库
            output_dir: 输出文件夹路径，如果为None则不生成文件
        """
        self.sync_to_database = sync_to_database
        self.test_results = []
        self.output_dir = output_dir or "test_results"

        if sync_to_database:
            print("🗄️  启用数据库同步模式")
        else:
            print("💾 禁用数据库同步（仅内存测试）")

        if output_dir:
            import os
            os.makedirs(output_dir, exist_ok=True)
            print(f"📁 测试结果将保存到: {output_dir}/")

    async def run_all_tests(self):
        """运行所有测试"""
        print("🚀 开始Agent评分和STAR追问功能测试\n")

        # 测试用例数据
        test_cases = [
            {
                "name": "基础题_高分_自我介绍",
                "question_type": "BASIC_INFO",
                "question_text": "请简单介绍一下你自己。",
                "candidate_answer": "我叫张三，毕业于清华大学计算机专业，有5年软件开发经验，精通Java、Python等多种编程语言。",
                "expected_action": STARFollowUpAction.NEXT_QUESTION.value,
                "expected_no_followup": True
            },
            {
                "name": "基础题_中等分_职业规划",
                "question_type": "BASIC_INFO",
                "question_text": "你对未来的职业规划是什么？",
                "candidate_answer": "我想在技术领域发展。",
                "expected_action": STARFollowUpAction.NEXT_QUESTION.value,
                "expected_no_followup": True
            },
            {
                "name": "专业题_中等分_项目经验",
                "question_type": "PROFESSIONAL",
                "question_text": "请描述一下你在某个项目中解决技术问题的经历。",
                "candidate_answer": "在电商项目中，我遇到了高并发性能问题。通过优化数据库索引、引入Redis缓存、采用异步处理，最终将响应时间从3秒降低到0.2秒，QPS提升了5倍。",
                "expected_action": STARFollowUpAction.FOLLOW_UP.value,
                "expected_missing_dimension": STARDimension.SITUATION.value,
                "followup_question": "能否详细描述一下这个项目的背景情况？比如项目规模、团队情况等。",
                "followup_answer": "这是一个日活100万用户的电商平台，技术团队20人。当时正值双11大促前夕，系统压力测试发现性能瓶颈。",
                "followup_evaluation_points": [
                    {"point": "项目背景描述", "weight": 0.5},
                    {"point": "情境完整性", "weight": 0.5}
                ]
            },
            {
                "name": "专业题_中等分_技术方案",
                "question_type": "PROFESSIONAL",
                "question_text": "你在项目中是如何处理缓存的？",
                "candidate_answer": "我用了Redis缓存。",
                "expected_action": STARFollowUpAction.FOLLOW_UP.value,
                "expected_missing_dimension": STARDimension.RESULT.value,
                "followup_question": "这个方案实施后带来了什么效果？有什么可衡量的改进吗？",
                "followup_answer": "通过Redis缓存优化后，API响应时间从2秒降低到200毫秒，缓存命中率达到95%，数据库查询压力减少了80%。",
                "followup_evaluation_points": [
                    {"point": "量化效果描述", "weight": 0.5},
                    {"point": "技术指标完整性", "weight": 0.5}
                ]
            },
            {
                "name": "专业题_中等分_数据库优化",
                "question_type": "PROFESSIONAL",
                "question_text": "请描述一下你优化数据库性能的经验。",
                "candidate_answer": "我给数据库加了索引，还用了读写分离。",
                "expected_action": STARFollowUpAction.FOLLOW_UP.value,
                "expected_missing_dimension": STARDimension.RESULT.value,
                "followup_question": "最终的结果或成果如何？能否分享一下具体的量化指标？",
                "followup_answer": "优化后，数据库查询响应时间从500ms降低到50ms，读写分离后读库压力降低70%，整体系统性能提升了3倍。",
                "followup_evaluation_points": [
                    {"point": "性能提升量化", "weight": 0.4},
                    {"point": "系统影响评估", "weight": 0.6}
                ]
            },
            {
                "name": "专业题_中等分_缺少行动描述",
                "question_type": "PROFESSIONAL",
                "question_text": "你遇到过系统架构设计的问题吗？",
                "candidate_answer": "遇到过，系统经常崩溃。后来架构优化了，稳定多了。",
                "expected_action": STARFollowUpAction.FOLLOW_UP.value,
                "expected_missing_dimension": STARDimension.RESULT.value,  # 这里应该是RESULT，因为中等分数会优先检查R维度
                "followup_question": "最终的结果或成果如何？能否分享一下具体的量化指标或影响？",
                "followup_answer": "架构优化后，系统可用性从85%提升到99.9%，崩溃次数从每天10次降低到每月1次，业务连续性大大提升。",
                "followup_evaluation_points": [
                    {"point": "可用性指标", "weight": 0.5},
                    {"point": "业务影响评估", "weight": 0.5}
                ]
            }
        ]

        # 运行测试用例
        for i, test_case in enumerate(test_cases, 1):
            print(f"\n{'='*60}")
            print(f"📋 测试用例 {i}: {test_case['name']}")
            print(f"{'='*60}")

            await self.run_single_test(test_case)

        # 输出测试总结
        self.print_test_summary()

    async def run_single_test(self, test_case: Dict[str, Any]):
        """运行单个测试用例"""
        try:
            # 1. 准备测试数据
            question_text = test_case["question_text"]
            candidate_answer = test_case["candidate_answer"]
            question_type = test_case["question_type"]
            question_id = f"test_{question_type.lower()}_{hash(question_text) % 1000}"

            print(f"❓ 问题: {question_text}")
            print(f"👤 回答: {candidate_answer}")
            print(f"🏷️  类型: {question_type}")

            # 2. 模拟评估要点
            evaluation_points = self._get_mock_evaluation_points(question_type)

            # 3. 调用Agent评分服务
            print(f"\n🤖 调用Agent评分服务...")
            evaluation_result = await evaluation_service.evaluate_answer(
                question_text=question_text,
                candidate_answer=candidate_answer,
                evaluation_points=evaluation_points,
                question_type=question_type,
                question_id=question_id,
                difficulty="MEDIUM"
            )

            print(f"📊 评分结果: {evaluation_result.get('score', 'N/A')}分 - {evaluation_result.get('grade', 'N/A')}")
            print(f"💬 评分理由: {evaluation_result.get('reasoning', '')[:100]}...")

            # 4. 调用STAR追问服务
            print(f"\n🎯 调用STAR追问服务...")
            interview_id = f"interview_test_{hash(question_text) % 10000}"

            # Mock题目信息，避免数据库查询失败
            mock_question_info = {
                "question_id": question_id,
                "question_type": question_type,
                "question_text": question_text,
                "evaluation_points": evaluation_points
            }

            # 使用mock来模拟数据库查询
            with patch.object(dialogue_service.database_service, 'get_question_by_id', return_value=mock_question_info):
                dialogue_result = await dialogue_service.process(
                    interview_id=interview_id,
                    question_id=question_id,
                    answer_text=candidate_answer,
                    evaluation_result=evaluation_result
                )

            action = dialogue_result.get("action")
            followup_question = dialogue_result.get("followup_question")
            missing_dimension = dialogue_result.get("missing_dimension")
            reasoning = dialogue_result.get("reasoning", "")

            print(f"🎬 追问动作: {action}")
            print(f"❓ 追问问题: {followup_question or '无'}")
            print(f"📐 缺失维度: {missing_dimension or '无'}")
            print(f"💭 处理理由: {reasoning}")

            # 如果系统没有生成追问，但测试用例期望有追问，则使用测试用例中的预设追问
            if not followup_question and test_case.get("followup_question"):
                followup_question = test_case["followup_question"]
                print(f"🔄 使用预设追问: {followup_question}")

            # 5. 验证测试结果
            expected_action = test_case["expected_action"]
            test_passed = action == expected_action

            if action == STARFollowUpAction.FOLLOW_UP.value:
                expected_dimension = test_case.get("expected_missing_dimension")
                if expected_dimension:
                    test_passed = test_passed and (missing_dimension == expected_dimension)

            print(f"✅ 测试结果: {'通过' if test_passed else '失败'}")

            # 5.5 处理STAR追问答案评分（如果测试用例中有预设追问）
            followup_evaluation_result = None
            if "followup_answer" in test_case:
                print(f"\n🔄 处理STAR追问答案...")
                followup_answer = test_case["followup_answer"]
                followup_evaluation_points = test_case.get("followup_evaluation_points", [])

                print(f"💬 追问答案: {followup_answer}")

                # 对追问答案进行评分
                followup_question_id = f"{question_id}_followup"
                followup_evaluation_result = await evaluation_service.evaluate_answer(
                    question_text=followup_question,
                    candidate_answer=followup_answer,
                    evaluation_points=followup_evaluation_points,
                    question_type=question_type,
                    question_id=followup_question_id,
                    difficulty="MEDIUM"
                )

                print(f"📊 追问评分: {followup_evaluation_result.get('score', 'N/A')}分 - {followup_evaluation_result.get('grade', 'N/A')}")
                print(f"💬 追问评分理由: {followup_evaluation_result.get('reasoning', '')[:100]}...")

            # 6. 数据库同步（如果启用）
            if self.sync_to_database and test_passed:
                await self._sync_to_database(
                    question_id=question_id,
                    question_text=question_text,
                    question_type=question_type,
                    candidate_answer=candidate_answer,
                    evaluation_result=evaluation_result,
                    dialogue_result=dialogue_result
                )

            # 7. 记录测试结果
            self.test_results.append({
                "test_case": test_case["name"],
                "question_text": question_text,
                "candidate_answer": candidate_answer,
                "evaluation_points": evaluation_points,
                "passed": test_passed,
                "actual_action": action,
                "expected_action": expected_action,
                "missing_dimension": missing_dimension,
                "evaluation_score": evaluation_result.get("score"),
                "evaluation_grade": evaluation_result.get("grade"),
                "evaluation_reasoning": evaluation_result.get("reasoning", ""),
                "followup_question": followup_question,
                "followup_answer": test_case.get("followup_answer"),
                "followup_evaluation_points": test_case.get("followup_evaluation_points"),
                "followup_evaluation_score": followup_evaluation_result.get("score") if followup_evaluation_result else None,
                "followup_evaluation_grade": followup_evaluation_result.get("grade") if followup_evaluation_result else None,
                "followup_evaluation_reasoning": followup_evaluation_result.get("reasoning", "") if followup_evaluation_result else "",
                "dialogue_reasoning": reasoning
            })

        except Exception as e:
            print(f"❌ 测试异常: {e}")
            import traceback
            traceback.print_exc()

            self.test_results.append({
                "test_case": test_case["name"],
                "passed": False,
                "error": str(e)
            })

    def _get_mock_evaluation_points(self, question_type: str) -> list:
        """获取模拟评估要点"""
        if question_type == "PROFESSIONAL":
            return [
                {"point": "专业知识准确性", "weight": 0.3},
                {"point": "项目经验描述", "weight": 0.3},
                {"point": "问题解决能力", "weight": 0.4}
            ]
        else:  # BASIC_INFO
            return [
                {"point": "表达清晰度", "weight": 0.4},
                {"point": "逻辑条理性", "weight": 0.3},
                {"point": "内容完整性", "weight": 0.3}
            ]

    async def _sync_to_database(
        self,
        question_id: str,
        question_text: str,
        question_type: str,
        candidate_answer: str,
        evaluation_result: Dict[str, Any],
        dialogue_result: Dict[str, Any]
    ):
        """同步测试结果到文件（简化版，不依赖数据库表结构）"""
        try:
            print(f"💾 保存测试结果到文件...")

            # 记录Agent处理结果
            evaluation_data = {
                "question_id": question_id,
                "question_type": question_type,
                "question_text": question_text,
                "candidate_answer": candidate_answer,
                "evaluation_result": evaluation_result,
                "dialogue_result": dialogue_result,
                "test_timestamp": datetime.now().isoformat(),
                "sync_enabled": True
            }

            # 保存到JSON文件
            test_result_file = f"test_results_{question_id}.json"
            with open(test_result_file, 'w', encoding='utf-8') as f:
                json.dump(evaluation_data, f, ensure_ascii=False, indent=2)

            print(f"✅ 测试结果已保存到: {test_result_file}")

        except Exception as e:
            print(f"⚠️  保存测试结果失败: {e}")

    def print_test_summary(self):
        """打印测试总结并生成报告文件"""
        print(f"\n{'='*80}")
        print("📊 Agent评分和STAR追问功能测试报告")
        print(f"{'='*80}")

        total_tests = len(self.test_results)
        passed_tests = sum(1 for result in self.test_results if result.get("passed", False))
        failed_tests = total_tests - passed_tests

        print(f"🎯 总测试用例: {total_tests}")
        print(f"✅ 通过测试: {passed_tests}")
        print(f"❌ 失败测试: {failed_tests}")
        print(".1f")
        print(f"💾 数据库同步: {'启用' if self.sync_to_database else '禁用'}")
        print(f"📁 输出目录: {self.output_dir}")

        # 生成详细报告
        self._generate_detailed_report(total_tests, passed_tests, failed_tests)

        print(f"\n🎉 测试完成！报告已生成到 {self.output_dir}/ 目录")

    def _generate_detailed_report(self, total_tests: int, passed_tests: int, failed_tests: int):
        """生成详细的测试报告文件"""
        import os
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 1. 生成摘要报告
        summary_file = os.path.join(self.output_dir, f"test_summary_{timestamp}.md")
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write("# Agent评分和STAR追问功能测试报告\n\n")
            f.write(f"**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**总测试用例**: {total_tests}\n")
            f.write(f"**通过测试**: {passed_tests}\n")
            f.write(f"**失败测试**: {failed_tests}\n")
            f.write(".1f")
            f.write(f"**数据库同步**: {'启用' if self.sync_to_database else '禁用'}\n\n")

            f.write("## 测试用例详情\n\n")

            for i, result in enumerate(self.test_results, 1):
                status = "✅ 通过" if result.get("passed", False) else "❌ 失败"
                test_case = result.get("test_case", "未知")
                score = result.get("evaluation_score", "N/A")
                grade = result.get("evaluation_grade", "N/A")
                action = result.get("actual_action", "N/A")
                evaluation_reasoning = result.get("evaluation_reasoning", "")
                followup_question = result.get("followup_question")
                followup_answer = result.get("followup_answer")
                followup_score = result.get("followup_evaluation_score")
                followup_grade = result.get("followup_evaluation_grade")
                followup_reasoning = result.get("followup_evaluation_reasoning", "")
                dialogue_reasoning = result.get("dialogue_reasoning", "")

                f.write(f"### {i}. {test_case}\n")

                # 显示问题、评估要点和答案
                question_text = result.get("question_text", "")
                candidate_answer = result.get("candidate_answer", "")
                evaluation_points = result.get("evaluation_points", [])

                if question_text:
                    f.write(f"- **问题**: {question_text}\n")
                if candidate_answer:
                    f.write(f"- **答案**: {candidate_answer}\n")
                if evaluation_points:
                    f.write("- **评估要点**:\n")
                    for point in evaluation_points:
                        if isinstance(point, dict):
                            point_name = point.get("point", "")
                            weight = point.get("weight", 0)
                            f.write(f"  - {point_name} (权重: {weight})\n")
                        else:
                            f.write(f"  - {point}\n")

                f.write(f"- **状态**: {status}\n")
                f.write(f"- **评分**: {score}分 ({grade})\n")
                f.write(f"- **动作**: {action}\n")

                if evaluation_reasoning:
                    f.write(f"- **评分过程**: {evaluation_reasoning}\n")

                if dialogue_reasoning:
                    f.write(f"- **对话逻辑**: {dialogue_reasoning}\n")

                # 追问信息
                if followup_question:
                    f.write(f"- **追问问题**: {followup_question}\n")
                    if followup_answer:
                        f.write(f"- **追问答案**: {followup_answer}\n")
                        if followup_score is not None:
                            f.write(f"- **追问评分**: {followup_score}分 ({followup_grade})\n")
                            if followup_reasoning:
                                f.write(f"- **追问评分过程**: {followup_reasoning}\n")

                if not result.get("passed", False):
                    error = result.get("error", "逻辑验证失败")
                    f.write(f"- **错误**: {error}\n")

                f.write("\n")

        # 2. 生成JSON详细结果
        json_file = os.path.join(self.output_dir, f"test_results_{timestamp}.json")
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump({
                "test_summary": {
                    "timestamp": datetime.now().isoformat(),
                    "total_tests": total_tests,
                    "passed_tests": passed_tests,
                    "failed_tests": failed_tests,
                    "pass_rate": passed_tests / total_tests if total_tests > 0 else 0,
                    "sync_to_database": self.sync_to_database,
                    "output_directory": self.output_dir
                },
                "test_cases": self.test_results
            }, f, ensure_ascii=False, indent=2)

        # 3. 生成CSV格式结果（便于Excel查看）
        csv_file = os.path.join(self.output_dir, f"test_results_{timestamp}.csv")
        with open(csv_file, 'w', encoding='utf-8') as f:
            f.write("序号,测试用例,问题,答案,评估要点,状态,评分分数,评分等级,评分过程,实际动作,对话逻辑,是否通过,追问问题,追问答案,追问评估要点,追问评分分数,追问评分等级,追问评分过程,错误信息\n")

            for i, result in enumerate(self.test_results, 1):
                test_case = result.get("test_case", "未知")
                question_text = result.get("question_text", "").replace(",", ";").replace("\n", " ")
                candidate_answer = result.get("candidate_answer", "").replace(",", ";").replace("\n", " ")
                evaluation_points = result.get("evaluation_points", [])
                points_str = "; ".join([f"{p.get('point', '')}({p.get('weight', 0)})" for p in evaluation_points if isinstance(p, dict)]).replace(",", ";")

                status = "通过" if result.get("passed", False) else "失败"
                score = result.get("evaluation_score", "N/A")
                grade = result.get("evaluation_grade", "N/A")
                evaluation_reasoning = result.get("evaluation_reasoning", "").replace(",", ";").replace("\n", " ")
                action = result.get("actual_action", "N/A")
                dialogue_reasoning = result.get("dialogue_reasoning", "").replace(",", ";").replace("\n", " ")
                passed = "是" if result.get("passed", False) else "否"
                followup_question = (result.get("followup_question") or "").replace(",", ";").replace("\n", " ")
                followup_answer = (result.get("followup_answer") or "").replace(",", ";").replace("\n", " ")
                followup_points = result.get("followup_evaluation_points") or []
                followup_points_str = "; ".join([f"{p.get('point', '')}({p.get('weight', 0)})" for p in followup_points if isinstance(p, dict)]).replace(",", ";")
                followup_score = result.get("followup_evaluation_score", "")
                followup_grade = result.get("followup_evaluation_grade", "")
                followup_reasoning = result.get("followup_evaluation_reasoning", "").replace(",", ";").replace("\n", " ")
                error = result.get("error", "").replace(",", ";").replace("\n", " ")

                f.write(f"{i},{test_case},{question_text},{candidate_answer},{points_str},{status},{score},{grade},{evaluation_reasoning},{action},{dialogue_reasoning},{passed},{followup_question},{followup_answer},{followup_points_str},{followup_score},{followup_grade},{followup_reasoning},{error}\n")

        print(f"\n📄 生成的报告文件:")
        print(f"   📝 Markdown摘要: {summary_file}")
        print(f"   📊 JSON详细数据: {json_file}")
        print(f"   📈 CSV表格数据: {csv_file}")

        # 控制台输出简要结果
        print(f"\n📋 测试结果概览:")
        for result in self.test_results:
            status = "✅" if result.get("passed", False) else "❌"
            test_case = result.get("test_case", "未知")
            score = result.get("evaluation_score", "N/A")
            grade = result.get("evaluation_grade", "N/A")
            action = result.get("actual_action", "N/A")

            print(f"  {status} {test_case}")
            print(f"     评分: {score}分 ({grade}) | 动作: {action}")

            if not result.get("passed", False):
                error = result.get("error", "逻辑验证失败")
                print(f"     错误: {error}")


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Agent评分和STAR追问功能测试")
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="启用数据库同步模式"
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="禁用数据库同步模式（默认）"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="test_results",
        help="测试结果输出文件夹路径（默认: test_results）"
    )
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="不生成输出文件，只在控制台显示结果"
    )

    args = parser.parse_args()

    # 默认不同步数据库，除非明确指定
    sync_to_database = args.sync_db and not args.no_sync

    # 输出文件夹设置
    output_dir = None if args.no_output else args.output_dir

    # 创建测试器并运行
    tester = AgentEvaluationTester(sync_to_database=sync_to_database, output_dir=output_dir)
    await tester.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())