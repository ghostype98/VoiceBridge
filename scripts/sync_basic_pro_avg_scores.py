#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步/补全 interview_evaluation_record.evaluation_structured 中的 basic_avg_score / pro_avg_score

场景：
- 列表页 basic_avg_score / pro_avg_score 为空
- 详情页“基础题平均分/专业题平均分”展示为空

实现：
1) 读取 interview_evaluation_record 的 evaluation_structured（JSON）
2) 若 basic_avg_score 或 pro_avg_score 缺失/不是数字，则按：
   - invitation_questions 里的 question_type（BASIC / SPECIALTY）计入分母
   - candidate_answers 里 final_score 作为分子（未作答按 0）
   计算均分并写回 evaluation_structured
3) 不做互相回填：若某一类题目数量为 0，则该类均分保持为 null

用法：
  cd /opt/voicebridge
  python3 scripts/sync_basic_pro_avg_scores.py --only-empty
  python3 scripts/sync_basic_pro_avg_scores.py --only-type-missing
  python3 scripts/sync_basic_pro_avg_scores.py --invitation-id INV_xxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database.service import database_service


def _is_number(x: Any) -> bool:
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return not (isinstance(x, float) and math.isnan(x))
    return False


def _parse_json_maybe(v: Any) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            obj = json.loads(v)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _compute_basic_pro_from_db(
    invitation_id: str,
) -> Tuple[Optional[float], Optional[float], int, int]:
    """
    计算基础/专业题平均分（保留两位小数）。
    """
    # 取最近一条 session（与 evaluation 时通常一致；如多 session，可在外部指定 invitation）
    sess_row = database_service.db.execute_one(
        """
        SELECT session_id
        FROM interview_session
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """,
        (invitation_id,),
    )
    if not sess_row or not sess_row.get("session_id"):
        return None, None

    session_id = sess_row["session_id"]

    invitation_questions = database_service.get_invitation_questions(invitation_id) or []
    # candidate_answers 取主问题答案（排除追问）
    answers = database_service.get_session_candidate_answers(session_id) or []
    main_answers = [a for a in answers if not a.get("is_follow_up", False)]

    # 每个 question_id 取“最后一条”（与评估服务对齐的“最新回答”口径）
    last_by_qid: Dict[str, Dict[str, Any]] = {}
    for a in main_answers:
        qid = a.get("question_id")
        if qid:
            last_by_qid[str(qid)] = a

    basic_sum = 0.0
    basic_n = 0
    pro_sum = 0.0
    pro_n = 0

    for q in invitation_questions:
        qid = q.get("question_id")
        qtype = (q.get("question_type") or "BASIC").upper()
        is_pro = qtype == "SPECIALTY"

        if is_pro:
            pro_n += 1
        else:
            basic_n += 1

        ans = last_by_qid.get(str(qid)) if qid else None
        fs = ans.get("final_score") if ans else None
        val = float(fs) if isinstance(fs, (int, float)) else 0.0

        if is_pro:
            pro_sum += val
        else:
            basic_sum += val

    basic_avg = round(basic_sum / basic_n, 2) if basic_n else None
    pro_avg = round(pro_sum / pro_n, 2) if pro_n else None

    return basic_avg, pro_avg, basic_n, pro_n


async def _sync_one(
    invitation_id: str,
    only_empty: bool,
    only_type_missing: bool,
    dry_run: bool,
) -> bool:
    row = database_service.db.execute_one(
        """
        SELECT invitation_id, evaluation_structured
        FROM interview_evaluation_record
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """,
        (invitation_id,),
    )
    if not row:
        print(f"跳过：找不到评估记录 invitation_id={invitation_id}")
        return False

    structured = _parse_json_maybe(row.get("evaluation_structured"))
    if structured is None:
        structured = {}

    basic = structured.get("basic_avg_score")
    pro = structured.get("pro_avg_score")

    basic_ok = _is_number(basic)
    pro_ok = _is_number(pro)

    if only_empty and not only_type_missing and basic_ok and pro_ok:
        return False

    basic_avg, pro_avg, basic_n, pro_n = _compute_basic_pro_from_db(invitation_id)
    if basic_avg is None and pro_avg is None:
        print(f"跳过：无法计算 basic/pro 均分 invitation_id={invitation_id}")
        return False

    if only_type_missing:
        # 仅当某一类题目数量为 0 时才更新（用于纠正旧版本互相回填造成的“非空”）
        if (basic_n != 0) and (pro_n != 0):
            return False

    # 写回
    structured["basic_avg_score"] = basic_avg
    structured["pro_avg_score"] = pro_avg

    if dry_run:
        print(
            f"[DRY] invitation_id={invitation_id} basic_avg_score={basic_avg} pro_avg_score={pro_avg}"
        )
        return True

    ok = await database_service.update_interview_evaluation_record(
        invitation_id=invitation_id,
        evaluation_structured=structured,
    )
    if ok:
        print(
            f"✅ 同步成功 invitation_id={invitation_id} basic_avg_score={basic_avg} pro_avg_score={pro_avg}"
        )
        return True
    print(f"❌ 同步失败 invitation_id={invitation_id}")
    return False


async def main() -> None:
    p = argparse.ArgumentParser(description="同步 basic_avg_score / pro_avg_score")
    p.add_argument("--invitation-id", "-i", dest="invitation_id", help="指定 invitation_id")
    p.add_argument("--only-empty", action="store_true", help="只对 basic/pro 为空的记录计算并写回")
    p.add_argument(
        "--only-type-missing",
        action="store_true",
        help="仅当 basic_n==0 或 pro_n==0 时更新（把旧版本回填的列纠正为 null）",
    )
    p.add_argument("--dry-run", action="store_true", help="仅打印不写回")
    p.add_argument("--limit", type=int, default=200, help="最多处理多少条（默认200）")
    args = p.parse_args()

    invitation_ids: List[str] = []
    if args.invitation_id:
        invitation_ids = [args.invitation_id]
    else:
        rows = database_service.db.execute_query(
            """
            SELECT invitation_id, evaluation_structured
            FROM interview_evaluation_record
            ORDER BY create_time DESC
            LIMIT %s
            """,
            (args.limit,),
        )
        for r in rows or []:
            if r.get("invitation_id"):
                invitation_ids.append(r["invitation_id"])

    if not invitation_ids:
        print("未找到可处理的 invitation_id")
        return

    print(f"待处理 invitation 数量: {len(invitation_ids)}")
    updated = 0
    start = datetime.now()
    for idx, iid in enumerate(invitation_ids, 1):
        print(f"[{idx}/{len(invitation_ids)}] processing {iid} ...")
        changed = await _sync_one(
            invitation_id=iid,
            only_empty=args.only_empty,
            only_type_missing=args.only_type_missing,
            dry_run=args.dry_run,
        )
        if changed:
            updated += 1

    cost = (datetime.now() - start).total_seconds()
    print(f"完成：更新/写回 {updated} 条，耗时 {cost:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())

