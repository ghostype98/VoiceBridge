# -*- coding: utf-8 -*-
"""
实时评分器
基于本地大模型对语音识别结果进行实时评分和追问决策
"""

import json
import os
import uuid
import re
import ast
from datetime import datetime
from typing import Dict, Any, Optional, List, Union, Set
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from config.settings import settings
from app.database.connection import DatabaseManager
from app.services.llm_service import LLMService

# 使用loguru logger


class RealtimeScorer:
    """实时评分器"""

    def __init__(self, llm_service: LLMService, db_manager: DatabaseManager, config: Dict[str, Any]):
        self.llm_service = llm_service
        self.db_manager = db_manager
        self.config = config

        # 评分配置
        self.enable_real_time_scoring = config.get('enable_real_time_scoring', True)
        self.follow_up_score_threshold = config.get('follow_up_score_threshold', 60)
        self.scoring_timeout = config.get('scoring_timeout', 3000)
        self.min_answer_length = config.get('min_answer_length', 10)
        streaming_config = config.get('streaming_interview', {}) or {}
        self.max_follow_ups_per_interview = int(
            streaming_config.get(
                'max_follow_ups_per_interview',
                config.get('max_follow_ups_per_interview', 1),
            )
        )
        self.max_follow_ups_per_question = int(
            streaming_config.get(
                'max_follow_ups_per_question',
                config.get('max_follow_ups_per_question', 1),
            )
        )

        # LLM配置
        llm_config = config.get('llm_scoring', {})
        self.scoring_prompt_template = llm_config.get('scoring_prompt_template', self._get_default_prompt())
        self.enable_async_scoring = llm_config.get('enable_async_scoring', True)
        self.batch_size = llm_config.get('batch_size', 5)
        # 配置优先、业务可覆盖：业务层默认不再写死 token 值
        self.scoring_max_tokens = llm_config.get('scoring_max_tokens', settings.LLM_MAX_TOKENS)
        self.follow_up_max_tokens = llm_config.get('follow_up_max_tokens', self.scoring_max_tokens)
        self.retry_on_truncation = llm_config.get('retry_on_truncation', True)
        self.retry_multiplier = llm_config.get('retry_multiplier', 2)
        self.retry_max_tokens_cap = llm_config.get('retry_max_tokens_cap', 4096)
        # 默认关闭长度二次惩罚，避免与提示词中的长度约束叠加
        self.enable_length_penalty_calibration = llm_config.get('enable_length_penalty_calibration', False)
        self.basic_min_expected_length = llm_config.get('basic_min_expected_length', 30)
        self.professional_min_expected_length = llm_config.get('professional_min_expected_length', 90)
        # 双轨评分维度配额（总和建议为100）
        self.basic_dimension_caps = self._parse_dimension_caps(
            llm_config.get('basic_dimension_caps'),
            (40, 20, 30, 10),  # BASIC：实质重于形式（内容优先）
        )
        self.professional_dimension_caps = self._parse_dimension_caps(
            llm_config.get('professional_dimension_caps'),
            (40, 20, 30, 10),  # SPECIALTY：强调命中与技术深度
        )
        # 记录同一进程内每个 session 的日志文件名，确保“同一批次合并、不同批次分文件”
        self._session_log_file_map: Dict[str, str] = {}

        # 线程池
        self.executor = ThreadPoolExecutor(max_workers=self.batch_size)

        logger.info("实时评分器初始化完成")

    def _get_invitation_id_for_session(self, session_id: str) -> Optional[str]:
        try:
            sql = "SELECT invitation_id FROM interview_session WHERE session_id = %s LIMIT 1"
            if self.db_manager.db_type != "postgresql":
                sql = sql.replace("%s", "?")
            row = self.db_manager.fetch_one(sql, (session_id,))
            if not row:
                return None
            return row.get("invitation_id") if isinstance(row, dict) else row[0]
        except Exception as e:
            logger.debug(f"查询 invitation_id 失败: {e}")
            return None

    def _get_candidate_name_for_invitation(self, invitation_id: str) -> Optional[str]:
        try:
            sql = "SELECT candidate_name FROM interview_invitation WHERE invitation_id = %s LIMIT 1"
            if self.db_manager.db_type != "postgresql":
                sql = sql.replace("%s", "?")
            row = self.db_manager.fetch_one(sql, (invitation_id,))
            if not row:
                return None
            return row.get("candidate_name") if isinstance(row, dict) else row[0]
        except Exception as e:
            logger.debug(f"查询 candidate_name 失败: {e}")
            return None

    def _write_per_question_realtime_eval_log(
        self,
        *,
        session_id: str,
        question_id: str,
        question_info: Dict[str, Any],
        answer_text: str,
        prompt: Optional[str],
        score_parsed: Optional[Dict[str, Any]],
        score_final: Dict[str, Any],
        llm_raw_response: Optional[Any],
        is_basic_question: bool,
        question_type: str,
        rescore_mode: bool,
        follow_up_scoring: bool,
        path_note: str = "",
    ) -> None:
        """将单题实时评分信息追加到同一次评估的汇总日志文件（便于审计）。"""
        try:
            # 同一 session 的多题评分写入 logs/realtime_merged 下同一个文件
            merged_dir = os.path.join(settings.PROJECT_ROOT, "logs", "realtime_merged")
            os.makedirs(merged_dir, exist_ok=True)
            inv_id = self._get_invitation_id_for_session(session_id) or ""
            cname = self._get_candidate_name_for_invitation(inv_id) if inv_id else None
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_qid = "".join(c if c.isalnum() or c in "._-" else "_" for c in question_id)[:56]
            safe_sid = "".join(c if c.isalnum() or c in "._-" else "_" for c in (session_id or ""))[:64] or "unknown_session"

            def _safe_token(raw: Optional[str], default: str, max_len: int = 24) -> str:
                token = "".join(c if c.isalnum() or c in "._-" else "_" for c in (raw or "").strip())
                token = token.strip("._-")
                if not token:
                    token = default
                return token[:max_len]

            safe_name = _safe_token(cname, "unknown_candidate", max_len=24)
            day = datetime.now().strftime("%Y%m%d")
            sid_short = safe_sid[-8:]
            inv_short = _safe_token(inv_id, "noinv", max_len=12)
            # 同一批次（同一进程同一session）复用同一文件；不同批次自动使用新时间戳新文件
            fname = self._session_log_file_map.get(session_id)
            if not fname:
                batch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"realtime_{safe_name}_{day}_{batch_ts}_{inv_short}_{sid_short}.txt"
                self._session_log_file_map[session_id] = fname
            path = os.path.join(merged_dir, fname)

            def _fmt_json(obj: Any) -> str:
                try:
                    return json.dumps(obj, ensure_ascii=False, indent=2)
                except Exception:
                    return str(obj)

            def _fmt_llm_raw(resp: Any) -> str:
                if resp is None:
                    return "(空)"
                if isinstance(resp, dict):
                    s = _fmt_json(resp)
                    if len(s) > 16000:
                        return s[:16000] + "\n\n... [截断，总长度超过 16000 字符] ..."
                    return s
                s = str(resp)
                return s[:16000] + ("..." if len(s) > 16000 else "")

            qc = question_info.get("question_content") or ""
            std = question_info.get("standard_answer")
            ref_type = question_info.get("ref_type", "")
            evp = question_info.get("evaluation_points")

            header_lines = [
                "=" * 100,
                "实时面试 — 单次评估汇总日志（多题合并）",
                "=" * 100,
                f"首次写入时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Session ID: {session_id}",
                f"Invitation ID: {inv_id or '(未解析)'}",
                f"候选人姓名: {cname or '(未解析)'}",
                "=" * 100,
                "",
            ]
            lines = [
                "-" * 100,
                f"题目日志写入时间: {ts}",
                f"Question ID: {question_id}",
                f"题目类型(库): {question_type}",
                f"是否基础题: {is_basic_question}",
                f"是否追问评分: {follow_up_scoring}",
                f"rescore_mode: {rescore_mode}",
                f"评分路径说明: {path_note or 'LLM 正常评分'}",
                "",
                "【候选人回答（语音转写）】",
                "-" * 100,
                (answer_text or "").strip() or "(空)",
                "",
                "-" * 100,
                "【本题参考信息（写入提示词的上下文）】",
                "-" * 100,
                "题干 (question_content):",
                qc or "(无)",
                "",
                f"ref_type: {ref_type or '(无)'}",
                "",
                "参考答案 (standard_answer):",
                (std or "(本题无参考答案或未配置)").strip() or "(无)",
                "",
                "评估要点 (evaluation_points):",
                _fmt_json(evp) if evp is not None else "(无)",
                "",
                "-" * 100,
                "【完整评分提示词（发送给 LLM 的 user 消息全文）】",
                "-" * 100,
                (prompt or "(未生成提示词 — 可能为短路分支)").strip() or "(无)",
                "",
                "-" * 100,
                "【LLM 原始响应（解析前）】",
                "-" * 100,
                _fmt_llm_raw(llm_raw_response),
                "",
                "-" * 100,
                "【解析后 / 校准后得分】",
                "-" * 100,
            ]
            if score_parsed is not None:
                lines.append("解析后（校准前）score: " + str(score_parsed.get("score")))
                lines.append("解析后 dimensions: " + _fmt_json(score_parsed.get("details") or {}))
                lines.append("解析后 reason: " + (str(score_parsed.get("reason") or "").strip() or "(无)"))
                lines.append("解析后 audit_trace: " + _fmt_json(score_parsed.get("audit_trace") or {}))
                lines.append("")
            lines.append("最终 score（落库）: " + str(score_final.get("score")))
            lines.append("最终 dimensions: " + _fmt_json(score_final.get("details") or {}))
            lines.append("最终 reason: " + (str(score_final.get("reason") or "").strip() or "(无)"))
            lines.append("最终 audit_trace: " + _fmt_json(score_final.get("audit_trace") or {}))
            lines.append("")
            lines.append("=" * 100)
            lines.append("")

            write_header = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if write_header:
                    f.write("\n".join(header_lines))
                f.write("\n".join(lines))
            logger.info(f"实时评估汇总日志已写入: {path}, question={safe_qid}")
        except Exception as e:
            logger.warning(f"写入单题评估日志失败: {e}")

    async def evaluate_answer(self, session_id: str, question_id: str, answer_text: str,
                              custom_question_content: str = None,
                              custom_evaluation_points: list = None,
                              *,
                              rescore_mode: bool = False,
                              persist_to_db: bool = True) -> Dict[str, Any]:
        """评估答案
        
        评分准则：
        - 专业题（PROFESSIONAL）：根据题目、参考答案、评估要点、回答进行评分，小于标准分则触发追问
        - 基础题（BASIC_INFO）：根据题目、评估要点、回答进行评分，不进行追问
        
        注意：
        - 必须从interview_question表的question_type字段判断题目类型
        - point_evaluations字段存储评估要点（evaluation_points），不是评分维度
        
        Args:
            session_id: 会话ID
            question_id: 题目ID
            answer_text: 答案文本
            custom_question_content: 自定义问题内容（用于追问评分）
            custom_evaluation_points: 自定义评估要点（用于追问评分，覆盖从数据库获取的评估要点）
            rescore_mode: 批量重评等场景跳过追问与会话追加
            persist_to_db: 为 False 时仅计算分数与日志，不写 candidate_answers / interview_session（供外部用
                其他转写文本重评，例如 DataStoreWare 百度云 ASR 侧车打分）
        """
        try:
            # 验证输入 - 只有完全没有答案或答案极短（< 3字符）才直接返回0分
            # 修改：只要有3个字符以上就调用LLM评估，让LLM判断答案质量
            if not answer_text or len(answer_text.strip()) < 3:
                logger.warning(f"答案为空或极短: session_id={session_id}, question_id={question_id}, answer_length={len(answer_text) if answer_text else 0}")
                
                # 获取题目信息以便保存到数据库
                question_info = self._get_question_info(question_id)
                
                # 创建0分评分结果
                score_result = {
                    'score': 0,
                    'reason': '未提供有效答案（答案为空或少于3个字符），无法进行评分。',
                    'details': {}
                }
                
                # 保存到数据库（即使是0分也要保存）
                if question_info:
                    if persist_to_db:
                        self._save_evaluation_result(
                            session_id, question_id, answer_text or '', 
                            score_result, False, None, question_info
                        )
                    qtype = question_info.get("question_type", "SPECIALTY")
                    is_basic = qtype in ("BASIC", "BASIC_INFO")
                    try:
                        short_prompt = self._build_scoring_prompt(
                            question_info, answer_text or "", is_basic
                        )
                    except Exception:
                        short_prompt = None
                    self._write_per_question_realtime_eval_log(
                        session_id=session_id,
                        question_id=question_id,
                        question_info=question_info,
                        answer_text=answer_text or "",
                        prompt=short_prompt,
                        score_parsed=None,
                        score_final=score_result,
                        llm_raw_response=None,
                        is_basic_question=is_basic,
                        question_type=qtype,
                        rescore_mode=rescore_mode,
                        follow_up_scoring=bool(custom_question_content),
                        path_note="答案过短(少于3字)，未调用 LLM",
                    )
                
                return {
                    'score': 0,
                    'reason': '未提供有效答案，请重新回答问题。',
                    'need_follow_up': False,
                    'follow_up_question': None,
                    'evaluation_details': {},
                    'point_evaluations': None,
                    'question_type': question_info.get('question_type', 'SPECIALTY') if question_info else 'SPECIALTY',
                    'timestamp': datetime.now().isoformat()
                }

            # 获取题目信息（包含题目类型）
            question_info = self._get_question_info(question_id)
            if not question_info:
                logger.warning(f"未找到题目信息: {question_id}")
                return self._create_empty_result()

            # 如果提供了自定义问题内容，使用自定义内容（用于追问评分）
            if custom_question_content:
                question_info['question_content'] = custom_question_content
                logger.info(f"使用自定义问题内容进行追问评分: question_id={question_id}")
            
            # 如果提供了自定义评估要点，使用自定义评估要点（用于追问评分）
            if custom_evaluation_points:
                question_info['evaluation_points'] = custom_evaluation_points
                logger.info(f"使用自定义评估要点进行追问评分: question_id={question_id}, points_count={len(custom_evaluation_points)}")

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
                # 追问评分时不得再触发「二次追问」（此处 question_content 已是追问题干，易误判为复述关键词）
                is_follow_up_scoring = bool((custom_question_content or "").strip())
                # 对于这种答案，直接给予低分，不需要调用LLM
                score_result = {
                    'score': 28,  # 低分（仅复述题干关键词）
                    'reason': '答案只是简单重复题目中的关键词，没有提供实际内容、具体措施或实例说明。请提供更详细的回答，包括具体的措施和实际经历。',
                    'details': {
                        'content_completeness': 5,
                        'logical_clarity': 10,
                        'professional_level': 10,
                        'expression_ability': 10
                    } if not is_basic_question else {
                        'content_completeness': 10,
                        'logical_clarity': 8,
                        'professional_level': 5,
                        'expression_ability': 5
                    }
                }
                if is_follow_up_scoring:
                    need_follow_up = False
                    follow_up_question = None
                else:
                    need_follow_up = True if not is_basic_question else False
                    follow_up_question = (
                        '您能否详细说明一下具体采取了哪些措施？能否举一个具体的例子？'
                        if not is_basic_question else None
                    )

                kw_prompt = self._build_scoring_prompt(question_info, answer_text, is_basic_question)
                
                # 保存评分结果到数据库（传递question_info以便获取评估要点）
                if persist_to_db:
                    self._save_evaluation_result(
                        session_id, question_id, answer_text, score_result, need_follow_up, follow_up_question, question_info
                    )
                self._write_per_question_realtime_eval_log(
                    session_id=session_id,
                    question_id=question_id,
                    question_info=question_info,
                    answer_text=answer_text,
                    prompt=kw_prompt,
                    score_parsed=None,
                    score_final=score_result,
                    llm_raw_response=None,
                    is_basic_question=is_basic_question,
                    question_type=question_type,
                    rescore_mode=rescore_mode,
                    follow_up_scoring=bool(custom_question_content),
                    path_note="关键词复述短路，未调用 LLM（提示词仍生成供对照）",
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

            # 调用LLM进行评分（较低温度，减少「人情分」式的中高分扎堆）
            llm_response = await self.llm_service.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.15,
                max_tokens=self.scoring_max_tokens
            )
            is_truncated = isinstance(llm_response, dict) and llm_response.get("finish_reason") == "length"
            if is_truncated:
                logger.error("LLM评分输出被截断（finish_reason=length），请继续提高max_tokens")

            if not llm_response:
                logger.error("LLM评分调用失败，使用默认评分结果并保存答案")
                # 即使LLM调用失败，也要保存答案到数据库
                score_result = {
                    'score': 0,  # 评分失败返回0
                    'reason': 'LLM评分服务暂时不可用，已记录答案待后续评分',
                    'details': {}
                }
                need_follow_up = False  # LLM失败时不触发追问
                follow_up_question = None
                
                # 保存答案到数据库（即使评分失败）
                if persist_to_db:
                    self._save_evaluation_result(
                        session_id, question_id, answer_text, score_result, need_follow_up, follow_up_question, question_info
                    )
                self._write_per_question_realtime_eval_log(
                    session_id=session_id,
                    question_id=question_id,
                    question_info=question_info,
                    answer_text=answer_text,
                    prompt=prompt,
                    score_parsed=None,
                    score_final=score_result,
                    llm_raw_response=None,
                    is_basic_question=is_basic_question,
                    question_type=question_type,
                    rescore_mode=rescore_mode,
                    follow_up_scoring=bool(custom_question_content),
                    path_note="LLM 调用失败或返回空，未解析模型输出",
                )
                
                return {
                    'score': score_result['score'],
                    'reason': score_result['reason'],
                    'need_follow_up': need_follow_up,
                    'follow_up_question': follow_up_question,
                    'evaluation_details': {},
                    'point_evaluations': None,
                    'question_type': question_type,
                    'timestamp': datetime.now().isoformat()
                }

            # 解析评分结果
            llm_response_for_log: Any = llm_response
            score_result = self._parse_llm_response(llm_response)
            scoring_used_retry = False
            if self.retry_on_truncation and (is_truncated or self._is_parse_fallback_result(score_result)):
                retry_max_tokens = min(
                    int(self.scoring_max_tokens * self.retry_multiplier),
                    int(self.retry_max_tokens_cap)
                )
                if retry_max_tokens > self.scoring_max_tokens:
                    logger.warning(
                        f"评分结果可能被截断或解析失败，准备重试一次: question_id={question_id}, "
                        f"max_tokens={self.scoring_max_tokens}->{retry_max_tokens}"
                    )
                    retry_response = await self.llm_service.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.15,
                        max_tokens=retry_max_tokens
                    )
                    if isinstance(retry_response, dict) and retry_response.get("finish_reason") == "length":
                        logger.error("评分重试后仍发生截断（finish_reason=length）")
                    retry_score_result = self._parse_llm_response(retry_response)
                    if not self._is_parse_fallback_result(retry_score_result):
                        score_result = retry_score_result
                        llm_response_for_log = retry_response
                        scoring_used_retry = True
                        logger.info("评分重试成功，已采用重试结果")

            # 对 LLM 分数做校准：抑制「回答很短/很碎却给 60+」
            score_parsed = dict(score_result)
            pre_calibration_score = score_result.get("score", 0)
            score_result = self._apply_realtime_score_calibration(
                dict(score_result),
                answer_text,
                is_basic_question,
            )
            score_result["_pre_calibration_score"] = pre_calibration_score
            score_result = self._apply_semantic_guardrails(
                dict(score_result),
                answer_text,
                question_info,
                is_basic_question,
            )

            # 决定是否需要追问（基础题不追问）
            # 关键修复：如果是追问评分（custom_question_content 非空），不应该再生成追问问题
            need_follow_up = False
            is_follow_up_evaluation = bool((custom_question_content or "").strip())

            if rescore_mode:
                logger.info(f"rescore_mode: 跳过追问逻辑，仅回写评分: question_id={question_id}")
            elif is_follow_up_evaluation:
                # 这是追问评分，不应该再生成追问问题
                logger.info(f"这是追问评分，不生成新的追问问题: question_id={question_id}")
                need_follow_up = False
            elif not is_basic_question:
                session_follow_up_count = 0
                if session_id and self.max_follow_ups_per_interview >= 0:
                    try:
                        session_sql = """
                        SELECT COUNT(1) AS cnt
                        FROM candidate_answers
                        WHERE session_id = %s
                          AND is_follow_up = FALSE
                          AND follow_up_question IS NOT NULL
                          AND trim(follow_up_question) <> ''
                        """
                        if self.db_manager.db_type != "postgresql":
                            session_sql = session_sql.replace("%s", "?")
                        session_row = self.db_manager.fetch_one(session_sql, (session_id,))
                        if session_row:
                            session_follow_up_count = (
                                session_row.get("cnt")
                                if isinstance(session_row, dict)
                                else session_row[0]
                            ) or 0
                    except Exception as ex:
                        logger.warning(f"检查整场追问次数失败（保守跳过追问）: {ex}")
                        session_follow_up_count = self.max_follow_ups_per_interview

                if (
                    self.max_follow_ups_per_interview >= 0
                    and session_follow_up_count >= self.max_follow_ups_per_interview
                ):
                    need_follow_up = False
                    logger.info(
                        f"整场追问次数已达上限，跳过追问生成: session_id={session_id}, "
                        f"used={session_follow_up_count}, limit={self.max_follow_ups_per_interview}, "
                        f"question_id={question_id}"
                    )
                else:
                    # 本题主答案若已写入过追问题干，说明已占用过「每题一次追问」，不再生成第二次
                    main_already_has_follow_up = False
                    if session_id and self.max_follow_ups_per_question >= 0:
                        try:
                            # 任一条本题主答案若已有追问题干或追问作答，则视为本题追问周期已开始/结束，不再生成第二次
                            ex_sql = """
                            SELECT 1 FROM candidate_answers
                            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
                              AND (
                                (follow_up_question IS NOT NULL AND trim(follow_up_question) <> '')
                                OR (follow_up_answer_text IS NOT NULL AND trim(follow_up_answer_text) <> '')
                              )
                            LIMIT 1
                            """
                            if self.db_manager.db_type != "postgresql":
                                ex_sql = ex_sql.replace("%s", "?")
                            ex_row = self.db_manager.fetch_one(ex_sql, (session_id, question_id))
                            main_already_has_follow_up = bool(ex_row)
                        except Exception as ex:
                            logger.warning(f"检查是否已有追问失败（忽略并继续）: {ex}")
                    if main_already_has_follow_up:
                        need_follow_up = False
                        logger.info(
                            f"本题主答案已存在追问题干，跳过二次追问生成: question_id={question_id}, session_id={session_id}"
                        )
                    else:
                        # 只有专业题才进行追问判断
                        need_follow_up = self._should_follow_up(score_result.get("score", 0))
            else:
                logger.info(f"基础题不进行追问: question_id={question_id}")

            # 生成追问问题和评估要点（如果需要）
            follow_up_question = None
            follow_up_evaluation_points = None
            if (not rescore_mode) and need_follow_up:
                logger.info(f"[追问生成-步骤1] 开始生成追问问题和评估要点: question_id={question_id}, answer_length={len(answer_text)}")
                try:
                    follow_up_result = await self._generate_follow_up_question(
                        question_info, answer_text, score_result
                    )
                    follow_up_question = follow_up_result.get('question')
                    follow_up_evaluation_points = follow_up_result.get('evaluation_points')
                    
                    logger.info(f"[追问生成-步骤2] 生成结果: follow_up_question={follow_up_question is not None}, follow_up_evaluation_points={follow_up_evaluation_points is not None}")
                    if follow_up_question:
                        logger.info(f"[追问生成-步骤2] 追问问题内容: {follow_up_question[:100]}...")
                    if follow_up_evaluation_points:
                        logger.info(f"[追问生成-步骤2] 评估要点数量: {len(follow_up_evaluation_points)}")
                    
                    # 关键修复：确保追问问题和评估要点都被正确生成
                    if not follow_up_question or not follow_up_evaluation_points:
                        logger.error(f"[追问生成-步骤2] ❌ 追问问题或评估要点生成失败: question_id={question_id}, follow_up_question={follow_up_question is not None}, follow_up_evaluation_points={follow_up_evaluation_points is not None}, follow_up_result={follow_up_result}")
                        # 如果生成失败，设置need_follow_up=False，避免保存空的追问问题
                        need_follow_up = False
                        follow_up_question = None
                        follow_up_evaluation_points = None
                    else:
                        logger.info(f"[追问生成-步骤3] ✅ 追问问题和评估要点已生成: question_length={len(follow_up_question)}, points_count={len(follow_up_evaluation_points)}")
                except Exception as e:
                    logger.error(f"[追问生成-步骤2] ❌ 生成追问问题异常: question_id={question_id}, error={str(e)}", exc_info=True)
                    # 如果生成异常，设置need_follow_up=False，避免保存空的追问问题
                    need_follow_up = False
                    follow_up_question = None
                    follow_up_evaluation_points = None

            # 保存评分结果到数据库（传递question_info和follow_up_evaluation_points）
            logger.info(f"[追问保存-步骤1] 准备保存评分结果: question_id={question_id}, need_follow_up={need_follow_up}, follow_up_question={follow_up_question is not None}, follow_up_evaluation_points={follow_up_evaluation_points is not None}, persist_to_db={persist_to_db}")
            if persist_to_db:
                self._save_evaluation_result(
                    session_id, question_id, answer_text, score_result, need_follow_up,
                    follow_up_question, question_info, follow_up_evaluation_points,
                    rescore_mode=rescore_mode,
                )
                logger.info(f"[追问保存-步骤2] 评分结果已保存: question_id={question_id}, need_follow_up={need_follow_up}")
            else:
                logger.info(f"[追问保存-步骤2] persist_to_db=False，跳过写库: question_id={question_id}")

            rt_path_note = ""
            if scoring_used_retry:
                rt_path_note = "评分曾触发 max_tokens 放大重试，日志中的 LLM 原始响应为重试响应"
            if not persist_to_db:
                rt_path_note = (rt_path_note + "；" if rt_path_note else "") + "persist_to_db=False（仅计分不写库）"

            self._write_per_question_realtime_eval_log(
                session_id=session_id,
                question_id=question_id,
                question_info=question_info,
                answer_text=answer_text,
                prompt=prompt,
                score_parsed=score_parsed,
                score_final=score_result,
                llm_raw_response=llm_response_for_log,
                is_basic_question=is_basic_question,
                question_type=question_type,
                rescore_mode=rescore_mode,
                follow_up_scoring=bool(custom_question_content),
                path_note=rt_path_note,
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

            # 追问评分返回给前端的字典中严禁再带「需要追问」，避免 onEvaluation 误弹第二次追问
            if is_follow_up_evaluation:
                need_follow_up = False
                follow_up_question = None
                follow_up_evaluation_points = None

            result = {
                'score': score_result.get('score', 0),
                'reason': score_result.get('reason', ''),
                'audit_trace': score_result.get('audit_trace', {}),
                'need_follow_up': need_follow_up,
                'follow_up_question': follow_up_question,
                'follow_up_evaluation_points': follow_up_evaluation_points,  # 追问评估要点（关键修复）
                'evaluation_details': score_result.get('details', {}),  # 评分维度（dimensions）
                'point_evaluations': evaluation_points,  # 评估要点
                'question_type': question_type,
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"答案评分完成: session={session_id}, question={question_id}, type={question_type}, score={result['score']}, follow_up={need_follow_up}, follow_up_points_count={len(follow_up_evaluation_points) if follow_up_evaluation_points else 0}")

            return result

        except Exception as e:
            logger.error(f"评估答案失败: {str(e)}", exc_info=True)
            # 即使评分失败，也要保存答案到数据库
            try:
                # 获取题目信息（如果之前没有获取）
                if 'question_info' not in locals() or not question_info:
                    question_info = self._get_question_info(question_id)
                
                if question_info and session_id and question_id and answer_text:
                    score_result = {
                        'score': 0,  # 评分失败返回0
                        'reason': f'评分过程发生错误: {str(e)}，已记录答案待后续评分',
                        'details': {}
                    }
                    need_follow_up = False
                    follow_up_question = None
                    
                    # 保存答案到数据库（即使评分失败）
                    if persist_to_db:
                        self._save_evaluation_result(
                            session_id, question_id, answer_text, score_result, need_follow_up, follow_up_question, question_info
                        )
                        logger.info(f"评分失败但已保存答案到数据库: session_id={session_id}, question_id={question_id}")
                    else:
                        logger.info(f"评分失败且 persist_to_db=False，跳过写库: session_id={session_id}, question_id={question_id}")
            except Exception as save_error:
                logger.error(f"保存答案失败: {str(save_error)}", exc_info=True)
            
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
            prompt += """# Role
你是一位拥有20年经验的技术面试官。你的任务是忽略语音转写噪音，基于语义内容评估候选人的真实能力。

# Evaluation Principles
1) 实事求是：不设“人为及格线硬抬”，但必须避免因 ASR、口癖、句子碎导致的系统性压分。
2) 语义优先：ASR同音错字、断句、重复词，只要语义可理解，不作为扣分理由。
3) BASIC模式：仅对照【评估要点】评分，不要求技术深度；基础题用于识别明显不配合/严重跑题，不是论文答辩。

# Step-by-Step Thought Process (Must follow internally)
1) 语义还原：修正常见ASR错误（如“腰形->遥信”），并忽略“呃/嗯/那个/然后”等填充词。
2) 无效回答识别：仅当语义基本不可还原（如乱码或词语拼接无意义）时才给 score=0；不要因为少量错字或口头语判无效。
3) 要点覆盖：按评估要点覆盖率给 content_completeness；若已在回答中体现态度/思路，不得因“未举例、未展开”把内容分打到极低。
4) 维度打分：professional_level 在基础题中表示职业素养/责任意识，不与术语数量绑定；thought 中若已肯定要点命中，则 dimensions 不得与 thought 明显矛盾。

# Score Rubric (Dimension Definitions)
- content_completeness (0-40): 对评估要点覆盖度（全覆盖35-40，主要覆盖25-34，部分覆盖18-24，很少覆盖0-17）。
- professional_level (0-30): 职业素养、责任意识、配合度，与术语数量无关；只要配合且思路正常，通常应在16分以上。
- logical_clarity (0-20): 因果与步骤是否可理解；口头语不扣分；能听出主线则通常不低于10分。
- expression_ability (0-10): 仅考核是否明显离题或语义不可懂；不得因口癖、碎句扣分；未离题时通常不低于6分。

请仅返回JSON（不要返回代码块、不要返回额外说明）：
{
  "thought": "描述提取到的证据及ASR纠错逻辑，重点分析评估要点匹配度。",
  "dimensions": {
    "content_completeness": 0,
    "logical_clarity": 0,
    "professional_level": 0,
    "expression_ability": 0
  },
  "score": 0,
  "reason": "[证据点] + [评价]。严禁以“字数少”作为唯一扣分理由。"
}

Constraints:
- score 必须严格等于 dimensions 四项分值之和。
- 所有维度必须在各自范围内（40/30/20/10）且后处理会钳位。
- 严禁因为表达能力差给 professional_level 打低分，维度必须解耦。
- 若候选人未离题、语义可理解且评估要点至少部分命中，总分(score)建议在 55-75 区间；仅明显不配合或答非所问时可低于 50。"""
        else:
            prompt += """# Role
你是一位拥有20年经验的技术面试官。你的任务是忽略语音转写噪音，基于语义内容评估候选人的真实能力。

# Evaluation Principles
1) 实事求是：不设分数下限保护，只看答案质量。
2) 语义优先：ASR同音错字、断句、重复词，只要语义可理解，不作为扣分理由。
3) SPECIALTY模式：同时对照【参考答案】与【评估要点】，采用命中率计算核心内容分。
4) 要点分层：将要点分为“核心必达要点（决定合格）”与“细节加分要点（只加分不反向扣分）”。

# Step-by-Step Thought Process (Must follow internally)
1) 语义还原：修正常见ASR错误并去掉口头填充词。
2) 无效回答识别：仅当语义基本不可还原（如乱码或词语拼接无意义）时才给 score=0。
3) 命中率计算：从参考答案提取核心动作/名词总数 total_points；统计语义命中数 hits。
4) content_completeness 计算：round((hits / max(total_points, 1)) * 40)。
5) 维度打分：不得因“未展开细节”否定已命中点；已命中核心点但缺细节时，content_completeness 的扣分不应超过该维度上限的20%。
6) 证据一致性：thought/理由中若已确认“有主线且命中核心点”，logical_clarity 与 professional_level 不得打成 0。

# Score Rubric (Dimension Definitions)
- content_completeness (0-40): 参考答案与评估要点命中覆盖度。
- professional_level (0-30): 技术深度与方案落地能力。
- logical_clarity (0-20): 流程顺序是否正确，不考核语气词。
- expression_ability (0-10): 仅考核是否离题，碎句不扣分。

请仅返回JSON（不要返回代码块、不要返回额外说明）：
{
  "thought": "描述提取到的证据、ASR纠错逻辑，并对比参考答案说明命中与缺失。",
  "hit_analysis": {
    "total_key_points": 0,
    "hit_points": [],
    "missing_points": [],
    "hit_rate": 0.0
  },
  "dimensions": {
    "content_completeness": 0,
    "logical_clarity": 0,
    "professional_level": 0,
    "expression_ability": 0
  },
  "score": 0,
  "reason": "[证据点] + [评价]。严禁以“字数少”作为唯一扣分理由。"
}

Constraints:
- score 必须严格等于 dimensions 四项分值之和。
- 严禁因为表达能力差给 professional_level 打低分，维度必须解耦。
- 专业题的 content_completeness 必须由命中率公式计算，不得随意调整。
- 所有维度必须在各自范围内（40/30/20/10）且后处理会钳位。"""

        return prompt

    @staticmethod
    def _integer_adjust_dimensions(
        dim_keys: tuple,
        values: list[float],
        caps: tuple,
        target: int,
    ) -> Dict[str, int]:
        """
        将各维非负浮点分调整为非负整数，满足每维上限 caps，且各维之和严格等于 target（在 target<=sum(caps) 时可达到）。
        """
        n = len(dim_keys)
        if n == 0 or len(values) != n or len(caps) != n:
            return {}
        max_sum = sum(caps)
        target = max(0, min(int(target), max_sum))

        s = sum(max(0.0, float(v)) for v in values)
        if s <= 1e-9:
            # 无有效分布信息时均分 target
            base = target // n
            rem = target % n
            out = [0] * n
            for i in range(n):
                out[i] = min(caps[i], base + (1 if i < rem else 0))
            diff = target - sum(out)
            guard = 0
            while diff != 0 and guard < 300:
                guard += 1
                if diff > 0:
                    j = max(range(n), key=lambda i: caps[i] - out[i])
                    if caps[j] - out[j] <= 0:
                        break
                    out[j] += 1
                    diff -= 1
                else:
                    j = max(range(n), key=lambda i: out[i])
                    if out[j] <= 0:
                        break
                    out[j] -= 1
                    diff += 1
            return {dim_keys[i]: int(out[i]) for i in range(n)}

        floats = [target * max(0.0, float(values[i])) / s for i in range(n)]
        out = [max(0, min(caps[i], int(round(floats[i])))) for i in range(n)]
        diff = target - sum(out)
        guard = 0
        while diff != 0 and guard < 400:
            guard += 1
            if diff > 0:
                j = max(range(n), key=lambda i: caps[i] - out[i])
                if caps[j] - out[j] <= 0:
                    break
                out[j] += 1
                diff -= 1
            else:
                j = max(range(n), key=lambda i: out[i])
                if out[j] <= 0:
                    break
                out[j] -= 1
                diff += 1
        return {dim_keys[i]: int(out[i]) for i in range(n)}

    @staticmethod
    def _parse_dimension_caps(raw_caps: Any, default_caps: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """解析四维配额配置，异常时回落到默认值。"""
        try:
            if isinstance(raw_caps, (list, tuple)) and len(raw_caps) == 4:
                vals = tuple(max(0, int(v)) for v in raw_caps)
                if sum(vals) > 0:
                    return vals
        except Exception:
            pass
        return default_caps

    def _apply_realtime_score_calibration(
        self,
        score_result: Dict[str, Any],
        answer_text: str,
        is_basic_question: bool,
    ) -> Dict[str, Any]:
        """平滑长度惩罚 + 维度按比例缩放与整数守恒，保证总分 == 各维整数分之和。"""
        out = dict(score_result)
        try:
            orig = float(out.get("score") or 0)
        except (TypeError, ValueError):
            orig = 0.0
        details = out.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        s = (answer_text or "").strip()
        content_len = len(s)

        dim_keys: tuple
        caps: tuple
        if is_basic_question:
            # 基础题与专业题统一为四维，避免下游字段不一致
            # 兼容模型偶发输出旧三维键：信息完整性/表达清晰度/语言流畅度
            if "content_completeness" not in details and "information_completeness" in details:
                details["content_completeness"] = details.get("information_completeness", 0)
            if "logical_clarity" not in details and "expression_clarity" in details:
                details["logical_clarity"] = details.get("expression_clarity", 0)
            if "expression_ability" not in details and "language_fluency" in details:
                details["expression_ability"] = details.get("language_fluency", 0)
            if "professional_level" not in details:
                # 基础题专业维允许较低权重，默认给0，避免臆造高分
                details["professional_level"] = 0
            dim_keys = (
                "content_completeness",
                "logical_clarity",
                "professional_level",
                "expression_ability",
            )
            caps = self.basic_dimension_caps
        else:
            dim_keys = (
                "content_completeness",
                "logical_clarity",
                "professional_level",
                "expression_ability",
            )
            caps = self.professional_dimension_caps

        vals: list[float] = []
        for k in dim_keys:
            v = details.get(k)
            if isinstance(v, (int, float)):
                vals.append(float(v))
            else:
                vals = []
                break

        min_expected = self.basic_min_expected_length if is_basic_question else self.professional_min_expected_length
        penalty_ratio = 1.0
        if self.enable_length_penalty_calibration and content_len < min_expected and min_expected > 0:
            penalty_ratio = max(0.52, (content_len / float(min_expected)) ** 0.5)

        # 维度不全：仅对 LLM 总分做长度惩罚，不改各维（避免臆造）
        if len(vals) != len(dim_keys):
            new_score = max(0.0, min(100.0, orig * penalty_ratio))
            rounded = int(round(new_score))
            # 校准幅度限制：无论何种校准，单次调整不超过原分数的20%
            if orig > 0:
                lo = int(round(orig * 0.8))
                hi = int(round(orig * 1.2))
                rounded = max(lo, min(hi, rounded))
            if abs(rounded - int(round(orig))) > 0 or penalty_ratio < 0.999:
                parts = []
                if penalty_ratio < 0.999:
                    parts.append(f"长度平滑系数≈{penalty_ratio:.2f}（维度不齐未改小分）")
                if orig > 0:
                    parts.append("校准幅度已限制在原分±20%")
                note = "（系统校准：" + "；".join(parts) + f"，总分 {int(round(orig))}→{rounded}）"
                out["reason"] = ((out.get("reason") or "").strip() + note).strip()[:480]
            out["score"] = rounded
            return out

        aligned_total = sum(vals)
        new_total = int(round(aligned_total * penalty_ratio))
        new_total = max(0, min(100, new_total, sum(caps)))

        new_dims = self._integer_adjust_dimensions(dim_keys, vals, caps, new_total)
        new_details = {**details, **new_dims}
        out["details"] = new_details
        out["score"] = sum(new_dims.values())

        # 校准幅度限制：无论何种校准，单次调整不超过原分数的20%
        if orig > 0:
            lo = int(round(orig * 0.8))
            hi = int(round(orig * 1.2))
            capped_total = max(lo, min(hi, int(out["score"])))
            if capped_total != int(out["score"]):
                new_dims = self._integer_adjust_dimensions(
                    dim_keys,
                    [float(new_details.get(k, 0)) for k in dim_keys],
                    caps,
                    capped_total,
                )
                out["details"] = {**new_details, **new_dims}
                out["score"] = sum(new_dims.values())

        if (
            abs(out["score"] - int(round(orig))) > 0
            or abs(aligned_total - orig) > 0.5
            or penalty_ratio < 0.999
        ):
            parts = []
            if abs(aligned_total - orig) > 0.5:
                parts.append(f"以维度之和为基准 {int(round(orig))}→{int(round(aligned_total))}")
            if penalty_ratio < 0.999:
                parts.append(
                    f"长度({content_len})低于期望({min_expected})，平滑系数≈{penalty_ratio:.2f}，已同步缩放各维"
                )
            if orig > 0:
                parts.append("校准幅度已限制在原分±20%")
            note = "（系统校准：" + "；".join(parts) + f"，最终总分 {int(round(orig))}→{out['score']}）"
            out["reason"] = ((out.get("reason") or "").strip() + note).strip()[:480]
        return out

    @staticmethod
    def _strip_fillers(text: str) -> str:
        s = text or ""
        s = re.sub(r"(呃|嗯|那个|然后|就是|这个|啊)+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _extract_reference_keywords(self, question_info: Dict[str, Any]) -> List[str]:
        keywords: List[str] = []
        std = str(question_info.get("standard_answer") or "")
        if std:
            keywords.extend(re.findall(r"[A-Za-z][A-Za-z0-9_/-]{1,}|[\u4e00-\u9fff]{2,}", std))
        evp = question_info.get("evaluation_points")
        if isinstance(evp, str):
            try:
                evp = json.loads(evp)
            except Exception:
                evp = []
        if isinstance(evp, list):
            for p in evp:
                if isinstance(p, dict):
                    txt = str(p.get("point") or "")
                else:
                    txt = str(p or "")
                keywords.extend(re.findall(r"[A-Za-z][A-Za-z0-9_/-]{1,}|[\u4e00-\u9fff]{2,}", txt))
        uniq: List[str] = []
        for k in keywords:
            kk = k.strip().lower()
            if len(kk) < 2:
                continue
            if kk not in uniq:
                uniq.append(kk)
        return uniq[:80]

    @staticmethod
    def _count_basic_evaluation_points(question_info: Dict[str, Any]) -> int:
        evp = question_info.get("evaluation_points")
        if isinstance(evp, str):
            try:
                evp = json.loads(evp)
            except Exception:
                evp = []
        if isinstance(evp, list) and evp:
            return len(evp)
        return 1

    @staticmethod
    def _cn_bigrams(text: str) -> Set[str]:
        s = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
        if len(s) < 2:
            return set()
        return {s[i : i + 2] for i in range(len(s) - 1)}

    def _basic_answer_has_substance(
        self,
        answer_text: str,
        question_info: Dict[str, Any],
    ) -> bool:
        """基础题：是否具备可评分的实质内容（无固定 ASR 映射，仅用长度/题干二元组重叠/评估要点词）。"""
        cleaned = self._strip_fillers(answer_text or "")
        if len(cleaned) < 10:
            return False
        if len(cleaned) >= 36:
            return True
        low = cleaned.lower()
        for kw in self._extract_reference_keywords(question_info):
            if len(kw) >= 2 and kw in low:
                return True
        q = str(question_info.get("question_content") or "")
        common = len(self._cn_bigrams(q) & self._cn_bigrams(cleaned))
        if common >= 2:
            return True
        return len(cleaned) >= 18

    @staticmethod
    def _boost_basic_sum_to_target(
        details: Dict[str, Any],
        caps: tuple[int, int, int, int],
        target_sum: int,
    ) -> None:
        """在 caps 内按优先级微调四维，使总分尽量达到 target_sum（用于基础题弱保底）。"""
        dim_keys = ("content_completeness", "logical_clarity", "professional_level", "expression_ability")
        caps_by = dict(zip(dim_keys, caps))
        order = ("content_completeness", "professional_level", "logical_clarity", "expression_ability")
        for _ in range(120):
            cur_sum = sum(int(details.get(k, 0) or 0) for k in dim_keys)
            if cur_sum >= target_sum:
                return
            progressed = False
            for k in order:
                cap = caps_by[k]
                cur = int(details.get(k, 0) or 0)
                if cur < cap:
                    details[k] = cur + 1
                    progressed = True
                    break
            if not progressed:
                return

    def _apply_semantic_guardrails(
        self,
        score_result: Dict[str, Any],
        answer_text: str,
        question_info: Dict[str, Any],
        is_basic_question: bool,
    ) -> Dict[str, Any]:
        out = dict(score_result)
        details = out.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        quality_recompute = False
        hit_recalc_note: Optional[str] = None
        audit_trace = out.get("audit_trace") if isinstance(out.get("audit_trace"), dict) else {}
        caps = self.basic_dimension_caps if is_basic_question else self.professional_dimension_caps
        dim_keys = ("content_completeness", "logical_clarity", "professional_level", "expression_ability")
        for i, k in enumerate(dim_keys):
            v = details.get(k, 0)
            if not isinstance(v, (int, float)):
                v = 0
            details[k] = max(0, min(int(round(v)), int(caps[i])))

        # 专业题：命中率与 content_completeness 对齐（允许±5浮动）
        if not is_basic_question:
            hit = out.get("hit_analysis") if isinstance(out.get("hit_analysis"), dict) else {}
            hr = hit.get("hit_rate")
            hit_points = hit.get("hit_points") if isinstance(hit.get("hit_points"), list) else []
            missing_points = hit.get("missing_points") if isinstance(hit.get("missing_points"), list) else []
            audit_trace.update({
                "hit_points_count": len(hit_points),
                "missing_points_count": len(missing_points),
                "hit_points_summary": [str(x) for x in hit_points[:8]],
                "missing_points_summary": [str(x) for x in missing_points[:8]],
            })
            old_hr = float(hr) if isinstance(hr, (int, float)) else None
            audit_trace["hit_rate_original"] = old_hr
            # 命中率一致性校验与兜底重算：hit_rate = hit_points / (hit_points + missing_points)
            recomputed_hr: Optional[float] = None
            denom = len(hit_points) + len(missing_points)
            if denom > 0:
                recomputed_hr = max(0.0, min(1.0, len(hit_points) / float(denom)))
            elif hit_points:
                recomputed_hr = 1.0
            if recomputed_hr is not None:
                need_recalc = (
                    old_hr is None
                    or old_hr < 0.0
                    or old_hr > 1.0
                    or abs(old_hr - recomputed_hr) > 0.08
                )
                audit_trace["hit_rate_recomputed"] = round(recomputed_hr, 4)
                audit_trace["hit_rate_recomputed_by_rule"] = "hits/(hits+missing)"
                audit_trace["hit_rate_recomputed_applied"] = bool(need_recalc)
                if need_recalc:
                    hit["hit_rate"] = round(recomputed_hr, 4)
                    out["hit_analysis"] = hit
                    hr = recomputed_hr
                    quality_recompute = True
                    old_disp = "None" if old_hr is None else f"{old_hr:.4f}"
                    hit_recalc_note = f"命中率重算 {old_disp}->{recomputed_hr:.4f}（hits={len(hit_points)}, missing={len(missing_points)}）"
            if isinstance(hr, (int, float)):
                expected = max(0, min(40, int(round(float(hr) * 40))))
                if abs(int(details.get("content_completeness", 0)) - expected) > 5:
                    details["content_completeness"] = expected

        # 低分且可读：按关键词命中避免 ASR 误杀
        cleaned = self._strip_fillers(answer_text)
        readable = len(cleaned) >= 10 and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned) is not None
        raw_score = out.get("score", 0)
        if isinstance(raw_score, (int, float)) and raw_score < 30 and readable:
            kws = self._extract_reference_keywords(question_info)
            hit_count = 0
            low = cleaned.lower()
            for kw in kws:
                if kw in low:
                    hit_count += 1
            if hit_count >= 1:
                details["content_completeness"] = max(int(details.get("content_completeness", 0)), 10)

        # 专业题兜底：可读且非“纯读题”时，避免四维全部清零导致误杀
        if not is_basic_question and readable:
            question_text = str(question_info.get("question_content") or "")
            is_repeat = self._is_answer_just_repeating_keywords(answer_text, question_text)
            all_zero = all(int(details.get(k, 0)) == 0 for k in dim_keys)
            if all_zero and not is_repeat:
                # 仅给弱保底，确保“有实质作答但表达粗糙”不会被判成无效回答
                details["content_completeness"] = max(int(details.get("content_completeness", 0)), 8)
                details["logical_clarity"] = max(int(details.get("logical_clarity", 0)), 8)
                details["professional_level"] = max(int(details.get("professional_level", 0)), 6)
                details["expression_ability"] = max(int(details.get("expression_ability", 0)), 4)

        # 专业题：若命中率>0，逻辑/表达不应同时为0（避免“命中却不可理解”的矛盾）
        if not is_basic_question:
            hit = out.get("hit_analysis") if isinstance(out.get("hit_analysis"), dict) else {}
            hr = hit.get("hit_rate")
            hit_points = hit.get("hit_points") if isinstance(hit.get("hit_points"), list) else []
            if isinstance(hr, (int, float)) and float(hr) > 0:
                # 有主线且存在命中时，逻辑/表达不应被打成极低
                details["logical_clarity"] = max(int(details.get("logical_clarity", 0)), 10)
                details["expression_ability"] = max(int(details.get("expression_ability", 0)), 6)
                # 命中率已不低时，professional 不应被压成 0（避免“命中明显但专业维塌陷”）
                if float(hr) >= 0.45:
                    details["professional_level"] = max(int(details.get("professional_level", 0)), 16)
                    # 命中较高但模型总分过低，标记为“质量重算”
                    if isinstance(raw_score, (int, float)) and float(raw_score) < 40:
                        quality_recompute = True
            # 即使模型把 hit_rate 写错为 0，只要命中点明显，也不能把专业题打成全零
            elif len(hit_points) >= 2 and readable:
                inferred = min(1.0, len(hit_points) / max(3.0, float(len(hit_points) + 2)))
                details["content_completeness"] = max(
                    int(details.get("content_completeness", 0)),
                    max(12, int(round(inferred * 40))),
                )
                details["logical_clarity"] = max(int(details.get("logical_clarity", 0)), 10)
                details["professional_level"] = max(int(details.get("professional_level", 0)), 12)
                details["expression_ability"] = max(int(details.get("expression_ability", 0)), 6)
                quality_recompute = True

        # 基础题：纠正 7B 将口癖打到表达维、或要点已覆盖却把 professional/logical 打过低的问题（无 ASR 词表）
        if is_basic_question and readable:
            question_text = str(question_info.get("question_content") or "")
            is_repeat = self._is_answer_just_repeating_keywords(answer_text, question_text)
            if not is_repeat:
                details["expression_ability"] = max(int(details.get("expression_ability", 0)), 6)
                if self._basic_answer_has_substance(answer_text, question_info):
                    details["professional_level"] = max(int(details.get("professional_level", 0)), 16)
                    details["logical_clarity"] = max(int(details.get("logical_clarity", 0)), 10)
                    n_pts = self._count_basic_evaluation_points(question_info)
                    if n_pts <= 1:
                        details["content_completeness"] = max(int(details.get("content_completeness", 0)), 20)
                    else:
                        details["content_completeness"] = max(int(details.get("content_completeness", 0)), 18)
                    cur_sum = sum(int(details.get(k, 0) or 0) for k in dim_keys)
                    target_floor = 57
                    lc = len(cleaned)
                    if lc >= 80:
                        target_floor = 63
                    elif lc >= 40:
                        target_floor = 60
                    elif n_pts <= 1 and lc >= 12:
                        # 单要点题：一句明确表态也应接近合格区间
                        target_floor = max(target_floor, 60)
                    if cur_sum < target_floor:
                        self._boost_basic_sum_to_target(details, caps, target_floor)

        # 最终收口：专业题在后处理阶段的调整幅度不超过“校准后原分”的20%
        if not is_basic_question:
            pre = out.get("_pre_calibration_score", raw_score)
            rs = pre if isinstance(pre, (int, float)) else (raw_score if isinstance(raw_score, (int, float)) else 0)
            if rs > 0 and not quality_recompute:
                cur_sum = sum(int(details.get(k, 0) or 0) for k in dim_keys)
                lo = int(round(rs * 0.8))
                hi = int(round(rs * 1.2))
                capped = max(lo, min(hi, cur_sum))
                if capped != cur_sum:
                    new_dims = self._integer_adjust_dimensions(
                        dim_keys,
                        [float(details.get(k, 0) or 0) for k in dim_keys],
                        caps,
                        capped,
                    )
                    for k in dim_keys:
                        details[k] = int(new_dims.get(k, details.get(k, 0)))
            elif quality_recompute:
                reason = (out.get("reason") or "").strip()
                tag = "（系统校准：命中证据纠偏后重算维度，未套用原分±20%限幅）"
                if tag not in reason:
                    out["reason"] = (reason + tag).strip()[:480]
            # 内部传递字段不对外保留
            out.pop("_pre_calibration_score", None)

        if hit_recalc_note:
            reason = (out.get("reason") or "").strip()
            note = f"（系统校准：{hit_recalc_note}）"
            if note not in reason:
                out["reason"] = (reason + note).strip()[:480]

        if audit_trace:
            out["audit_trace"] = audit_trace
        out["details"] = details
        out["score"] = int(sum(int(details[k]) for k in dim_keys))
        return out

    @staticmethod
    def _parse_json_lenient(raw: str) -> Dict[str, Any]:
        """解析评分 JSON；若模型在合法 JSON 后又追加了解释文字，只取第一个完整对象。"""
        text = (raw or "").strip()
        if not text:
            raise ValueError("empty json")
        try:
            val = json.loads(text)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
        dec = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _end = dec.raw_decode(text, i)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        raise ValueError("no valid json object in content")

    @staticmethod
    def _safe_eval_numeric_expr(expr: str) -> Optional[float]:
        """安全计算简单数值表达式（用于修复模型输出中的 round()/max()/min() 公式）。"""
        if not expr:
            return None
        text = expr.strip()
        if len(text) > 120:
            return None
        if re.search(r'[^0-9\.\+\-\*\/\(\),\sA-Za-z_]', text):
            return None

        allowed_funcs = {"round": round, "max": max, "min": min}

        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.Num):
                return node.n
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                l = _eval(node.left)
                r = _eval(node.right)
                if isinstance(node.op, ast.Add):
                    return l + r
                if isinstance(node.op, ast.Sub):
                    return l - r
                if isinstance(node.op, ast.Mult):
                    return l * r
                return l / r if r != 0 else 0
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                v = _eval(node.operand)
                return v if isinstance(node.op, ast.UAdd) else -v
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                fn = allowed_funcs.get(node.func.id)
                if not fn:
                    raise ValueError("unsafe func")
                args = [_eval(a) for a in node.args]
                return fn(*args)
            raise ValueError("unsafe expr")

        try:
            tree = ast.parse(text, mode="eval")
            v = _eval(tree)
            return float(v)
        except Exception:
            return None

    def _normalize_formula_numbers_in_json_like(self, text: str) -> str:
        """
        将 JSON-like 文本中常见数值公式（如 round((2/3)*40)）转换成数字，
        以提升 Qwen 输出的可解析性。
        """
        if not text:
            return text

        numeric_keys = [
            "score",
            "content_completeness",
            "logical_clarity",
            "professional_level",
            "expression_ability",
            "total_key_points",
            "hit_rate",
        ]

        out = text

        def _replace_numeric_value(src: str, key: str) -> str:
            token = f'"{key}"'
            idx = 0
            while True:
                pos = src.find(token, idx)
                if pos < 0:
                    break
                colon = src.find(":", pos + len(token))
                if colon < 0:
                    break
                v_start = colon + 1
                while v_start < len(src) and src[v_start].isspace():
                    v_start += 1
                if v_start >= len(src):
                    break
                if src[v_start] == '"':
                    idx = v_start + 1
                    continue

                j = v_start
                paren_depth = 0
                in_string = False
                while j < len(src):
                    ch = src[j]
                    if ch == '"' and (j == 0 or src[j - 1] != "\\"):
                        in_string = not in_string
                    if not in_string:
                        if ch == "(":
                            paren_depth += 1
                        elif ch == ")":
                            paren_depth = max(paren_depth - 1, 0)
                        elif paren_depth == 0 and ch in ",}\n":
                            break
                    j += 1

                raw_val = src[v_start:j].strip()
                if not raw_val or re.fullmatch(r"-?\d+(\.\d+)?", raw_val):
                    idx = j + 1
                    continue

                v = self._safe_eval_numeric_expr(raw_val)
                if v is None:
                    idx = j + 1
                    continue

                if key in (
                    "score",
                    "content_completeness",
                    "logical_clarity",
                    "professional_level",
                    "expression_ability",
                    "total_key_points",
                ):
                    rep = str(int(round(v)))
                else:
                    rep = str(round(v, 4))

                src = src[:v_start] + rep + src[j:]
                idx = v_start + len(rep) + 1
            return src

        for key in numeric_keys:
            out = _replace_numeric_value(out, key)

        # 兼容模型偶发输出：`"x": round(...) = 10` 或 `"x": xxx = 10`
        # 优先采用等号右侧的显式数字，避免整段 JSON 解析失败。
        for key in numeric_keys:
            out = re.sub(
                rf'("{key}"\s*:\s*)[^,\n\}}]*=\s*(-?\d+(?:\.\d+)?)',
                rf'\1\2',
                out,
            )

        return out

    def _parse_llm_response(self, response: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        """解析LLM响应
        
        LLM服务返回格式: {"content": "...", "usage": {...}, "model": "...", "finish_reason": "..."}
        content字段包含实际的评分JSON: {"score": 85, "reason": "...", "dimensions": {...}}
        """
        try:
            # 检查response类型
            if not response:
                logger.warning("LLM响应为空")
                return {
                    'score': 0,
                    'reason': 'LLM响应为空，无法评分',
                    'details': {}
                }
            
            # 关键修复：如果response是字典（LLM服务返回的格式），提取content字段
            if isinstance(response, dict):
                # LLM服务返回的是 {"content": "...", "usage": {...}, ...}
                # 需要提取content字段
                if 'content' in response:
                    content = response['content']
                    logger.debug(f"从LLM响应中提取content字段: {content[:100]}...")
                    
                    # 如果content是字符串，尝试解析为JSON
                    if isinstance(content, str):
                        content = content.strip()
                        # 清理可能的markdown代码块标记
                        if content.startswith('```json'):
                            content = content[7:]
                        if content.startswith('```'):
                            content = content[3:]
                        if content.endswith('```'):
                            content = content[:-3]
                        content = content.strip()
                        content = self._normalize_formula_numbers_in_json_like(content)
                        
                        # 尝试解析JSON（容忍 JSON 后的额外文本）
                        try:
                            result = self._parse_json_lenient(content)
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.error(f"解析content JSON失败: {e}, content: {content[:200]}")
                            import re
                            json_match = re.search(r"\{", content)
                            if json_match:
                                result = self._parse_json_lenient(content[json_match.start() :])
                            else:
                                raise
                    elif isinstance(content, dict):
                        # content已经是字典
                        result = content
                    else:
                        logger.error(f"content类型异常: {type(content)}")
                        raise ValueError(f"content类型异常: {type(content)}")
                    
                    # 提取评分结果
                    score = result.get('score', 0)
                    if not isinstance(score, (int, float)):
                        try:
                            score = float(score)
                        except:
                            score = 0
                            logger.warning(f"无法解析score: {result.get('score')}, 评分失败")
                    
                    # 如果score为0，保持0分（LLM明确评分为0）
                    if score == 0:
                        reason = result.get('reason', '') or result.get('reasoning', '')
                        logger.info(f"LLM评分为0分，理由: {reason}")
                    
                    return {
                        'score': score,
                        'reason': result.get('reason', '') or result.get('reasoning', ''),
                        'details': result.get('dimensions', {}) or result.get('details', {}),
                        'hit_analysis': result.get('hit_analysis', {}) if isinstance(result.get('hit_analysis', {}), dict) else {},
                        'audit_trace': result.get('audit_trace', {}) if isinstance(result.get('audit_trace', {}), dict) else {}
                    }
                else:
                    # 如果没有content字段，可能直接就是评分结果
                    logger.warning("LLM响应字典中没有content字段，尝试直接解析")
                    score = response.get('score', 0)
                    if not isinstance(score, (int, float)):
                        try:
                            score = float(score)
                        except:
                            score = 0
                    
                    if score == 0:
                        logger.warning("评分为0，评分失败")
                        score = 0
                    
                    return {
                        'score': score,
                        'reason': response.get('reason', '') or response.get('reasoning', ''),
                        'details': response.get('dimensions', {}) or response.get('details', {}),
                        'hit_analysis': response.get('hit_analysis', {}) if isinstance(response.get('hit_analysis', {}), dict) else {},
                        'audit_trace': response.get('audit_trace', {}) if isinstance(response.get('audit_trace', {}), dict) else {}
                    }
            
            # 如果response是字符串，直接解析
            if isinstance(response, str):
                response = response.strip()
                # 清理可能的markdown代码块标记
                if response.startswith('```json'):
                    response = response[7:]
                if response.startswith('```'):
                    response = response[3:]
                if response.endswith('```'):
                    response = response[:-3]
                response = response.strip()
                response = self._normalize_formula_numbers_in_json_like(response)
                
                # 尝试提取 JSON（从首个 { 起宽松解析）
                import re
                json_match = re.search(r"\{", response)
                if json_match:
                    result = self._parse_json_lenient(response[json_match.start() :])
                    
                    score = result.get('score', 0)
                    if not isinstance(score, (int, float)):
                        try:
                            score = float(score)
                        except:
                            score = 0
                            logger.warning(f"无法解析score: {result.get('score')}, 评分失败")
                    
                    if score == 0:
                        logger.warning("评分为0，评分失败")
                        score = 0

                    return {
                        'score': score,
                        'reason': result.get('reason', ''),
                        'details': result.get('dimensions', {}),
                        'hit_analysis': result.get('hit_analysis', {}) if isinstance(result.get('hit_analysis', {}), dict) else {},
                        'audit_trace': result.get('audit_trace', {}) if isinstance(result.get('audit_trace', {}), dict) else {}
                    }

        except Exception as e:
            logger.error(f"解析LLM响应失败: {str(e)}", exc_info=True)

        # 如果所有解析都失败，使用默认值
        response_str = str(response) if response else ''
        response_preview = response_str[:200] if len(response_str) > 200 else response_str
        logger.error(f"LLM响应解析失败，响应内容: {response_preview}")
        return {
            'score': 0,  # 评分失败返回0
            'reason': f'LLM响应解析失败，使用默认评分。响应类型: {type(response).__name__}',
            'details': {},
            'hit_analysis': {},
            'audit_trace': {}
        }

    def _should_follow_up(self, score: float) -> bool:
        """判断是否需要追问"""
        return score < self.follow_up_score_threshold

    def _is_parse_fallback_result(self, score_result: Dict[str, Any]) -> bool:
        """判断是否命中了解析失败兜底结果。"""
        reason = (score_result or {}).get('reason', '')
        return isinstance(reason, str) and reason.startswith('LLM响应解析失败')

    async def _generate_follow_up_question(self, question_info: Dict[str, Any],
                                        answer_text: str, score_result: Dict[str, Any]) -> Dict[str, Any]:
        """生成追问问题和评估要点
        
        Returns:
            Dict包含:
            - question: 追问问题文本
            - evaluation_points: 追问评估要点列表
        """
        try:
            question_content = question_info.get('question_content', '')
            original_evaluation_points = question_info.get('evaluation_points', [])

            prompt = f"""基于以下信息，生成一个针对性的追问问题和对应的评估要点：

原问题：{question_content}
候选人答案：{answer_text}
评分结果：{score_result.get('score', 0)}分
评分理由：{score_result.get('reason', '')}

请生成：
1. 一个简洁、有针对性的追问问题，帮助候选人更好地展示自己的能力
2. 针对追问问题的3-5个评估要点（每个要点包含point和weight字段）

追问问题应该：
- 针对答案中的薄弱环节
- 引导候选人提供更具体或深入的回答
- 保持专业性和建设性

评估要点应该：
- 针对追问问题的具体内容
- 明确评估的关键点
- 设置合理的权重（总和为100）

请以JSON格式返回：
{{
  "follow_up_question": "追问问题文本",
  "evaluation_points": [
    {{"point": "评估要点1", "weight": 30}},
    {{"point": "评估要点2", "weight": 30}},
    {{"point": "评估要点3", "weight": 40}}
  ]
}}"""

            response = await self.llm_service.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=self.follow_up_max_tokens
            )
            is_truncated = isinstance(response, dict) and response.get("finish_reason") == "length"
            if is_truncated:
                logger.error("追问生成输出被截断（finish_reason=length），请继续提高max_tokens")
                if self.retry_on_truncation:
                    retry_max_tokens = min(
                        int(self.follow_up_max_tokens * self.retry_multiplier),
                        int(self.retry_max_tokens_cap)
                    )
                    if retry_max_tokens > self.follow_up_max_tokens:
                        logger.warning(
                            f"追问生成准备重试一次: max_tokens={self.follow_up_max_tokens}->{retry_max_tokens}"
                        )
                        response = await self.llm_service.chat_completion(
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.7,
                            max_tokens=retry_max_tokens
                        )

            if response:
                # 处理response可能是字典或字符串的情况
                if isinstance(response, dict):
                    content = response.get('content', '') or response.get('text', '') or str(response)
                elif isinstance(response, str):
                    content = response
                else:
                    content = str(response)
                
                # 清理响应文本
                content = content.strip()
                if content.startswith('```json'):
                    content = content[7:]
                if content.startswith('```'):
                    content = content[3:]
                if content.endswith('```'):
                    content = content[:-3]
                content = content.strip()
                
                # 解析JSON
                result = json.loads(content)
                
                follow_up_question = result.get('follow_up_question', '')
                evaluation_points = result.get('evaluation_points', [])
                
                if follow_up_question and evaluation_points:
                    logger.info(f"成功生成追问问题和{len(evaluation_points)}个评估要点")
                    return {
                        'question': follow_up_question,
                        'evaluation_points': evaluation_points
                    }

        except Exception as e:
            logger.error(f"生成追问问题和评估要点失败: {str(e)}", exc_info=True)
            # 关键修复：生成失败时返回None，不使用默认值，让调用方知道生成失败
            return {
                'question': None,
                'evaluation_points': None
            }

    def _save_evaluation_result(self, session_id: str, question_id: str, answer_text: str,
                               score_result: Dict[str, Any], need_follow_up: bool,
                               follow_up_question: Optional[str], question_info: Optional[Dict[str, Any]] = None,
                               follow_up_evaluation_points: Optional[List[Dict[str, Any]]] = None,
                               rescore_mode: bool = False):
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
            # 批量重评时不追加 session_content，避免重复堆积 AI 评分段落
            if not rescore_mode:
                # 注意：不在此处递增 follow_up_used。若在每次 need_follow_up 的主评分保存时 +1，
                # 会与「每道专业题各追问一次」冲突，且导致 WebSocket 侧误判已达会话上限、走错误分支。
                update_session_sql = """
                UPDATE interview_session
                SET candidate_answer = %s,
                    session_content = COALESCE(session_content, '') || %s,
                    session_status = CASE WHEN %s THEN 'COMPLETED' ELSE 'IN_PROGRESS' END,
                    end_time = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE end_time END
                WHERE session_id = %s
                """

                if self.db_manager.db_type != 'postgresql':
                    update_session_sql = update_session_sql.replace('%s', '?')

                # 将分数转换为1-5分制（原来是1-100分）
                score_5_scale = min(5, max(1, round(score_result.get('score', 60) / 20)))

                session_content = f"\n[AI评分:{score_5_scale}/5] {score_result.get('reason', '')}"
                if need_follow_up and follow_up_question:
                    session_content += f"\n[AI追问] {follow_up_question}"

                rows_affected = self.db_manager.execute_update(update_session_sql, (
                    answer_text,
                    session_content,
                    not need_follow_up,  # 如果不需要追问，则标记为完成
                    not need_follow_up,  # 如果不需要追问，则设置结束时间
                    session_id,
                ))
                if rows_affected == 0:
                    logger.error(f"更新interview_session失败: 没有行被更新, session_id={session_id}, question_id={question_id}, answer_length={len(answer_text)}")
                else:
                    logger.info(f"已更新interview_session表: session_id={session_id}, question_id={question_id}, answer_length={len(answer_text)}, rows_affected={rows_affected}")
            else:
                logger.info(f"rescore_mode: 跳过 interview_session 更新: session_id={session_id}")

            # 2. 保存到candidate_answers表（主问题答案，is_follow_up=false）
            # 检查是否已存在该答案记录
            check_answer_sql = """
            SELECT id FROM candidate_answers 
            WHERE session_id = %s AND question_id = %s AND is_follow_up = FALSE
            ORDER BY create_time DESC
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
                'audit_trace': score_result.get('audit_trace', {}),  # 审计字段：重算前后+命中/漏点摘要
                'need_follow_up': need_follow_up,
                'follow_up_question': follow_up_question,
                'timestamp': datetime.now().isoformat()
            }
            
            if existing_answer:
                # 更新现有答案记录
                answer_id = existing_answer['id']
                # 关键修复：如果需要追问，状态应该是 'waiting_follow_up' 而不是 'evaluated'
                status_to_set = 'waiting_follow_up' if need_follow_up else 'evaluated'
                
                # 关键修复：确保follow_up_question和follow_up_evaluation_points被正确保存
                # 如果follow_up_question或follow_up_evaluation_points为None，但need_follow_up为True，说明生成失败，应该记录警告
                if need_follow_up and not follow_up_question:
                    logger.warning(f"[追问保存-步骤2] ⚠️ need_follow_up=True但follow_up_question为None，可能生成失败: question_id={question_id}, answer_id={answer_id}")
                if need_follow_up and not follow_up_evaluation_points:
                    logger.warning(f"[追问保存-步骤2] ⚠️ need_follow_up=True但follow_up_evaluation_points为None，可能生成失败: question_id={question_id}, answer_id={answer_id}")
                
                logger.info(f"[追问保存-步骤2] 准备UPDATE: answer_id={answer_id}, follow_up_question={follow_up_question[:50] if follow_up_question else None}..., follow_up_evaluation_points={len(follow_up_evaluation_points) if follow_up_evaluation_points else 0}个")
                
                # 关键修复：检查数据库中现有的answer_text，用于日志记录
                # 如果是追问评分（follow_up_question为None），不应该更新answer_text字段
                # 因为answer_text应该保存主问题的答案，不应该被追问答案覆盖
                # 追问答案应该保存在follow_up_answer_text字段中（由websocket_server.py的perform_real_time_evaluation方法更新）
                check_existing_sql = "SELECT answer_text FROM candidate_answers WHERE id = %s"
                if self.db_manager.db_type != 'postgresql':
                    check_existing_sql = check_existing_sql.replace('%s', '?')
                existing_answer_data = self.db_manager.fetch_one(check_existing_sql, (answer_id,))
                existing_answer_text = None
                if existing_answer_data:
                    existing_answer_text = existing_answer_data.get('answer_text') if isinstance(existing_answer_data, dict) else existing_answer_data[0]
                
                # 关键修复：如果是追问评分（follow_up_question为None），则只更新必要的字段，不覆盖answer_text、follow_up_question和follow_up_evaluation_points
                if follow_up_question is None:
                    # 追问评分时，不更新answer_text、follow_up_question和follow_up_evaluation_points，保留原有值
                    # answer_text应该保存主问题的答案，不应该被追问答案覆盖
                    # 追问答案应该保存在follow_up_answer_text字段中（由websocket_server.py更新）
                    logger.info(f"[追问保存-步骤2] 🔒 追问评分模式：不更新answer_text字段（保留主问题答案），现有answer_text={existing_answer_text[:50] if existing_answer_text else None}..., 追问答案长度={len(answer_text)}，追问答案内容={answer_text[:50] if answer_text else None}...")
                    update_answer_sql = """
                    UPDATE candidate_answers
                    SET status = %s,
                        point_evaluations = %s,
                        final_score = %s,
                        need_follow_up = %s,
                        evaluation_result = %s,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                else:
                    # 主问题评分时，更新所有字段包括answer_text、follow_up_question和follow_up_evaluation_points
                    logger.info(f"[追问保存-步骤2] ✅ 主问题评分模式：更新answer_text字段，answer_text长度={len(answer_text)}")
                    update_answer_sql = """
                    UPDATE candidate_answers
                    SET answer_text = %s,
                        status = %s,
                        point_evaluations = %s,
                        final_score = %s,
                        need_follow_up = %s,
                        follow_up_question = %s,
                        follow_up_evaluation_points = %s,
                        evaluation_result = %s,
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """
                if self.db_manager.db_type != 'postgresql':
                    update_answer_sql = update_answer_sql.replace('%s', '?')
                
                import json
                follow_up_question_to_save = follow_up_question  # 即使为None也要保存
                follow_up_points_json = json.dumps(follow_up_evaluation_points, ensure_ascii=False) if follow_up_evaluation_points else None
                logger.info(f"[追问保存-步骤2] 保存数据: follow_up_question长度={len(follow_up_question_to_save) if follow_up_question_to_save else 0}, follow_up_evaluation_points长度={len(follow_up_points_json) if follow_up_points_json else 0}")
                
                # 关键修复：根据SQL语句是否包含answer_text、follow_up_question和follow_up_evaluation_points字段，传递相应数量的参数
                if follow_up_question is None:
                    # 追问评分时，SQL不包含answer_text、follow_up_question和follow_up_evaluation_points字段，只传递6个参数
                    logger.info(f"[追问保存-步骤2] 🔒 追问评分：不更新answer_text（保留主问题答案），只更新评分相关字段")
                    rows_affected = self.db_manager.execute_update(update_answer_sql, (
                        status_to_set,  # 动态状态：需要追问时为waiting_follow_up，否则为evaluated
                        json.dumps(point_evaluations) if point_evaluations else None,  # point_evaluations存储评估要点
                        final_score,
                        need_follow_up,
                        json.dumps(evaluation_result),  # evaluation_result存储完整的评估结果
                        answer_id
                    ))
                else:
                    # 主问题评分时，SQL包含answer_text、follow_up_question和follow_up_evaluation_points字段，传递9个参数
                    logger.info(f"[追问保存-步骤2] ✅ 主问题评分：更新answer_text和追问相关字段")
                    rows_affected = self.db_manager.execute_update(update_answer_sql, (
                        answer_text,
                        status_to_set,  # 动态状态：需要追问时为waiting_follow_up，否则为evaluated
                        json.dumps(point_evaluations) if point_evaluations else None,  # point_evaluations存储评估要点
                        final_score,
                        need_follow_up,
                        follow_up_question_to_save,  # 关键修复：确保follow_up_question被保存
                        follow_up_points_json,  # 追问评估要点
                        json.dumps(evaluation_result),  # evaluation_result存储完整的评估结果
                        answer_id
                    ))
                if rows_affected == 0:
                    logger.error(f"[追问保存-步骤2] 更新candidate_answers失败: 没有行被更新, answer_id={answer_id}, session_id={session_id}, question_id={question_id}, score={final_score}")
                else:
                    logger.info(f"[追问保存-步骤2] ✅ 已更新candidate_answers记录: answer_id={answer_id}, session_id={session_id}, question_id={question_id}, score={final_score}, follow_up_question={follow_up_question_to_save is not None}, follow_up_evaluation_points={follow_up_points_json is not None}, rows_affected={rows_affected}")
            else:
                # 创建新答案记录
                # 关键修复：如果需要追问，状态应该是 'waiting_follow_up' 而不是 'evaluated'
                status_to_set = 'waiting_follow_up' if need_follow_up else 'evaluated'
                
                # 关键修复：确保follow_up_question和follow_up_evaluation_points被正确保存
                # 如果follow_up_question或follow_up_evaluation_points为None，但need_follow_up为True，说明生成失败，应该记录警告
                if need_follow_up and not follow_up_question:
                    logger.warning(f"[追问保存-步骤2] ⚠️ need_follow_up=True但follow_up_question为None，可能生成失败: question_id={question_id}, session_id={session_id}")
                if need_follow_up and not follow_up_evaluation_points:
                    logger.warning(f"[追问保存-步骤2] ⚠️ need_follow_up=True但follow_up_evaluation_points为None，可能生成失败: question_id={question_id}, session_id={session_id}")
                
                answer_id = f"ANS_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
                logger.info(f"[追问保存-步骤2] 准备INSERT: answer_id={answer_id}, follow_up_question={follow_up_question[:50] if follow_up_question else None}..., follow_up_evaluation_points={len(follow_up_evaluation_points) if follow_up_evaluation_points else 0}个")
                insert_answer_sql = """
                INSERT INTO candidate_answers (
                    id, session_id, question_id, answer_text,
                    is_follow_up, parent_answer_id,
                    status, point_evaluations, final_score,
                    need_follow_up, follow_up_question, follow_up_evaluation_points,
                    evaluation_result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                if self.db_manager.db_type != 'postgresql':
                    insert_answer_sql = insert_answer_sql.replace('%s', '?')
                
                import json
                follow_up_question_to_save = follow_up_question  # 即使为None也要保存
                follow_up_points_json = json.dumps(follow_up_evaluation_points, ensure_ascii=False) if follow_up_evaluation_points else None
                logger.info(f"[追问保存-步骤2] 保存数据: follow_up_question长度={len(follow_up_question_to_save) if follow_up_question_to_save else 0}, follow_up_evaluation_points长度={len(follow_up_points_json) if follow_up_points_json else 0}")
                rows_affected = self.db_manager.execute_update(insert_answer_sql, (
                    answer_id,
                    session_id,
                    question_id,
                    answer_text,
                    False,  # is_follow_up = False（主问题答案）
                    None,  # parent_answer_id = None（主问题没有父答案）
                    status_to_set,  # 动态状态：需要追问时为waiting_follow_up，否则为evaluated
                    json.dumps(point_evaluations) if point_evaluations else None,  # point_evaluations存储评估要点
                    final_score,
                    need_follow_up,
                    follow_up_question_to_save,  # 关键修复：确保follow_up_question被保存，即使为None也要保存
                    follow_up_points_json,  # 追问评估要点
                    json.dumps(evaluation_result)  # evaluation_result存储完整的评估结果
                ))
                if rows_affected == 0:
                    logger.error(f"[追问保存-步骤2] 插入candidate_answers失败: 没有行被插入, answer_id={answer_id}, session_id={session_id}, question_id={question_id}, answer_length={len(answer_text)}")
                else:
                    logger.info(f"[追问保存-步骤2] ✅ 已创建candidate_answers记录: answer_id={answer_id}, session_id={session_id}, question_id={question_id}, answer_length={len(answer_text)}, score={final_score}, follow_up_question={follow_up_question_to_save is not None}, follow_up_evaluation_points={follow_up_points_json is not None}, rows_affected={rows_affected}")

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

        # 去掉题干词后几乎无「自有词」：多为复述题干（比单纯阈值更稳）
        remaining_words = answer_words - question_words
        if len(answer_words) >= 4 and len(remaining_words) <= 2 and overlap_ratio > 0.55 and len(answer_text) < 120:
            return True

        # 轻量「字符熵」启发：篇幅不短但字符种类极少，且与题干重叠高 → 疑似刷同一专业词/复读
        compact = re.sub(r"\s+", "", answer_text)
        if len(compact) >= 36:
            uniq_ratio = len(set(compact)) / len(compact)
            if uniq_ratio < 0.18 and overlap_ratio > 0.52:
                return True

        # 若大量词来自题干且篇幅短，视为「读题 / 复述题干」
        if overlap_ratio > 0.68 and len(answer_text) < 85:
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
                AVG(evaluation_score) as avg_score,
                MIN(evaluation_score) as min_score,
                MAX(evaluation_score) as max_score,
                SUM(CASE WHEN (evaluation_result->>'need_follow_up')::boolean THEN 1 ELSE 0 END) as follow_up_count
            FROM candidate_answers
            WHERE create_time >= CURRENT_DATE
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
            DELETE FROM candidate_answers
            WHERE create_time < CURRENT_DATE - INTERVAL '%s days'
            """

            if self.db_manager.db_type != 'postgresql':
                sql = sql.replace("CURRENT_DATE - INTERVAL '%s days'", "datetime('now', '-%s days')")

            self.db_manager.execute(sql, (days,))

            logger.info(f"已清理{days}天前的评分结果")

        except Exception as e:
            logger.error(f"清理旧评分结果失败: {str(e)}")