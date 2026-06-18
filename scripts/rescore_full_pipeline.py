#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单脚本执行语音面试重评全流程：
1) RealtimeScorer 逐题重评（更新 candidate_answers，并写 logs/realtime_merged）
2) 面试报告重评（更新 interview_evaluation_record）

用法：
  # 仅按姓名查询（若同名>1，只展示候选列表并退出）
  python3 scripts/rescore_full_pipeline.py --name 张三

  # 指定 invitation_id 执行全流程
  python3 scripts/rescore_full_pipeline.py --invitation-id INV_xxx

  # 指定 invitation_id + session_id（最稳）
  python3 scripts/rescore_full_pipeline.py --invitation-id INV_xxx --session-id SESSION_xxx
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


def _get_database_service():
    from app.database.service import database_service  # noqa: E402

    return database_service


def _get_settings():
    from config.settings import settings  # noqa: E402

    return settings


def _find_invitations_by_name(name: str) -> List[Dict[str, Any]]:
    database_service = _get_database_service()
    pattern = f"%{name.strip()}%"
    q = """
    SELECT
        ii.invitation_id,
        ii.candidate_name,
        ii.interview_status,
        ii.created_time,
        ii.interview_actual_end_time,
        ier.update_time AS evaluation_update_time,
        ier.create_time AS evaluation_create_time,
        ier.overall_score
    FROM interview_invitation ii
    LEFT JOIN interview_evaluation_record ier
        ON ier.invitation_id = ii.invitation_id
    WHERE ii.candidate_name ILIKE %s
    ORDER BY ii.created_time DESC
    """
    return database_service.db.execute_query(q, (pattern,)) or []


def _resolve_session_id(invitation_id: str) -> Optional[str]:
    database_service = _get_database_service()
    q = """
    SELECT ca.session_id, COUNT(*) AS answer_count
    FROM candidate_answers ca
    WHERE ca.question_id IN (
        SELECT question_id FROM interview_question WHERE invitation_id = %s
    )
    GROUP BY ca.session_id
    ORDER BY answer_count DESC
    LIMIT 1
    """
    r = database_service.db.execute_one(q, (invitation_id,))
    if r and r.get("session_id"):
        return r["session_id"]

    r2 = database_service.db.execute_one(
        """
        SELECT session_id
        FROM interview_session
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """,
        (invitation_id,),
    )
    return r2.get("session_id") if r2 else None


def _fmt_dt(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def _print_name_matches(name: str, rows: List[Dict[str, Any]]) -> None:
    print(f"姓名「{name}」匹配到 {len(rows)} 条记录：")
    print(
        "  invitation_id | status | 邀请创建时间 | 面试结束时间 | 评估更新时间 | overall_score"
    )
    for r in rows:
        eval_time = r.get("evaluation_update_time") or r.get("evaluation_create_time")
        print(
            f"  {r.get('invitation_id')} | "
            f"{r.get('interview_status') or '-'} | "
            f"{_fmt_dt(r.get('created_time'))} | "
            f"{_fmt_dt(r.get('interview_actual_end_time'))} | "
            f"{_fmt_dt(eval_time)} | "
            f"{r.get('overall_score') if r.get('overall_score') is not None else '-'}"
        )


def _build_evaluation_config() -> Dict[str, Any]:
    settings = _get_settings()
    voice = settings.get_config("voice_streaming")
    thr = settings.get_config("scoring_thresholds")
    ev: Dict[str, Any] = dict(voice.get("evaluation") or {})
    ev["follow_up_score_threshold"] = thr.get("follow_up_score_threshold", 60)
    for k, v in (voice.get("streaming_interview") or {}).items():
        ev.setdefault(k, v)

    cfg_path = ROOT / "config" / "config.yaml"
    if yaml is not None and cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
            vs = (y.get("voice_streaming") or {}).get("evaluation") or {}
            llm_scoring = vs.get("llm_scoring")
            if isinstance(llm_scoring, dict):
                ev.setdefault("llm_scoring", llm_scoring)
        except Exception:
            pass
    return ev


async def _run_realtime_rescore(session_id: str) -> None:
    from app.database.connection import DatabaseManager  # noqa: E402
    from app.services.llm_service import LLMService  # noqa: E402
    from app.voice_streaming.realtime_scorer import RealtimeScorer  # noqa: E402

    database_service = _get_database_service()
    db_manager = DatabaseManager()
    llm_service = LLMService()
    scorer = RealtimeScorer(
        llm_service=llm_service,
        db_manager=db_manager,
        config=_build_evaluation_config(),
    )
    rows = database_service.get_session_candidate_answers(session_id) or []
    mains = [r for r in rows if not r.get("is_follow_up")]
    if not mains:
        raise SystemExit(f"session_id={session_id} 下没有主问题答案记录")

    print(f"共 {len(mains)} 道主问题，开始逐题重评（写 logs/realtime_merged）...")
    for row in mains:
        qid = row.get("question_id")
        old = row.get("final_score")
        text = (row.get("answer_text") or "").strip()
        print(f"  - {qid} 旧分={old} 字数={len(text)}")
        out = await scorer.evaluate_answer(
            session_id,
            qid,
            row.get("answer_text") or "",
            rescore_mode=True,
        )
        print(f"    新分={out.get('score')} follow_up={out.get('need_follow_up')}")


async def _run_report_reevaluate(session_id: str, invitation_id: str) -> None:
    from agent.interview_evaluation_service import interview_evaluation_service  # noqa: E402

    print("开始重算面试评价报告（interview_evaluation_record）...")
    result = await interview_evaluation_service.evaluate_interview(
        session_id=session_id,
        invitation_id=invitation_id,
    )
    print(
        f"报告已更新: overall_score={result.get('overall_score')} "
        f"is_passed={result.get('is_passed')}"
    )


async def _async_main(name: Optional[str], invitation_id: Optional[str], session_id: Optional[str]) -> None:
    database_service = _get_database_service()
    if invitation_id:
        iid = invitation_id
    else:
        if not name:
            raise SystemExit("请提供 --name 或 --invitation-id")
        rows = _find_invitations_by_name(name)
        if not rows:
            raise SystemExit(f"未找到姓名匹配「{name}」的邀请")
        if len(rows) > 1:
            _print_name_matches(name, rows)
            raise SystemExit(
                "\n检测到同名多条记录。请复制目标 invitation_id 后，用 "
                "--invitation-id 重新执行脚本。"
            )
        _print_name_matches(name, rows)
        iid = rows[0].get("invitation_id")
        if not iid:
            raise SystemExit("无法解析 invitation_id")

    sid = session_id or _resolve_session_id(iid)
    if not sid:
        raise SystemExit("无法解析 session_id，请使用 --session-id 显式传入")

    print(f"执行目标: invitation_id={iid} session_id={sid}")
    await _run_realtime_rescore(sid)
    await _run_report_reevaluate(sid, iid)
    print("\n全流程完成。")


def main() -> None:
    parser = argparse.ArgumentParser(description="逐题重评 + 面试报告重评（单脚本）")
    parser.add_argument("--name", "-n", help="候选人姓名（模糊匹配）")
    parser.add_argument("--invitation-id", "-i", help="邀请ID（推荐）")
    parser.add_argument("--session-id", "-s", help="会话ID（可选，不填则自动解析）")
    args = parser.parse_args()
    asyncio.run(_async_main(args.name, args.invitation_id, args.session_id))


if __name__ == "__main__":
    main()
