"""
面试回答评分服务
专门用于对候选人回答进行LLM智能评分的独立服务
支持不同问题类型的差异化评分和动态评估要点解析
"""

import json
import os
from typing import Dict, Any, Optional, List
from loguru import logger

from app.services.llm_service import LLMService


class EvaluationService:
    """面试回答评分服务类"""

    # 支持的问题类型
    QUESTION_TYPES = {
        "BASIC": "基础信息类问题",
        "SPECIALTY": "专业技能类问题"
    }

    def __init__(self):
        self.llm_service = LLMService()
        # 缓存加载的提示词模板
        self._prompt_cache = {}

    def _load_evaluation_prompt_by_type(self, question_type: str) -> str:
        """
        根据问题类型加载对应的评分提示词模板

        Args:
            question_type: 问题类型 ('BASIC_INFO', 'PROFESSIONAL')

        Returns:
            评分提示词模板内容
        """
        if question_type in self._prompt_cache:
            return self._prompt_cache[question_type]

        # 根据问题类型确定模板文件
        template_mapping = {
            "BASIC": "evaluation_prompt_basic.md",
            "SPECIALTY": "evaluation_prompt_professional.md"
        }

        template_file = template_mapping.get(question_type, "evaluation_prompt_basic.md")
        prompt_file = os.path.join(os.path.dirname(__file__), "prompts", template_file)

        try:
            with open(prompt_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 提取实际的提示词内容（去掉markdown标题）
            lines = content.split('\n')
            prompt_lines = []

            # 从第一个非标题行开始读取
            for line in lines:
                if line.startswith('#'):
                    continue
                if line.strip():
                    prompt_lines.append(line)

            prompt_content = '\n'.join(prompt_lines).strip()
            self._prompt_cache[question_type] = prompt_content

            logger.info(f"成功加载{question_type}类型评分模板: {template_file}")
            return prompt_content

        except Exception as e:
            logger.error(f"加载{question_type}类型评分提示词失败: {e}")
            # 返回通用默认提示词
            default_prompt = """你是一个专业的面试官，负责对候选人的回答进行量化评分。

请严格按照以下JSON格式返回评分结果：
{
  "score": 最终得分（0-100之间的数字）,
  "grade": "等级（'优'/'良'/'及格'/'差'）",
  "point_scores": [
    {"point": "要点名称", "score": 0.0/0.5/1.0, "weight": 权重值}
  ],
  "reasoning": "详细评分理由说明",
  "suggestions": "改进建议（如果有的话）"
}"""
            self._prompt_cache[question_type] = default_prompt
            return default_prompt

    async def evaluate_answer(
        self,
        question_text: str,
        candidate_answer: str,
        evaluation_points: Any,
        question_type: str = "BASIC",
        question_id: Optional[str] = None,
        difficulty: Optional[str] = None,
        standard_answer: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        使用LLM对候选人回答进行评分（支持不同问题类型）

        Args:
            question_text: 问题文本
            candidate_answer: 候选人回答
            evaluation_points: 评估要点（支持多种格式：JSON字符串、JSON对象、None）
            question_type: 问题类型 ('BASIC', 'SPECIALTY')
            question_id: 问题ID（用于追溯）
            difficulty: 难度等级（预留扩展字段）
            standard_answer: 参考答案（仅专业题使用）

        Returns:
            评分结果字典，包含元数据
        """
        try:
            # 1. 动态解析评估要点
            parsed_evaluation_points = self._parse_evaluation_points(evaluation_points, question_type)

            # 2. 加载差异化模板
            prompt_template = self._load_evaluation_prompt_by_type(question_type)

            # 3. 构建用户提示
            user_prompt = self._build_evaluation_prompt(
                question_text=question_text,
                candidate_answer=candidate_answer,
                evaluation_points=parsed_evaluation_points,
                question_type=question_type,
                difficulty=difficulty,
                standard_answer=standard_answer
            )

            # 4. 调用LLM服务
            messages = [
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": user_prompt}
            ]

            result = await self.llm_service.chat_completion(
                messages=messages,
                temperature=0.1,  # 降低温度以获得更稳定的评分
                max_tokens=800  # 减少token输出，只输出必要信息
            )

            content = result.get("content", "").strip()

            # 5. 解析和验证评分结果
            evaluation_result = self._parse_evaluation_result(content, question_type)

            # 6. 添加可追溯性元数据
            evaluation_result.update({
                "question_id": question_id,
                "question_type": question_type,
                "difficulty": difficulty,
                "evaluation_points_used": parsed_evaluation_points,
                "llm_model": result.get("model", "unknown"),
                "timestamp": json.dumps(None)  # 将在调用处设置实际时间戳
            })

            logger.info(f"LLM评分成功: question_id={question_id}, type={question_type}, "
                       f"得分={evaluation_result.get('score', 0)}, 等级={evaluation_result.get('grade', '未知')}")
            return evaluation_result

        except Exception as e:
            logger.error(f"LLM评分异常: question_id={question_id}, type={question_type}, error={e}")
            # 返回默认评分结果
            return self._get_default_evaluation_result(
                error_message=f"评分系统异常: {str(e)}",
                question_id=question_id,
                question_type=question_type
            )

    def _parse_evaluation_points(self, evaluation_points: Any, question_type: str) -> List[Dict[str, Any]]:
        """
        动态解析评估要点，支持多种输入格式

        Args:
            evaluation_points: 评估要点（多种格式）
            question_type: 问题类型，用于提供默认要点

        Returns:
            解析后的评估要点列表
        """
        try:
            # 情况1：已经是解析后的JSON对象
            if isinstance(evaluation_points, list):
                return evaluation_points

            # 情况2：JSON字符串
            if isinstance(evaluation_points, str):
                try:
                    parsed = json.loads(evaluation_points)
                    if isinstance(parsed, list):
                        return parsed
                    elif isinstance(parsed, dict):
                        # 如果是单个对象，转换为列表
                        return [parsed]
                except json.JSONDecodeError:
                    logger.warning(f"评估要点JSON解析失败: {evaluation_points}")

            # 情况3：其他格式或解析失败，使用默认评估要点
            logger.info(f"使用默认评估要点，问题类型: {question_type}")
            return self._get_default_evaluation_points(question_type)

        except Exception as e:
            logger.error(f"评估要点解析异常: {e}")
            return self._get_default_evaluation_points(question_type)

    def _get_default_evaluation_points(self, question_type: str) -> List[Dict[str, Any]]:
        """获取默认评估要点"""
        if question_type == "SPECIALTY":
            return [
                {"point": "专业知识准确性", "weight": 0.3},
                {"point": "项目经验描述", "weight": 0.3},
                {"point": "问题解决能力", "weight": 0.4}
            ]
        else:  # BASIC 或其他
            return [
                {"point": "表达清晰度", "weight": 0.4},
                {"point": "逻辑条理性", "weight": 0.3},
                {"point": "内容完整性", "weight": 0.3}
            ]

    def _build_evaluation_prompt(
        self,
        question_text: str,
        candidate_answer: str,
        evaluation_points: List[Dict[str, Any]],
        question_type: str,
        difficulty: Optional[str],
        standard_answer: Optional[str] = None
    ) -> str:
        """构建评估提示"""
        prompt_parts = [
            f"问题：{question_text}",
            "",
            f"候选人回答：{candidate_answer}",
            ""
        ]
        
        # 专业题：如果有参考答案，添加参考答案
        if question_type == "SPECIALTY" and standard_answer:
            prompt_parts.extend([
                "参考答案：",
                standard_answer,
                ""
            ])
        
        prompt_parts.append("评估要点：")
        # 添加评估要点
        prompt_parts.append(json.dumps(evaluation_points, ensure_ascii=False, indent=2))

        # 添加问题类型和难度信息
        if question_type:
            type_desc = self.QUESTION_TYPES.get(question_type, question_type)
            prompt_parts.extend(["", f"问题类型：{type_desc}"])

        if difficulty:
            prompt_parts.extend(["", f"难度等级：{difficulty}"])

        prompt_parts.extend([
            "",
            "## 评分要求",
            "1. reasoning字段必须引用候选人回答的具体内容，格式：'候选人提到[具体引用的内容]，但[缺失/不足]'",
            "2. 禁止空泛描述，如'回答一般'、'内容不足'等",
            "3. 必须从候选人回答中提取具体的关键词或短语进行引用",
            "",
            "请对以上回答进行评分，按照系统提示的要求进行分析和打分，只返回JSON格式结果。"
        ])

        return "\n".join(prompt_parts)

    def _parse_evaluation_result(self, content: str, question_type: str = "BASIC") -> Dict[str, Any]:
        """
        解析LLM返回的评分结果

        Args:
            content: LLM返回的原始内容
            question_type: 问题类型，用于确定reasoning字段长度限制

        Returns:
            解析后的评分结果
        """
        try:
            # 清理内容，移除可能的markdown代码块标记
            content = content.strip()
            if content.startswith('```json'):
                content = content[7:]
            if content.startswith('```'):
                content = content[3:]
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()

            evaluation_result = json.loads(content)

            # 验证必需字段
            required_fields = ['score', 'grade', 'point_scores', 'reasoning']
            for field in required_fields:
                if field not in evaluation_result:
                    raise ValueError(f"缺少必需字段: {field}")
            
            # 确保reasoning字段不超过限制（基础题30字，专业题40字）
            if 'reasoning' in evaluation_result:
                max_length = 40 if question_type == "SPECIALTY" else 30
                if len(evaluation_result['reasoning']) > max_length:
                    evaluation_result['reasoning'] = evaluation_result['reasoning'][:max_length]

            # 确保score是数字
            if not isinstance(evaluation_result['score'], (int, float)):
                evaluation_result['score'] = 50
            evaluation_result['score'] = max(0, min(100, evaluation_result['score']))

            # 标准化grade
            valid_grades = {'优', '良', '及格', '差', '优秀', '良好', '合格', '不合格'}
            if evaluation_result['grade'] not in valid_grades:
                # 根据分数自动判断等级
                score = evaluation_result['score']
                if score >= 82:
                    evaluation_result['grade'] = '优'
                elif score >= 58:
                    evaluation_result['grade'] = '良'
                elif score >= 42:
                    evaluation_result['grade'] = '及格'
                else:
                    evaluation_result['grade'] = '差'

            return evaluation_result

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"解析LLM评分结果失败: {e}, 原始内容: {content}")
            raise Exception(f"评分结果解析失败: {str(e)}")

    def _get_default_evaluation_result(
        self,
        error_message: str,
        question_id: Optional[str] = None,
        question_type: str = "UNKNOWN"
    ) -> Dict[str, Any]:
        """
        获取默认评分结果

        Args:
            error_message: 错误信息
            question_id: 问题ID
            question_type: 问题类型
        """
        # 根据问题类型提供不同的默认要点
        default_points = self._get_default_evaluation_points(question_type)

        return {
            "score": 50,
            "grade": "及格",
            "point_scores": [
                {**point, "score": 0.5} for point in default_points
            ],
            "reasoning": f"{error_message}，已使用默认评分",
            "suggestions": "建议人工审核该回答",
            "question_id": question_id,
            "question_type": question_type,
            "difficulty": None,
            "evaluation_points_used": default_points,
            "llm_model": "default_fallback",
            "timestamp": json.dumps(None)
        }

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            if not self.llm_service or not self.llm_service.enabled:
                return {
                    "status": "unhealthy",
                    "message": "LLM服务不可用"
                }

            # 检查提示词文件是否存在
            prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")
            required_files = [
                "evaluation_prompt_basic.md",
                "evaluation_prompt_professional.md"
            ]

            missing_files = []
            for filename in required_files:
                if not os.path.exists(os.path.join(prompts_dir, filename)):
                    missing_files.append(filename)

            if missing_files:
                return {
                    "status": "unhealthy",
                    "message": f"评分提示词文件缺失: {', '.join(missing_files)}"
                }

            return {
                "status": "healthy",
                "message": "评分服务正常",
                "supported_question_types": list(self.QUESTION_TYPES.keys()),
                "llm_status": await self.llm_service.health_check()
            }

        except Exception as e:
            logger.error(f"评分服务健康检查失败: {e}")
            return {
                "status": "unhealthy",
                "message": f"健康检查异常: {str(e)}"
            }


# 全局评分服务实例
evaluation_service = EvaluationService()