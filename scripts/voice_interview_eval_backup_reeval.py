#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音面试评价：按候选人姓名备份当前 interview_evaluation_record、恢复备份、或重新走评估管线（新评分机制）。

用法示例：
  # 备份「候选人姓名」当前评价（默认选：有评估记录且面试已完成的邀请中最新一条）
  python3 scripts/voice_interview_eval_backup_reeval.py backup --name 张三

  # 从备份 JSON 恢复（覆盖该 invitation_id 对应评估行）
  python3 scripts/voice_interview_eval_backup_reeval.py restore --file backups/interview_evaluation/xxx.json

  # 重新评估（会先自动备份，再调用 interview_evaluation_service.evaluate_interview）
  python3 scripts/voice_interview_eval_backup_reeval.py reevaluate --name 张三

  # 指定 invitation_id / session_id（多人同名时用）
  python3 scripts/voice_interview_eval_backup_reeval.py backup --invitation-id INV_xxx
  python3 scripts/voice_interview_eval_backup_reeval.py reevaluate --invitation-id INV_xxx --session-id SES_xxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根目录
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database.service import database_service


DEFAULT_BACKUP_DIR = ROOT / "backups" / "interview_evaluation"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _serialize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (bytes, memoryview)):
            out[k] = v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v)
        else:
            out[k] = v
    return out


def find_invitations_by_name(name: str) -> List[Dict[str, Any]]:
    pattern = f"%{name.strip()}%"
    q = """
    SELECT invitation_id, candidate_name, interview_status, position, department, created_time
    FROM interview_invitation
    WHERE candidate_name ILIKE %s
    ORDER BY created_time DESC
    """
    return database_service.db.execute_query(q, (pattern,)) or []


def has_evaluation_record(invitation_id: str) -> bool:
    r = database_service.db.execute_one(
        "SELECT 1 AS x FROM interview_evaluation_record WHERE invitation_id = %s LIMIT 1",
        (invitation_id,),
    )
    return bool(r)


def pick_invitation_for_backup(invitations: List[Dict[str, Any]]) -> Optional[str]:
    """优先：有评估 + COMPLETED；否则有评估；否则第一条。"""
    if not invitations:
        return None
    scored = []
    for inv in invitations:
        iid = inv.get("invitation_id")
        if not iid:
            continue
        if has_evaluation_record(iid):
            scored.append(inv)
    pool = scored if scored else invitations
    completed = [x for x in pool if (x.get("interview_status") or "").upper() == "COMPLETED"]
    pick = (completed[0] if completed else pool[0])
    return pick.get("invitation_id")


def resolve_session_id_for_invitation(invitation_id: str) -> Optional[str]:
    """与 test_interview_evaluation_api 一致：按答案量最多的 session。"""
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
    # 兜底：邀请下任意 session
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


def fetch_evaluation_row(invitation_id: str) -> Optional[Dict[str, Any]]:
    return database_service.db.execute_one(
        """
        SELECT * FROM interview_evaluation_record
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """,
        (invitation_id,),
    )


