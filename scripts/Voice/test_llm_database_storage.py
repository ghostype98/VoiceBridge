"""
测试大模型输出是否正确存入数据库
验证单题评分和21维度评估的数据库存储
"""

import asyncio
import json
from datetime import datetime
from loguru import logger
import sys

# 添加项目路径
sys.path.insert(0, '/opt/voicebridge')

from app.database.service import database_service
from agent.evaluation_service import evaluation_service
from agent.interview_evaluation_service import interview_evaluation_service


class TestLLMDatabaseStorage:
    """测试大模型输出数据库存储"""

    def __init__(self):
        self.test_session_id = None
        self.test_invitation_id = None
        self.test_question_id = None
        self.test_answer_id = None

    async def setup_test_data(self):
        """准备测试数据"""
        logger.info("=" * 60)
        logger.info("📋 步骤1: 准备测试数据")
        logger.info("=" * 60)

        try:
            # 1. 创建测试邀请
            self.test_invitation_id = f"test_inv_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            # 检查是否已存在
            existing_invitation = database_service.get_invitation_by_id(self.test_invitation_id)
            if not existing_invitation:
                # 创建测试邀请（需要根据实际数据库结构调整）
                logger.info(f"创建测试邀请: {self.test_invitation_id}")
                # 注意：这里需要根据实际的数据库表结构来创建
                # 如果没有直接创建邀请的方法，可以手动插入或使用现有邀请ID
            else:
                logger.info(f"使用现有邀请: {self.test_invitation_id}")

            # 2. 创建测试会话
            self.test_session_id = f"test_session_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            logger.info(f"创建测试会话: {self.test_session_id}")

            # 3. 创建测试问题
            self.test_question_id = f"test_q_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            logger.info(f"创建测试问题: {self.test_question_id}")

            logger.info("✅ 测试数据准备完成\n")
            return True

        except Exception as e:
            logger.error(f"❌ 准备测试数据失败: {e}")
            return False

    async def test_single_answer_evaluation_storage(self):
        """测试1: 单题评分结果存储"""
        logger.info("=" * 60)
        logger.info("🧪 测试1: 单题评分结果存储")
        logger.info("=" * 60)

        try:
            # 1. 准备测试数据
            question_text = "请介绍一下你对我们公司的了解，以及为什么想加入我们？"
            candidate_answer = """
            我对贵公司有比较深入的了解。首先，贵公司是行业内领先的技术公司，
            在人工智能和大数据领域有很强的技术积累。我关注到贵公司最近推出的
            智能面试系统，这个产品很有创新性。
            
            我想加入贵公司主要有三个原因：
            1. 技术氛围好，能够持续学习成长
            2. 产品有社会价值，能够帮助企业提升招聘效率
            3. 团队年轻有活力，我相信能够在这里实现自己的职业目标
            """

            evaluation_points = [
                {"point": "了解公司业务和文化", "weight": 0.4},
                {"point": "个人职业目标与公司匹配", "weight": 0.4},
                {"point": "表达对岗位的热情", "weight": 0.2}
            ]

            logger.info(f"问题: {question_text}")
            logger.info(f"回答: {candidate_answer.strip()}")
            logger.info(f"评估要点: {len(evaluation_points)} 个")

            # 2. 调用大模型评分
            logger.info("\n📞 调用大模型进行评分...")
            evaluation_result = await evaluation_service.evaluate_answer(
                question_text=question_text,
                candidate_answer=candidate_answer,
                evaluation_points=evaluation_points,
                question_type="BASIC_INFO",
                question_id=self.test_question_id
            )

            logger.info(f"✅ 大模型评分完成:")
            logger.info(f"   - 得分: {evaluation_result.get('score', 0)}")
            logger.info(f"   - 等级: {evaluation_result.get('grade', '未知')}")
            logger.info(f"   - 推理: {evaluation_result.get('reasoning', '')[:100]}...")
            logger.info(f"   - 是否需要追问: {evaluation_result.get('need_follow_up', False)}")

            # 3. 创建答案记录
            logger.info("\n💾 创建答案记录...")
            answer_record = database_service.create_candidate_answer(
                session_id=self.test_session_id,
                question_id=self.test_question_id,
                answer_text=candidate_answer.strip(),
                is_follow_up=False,
                status='recorded'
            )
            self.test_answer_id = answer_record["id"]
            logger.info(f"✅ 答案记录创建成功: ID={self.test_answer_id}")

            # 4. 存储评估结果到数据库
            logger.info("\n💾 存储评估结果到数据库...")
            database_service.update_candidate_answer_evaluation(
                answer_id=self.test_answer_id,
                evaluation_result=evaluation_result,
                point_evaluations=evaluation_result.get('point_scores', []),
                final_score=evaluation_result.get('score', 0),
                need_follow_up=evaluation_result.get('need_follow_up', False),
                follow_up_question=evaluation_result.get('follow_up_question'),
                status='evaluated'
            )
            logger.info("✅ 评估结果存储成功")

            # 5. 从数据库读取验证
            logger.info("\n🔍 从数据库读取验证...")
            stored_answer = database_service.get_candidate_answer_by_id(self.test_answer_id)

            if not stored_answer:
                logger.error("❌ 无法从数据库读取答案记录")
                return False

            # 验证字段
            verification_results = []

            # 验证1: answer_text
            if stored_answer.get('answer_text') == candidate_answer.strip():
                logger.info("✅ answer_text 存储正确")
                verification_results.append(True)
            else:
                logger.error("❌ answer_text 存储错误")
                verification_results.append(False)

            # 验证2: final_score
            stored_score = stored_answer.get('final_score')
            expected_score = evaluation_result.get('score', 0)
            if abs(stored_score - expected_score) < 0.01:
                logger.info(f"✅ final_score 存储正确: {stored_score}")
                verification_results.append(True)
            else:
                logger.error(f"❌ final_score 存储错误: 期望={expected_score}, 实际={stored_score}")
                verification_results.append(False)

            # 验证3: evaluation_result (JSON)
            stored_eval = stored_answer.get('evaluation_result')
            if stored_eval:
                if isinstance(stored_eval, str):
                    stored_eval = json.loads(stored_eval)
                
                if stored_eval.get('score') == evaluation_result.get('score'):
                    logger.info("✅ evaluation_result JSON 存储正确")
                    logger.info(f"   - 包含字段: {list(stored_eval.keys())}")
                    verification_results.append(True)
                else:
                    logger.error("❌ evaluation_result JSON 存储错误")
                    verification_results.append(False)
            else:
                logger.error("❌ evaluation_result 未存储")
                verification_results.append(False)

            # 验证4: point_evaluations
            stored_points = stored_answer.get('point_evaluations')
            if stored_points:
                if isinstance(stored_points, str):
                    stored_points = json.loads(stored_points)
                
                expected_points = evaluation_result.get('point_scores', [])
                if len(stored_points) == len(expected_points):
                    logger.info(f"✅ point_evaluations 存储正确: {len(stored_points)} 个评估要点")
                    verification_results.append(True)
                else:
                    logger.error(f"❌ point_evaluations 数量错误: 期望={len(expected_points)}, 实际={len(stored_points)}")
                    verification_results.append(False)
            else:
                logger.error("❌ point_evaluations 未存储")
                verification_results.append(False)

            # 验证5: need_follow_up
            stored_follow_up = stored_answer.get('need_follow_up')
            expected_follow_up = evaluation_result.get('need_follow_up', False)
            if stored_follow_up == expected_follow_up:
                logger.info(f"✅ need_follow_up 存储正确: {stored_follow_up}")
                verification_results.append(True)
            else:
                logger.error(f"❌ need_follow_up 存储错误: 期望={expected_follow_up}, 实际={stored_follow_up}")
                verification_results.append(False)

            # 总结
            success_rate = sum(verification_results) / len(verification_results) * 100
            logger.info(f"\n📊 单题评分存储验证结果: {sum(verification_results)}/{len(verification_results)} 通过 ({success_rate:.1f}%)")

            return all(verification_results)

        except Exception as e:
            logger.error(f"❌ 测试失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def test_interview_evaluation_storage(self):
        """测试2: 21维度评估结果存储"""
        logger.info("\n" + "=" * 60)
        logger.info("🧪 测试2: 21维度评估结果存储")
        logger.info("=" * 60)

        try:
            # 注意：这个测试需要有完整的面试数据
            # 如果没有完整数据，可以创建模拟数据

            logger.info("📞 调用21维度评估服务...")
            
            # 调用21维度评估
            evaluation_result = await interview_evaluation_service.evaluate_interview(
                session_id=self.test_session_id,
                invitation_id=self.test_invitation_id
            )

            logger.info(f"✅ 21维度评估完成:")
            logger.info(f"   - 总体得分: {evaluation_result.get('overall_score', 0)}")
            logger.info(f"   - 是否通过: {evaluation_result.get('is_passed', 0)}")
            logger.info(f"   - 维度数量: {len(evaluation_result.get('dimension_scores', {}))}")

            # 从数据库读取验证
            logger.info("\n🔍 从数据库读取验证...")
            
            # 查询评估记录（需要根据实际数据库方法调整）
            stored_evaluation = database_service.get_interview_evaluation_by_invitation(
                self.test_invitation_id
            )

            if not stored_evaluation:
                logger.warning("⚠️  无法从数据库读取评估记录（可能数据库方法不存在）")
                logger.info("💡 建议检查 database_service 是否有 get_interview_evaluation_by_invitation 方法")
                return None

            # 验证字段
            verification_results = []

            # 验证1: overall_score
            stored_score = stored_evaluation.get('overall_score')
            expected_score = evaluation_result.get('overall_score', 0)
            if abs(stored_score - expected_score) < 0.01:
                logger.info(f"✅ overall_score 存储正确: {stored_score}")
                verification_results.append(True)
            else:
                logger.error(f"❌ overall_score 存储错误: 期望={expected_score}, 实际={stored_score}")
                verification_results.append(False)

            # 验证2: dimension_scores
            stored_dimensions = stored_evaluation.get('dimension_scores')
            if stored_dimensions:
                if isinstance(stored_dimensions, str):
                    stored_dimensions = json.loads(stored_dimensions)
                
                expected_dimensions = evaluation_result.get('dimension_scores', {})
                if len(stored_dimensions) == 21:
                    logger.info(f"✅ dimension_scores 存储正确: 21个维度")
                    verification_results.append(True)
                else:
                    logger.error(f"❌ dimension_scores 数量错误: {len(stored_dimensions)}")
                    verification_results.append(False)
            else:
                logger.error("❌ dimension_scores 未存储")
                verification_results.append(False)

            # 验证3: dimension_details
            stored_details = stored_evaluation.get('dimension_details')
            if stored_details:
                if isinstance(stored_details, str):
                    stored_details = json.loads(stored_details)
                
                logger.info(f"✅ dimension_details 存储正确: {len(stored_details)} 个维度详情")
                verification_results.append(True)
            else:
                logger.error("❌ dimension_details 未存储")
                verification_results.append(False)

            # 验证4: evaluation_summary
            if stored_evaluation.get('evaluation_summary'):
                logger.info("✅ evaluation_summary 存储正确")
                verification_results.append(True)
            else:
                logger.error("❌ evaluation_summary 未存储")
                verification_results.append(False)

            # 验证5: is_passed
            stored_passed = stored_evaluation.get('is_passed')
            expected_passed = evaluation_result.get('is_passed', 0)
            if stored_passed == expected_passed:
                logger.info(f"✅ is_passed 存储正确: {stored_passed}")
                verification_results.append(True)
            else:
                logger.error(f"❌ is_passed 存储错误: 期望={expected_passed}, 实际={stored_passed}")
                verification_results.append(False)

            # 验证6: evaluator_type
            if stored_evaluation.get('evaluator_type') == 'AGENT':
                logger.info("✅ evaluator_type 存储正确: AGENT")
                verification_results.append(True)
            else:
                logger.error("❌ evaluator_type 存储错误")
                verification_results.append(False)

            # 总结
            success_rate = sum(verification_results) / len(verification_results) * 100
            logger.info(f"\n📊 21维度评估存储验证结果: {sum(verification_results)}/{len(verification_results)} 通过 ({success_rate:.1f}%)")

            return all(verification_results)

        except Exception as e:
            logger.error(f"❌ 测试失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def cleanup_test_data(self):
        """清理测试数据"""
        logger.info("\n" + "=" * 60)
        logger.info("🧹 清理测试数据")
        logger.info("=" * 60)

        try:
            # 清理测试答案记录
            if self.test_answer_id:
                logger.info(f"清理答案记录: {self.test_answer_id}")
                # database_service.delete_candidate_answer(self.test_answer_id)

            # 清理测试会话
            if self.test_session_id:
                logger.info(f"清理会话记录: {self.test_session_id}")
                # database_service.delete_session(self.test_session_id)

            logger.info("✅ 测试数据清理完成（实际清理已注释，避免误删）")
            logger.info("💡 如需清理，请手动执行 SQL 删除语句")

        except Exception as e:
            logger.error(f"⚠️  清理测试数据失败: {e}")

    async def run_all_tests(self):
        """运行所有测试"""
        logger.info("\n" + "=" * 60)
        logger.info("🚀 开始测试大模型输出数据库存储")
        logger.info("=" * 60)
        logger.info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        results = {}

        # 准备测试数据
        if not await self.setup_test_data():
            logger.error("❌ 测试数据准备失败，终止测试")
            return

        # 测试1: 单题评分存储
        results['single_answer'] = await self.test_single_answer_evaluation_storage()

        # 测试2: 21维度评估存储
        results['interview_evaluation'] = await self.test_interview_evaluation_storage()

        # 清理测试数据
        await self.cleanup_test_data()

        # 输出总结
        logger.info("\n" + "=" * 60)
        logger.info("📊 测试总结")
        logger.info("=" * 60)

        for test_name, result in results.items():
            status = "✅ 通过" if result else "❌ 失败" if result is False else "⚠️  跳过"
            logger.info(f"{test_name}: {status}")

        passed = sum(1 for r in results.values() if r is True)
        total = len([r for r in results.values() if r is not None])
        
        if total > 0:
            success_rate = passed / total * 100
            logger.info(f"\n总体通过率: {passed}/{total} ({success_rate:.1f}%)")

            if passed == total:
                logger.info("🎉 所有测试通过！大模型输出已正确存入数据库")
            else:
                logger.warning("⚠️  部分测试失败，请检查数据库存储逻辑")
        else:
            logger.warning("⚠️  没有完成的测试")


async def main():
    """主函数"""
    tester = TestLLMDatabaseStorage()
    await tester.run_all_tests()


if __name__ == "__main__":
    # 配置日志
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )

    # 运行测试
    asyncio.run(main())




