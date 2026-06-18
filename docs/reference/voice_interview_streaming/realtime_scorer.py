# -*- coding: utf-8 -*-
"""
实时评分器
基于本地大模型对语音识别结果进行实时评分和追问决策
"""

import json
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor

from shared.config.logging_config import get_logger
from shared.config.database_config import get_default_db_config
from shared.tools.database.db_manager import DatabaseManager
from shared.tools.llm.langchain_wrapper import LangChainWrapper

logger = get_logger(__name__)


class RealtimeScorer:
    """实时评分器"""

    def __init__(self, llm_wrapper: LangChainWrapper, db_manager: DatabaseManager, config: Dict[str, Any]):
        self.llm_wrapper = llm_wrapper
        self.db_manager = db_manager
        self.config = config

        # 评分配置
        self.enable_real_time_scoring = config.get('enable_real_time_scoring', True)
        self.follow_up_score_threshold = config.get('follow_up_score_threshold', 60)
        self.scoring_timeout = config.get('scoring_timeout', 3000)
        self.min_answer_length = config.get('min_answer_length', 10)

        # LLM配置
        llm_config = config.get('llm_scoring', {})
        self.scoring_prompt_template = llm_config.get('scoring_prompt_template', self._get_default_prompt())
        self.enable_async_scoring = llm_config.get('enable_async_scoring', True)
        self.batch_size = llm_config.get('batch_size', 5)

        # 线程池
        self.executor = ThreadPoolExecutor(max_workers=self.batch_size)

        logger.info("实时评分器初始化完成")

    def evaluate_answer(self, session_id: str, question_id: str, answer_text: str) -> Dict[str, Any]:
        """评估答案
        
        评分准则：
        - 专业题（PROFESSIONAL）：根据题目、参考答案、评估要点、回答进行评分，小于标准分则触发追问
        - 基础题（BASIC_INFO）：根据题目、评估要点、回答进行评分，不进行追问
        
        注意：
        - 必须从interview_question表的question_type字段判断题目类型
        - point_evaluations字段存储评估要点（evaluation_points），不是评分维度
        """
        try:
            # 验证输入
            if not answer_text or len(answer_text.strip()) < self.min_answer_length:
                return self._create_empty_result()

            # 获取题目信息（包含题目类型）
            question_info = self._get_question_info(question_id)
            if not question_info:
                logger.warning(f"未找到题目信息: {question_id}")
                return self._create_empty_result()

            # 获取题目类型（必须从interview_question表的question_type字段获取）
            question_type = question_info.get('question_type', 'SPECIALTY')
            # 确保question_type是从数据库获取的，不是默认值
            # 注意：数据库中的类型是BASIC和SPECIALTY，不是BASIC_INFO和PROFESSIONAL
            if question_type not in ['BASIC', 'BASIC_INFO', 'SPECIALTY', 'PROFESSIONAL']:
                # 如果question_type不在预期值中，从数据库重新查询
                check_type_sql = "SELECT question_type FROM interview_question WHERE question_id = %s LIMIT 1"
                if self.db_manager.db_type != 'postgresql':
                    check_type_sql = check_type_sql.replace('%s', '?')
                type_data = self.db_manager.fetch_one(check_type_sql, (question_id,))
                if type_data:
                    question_type = type_data.get('question_type', 'SPECIALTY')
            
            # 判断是否为基础题：BASIC或BASIC_INFO都视为基础题
            is_basic_question = question_type in ['BASIC', 'BASIC_INFO']
            logger.info(f"题目类型判断: question_id={question_id}, question_type={question_type}, is_basic_question={is_basic_question}")
            
            # 如果类型判断异常，记录警告
            if question_type not in ['BASIC', 'BASIC_INFO', 'SPECIALTY', 'PROFESSIONAL']:
                logger.warning(f"未知的题目类型: question_id={question_id}, question_type={question_type}")

            # 检查答案质量：是否只是重复题目关键词
            question_content = question_info.get('question_content', '')
            is_keyword_repeat = self._is_answer_just_repeating_keywords(answer_text, question_content)
            
            if is_keyword_repeat:
                logger.warning(f"答案只是重复题目关键词，质量较差: question_id={question_id}, answer_length={len(answer_text)}")
                # 对于这种答案，直接给予低分，不需要调用LLM
                score_result = {
                    'score': 35,  # 低分
                    'reason': '答案只是简单重复题目中的关键词，没有提供实际内容、具体措施或实例说明。请提供更详细的回答，包括具体的措施和实际经历。',
                    'details': {
                        'content_completeness': 5,
                        'logical_clarity': 10,
                        'professional_level': 10,
                        'expression_ability': 10
                    } if not is_basic_question else {
                        'information_completeness': 10,
                        'expression_clarity': 10,
                        'language_fluency': 15
                    }
                }
                need_follow_up = True if not is_basic_question else False
                follow_up_question = '您能否详细说明一下具体采取了哪些措施？能否举一个具体的例子？' if not is_basic_question else None
                
                # 保存评分结果到数据库（传递question_info以便获取评估要点）
                self._save_evaluation_result(
                    session_id, question_id, answer_text, score_result, need_follow_up, follow_up_question, question_info
                )
                
                # 获取评估要点
                evaluation_points = question_info.get('evaluation_points')
                if evaluation_points:
                    try:
                        if isinstance(evaluation_points, str):
                            evaluation_points = json.loads(evaluation_points)
                    except Exception as e:
                        logger.warning(f"解析评估要点失败: {str(e)}")
                        evaluation_points = None
                else:
                    evaluation_points = None
                
                return {
                    'score': score_result['score'],
                    'reason': score_result['reason'],
                    'need_follow_up': need_follow_up,
                    'follow_up_question': follow_up_question,
                    'evaluation_details': score_result['details'],  # 评分维度
                    'point_evaluations': evaluation_points,  # 评估要点
                    'question_type': question_type,
                    'timestamp': datetime.now().isoformat()
                }

            # 构建评分提示（根据题目类型使用不同的提示模板）
            prompt = self._build_scoring_prompt(question_info, answer_text, is_basic_question)

            # 调用LLM进行评分（使用chat方法，不是chat_completion）
            llm_response = self.llm_wrapper.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,  # 降低随机性，提高评分一致性
                max_tokens=500
                # 注意：timeout参数可能不被支持，如果需要超时控制，应该在外部实现
            )

            if not llm_response:
                logger.error("LLM评分调用失败")
                return self._create_empty_result()

            # 解析评分结果
            score_result = self._parse_llm_response(llm_response)

            # 决定是否需要追问（基础题不追问）
            need_follow_up = False
            if not is_basic_question:
                # 只有专业题才进行追问判断
                need_follow_up = self._should_follow_up(score_result.get('score', 0))
            else:
                logger.info(f"基础题不进行追问: question_id={question_id}")

            # 生成追问问题（如果需要）
            follow_up_question = None
            if need_follow_up:
                follow_up_question = self._generate_follow_up_question(
                    question_info, answer_text, score_result
                )

            # 保存评分结果到数据库（传递question_info以便获取评估要点）
            self._save_evaluation_result(
                session_id, question_id, answer_text, score_result, need_follow_up, follow_up_question, question_info
            )

            result = {
                'score': score_result.get('score', 0),
                'reason': score_result.get('reason', ''),
                'need_follow_up': need_follow_up,
                'follow_up_question': follow_up_question,
                'evaluation_details': score_result.get('details', {}),  # 评分维度（dimensions）
                'point_evaluations': point_evaluations if 'point_evaluations' in locals() else None,  # 评估要点
                'question_type': question_type,
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"答案评分完成: session={session_id}, question={question_id}, type={question_type}, score={result['score']}, follow_up={need_follow_up}")

            return result

        except Exception as e:
            logger.error(f"评估答案失败: {str(e)}", exc_info=True)
            return self._create_empty_result()

    def _get_question_info(self, question_id: str) -> Optional[Dict[str, Any]]:
        """获取题目信息（包含题目类型）"""
        try:
            sql = """
            SELECT
                iq.question_id,
                iq.question_type,
                iq.atomic_question_id,
                iqs.content as question_content,
                iqs.ref_type,
                iqs.evaluation_points,
                iqs.standard_answer
            FROM interview_question iq
            JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iq.question_id = %s
            """

            if self.db_manager.db_type != 'postgresql':
                sql = sql.replace('%s', '?')

            result = self.db_manager.fetch_one(sql, (question_id,))
            return result

        except Exception as e:
            logger.error(f"获取题目信息失败: {str(e)}")
            return None

    def _build_scoring_prompt(self, question_info: Dict[str, Any], answer_text: str, is_basic_question: bool = False) -> str:
        """构建评分提示
        
        Args:
            question_info: 题目信息
            answer_text: 候选人答案
            is_basic_question: 是否为基础题（BASIC_INFO）
        """
        question_content = question_info.get('question_content', '')
        evaluation_points = question_info.get('evaluation_points')
        standard_answer = question_info.get('standard_answer')
        ref_type = question_info.get('ref_type', 'AI_RUBRIC')
        question_type = question_info.get('question_type', 'PROFESSIONAL')

        # 根据题目类型选择不同的提示模板
        if is_basic_question:
            prompt = f"""请对以下基础信息面试答案进行评分：

问题：{question_content}
候选人答案：{answer_text}

"""
        else:
            prompt = f"""请对以下专业面试答案进行评分：

问题：{question_content}
候选人答案：{answer_text}

"""

        # 专业题：如果有参考答案，提供参考答案（所有专业题都使用，不限制ref_type）
        if not is_basic_question and standard_answer:
            prompt += f"""参考答案：{standard_answer}

"""

        # 评估要点（基础题和专业题都使用）
        if evaluation_points:
            try:
                if isinstance(evaluation_points, str):
                    evaluation_points = json.loads(evaluation_points)
                prompt += "评估要点：\n"
                for point in evaluation_points:
                    prompt += f"- {point.get('point', '')} (权重: {point.get('weight', 0)})\n"
                prompt += "\n"
            except Exception as e:
                logger.warning(f"解析评估要点失败: {str(e)}")

        # 根据题目类型选择不同的评分维度
        if is_basic_question:
            prompt += """请从以下维度进行评分（总分100分）：
1. 信息完整性（40分）：答案是否完整回答了问题
2. 表达清晰度（30分）：回答是否清晰易懂
3. 语言流畅度（30分）：语言表达的流畅度和得体程度

重要评分规则：
- **严格评分**：如果答案只是简单重复题目中的关键词，没有实际内容，应给予低分（20-40分）
- **答案质量判断**：
  * 优秀答案（80-100分）：信息完整、表达清晰、语言流畅、有具体内容
  * 良好答案（60-79分）：信息基本完整、表达基本清晰、有一定内容
  * 较差答案（40-59分）：只是重复题目关键词、没有实际内容、内容空洞
  * 极差答案（0-39分）：完全无法理解、与问题无关、只是读题目
- **特别严格**：如果答案只是简单读了题目内容，没有提供任何实际信息，应给予极低分（10-30分）
- 评分要客观公正，基于答案的实际质量
- 理由要具体详细，便于面试官理解

注意：基础题不进行追问，只进行评分。

请以JSON格式返回评分结果：
{
  "score": 85,
  "reason": "详细的评分理由",
  "dimensions": {
    "information_completeness": 35,
    "expression_clarity": 25,
    "language_fluency": 25
  }
}"""
        else:
            prompt += """请从以下维度进行评分（总分100分）：
1. 内容完整性（25分）：答案是否全面回答了问题
2. 逻辑清晰度（25分）：回答是否逻辑清晰，有条理
3. 专业程度（25分）：回答的专业性和准确性
4. 表达能力（25分）：语言表达的流畅度和得体程度

重要评分规则：
- **严格评分**：如果答案只是简单重复题目中的关键词，没有实际内容、没有具体措施、没有实例说明，应给予低分（20-40分）
- **特别严格**：如果答案只是简单读了题目内容，没有提供任何实际信息、措施或实例，应给予极低分（10-30分）
- **答案质量判断**：
  * 优秀答案（80-100分）：有具体措施、有实例、逻辑清晰、表达流畅、内容充实
  * 良好答案（60-79分）：有基本措施、有一定逻辑，但缺少实例或不够深入
  * 较差答案（40-59分）：只是重复题目关键词、没有实际内容、逻辑混乱、内容空洞
  * 极差答案（0-39分）：完全无法理解、与问题无关、只是读题目
- **内容完整性要求**：
  * 必须回答"采取哪些措施"（不能只说"采取措施"）
  * 必须回答"是否有过经历"（不能只说"有经历"）
  * 必须有具体内容，不能只是题目关键词的简单重复
  * 如果只是读了题目，没有实际回答，应给予极低分（10-30分）
- 评分要客观公正，基于答案的实际质量
- 理由要具体详细，便于面试官理解
- 如果答案质量较差（低于60分），建议提供追问的方向

请以JSON格式返回评分结果：
{
  "score": 85,
  "reason": "详细的评分理由和改进建议",
  "dimensions": {
    "content_completeness": 20,
    "logical_clarity": 22,
    "professional_level": 18,
    "expression_ability": 25
  }
}"""

        return prompt

    def _parse_llm_response(self, response: str) -> Dict[str, Any]:
        """解析LLM响应"""
        try:
            # 尝试提取JSON
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                result = json.loads(json_str)

                return {
                    'score': result.get('score', 0),
                    'reason': result.get('reason', ''),
                    'details': result.get('dimensions', {})
                }

        except Exception as e:
            logger.warning(f"解析LLM响应失败: {str(e)}")

        # 如果JSON解析失败，使用默认值
        return {
            'score': 60,  # 默认中等分数
            'reason': f'LLM响应解析失败，使用默认评分。原始响应: {response[:200]}...',
            'details': {}
        }

    def _should_follow_up(self, score: float) -> bool:
        """判断是否需要追问"""
        return score < self.follow_up_score_threshold

    def _generate_follow_up_question(self, question_info: Dict[str, Any],
                                   answer_text: str, score_result: Dict[str, Any]) -> str:
        """生成追问问题"""
        try:
            question_content = question_info.get('question_content', '')

            prompt = f"""基于以下信息，生成一个针对性的追问问题：

原问题：{question_content}
候选人答案：{answer_text}
评分结果：{score_result.get('score', 0)}分
评分理由：{score_result.get('reason', '')}

请生成一个简洁、有针对性的追问问题，帮助候选人更好地展示自己的能力。
追问问题应该：
1. 针对答案中的薄弱环节
2. 引导候选人提供更具体或深入的回答
3. 保持专业性和建设性

只需返回追问问题文本，不要其他内容。"""

            response = self.llm_wrapper.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=200,
                timeout=10
            )

            if response:
                # 清理响应文本
                follow_up = response.strip()
                if follow_up.startswith('"') and follow_up.endswith('"'):
                    follow_up = follow_up[1:-1]
                return follow_up

        except Exception as e:
            logger.error(f"生成追问问题失败: {str(e)}")

        # 默认追问问题
        return f"关于刚才的问题，您能否再详细说明一下具体是如何实现的？"

    def _save_evaluation_result(self, session_id: str, question_id: str, answer_text: str,
                               score_result: Dict[str, Any], need_follow_up: bool,
                               follow_up_question: Optional[str], question_info: Optional[Dict[str, Any]] = None):
        """保存评分结果到数据库
        
        同时更新两个表：
        1. interview_session表：更新会话内容和状态
        2. candidate_answers表：存储答案和详细评分结果
        
        注意：
        - point_evaluations字段存储评估要点（evaluation_points），不是评分维度
        - 评分维度（dimensions）存储在evaluation_details中
        """
        try:
            import uuid
            import json
            from datetime import datetime
            
            # 获取评估要点（如果question_info未提供，则从数据库查询）
            if not question_info:
                question_info = self._get_question_info(question_id)
            
            evaluation_points = None
            if question_info:
                evaluation_points = question_info.get('evaluation_points')
                if evaluation_points:
                    try:
                        if isinstance(evaluation_points, str):
                            evaluation_points = json.loads(evaluation_points)
                    except Exception as e:
                        logger.warning(f"解析评估要点失败: {str(e)}")
                        evaluation_points = None
            
            # 1. 更新interview_session表中的评估信息（保留用于会话文本展示）
            update_session_sql = """
            UPDATE interview_session
            SET candidate_answer = %s,
                session_content = COALESCE(session_content, '') || %s,
                session_status = CASE WHEN %s THEN 'COMPLETED' ELSE 'IN_PROGRESS' END,
                end_time = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE end_time END,
                follow_up_used = COALESCE(follow_up_used, 0) + CASE WHEN %s THEN 1 ELSE 0 END
            WHERE session_id = %s
            """

            if self.db_manager.db_type != 'postgresql':
                update_session_sql = update_session_sql.replace('%s', '?')

            # 将分数转换为1-5分制（原来是1-100分）
            score_5_scale = min(5, max(1, round(score_result.get('score', 60) / 20)))

            session_content = f"\n[AI评分:{score_5_scale}/5] {score_result.get('reason', '')}"
            if need_follow_up and follow_up_question:
                session_content += f"\n[AI追问] {follow_up_question}"

            self.db_manager.execute(update_session_sql, (
                answer_text,
                session_content,
                not need_follow_up,  # 如果不需要追问，则标记为完成
                not need_follow_up,  # 如果不需要追问，则设置结束时间
                need_follow_up,      # 如果需要追问，则增加追问计数
                session_id
            ))

            # 2. 保存到candidate_answers表（主问题答案，is_follow_up=false）
            # 检查是否已存在该答案记录
            check_answer_sql = """
            SELECT id FROM candidate_answers 
            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
            LIMIT 1
            """
            if self.db_manager.db_type != 'postgresql':
                check_answer_sql = check_answer_sql.replace('%s', '?')
            
            existing_answer = self.db_manager.fetch_one(check_answer_sql, (session_id, question_id))
            
            # 准备评估数据
            final_score = score_result.get('score', 0)
            
            # point_evaluations字段存储评估要点（evaluation_points），不是评分维度
            # 评分维度（dimensions）存储在evaluation_details中，不存储在point_evaluations
            point_evaluations = evaluation_points  # 将evaluation_points赋值给point_evaluations用于存储
            
            # 构建完整的评估结果（JSON格式）
            evaluation_result = {
                'score': final_score,
                'reason': score_result.get('reason', ''),
                'dimensions': score_result.get('details', {}),  # 评分维度详情
                'need_follow_up': need_follow_up,
                'follow_up_question': follow_up_question,
                'timestamp': datetime.now().isoformat()
            }
            
            if existing_answer:
                # 更新现有答案记录
                answer_id = existing_answer['id']
                update_answer_sql = """
                UPDATE candidate_answers
                SET answer_text = %s,
                    status = 'evaluated',
                    point_evaluations = %s,
                    final_score = %s,
                    need_follow_up = %s,
                    follow_up_question = %s,
                    evaluation_result = %s,
                    update_time = CURRENT_TIMESTAMP
                WHERE id = %s
                """
                if self.db_manager.db_type != 'postgresql':
                    update_answer_sql = update_answer_sql.replace('%s', '?')
                
                import json
                self.db_manager.execute(update_answer_sql, (
                    answer_text,
                    json.dumps(point_evaluations) if point_evaluations else None,  # point_evaluations存储评估要点
                    final_score,
                    need_follow_up,
                    follow_up_question,
                    json.dumps(evaluation_result),  # evaluation_result存储完整的评估结果
                    answer_id
                ))
                logger.debug(f"已更新candidate_answers记录: answer_id={answer_id}, score={final_score}")
            else:
                # 创建新答案记录
                answer_id = f"ANS_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
                insert_answer_sql = """
                INSERT INTO candidate_answers (
                    id, session_id, question_id, answer_text,
                    is_follow_up, parent_answer_id,
                    status, point_evaluations, final_score,
                    need_follow_up, follow_up_question, evaluation_result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                if self.db_manager.db_type != 'postgresql':
                    insert_answer_sql = insert_answer_sql.replace('%s', '?')
                
                import json
                self.db_manager.execute(insert_answer_sql, (
                    answer_id,
                    session_id,
                    question_id,
                    answer_text,
                    False,  # is_follow_up = False（主问题答案）
                    None,  # parent_answer_id = None（主问题没有父答案）
                    'evaluated',  # status = 'evaluated'
                    json.dumps(point_evaluations) if point_evaluations else None,  # point_evaluations存储评估要点
                    final_score,
                    need_follow_up,
                    follow_up_question,
                    json.dumps(evaluation_result)  # evaluation_result存储完整的评估结果
                ))
                logger.debug(f"已创建candidate_answers记录: answer_id={answer_id}, score={final_score}")

            logger.debug(f"语音评估结果已保存: session_id={session_id}, answer_id={answer_id}, score={final_score}")

        except Exception as e:
            logger.error(f"保存评分结果失败: {str(e)}", exc_info=True)

    def _is_answer_just_repeating_keywords(self, answer_text: str, question_text: str) -> bool:
        """检查答案是否只是重复题目关键词或只是读了题目
        
        Args:
            answer_text: 候选人答案
            question_text: 题目内容
            
        Returns:
            bool: 如果答案只是重复关键词或只是读题目，返回True
        """
        if not answer_text or not question_text:
            return False
        
        # 检查答案是否只是题目的简单重复（去除标点符号和空格后比较）
        import re
        answer_clean = re.sub(r'[，。！？、；：\s]', '', answer_text)
        question_clean = re.sub(r'[，。！？、；：\s]', '', question_text)
        
        # 如果答案和题目高度相似（相似度>80%），认为是读题目
        if len(answer_clean) > 0 and len(question_clean) > 0:
            # 计算字符重叠度
            answer_chars = set(answer_clean)
            question_chars = set(question_clean)
            if len(answer_chars) > 0:
                overlap_ratio = len(answer_chars & question_chars) / len(answer_chars)
                if overlap_ratio > 0.8:
                    return True
        
        # 提取题目中的关键词（去除常见停用词）
        stop_words = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这', '请', '您', '如何', '什么', '哪些', '怎样'}
        
        # 提取题目中的关键词（长度>=2的词语）
        question_words = set()
        for word in re.findall(r'[\u4e00-\u9fff]{2,}', question_text):
            if word not in stop_words:
                question_words.add(word)
        
        # 提取答案中的关键词
        answer_words = set()
        for word in re.findall(r'[\u4e00-\u9fff]{2,}', answer_text):
            if word not in stop_words:
                answer_words.add(word)
        
        # 如果答案中的关键词大部分都来自题目，且答案很短，认为是重复关键词
        if len(answer_words) == 0:
            return False
        
        # 计算答案中来自题目的关键词比例
        common_words = answer_words & question_words
        overlap_ratio = len(common_words) / len(answer_words) if len(answer_words) > 0 else 0
        
        # 如果重叠比例>70%且答案很短（<50字符），认为是重复关键词
        if overlap_ratio > 0.7 and len(answer_text) < 50:
            return True
        
        # 如果答案只是题目的片段（答案长度<题目长度的30%）
        if len(answer_text) < len(question_text) * 0.3:
            # 检查答案是否只是题目的片段
            answer_chars = set(answer_text.replace(' ', '').replace('，', '').replace('。', ''))
            question_chars = set(question_text.replace(' ', '').replace('，', '').replace('。', ''))
            if len(answer_chars) > 0 and len(answer_chars & question_chars) / len(answer_chars) > 0.8:
                return True
        
        return False

    def _create_empty_result(self) -> Dict[str, Any]:
        """创建空结果"""
        return {
            'score': 0,
            'reason': '答案过短或无效，无法进行有效评分',
            'need_follow_up': False,
            'follow_up_question': None,
            'evaluation_details': {},
            'timestamp': datetime.now().isoformat()
        }

    def _get_default_prompt(self) -> str:
        """获取默认评分提示模板"""
        return """请对以下面试答案进行评分（1-100分）：