def cmd_backup(name: Optional[str], invitation_id: Optional[str], out_dir: Path) -> Path:
    if invitation_id:
        iid = invitation_id
        inv = database_service.get_invitation_by_id(iid)
        if not inv:
            raise SystemExit(f"未找到邀请: {iid}")
        cname = inv.get("candidate_name", "")
    else:
        if not name:
            raise SystemExit("请提供 --name 或 --invitation-id")
        invitations = find_invitations_by_name(name)
        if not invitations:
            raise SystemExit(f"未找到候选人姓名匹配「{name}」的邀请")
        iid = pick_invitation_for_backup(invitations)
        if not iid:
            raise SystemExit("无法确定 invitation_id")
        inv = database_service.get_invitation_by_id(iid)
        cname = inv.get("candidate_name", name) if inv else name
        print("匹配到的邀请（按优先级已选一条）:")
        for row in invitations[:8]:
            mark = " ← 已选" if row.get("invitation_id") == iid else ""
            print(
                f"  {row.get('invitation_id')} | {row.get('candidate_name')} | "
                f"{row.get('interview_status')} | {row.get('position')}{mark}"
            )
        if len(invitations) > 8:
            print(f"  ... 共 {len(invitations)} 条，仅展示前 8 条")

    row = fetch_evaluation_row(iid)
    if not row:
        raise SystemExit(f"该邀请尚无 interview_evaluation_record，无可备份: invitation_id={iid}")

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (cname or "unknown"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"eval_backup_{safe_name}_{iid[:24]}_{ts}.json"

    payload = {
        "backup_meta": {
            "candidate_name": cname,
            "invitation_id": iid,
            "backed_up_at": datetime.now().isoformat(),
            "script": "voice_interview_eval_backup_reeval.py",
        },
        "evaluation_row": _serialize_row(dict(row)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已备份到: {path}")
    return path


def cmd_restore(backup_file: Path) -> None:
    if not backup_file.is_file():
        raise SystemExit(f"文件不存在: {backup_file}")
    payload = json.loads(backup_file.read_text(encoding="utf-8"))
    row = payload.get("evaluation_row")
    if not isinstance(row, dict):
        raise SystemExit("备份文件缺少 evaluation_row")
    invitation_id = row.get("invitation_id") or payload.get("backup_meta", {}).get("invitation_id")
    if not invitation_id:
        raise SystemExit("无法从备份中解析 invitation_id")

    # 仅更新备份里出现的列，避免 NOT NULL 未知列问题
    skip = {"backup_meta"}
    cols = [k for k in row.keys() if k and k not in skip]
    if not cols:
        raise SystemExit("evaluation_row 为空")

    set_parts = []
    values = []
    for c in cols:
        if c == "invitation_id":
            continue
        set_parts.append(f"{c} = %s")
        values.append(row[c])
    values.append(invitation_id)

    sql = f"""
    UPDATE interview_evaluation_record
    SET {", ".join(set_parts)}
    WHERE invitation_id = %s
    """
    n = database_service.db.execute_update(sql, tuple(values))
    if n <= 0:
        raise SystemExit(
            "UPDATE 影响 0 行：可能该邀请尚无评估记录。可先跑一次完整评估生成行，或手动 INSERT。"
        )
    print(f"✅ 已从备份恢复 invitation_id={invitation_id} ，更新字段数: {len(set_parts)}")


async def cmd_reevaluate(
    name: Optional[str],
    invitation_id: Optional[str],
    session_id: Optional[str],
    backup_first: bool,
    out_dir: Path,
) -> None:
    if invitation_id:
        iid = invitation_id
        inv = database_service.get_invitation_by_id(iid)
        if not inv:
            raise SystemExit(f"未找到邀请: {iid}")
        cname = inv.get("candidate_name", "")
    else:
        if not name:
            raise SystemExit("请提供 --name 或 --invitation-id")
        invitations = find_invitations_by_name(name)
        if not invitations:
            raise SystemExit(f"未找到候选人姓名匹配「{name}」的邀请")
        iid = pick_invitation_for_backup(invitations)
        if not iid:
            raise SystemExit("无法确定 invitation_id")
        inv = database_service.get_invitation_by_id(iid)
        cname = inv.get("candidate_name", name) if inv else name
        print("用于重评的邀请:")
        print(f"  invitation_id={iid} | {cname} | status={inv.get('interview_status') if inv else '?'}")

    sid = session_id or resolve_session_id_for_invitation(iid)
    if not sid:
        raise SystemExit("无法解析 session_id，请用 --session-id 显式传入")

    if backup_first and fetch_evaluation_row(iid):
        print("📦 重评前自动备份...")
        cmd_backup(None, iid, out_dir)
    elif backup_first:
        print("⚠️ 当前无评估记录，跳过备份")

    print(f"🚀 开始重新评估（新机制） session_id={sid} invitation_id={iid}")
    from agent.interview_evaluation_service import interview_evaluation_service

    result = await interview_evaluation_service.evaluate_interview(session_id=sid, invitation_id=iid)
    print("✅ 评估完成")
    print(f"   overall_score={result.get('overall_score')} is_passed={result.get('is_passed')}")
    if result.get("conclusion"):
        print(f"   conclusion: {result.get('conclusion')[:120]}...")


def main() -> None:
    p = argparse.ArgumentParser(description="语音面试评价备份 / 恢复 / 重新评估")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backup", help="备份当前 interview_evaluation_record")
    pb.add_argument("--name", "-n", help="候选人姓名（模糊匹配）")
    pb.add_argument("--invitation-id", "-i", help="直接指定邀请 ID")
    pb.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"备份目录，默认 {DEFAULT_BACKUP_DIR}",
    )

    pr = sub.add_parser("restore", help="从备份 JSON 恢复")
    pr.add_argument("--file", "-f", type=Path, required=True, help="备份文件路径")

    pe = sub.add_parser("reevaluate", help="重新执行评估（写入库）")
    pe.add_argument("--name", "-n", help="候选人姓名（模糊匹配）")
    pe.add_argument("--invitation-id", "-i", help="直接指定邀请 ID")
    pe.add_argument("--session-id", "-s", help="会话 ID（不填则自动解析）")
    pe.add_argument(
        "--no-backup-first",
        action="store_true",
        help="重评前不自动备份（默认会先备份）",
    )
    pe.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"自动备份目录，默认 {DEFAULT_BACKUP_DIR}",
    )

    args = p.parse_args()
    if args.cmd == "backup":
        cmd_backup(args.name, args.invitation_id, args.out_dir)
    elif args.cmd == "restore":
        cmd_restore(args.file)
    elif args.cmd == "reevaluate":
        asyncio.run(
            cmd_reevaluate(
                args.name,
                args.invitation_id,
                args.session_id,
                backup_first=not args.no_backup_first,
                out_dir=args.out_dir,
            )
        )


if __name__ == "__main__":
    main()
