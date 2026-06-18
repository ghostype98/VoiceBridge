#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导入 Excel 问答并触发评分流水线。

Excel 必填 9 列（列名需完全一致）：
- 类型
- 类别
- 题目内容
- 候选人回答
- 难度
- 时长
- 评估要点
- 参考答案
- （可选）题目顺序

使用示例：
python3 scripts/import_excel_and_score.py \
  --excel /path/to/input.xlsx \
  --invitation-id INV_20260508_123456_A \
  --replace
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 pandas，请先安装：pip install pandas openpyxl") from exc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database.connection import get_db_manager  # noqa: E402
from app.database.service import database_service  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from app.voice_streaming.realtime_scorer import RealtimeScorer  # noqa: E402
from agent.interview_evaluation_service import interview_evaluation_service  # noqa: E402
from config.settings import settings  # noqa: E402


REQUIRED_COLUMNS = [
    "类型",
    "类别",
    "题目内容",
    "候选人回答",
    "难度",
    "时长",
    "评估要点",
    "参考答案",
]

COLUMN_ALIASES = {
    "时长(分钟)": "时长",
    "时长（分钟）": "时长",
    "时长(分)": "时长",
    "时长（分）": "时长",
}


def _normalize_dataframe_columns(df: "pd.DataFrame") -> "pd.DataFrame":
    """
    统一处理 Excel 列名：
    1) 去掉首尾空白
    2) 将常见别名映射到标准列名（如 时长(分钟) -> 时长）
    """
    rename_map: Dict[str, str] = {}
    for col in df.columns:
        normalized = str(col).strip()
        normalized = COLUMN_ALIASES.get(normalized, normalized)
        rename_map[col] = normalized
    return df.rename(columns=rename_map)


