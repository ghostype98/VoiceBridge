"""
对话管理服务
处理多轮对话逻辑、状态管理、STAR法则追问等
"""
from typing import Dict, Any, Optional
from loguru import logger
import json
from enum import Enum

from config.settings import settings
from app.database.service import database_service
from agent.evaluation_service import evaluation_service


# STAR法则追问核心规则定义
class STARDimension(Enum):
    """STAR法则四个维度枚举"""
    SITUATION = "situation"  # 情境(S)：当时的情况和背景
    TASK = "task"         # 任务(T)：面临的具体任务和目标
    ACTION = "action"     # 行动(A)：采取的具体行动和步骤
    RESULT = "result"     # 结果(R)：最终的成果和影响

    @property
    def description(self) -> str:
        """获取维度描述"""
        descriptions = {
            "situation": "情境：当时的情况、背景、环境",
            "task": "任务：面临的具体任务、目标、要求",
            "action": "行动：采取的具体行动、步骤、方法",
            "result": "结果：最终的成果、影响、量化指标"
        }
        return descriptions[self.value]

    @property
    def keywords(self) -> list:
        """获取维度关键词（用于快速检测）"""
        keyword_map = {
            "situation": ["情况", "背景", "环境", "当时", "项目中", "公司", "客户"],
            "task": ["任务", "目标", "要求", "需要", "负责", "目标是", "要做"],
            "action": ["行动", "做法", "步骤", "措施", "方案", "方法", "实现", "使用", "采用"],
            "result": ["结果", "成果", "效果", "影响", "提升", "降低", "提高", "改善", "%", "倍"]
        }
        return keyword_map[self.value]

    @property
    def priority(self) -> int:
        """获取维度优先级（数字越大优先级越高）"""
        priority_map = {
            "result": 4,     # R > A > T > S
            "action": 3,
            "task": 2,
            "situation": 1
        }
        return priority_map[self.value]


class STARFollowUpAction(Enum):
    """追问动作枚举"""
    FOLLOW_UP = "follow_up"           # 触发追问
    NEXT_QUESTION = "next_question"   # 进入下一题
    END_INTERVIEW = "end_interview"   # 结束面试


# 追问规则常量
STAR_RULES = {
    "max_followups_per_question": 1,      # 单题最多追问次数
    "score_threshold_no_followup": 85,    # 评分≥85分不追问
    "score_threshold_force_followup": 50, # 评分<50分强制追问
    "only_professional_questions": True,  # 仅专业题触发STAR追问
}