问题：{question}
答案：{answer}

评分标准：
- 内容完整性：答案是否全面回答了问题
- 逻辑清晰度：回答是否逻辑清晰
- 专业程度：回答的专业性和准确性
- 表达能力：语言表达的流畅度

请返回JSON格式：{{"score": 85, "reason": "详细评分理由"}}"""

    def get_evaluation_stats(self) -> Dict[str, Any]:
        """获取评分统计信息"""
        try:
            sql = """
            SELECT
                COUNT(*) as total_evaluations,
                AVG(score) as avg_score,
                MIN(score) as min_score,
                MAX(score) as max_score,
                SUM(CASE WHEN need_follow_up THEN 1 ELSE 0 END) as follow_up_count
            FROM voice_evaluation_results
            WHERE created_at >= CURRENT_DATE
            """

            result = self.db_manager.fetch_one(sql)
            return result or {}

        except Exception as e:
            logger.error(f"获取评分统计失败: {str(e)}")
            return {}

    def cleanup_old_results(self, days: int = 30):
        """清理旧的评分结果"""
        try:
            sql = """
            DELETE FROM voice_evaluation_results
            WHERE created_at < CURRENT_DATE - INTERVAL '%s days'
            """

            if self.db_manager.db_type != 'postgresql':
                sql = sql.replace("CURRENT_DATE - INTERVAL '%s days'", "datetime('now', '-%s days')")

            self.db_manager.execute(sql, (days,))

            logger.info(f"已清理{days}天前的评分结果")

        except Exception as e:
            logger.error(f"清理旧评分结果失败: {str(e)}")