def _normalize_question_type(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    if value in ("BASIC", "BASIC_INFO", "基础", "基础题", "基本"):
        return "BASIC"
    if value in ("SPECIALTY", "PROFESSIONAL", "专业", "专业题"):
        return "SPECIALTY"
    return "BASIC"


def _safe_json_parse(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except Exception:
        # 非 JSON 时，回退为单要点形式
        if isinstance(fallback, list):
            return [{"point": text, "weight": 1.0}]
        return fallback


def _build_realtime_config() -> Dict[str, Any]:
    voice = settings.get_config("voice_streaming")
    thresholds = settings.get_config("scoring_thresholds")
    cfg: Dict[str, Any] = dict((voice or {}).get("evaluation") or {})
    cfg["follow_up_score_threshold"] = (thresholds or {}).get("follow_up_score_threshold", 60)
    for key, val in ((voice or {}).get("streaming_interview") or {}).items():
        cfg.setdefault(key, val)
    return cfg


def _generate_question_id() -> str:
    return f"Q_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"


def _generate_session_id() -> str:
    return f"SES_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"


def _parse_duration_seconds(raw: Any) -> int:
    """Excel 的「时长」按分钟输入，入库前换算为秒。"""
    if raw is None:
        return 180
    if isinstance(raw, (int, float)):
        return max(30, min(1800, int(float(raw) * 60)))
    text = str(raw).strip()
    if not text:
        return 180
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return 180
    minutes = float(match.group(0))
    return max(30, min(1800, int(minutes * 60)))


def _validate_excel_columns(df: "pd.DataFrame") -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Excel 缺少必填列: {missing}")


def _create_atomic_question(
    db,
    question_text: str,
    standard_answer: str,
    evaluation_points: Any,
    question_type: str,
    question_category: str,
) -> Any:
    sql = """
    INSERT INTO interview_questions (
        id, content, question_category, question_type, standard_answer, evaluation_points
    )
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING id
    """
    atomic_id = f"IQ_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"
    row = db.execute_one(
        sql,
        (
            atomic_id,
            question_text,
            question_category or "general",
            question_type or "BASIC",
            standard_answer or "",
            json.dumps(evaluation_points, ensure_ascii=False),
        ),
    )
    if not row:
        raise RuntimeError("创建 interview_questions 题库记录失败")
    return row["id"]


def _replace_invitation_questions(db, invitation_id: str) -> None:
    q_sql = "SELECT question_id FROM interview_question WHERE invitation_id = %s"
    rows = db.execute_query(q_sql, (invitation_id,)) or []
    question_ids = [r["question_id"] for r in rows]

    if question_ids:
        placeholders = ",".join(["%s"] * len(question_ids))
        del_answers = f"DELETE FROM candidate_answers WHERE question_id IN ({placeholders})"
        db.execute_update(del_answers, tuple(question_ids))

        del_sessions = "DELETE FROM interview_session WHERE invitation_id = %s"
        db.execute_update(del_sessions, (invitation_id,))

        del_questions = "DELETE FROM interview_question WHERE invitation_id = %s"
        db.execute_update(del_questions, (invitation_id,))

    db.execute_update(
        "DELETE FROM interview_evaluation_record WHERE invitation_id = %s",
        (invitation_id,),
    )


def _insert_questions_and_answers(
    db,
    invitation_id: str,
    session_id: str,
    rows: List[Dict[str, Any]],
) -> None:
    for idx, row in enumerate(rows, start=1):
        question_text = str(row.get("题目内容") or "").strip()
        answer_text = str(row.get("候选人回答") or "").strip()
        if not question_text:
            raise ValueError(f"第 {idx} 行题目内容为空")
        if not answer_text:
            raise ValueError(f"第 {idx} 行候选人回答为空")

        evaluation_points = _safe_json_parse(row.get("评估要点"), [])
        standard_answer = str(row.get("参考答案") or "").strip()
        q_type = _normalize_question_type(row.get("类型"))
        question_category = str(row.get("类别") or "").strip()
        difficulty = str(row.get("难度") or "中等").strip() or "中等"
        estimated_duration = _parse_duration_seconds(row.get("时长"))
        question_order = int(row.get("题目顺序") or idx)

        atomic_question_id = _create_atomic_question(
            db=db,
            question_text=question_text,
            standard_answer=standard_answer,
            evaluation_points=evaluation_points,
            question_type=q_type,
            question_category=question_category,
        )

        question_id = _generate_question_id()
        db.execute_update(
            """
            INSERT INTO interview_question (
                question_id, invitation_id, atomic_question_id, question_type,
                question_category, question_order, evaluation_points,
                estimated_duration, difficulty, question_text, create_time
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """,
            (
                question_id,
                invitation_id,
                atomic_question_id,
                q_type,
                question_category,
                question_order,
                json.dumps(evaluation_points, ensure_ascii=False),
                estimated_duration,
                difficulty,
                question_text,
            ),
        )

        database_service.create_candidate_answer(
            session_id=session_id,
            question_id=question_id,
            answer_text=answer_text,
            is_follow_up=False,
            status="recorded",
        )


async def _run_scoring_pipeline(session_id: str, invitation_id: str) -> Dict[str, Any]:
    llm_service = LLMService()
    scorer = RealtimeScorer(
        llm_service=llm_service,
        db_manager=get_db_manager(),
        config=_build_realtime_config(),
    )

    answers = database_service.get_session_candidate_answers(session_id) or []
    main_answers = [x for x in answers if not x.get("is_follow_up")]
    if not main_answers:
        raise RuntimeError("未找到可评分的主问题答案")

    for ans in main_answers:
        await scorer.evaluate_answer(
            session_id=session_id,
            question_id=ans["question_id"],
            answer_text=ans.get("answer_text") or "",
            rescore_mode=True,
        )

    result = await interview_evaluation_service.evaluate_interview(
        session_id=session_id,
        invitation_id=invitation_id,
    )
    return result


async def _async_main(args: argparse.Namespace) -> None:
    if not os.path.exists(args.excel):
        raise FileNotFoundError(f"Excel 文件不存在: {args.excel}")

    invitation = database_service.get_invitation_by_id(args.invitation_id)
    if not invitation:
        raise ValueError(f"invitation_id 不存在: {args.invitation_id}")

    df = pd.read_excel(args.excel)
    df = _normalize_dataframe_columns(df)
    _validate_excel_columns(df)
    data_rows = df.to_dict(orient="records")
    if not data_rows:
        raise ValueError("Excel 没有可用数据行")

    db = get_db_manager()
    if args.replace:
        _replace_invitation_questions(db, args.invitation_id)

    session_id = args.session_id or _generate_session_id()
    database_service.create_interview_session_record(
        invitation_id=args.invitation_id,
        session_id=session_id,
        session_status="IN_PROGRESS",
    )

    _insert_questions_and_answers(
        db=db,
        invitation_id=args.invitation_id,
        session_id=session_id,
        rows=data_rows,
    )

    score_result = await _run_scoring_pipeline(session_id, args.invitation_id)

    database_service.update_invitation_status(
        invitation_id=args.invitation_id,
        status="COMPLETED",
        end_time=datetime.now(),
    )

    print("导入并评分完成")
    print(f"invitation_id={args.invitation_id}")
    print(f"session_id={session_id}")
    print(f"overall_score={score_result.get('overall_score')}")
    print(f"is_passed={score_result.get('is_passed')}")
    print("状态已更新为 COMPLETED，可在评估列表查看")


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 Excel 并执行评分")
    parser.add_argument("--excel", required=True, help="Excel 文件路径")
    parser.add_argument("--invitation-id", required=True, help="已有 invitation_id")
    parser.add_argument("--session-id", help="可选，指定 session_id")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="覆盖该 invitation 现有题目/答案/评估记录后再导入",
    )
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