class DialogueService:
    """对话管理服务类"""

    def __init__(self):
        self.conversation_states = {}  # 存储对话状态
        self.database_service = database_service
        self.evaluation_service = evaluation_service
        self.max_followups_per_question = STAR_RULES["max_followups_per_question"]

        # STAR维度关键词映射（兼容旧代码）
        self.star_keywords = {
            "situation": ["情况", "背景", "环境", "当时"],
            "task": ["任务", "目标", "要求", "需要"],
            "action": ["行动", "做法", "步骤", "如何"],
            "result": ["结果", "成果", "效果", "影响"]
        }
    
    async def process(
        self,
        interview_id: str,
        question_id: str,
        answer_text: str,
        evaluation_result: Optional[Dict[str, Any]] = None,
        conversation_state: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        STAR法则追问核心处理入口

        Args:
            interview_id: 面试会话ID
            question_id: 当前题目ID
            answer_text: 用户回答文本
            evaluation_result: LLM评分结果
            conversation_state: 对话状态（可选）

        Returns:
            结构化流程指令字典
            {
                "action": STARFollowUpAction枚举值,
                "followup_question": 追问问题文本（如果需要追问）,
                "missing_dimension": 缺失的STAR维度（如果需要追问）,
                "next_question_id": 下一题ID（如果进入下一题）,
                "reasoning": 处理逻辑说明,
                "conversation_state": 更新后的对话状态
            }
        """
        # 初始化question_info
        question_info = None

        try:
            # 1. 查询题目信息，判断是否需要STAR追问
            question_info = self.database_service.get_question_by_id(question_id)
            if not question_info:
                logger.error(f"题目不存在: {question_id}")
                return self._create_next_question_response(interview_id, question_id, "题目信息查询失败")

            question_type = question_info.get("question_type", "BASIC_INFO")

            # 2. 仅对PROFESSIONAL类型题目进行STAR追问判断
            if question_type != "PROFESSIONAL":
                logger.info(f"题目类型{question_type}，跳过STAR追问")
                return self._create_next_question_response(
                    interview_id, question_id, f"题目类型{question_type}，无需STAR追问"
                )

            # 3. 获取或初始化对话状态
            state = conversation_state or self._get_conversation_state(interview_id)

            # 4. 检查是否已对当前题目进行过追问
            followup_count = self._get_followup_count_for_question(state, question_id)
            if followup_count >= self.max_followups_per_question:
                logger.info(f"题目{question_id}已追问{followup_count}次，达到上限")
                return self._create_next_question_response(
                    interview_id, question_id, f"已达到单题最大追问次数({self.max_followups_per_question})"
                )

            # 5. 基于评分结果判断是否需要STAR追问
            followup_decision = await self._decide_star_followup(
                evaluation_result, state, question_id, answer_text, question_info
            )

            if followup_decision["should_followup"]:
                # 触发STAR追问
                missing_dimension = followup_decision["missing_dimension"]
                followup_question = self._generate_star_followup_question(missing_dimension, question_info, answer_text)

                # 更新对话状态和追问计数
                state["slot"]["current_followup_dimension"] = missing_dimension.value
                self._increment_followup_count(state, question_id)

                new_followup_count = self._get_followup_count_for_question(state, question_id)
                logger.info(f"触发STAR追问: 题目{question_id}, 维度{missing_dimension.value}, 追问次数{new_followup_count}")

                # 生成追问的评估要点
                follow_up_evaluation_points = self._generate_followup_evaluation_points(missing_dimension)

                return {
                    "action": STARFollowUpAction.FOLLOW_UP.value,
                    "followup_question": followup_question,
                    "follow_up_evaluation_points": follow_up_evaluation_points,
                    "missing_dimension": missing_dimension.value,
                    "next_question_id": None,
                    "reasoning": followup_decision["reasoning"],
                    "conversation_state": state
                }
            else:
                # 进入下一题
                logger.info(f"无需STAR追问: 题目{question_id}, 原因:{followup_decision['reasoning']}")
                return self._create_next_question_response(
                    interview_id, question_id, followup_decision["reasoning"]
                )

        except Exception as e:
            logger.error(f"STAR追问处理异常: interview_id={interview_id}, question_id={question_id}, error={e}")
            # 异常情况下仍返回下一题，避免流程中断
            return self._create_next_question_response(
                interview_id, question_id, f"追问处理异常: {str(e)}"
            )

    async def _decide_star_followup(
        self,
        evaluation_result: Optional[Dict[str, Any]],
        state: Dict[str, Any],
        question_id: str,
        answer_text: str,
        question_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        决定是否需要STAR追问

        Args:
            evaluation_result: LLM评分结果
            state: 对话状态
            question_id: 题目ID
            answer_text: 用户回答文本

        Returns:
            {
                "should_followup": bool,
                "missing_dimension": STARDimension or None,
                "reasoning": str
            }
        """
        # 1. 检查评分阈值
        if not evaluation_result:
            return {
                "should_followup": False,
                "missing_dimension": None,
                "reasoning": "无评分结果，跳过STAR追问"
            }

        score = evaluation_result.get("score", 50)

        # 评分≥85分，直接进入下一题，不触发追问
        if score >= STAR_RULES["score_threshold_no_followup"]:
            return {
                "should_followup": False,
                "missing_dimension": None,
                "reasoning": f"评分{score}分≥{STAR_RULES['score_threshold_no_followup']}分，优等表现，直接进入下一题"
            }

        # 评分<50分，直接进入下一题，不触发追问
        if score < STAR_RULES["score_threshold_force_followup"]:
            return {
                "should_followup": False,
                "missing_dimension": None,
                "reasoning": f"评分{score}分<{STAR_RULES['score_threshold_force_followup']}分，基础薄弱，直接进入下一题"
            }

        # 评分50-84分（中等），检查是否有明显缺失的STAR维度
        missing_dimension = self._get_missing_star_dimension(evaluation_result, answer_text, question_info)
        if missing_dimension:
            return {
                "should_followup": True,
                "missing_dimension": missing_dimension,
                "reasoning": f"评分{score}分中等，检测到缺失{missing_dimension.description}，触发1次追问"
            }

        # 评分中等但未检测到明显缺失的维度，直接进入下一题
        return {
            "should_followup": False,
            "missing_dimension": None,
            "reasoning": f"评分{score}分中等，未检测到明显缺失的STAR维度，直接进入下一题"
        }

    def _get_missing_star_dimension(
        self,
        evaluation_result: Dict[str, Any],
        answer_text: str,
        question_info: Optional[Dict[str, Any]] = None
    ) -> Optional[STARDimension]:
        """
        使用LLM主导判断缺失的STAR维度

        传入原问题、回答文本及明确维度标准，按R>A>T>S优先级返回首个缺失维度

        Args:
            evaluation_result: LLM评分结果
            answer_text: 用户回答文本
            question_info: 题目信息（包含question_text等）

        Returns:
            最优先缺失的STAR维度，如果没有明显缺失则返回None
        """
        try:
            # 获取原问题文本
            question_text = ""
            if question_info and isinstance(question_info, dict):
                question_text = question_info.get("question_text", "")

            # 构建LLM判断提示
            dimension_standards = {
                "result": "结果：需包含量化数据（如性能提升百分比、效率改善倍数等具体数值）或具体成果描述",
                "action": "行动：需描述具体步骤、方法、措施的实施过程",
                "task": "任务：需明确面临的具体任务、目标、要求",
                "situation": "情境：需描述当时的情况、背景、环境、挑战"
            }

            # 按优先级检查每个维度（R > A > T > S）
            priority_dimensions = [
                STARDimension.RESULT,
                STARDimension.ACTION,
                STARDimension.TASK,
                STARDimension.SITUATION
            ]

            for dimension in priority_dimensions:
                # 使用LLM判断该维度是否缺失
                if self._is_dimension_missing_by_llm(
                    dimension,
                    question_text,
                    answer_text,
                    evaluation_result,
                    dimension_standards[dimension.value]
                ):
                    return dimension

            return None

        except Exception as e:
            logger.error(f"STAR维度检测异常: {e}")
            # 异常情况下返回None，避免误触发追问
            return None

    def _is_dimension_missing_by_llm(
        self,
        dimension: STARDimension,
        question_text: str,
        answer_text: str,
        evaluation_result: Dict[str, Any],
        dimension_standard: str
    ) -> bool:
        """
        通过评分推理判断特定STAR维度是否缺失

        Args:
            dimension: STAR维度
            question_text: 原问题文本
            answer_text: 用户回答文本
            evaluation_result: 评分结果
            dimension_standard: 该维度的明确标准描述

        Returns:
            True如果维度确实缺失
        """
        try:
            # 使用评分推理中的关键词进行维度缺失判断
            reasoning = evaluation_result.get("reasoning", "").lower()

            # 根据维度定义关键词映射
            dimension_indicators = {
                "result": ["结果", "成果", "效果", "影响", "量化", "提升", "降低", "改善", "性能", "效率"],
                "action": ["行动", "做法", "步骤", "措施", "方案", "方法", "实现", "采用", "具体做", "如何"],
                "task": ["任务", "目标", "要求", "负责", "需要", "目标是", "职责"],
                "situation": ["情况", "背景", "环境", "当时", "项目中", "公司", "客户", "场景"]
            }

            indicators = dimension_indicators.get(dimension.value, [])
            missing_keywords = ["缺少", "不足", "缺乏", "没有", "未提及", "需要补充", "未提供", "没有具体"]

            # 检查是否明确提到该维度缺失
            for indicator in indicators:
                if indicator in reasoning:
                    for missing_kw in missing_keywords:
                        if missing_kw in reasoning:
                            return True

            # 检查是否明确提到该维度缺失（不包括通用改进建议）
            # 只有当推理明确指出特定维度缺失时才触发

            # 检查回答文本中是否包含该维度的关键词
            answer_lower = answer_text.lower()
            has_dimension_keywords = any(keyword in answer_lower for keyword in indicators)

            # 对于result维度，额外检查是否包含量化指标
            if dimension.value == "result":
                quantifiers = ["%", "倍", "降低", "提升", "提高", "减少", "从", "到"]
                has_quantifiers = any(q in answer_lower for q in quantifiers)
                has_dimension_keywords = has_dimension_keywords and has_quantifiers

            if not has_dimension_keywords:
                # 如果推理中提到需要改进或补充，且回答中没有相关内容，也视为缺失
                improvement_indicators = ["可以补充", "建议增加", "需要完善", "缺乏"]
                if any(indicator in reasoning for indicator in improvement_indicators):
                    return True

        except Exception as e:
            logger.error(f"维度判断异常: {e}")
            # 异常情况下保守处理，返回False避免误触发追问
            return False

        return False

    def _generate_followup_evaluation_points(self, missing_dimension: STARDimension) -> list:
        """生成追问的评估要点"""
        dimension_eval_map = {
            STARDimension.SITUATION: [
                {"point": "清晰描述问题发生的具体情境", "weight": 0.4},
                {"point": "说明背景信息和相关人员", "weight": 0.3},
                {"point": "描述环境和条件因素", "weight": 0.3}
            ],
            STARDimension.TASK: [
                {"point": "明确阐述具体任务目标", "weight": 0.4},
                {"point": "说明任务要求和标准", "weight": 0.3},
                {"point": "描述任务的复杂度和挑战", "weight": 0.3}
            ],
            STARDimension.ACTION: [
                {"point": "详细说明具体采取的行动步骤", "weight": 0.4},
                {"point": "描述使用的方法和技术", "weight": 0.3},
                {"point": "说明决策依据和考虑因素", "weight": 0.3}
            ],
            STARDimension.RESULT: [
                {"point": "量化描述最终成果和影响", "weight": 0.4},
                {"point": "说明结果的可持续性", "weight": 0.3},
                {"point": "描述经验教训和改进空间", "weight": 0.3}
            ]
        }

        return dimension_eval_map.get(missing_dimension, [
            {"point": "回答的完整性和针对性", "weight": 0.4},
            {"point": "内容的逻辑性和条理性", "weight": 0.3},
            {"point": "描述的详细程度", "weight": 0.3}
        ])

    def _generate_star_followup_question(
        self,
        missing_dimension: STARDimension,
        question_info: Dict[str, Any],
        original_answer: str
    ) -> str:
        """
        根据缺失维度生成自然流畅的追问话术

        Args:
            missing_dimension: 缺失的STAR维度
            question_info: 题目信息
            original_answer: 用户原始回答

        Returns:
            追问问题文本
        """
        question_templates = {
            "result": [
                "最终的结果或成果如何？能否分享一下具体的量化指标或影响？",
                "这个方案实施后带来了什么效果？有什么可衡量的改进吗？",
                "最后取得了什么样的成果？对项目或业务产生了什么影响？"
            ],
            "action": [
                "你采取了哪些具体的行动或步骤来解决问题？",
                "你是如何实施这个方案的？具体做了哪些工作？",
                "面对这个问题，你具体采用了什么方法或措施？"
            ],
            "task": [
                "你面临的具体任务或目标是什么？",
                "当时需要完成什么样的工作目标？",
                "这个项目中你的具体职责和任务是什么？"
            ],
            "situation": [
                "在这个项目中，当时的具体情况或背景是什么？",
                "面临这个问题的时候，项目的背景和环境是怎样的？",
                "当时遇到的具体情况和挑战是什么？"
            ]
        }

        # 获取对应维度的追问模板
        templates = question_templates.get(missing_dimension.value, [])
        if not templates:
            # 默认模板
            return f"关于{missing_dimension.description}，能否再详细说明一下？"

        # 根据题目内容和原始回答选择最合适的模板
        question_text = question_info.get("question_text", "").lower()
        answer_text = original_answer.lower()

        # 简单的模板选择逻辑（可扩展为更智能的选择）
        selected_template = templates[0]  # 默认选择第一个

        # 如果问题包含特定关键词，可以选择更合适的模板
        if "项目" in question_text and missing_dimension.value == "result":
            selected_template = templates[1]  # 选择项目相关的结果模板
        elif "技术" in question_text and missing_dimension.value == "action":
            selected_template = templates[1]  # 选择技术相关的行动模板

        return selected_template

    def _create_next_question_response(
        self,
        interview_id: str,
        current_question_id: str,
        reasoning: str
    ) -> Dict[str, Any]:
        """
        创建进入下一题的响应

        Args:
            interview_id: 面试ID
            current_question_id: 当前题目ID
            reasoning: 不追问的原因说明

        Returns:
            标准化的响应结构
        """
        # 获取下一题ID（这里需要调用面试会话服务）
        # 为了简化，这里返回None，让上层逻辑处理
        return {
            "action": STARFollowUpAction.NEXT_QUESTION.value,
            "followup_question": None,
            "missing_dimension": None,
            "next_question_id": None,  # 由上层逻辑获取
            "reasoning": reasoning,
            "conversation_state": self._get_conversation_state(interview_id)
        }

    def _get_conversation_state(self, interview_id: str) -> Dict[str, Any]:
        """
        获取或初始化对话状态

        Args:
            interview_id: 面试ID

        Returns:
            对话状态字典
        """
        if interview_id not in self.conversation_states:
            self.conversation_states[interview_id] = {
                "slot": {
                    "followup_counts": {},  # 各题目的追问次数统计
                    "total_followups": 0    # 总追问次数
                },
                "step": "interview",
                "history": []
            }

        return self.conversation_states[interview_id]

    def _get_followup_count_for_question(
        self,
        state: Dict[str, Any],
        question_id: str
    ) -> int:
        """
        获取指定题目的追问次数

        Args:
            state: 对话状态
            question_id: 题目ID

        Returns:
            该题目的追问次数
        """
        followup_counts = state.get("slot", {}).get("followup_counts", {})
        return followup_counts.get(question_id, 0)

    def _increment_followup_count(
        self,
        state: Dict[str, Any],
        question_id: str
    ) -> None:
        """
        增加指定题目的追问次数

        Args:
            state: 对话状态
            question_id: 题目ID
        """
        if "followup_counts" not in state["slot"]:
            state["slot"]["followup_counts"] = {}

        state["slot"]["followup_counts"][question_id] = \
            state["slot"]["followup_counts"].get(question_id, 0) + 1

        state["slot"]["total_followups"] = \
            state["slot"].get("total_followups", 0) + 1
    
    def _generate_response(
        self,
        asr_text: str,
        state: Dict[str, Any],
        intent: Optional[str],
        evaluation_result: Optional[Dict[str, Any]] = None
    ) -> tuple:
        """生成回复文本和新的状态"""
        step = state.get("step", "greeting")
        slot = state.get("slot", {})

        # 智能追问判断（仅对PROFESSIONAL类型题目）
        question_type = slot.get("question_type", "BASIC_INFO")
        followup_triggered = self._should_trigger_star_followup(evaluation_result, state, question_type)

        if followup_triggered:
            followup_question = self._generate_star_followup_question(state, evaluation_result)
            if followup_question:
                return followup_question, {
                    "step": f"star_{followup_triggered}",
                    "slot": slot
                }
        
        # 根据步骤生成不同的回复
        if step == "greeting":
            return "你好，欢迎参加本次面试。请先简单介绍一下你自己。", {
                "step": "self_intro",
                "slot": {}
            }
        
        elif step == "self_intro":
            # 提取项目信息
            project_name = self._extract_project_name(asr_text)
            if project_name:
                slot["project_name"] = project_name
                return f"你提到的{project_name}项目很有意思。请详细描述一下你在项目中负责的核心模块是什么？", {
                    "step": "module_responsibility",
                    "slot": slot
                }
            else:
                return "请具体说一下你参与过的项目名称。", {
                    "step": "self_intro",
                    "slot": slot
                }
        
        elif step == "module_responsibility":
            # 智能STAR追问（仅对PROFESSIONAL类型题目）
            question_type = slot.get("question_type", "BASIC_INFO")
            if question_type == "PROFESSIONAL":
                # 检查是否需要STAR追问
                followup_element = self._should_trigger_star_followup(evaluation_result, state, question_type)
                if followup_element:
                    followup_question = self._generate_star_followup_question(state, evaluation_result)
                    if followup_question:
                        return followup_question, {
                            "step": f"star_{followup_element}",
                            "slot": slot
                        }

            # 如果不需要追问或不是专业问题，直接进入下一阶段
            return "很好，你已经描述了项目经验。还有其他项目想分享吗？", {
                "step": "other_projects",
                "slot": slot
            }
        
        elif step.startswith("star_"):
            # 处理STAR追问回答
            followup_element = step.replace("star_", "")
            slot[f"star_{followup_element}"] = asr_text

            # 标记这是一个追问回答
            slot["last_followup_element"] = followup_element

            # 追问完成后，返回到正常流程
            return "谢谢你的详细说明。还有其他项目经验想分享吗？", {
                "step": "other_projects",
                "slot": slot
            }
        
        else:
            # 默认回复
            return "请继续说明。", {
                "step": step,
                "slot": slot
            }
    
    def _should_trigger_star_followup(
        self,
        evaluation_result: Optional[Dict[str, Any]],
        state: Dict[str, Any],
        question_type: str
    ) -> Optional[str]:
        """
        判断是否应该触发STAR追问

        Args:
            evaluation_result: LLM评分结果
            state: 对话状态
            question_type: 问题类型

        Returns:
            需要追问的STAR元素 ('result', 'action', 'task', 'situation') 或 None
        """
        # 仅对PROFESSIONAL类型题目触发STAR追问
        if question_type != "PROFESSIONAL":
            return None

        # 检查是否已对当前问题进行过追问（单题最多追问1次）
        current_question_id = state.get("slot", {}).get("current_question_id")
        if current_question_id:
            followup_count = sum(1 for h in state.get("history", [])
                               if h.get("question_id") == current_question_id and
                               h.get("is_followup", False))
            if followup_count >= 1:
                return None

        # 如果没有评分结果，不触发追问
        if not evaluation_result:
            return None

        score = evaluation_result.get("score", 50)

        # 评分≥85（优），无需追问
        if score >= 85:
            return None

        # 评分<50（差）且核心信息缺失，触发追问
        if score < 50:
            return self._identify_missing_star_element(evaluation_result, state)

        # 评分在50-84之间，根据优先级检查缺失元素
        return self._identify_missing_star_element(evaluation_result, state)

    def _identify_missing_star_element(
        self,
        evaluation_result: Dict[str, Any],
        state: Dict[str, Any]
    ) -> Optional[str]:
        """
        识别缺失的STAR元素（按优先级）

        优先级：R（结果）> A（行动）> T（任务）> S（情境）
        """
        slot = state.get("slot", {})

        # 检查是否已有STAR元素
        star_elements = {
            "result": slot.get("star_result"),
            "action": slot.get("star_action"),
            "task": slot.get("star_task"),
            "situation": slot.get("star_situation")
        }

        # 按优先级检查缺失的元素
        priority_order = ["result", "action", "task", "situation"]

        for element in priority_order:
            if not star_elements[element]:
                # 检查评分结果中是否反映了该元素的信息缺失
                if self._is_element_missing_in_evaluation(evaluation_result, element):
                    return element

        return None

    def _is_element_missing_in_evaluation(
        self,
        evaluation_result: Dict[str, Any],
        element: str
    ) -> bool:
        """
        根据评分结果判断特定STAR元素是否缺失
        """
        reasoning = evaluation_result.get("reasoning", "").lower()
        suggestions = evaluation_result.get("suggestions", "").lower()

        # 定义各元素的关键词
        element_keywords = {
            "result": ["结果", "成果", "效果", "影响", "outcome", "achievement"],
            "action": ["行动", "做法", "步骤", "措施", "action", "approach", "method"],
            "task": ["任务", "目标", "要求", "需要", "task", "objective", "requirement"],
            "situation": ["情况", "背景", "环境", "当时", "situation", "background", "context"]
        }

        keywords = element_keywords.get(element, [])

        # 检查推理和建议中是否提到了该元素的缺失
        combined_text = reasoning + " " + suggestions
        for keyword in keywords:
            if keyword in combined_text and ("缺少" in combined_text or "不足" in combined_text or "缺乏" in combined_text):
                return True

        return False


    def _extract_project_name(self, text: str) -> Optional[str]:
        """从文本中提取项目名称"""
        # 简单的关键词匹配，实际应该使用NLP模型
        project_keywords = ["项目", "系统", "平台", "应用", "软件"]
        for keyword in project_keywords:
            if keyword in text:
                # 尝试提取项目名称（简化版）
                words = text.split(keyword)
                if len(words) > 1:
                    # 取关键词前后的词作为项目名
                    project_name = words[0].split()[-1] + keyword + words[1].split()[0] if len(words[1].split()) > 0 else words[0].split()[-1] + keyword
                    return project_name[:20]  # 限制长度
        return None
    
    def _check_fallback(self, asr_text: str, state: Dict[str, Any]) -> bool:
        """检查是否需要触发兜底逻辑"""
        # 如果用户连续多次说"不知道"或"不清楚"
        fallback_keywords = ["不知道", "不清楚", "不明白", "没听懂"]
        if any(keyword in asr_text for keyword in fallback_keywords):
            # 检查历史记录
            history = state.get("history", [])
            if len(history) >= 2:
                recent_fallbacks = sum(1 for h in history[-2:] if any(kw in h.get("user", "") for kw in fallback_keywords))
                if recent_fallbacks >= 2:
                    return True
        return False
    
    async def get_status(self, user_id: str) -> Dict[str, Any]:
        """获取用户的对话状态"""
        if user_id in self.conversation_states:
            return {
                "user_id": user_id,
                "status": "active",
                "step": self.conversation_states[user_id].get("step"),
                "slot_count": len(self.conversation_states[user_id].get("slot", {})),
                "history_count": len(self.conversation_states[user_id].get("history", []))
            }
        else:
            return {
                "user_id": user_id,
                "status": "not_started"
            }


# 全局对话服务实例
dialogue_service = DialogueService()

