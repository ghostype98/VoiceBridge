#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用当前 RealtimeScorer（含提示词与分数校准）对某场语音面试的主问题逐题重新打分并写回 candidate_answers。

不修改追问文案与追问答案字段：rescore_mode 下不生成追问、不追加 interview_session 内容。

示例：
  python3 scripts/rescore_session_realtime.py --name 张三
  python3 scripts/rescore_session_realtime.py --invitation-id INV_xxx --session-id UUID
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
    yaml = None  # noqa: N816

from app.database.connection import DatabaseManager  # noqa: E402
from app.database.service import database_service  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from app.voice_streaming.realtime_scorer import RealtimeScorer  # noqa: E402
from config.settings import settings  # noqa: E402


def _find_invitations_by_name(name: str) -> List[Dict[str, Any]]:
    pattern = f"%{name.strip()}%"
    q = """
    SELECT invitation_id, candidate_name, interview_status, created_time
    FROM interview_invitation
    WHERE candidate_name ILIKE %s
    ORDER BY created_time DESC
    """
    return database_service.db.execute_query(q, (pattern,)) or []


def _pick_invitation(invitations: List[Dict[str, Any]]) -> Optional[str]:
    if not invitations:
        return None
    completed = [x for x in invitations if (x.get("interview_status") or "").upper() == "COMPLETED"]
    pool = completed if completed else invitations
    return pool[0].get("invitation_id")


def _resolve_session_id(invitation_id: str) -> Optional[str]:
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
        SELECT session_id FROM interview_session
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """,
        (invitation_id,),
    )
    return r2.get("session_id") if r2 else None


def _build_evaluation_config() -> Dict[str, Any]:
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


async def _run_rescore(session_id: str) -> None:
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

    print(f"共 {len(mains)} 道主问题，开始重评（rescore_mode）…")
    for row in mains:
        qid = row.get("question_id")
        old = row.get("final_score")
        text = (row.get("answer_text") or "").strip()
        print(f"\n→ {qid} 旧分={old} 字数={len(text)}")
        out = await scorer.evaluate_answer(
            session_id,
            qid,
            row.get("answer_text") or "",
            rescore_mode=True,
        )
        print(f"   新分={out.get('score')} follow_up={out.get('need_follow_up')}")


async def _async_main(name: Optional[str], invitation_id: Optional[str], session_id: Optional[str]) -> None:
    iid = invitation_id
    if not iid:
        if not name:
            raise SystemExit("请指定 --name 或 --invitation-id")
        invs = _find_invitations_by_name(name)
        if not invs:
            raise SystemExit(f"未找到姓名匹配「{name}」的邀请")
        iid = _pick_invitation(invs)
        print("使用邀请:")
        for r in invs[:6]:
            mark = " ←" if r.get("invitation_id") == iid else ""
            print(f"  {r.get('invitation_id')} | {r.get('candidate_name')} | {r.get('interview_status')}{mark}")
        if len(invs) > 6:
            print(f"  … 共 {len(invs)} 条")

    sid = session_id or _resolve_session_id(iid)
    if not sid:
        raise SystemExit("无法解析 session_id，请用 --session-id 传入")
    print(f"session_id={sid} invitation_id={iid}")
    await _run_rescore(sid)


def main() -> None:
    p = argparse.ArgumentParser(description="RealtimeScorer 批量重评并写库")
    p.add_argument("--name", "-n", help="候选人姓名（模糊匹配）")
    p.add_argument("--invitation-id", "-i", help="邀请 ID")
    p.add_argument("--session-id", "-s", help="会话 ID（可选，默认按邀请解析）")
    args = p.parse_args()
    asyncio.run(_async_main(args.name, args.invitation_id, args.session_id))


if __name__ == "__main__":
    main()
