#!/usr/bin/env python3
"""
按 invitation_id 导出面试题目明细：
- 评估人姓名（候选人姓名）
- 题目
- 题目类型（基础 / 专业）
- 题目答案
- 评估要点
- 题目顺序
"""

import sys
import os
import argparse
import json
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    print("错误: 缺少 pandas 库，请运行: pip install pandas openpyxl")
    sys.exit(1)

try:
    import openpyxl  # noqa: F401
except ImportError:
    print("错误: 缺少 openpyxl 库，请运行: pip install openpyxl")
    sys.exit(1)

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import get_db_manager  # type: ignore
from loguru import logger  # type: ignore


def _format_question_type(q_type: str) -> str:
    """将数据库中的题目类型转换为中文描述"""
    if not q_type:
        return ""
    q_type = q_type.upper()
    if q_type in ("BASIC", "BASIC_INFO"):
        return "基础题"
    if q_type in ("SPECIALTY", "PROFESSIONAL"):
        return "专业题"
    return q_type


def _format_evaluation_points(evaluation_points):
    """将评估要点字段统一格式化为字符串，方便在 Excel 中查看"""
    if evaluation_points is None:
        return ""

    # 已经是列表/字典
    if isinstance(evaluation_points, (list, dict)):
        try:
            return json.dumps(evaluation_points, ensure_ascii=False, indent=2)
        except Exception:
            return str(evaluation_points)

    # 是字符串，尝试解析为 JSON
    if isinstance(evaluation_points, str):
        text = evaluation_points.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            # 不是合法 JSON，就按原样返回
            return evaluation_points

    # 其它类型（int/float等），直接转字符串
    return str(evaluation_points)


def export_invitation_questions(invitation_id: str, output_file: str = None) -> bool:
    """
    导出指定 invitation_id 的题目明细
    """
    db_manager = get_db_manager()

    if not db_manager:
        logger.error("数据库连接失败")
        return False

    try:
        # 1. 查询 invitation 基本信息（候选人姓名）
        logger.info(f"正在查询邀请: {invitation_id}")
        invitation_sql = """
            SELECT invitation_id, candidate_name, position, department, requester
            FROM interview_invitation
            WHERE invitation_id = %s
            LIMIT 1
        """
        invitation = db_manager.execute_one(invitation_sql, (invitation_id,))
        if not invitation:
            logger.warning(f"未找到 invitation_id 为 '{invitation_id}' 的记录")
            return False

        candidate_name = invitation.get("candidate_name", "")

        # 2. 查询该邀请下所有“真题”信息：
        #    - 题目内容
        #    - 题目类型
        #    - 题目顺序（interview_question.question_order）
        #    - 参考答案、评估要点（均从 interview_questions 中读取）
        logger.info("正在查询题目信息（含参考答案与评估要点）")
        detail_sql = """
            SELECT
                iq.question_id,
                iq.question_order,
                iq.question_type,
                COALESCE(iqs.content, iq.question_text, '题目内容暂无') AS question_text,
                iqs.standard_answer,
                iqs.evaluation_points
            FROM interview_question iq
            LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iq.invitation_id = %s
            ORDER BY
                CASE iq.question_type
                    WHEN 'BASIC' THEN 1
                    WHEN 'BASIC_INFO' THEN 1
                    WHEN 'SPECIALTY' THEN 2
                    WHEN 'PROFESSIONAL' THEN 2
                    ELSE 3
                END,
                iq.question_order ASC,
                iq.question_id ASC
        """

        rows = db_manager.execute_query(detail_sql, (invitation_id,))
        if not rows:
            logger.warning(f"邀请 {invitation_id} 未找到题目数据")
            return False

        # 3. 先构造成列表，后面在 DataFrame 里做“基础题 1-N / 专业题 1-M”的重新编号
        data = []
        for r in rows:
            row = dict(r)
            q_type_raw = (row.get("question_type") or "").upper()
            eval_raw = row.get("evaluation_points")

            data.append(
                {
                    "invitation_id": invitation_id,
                    "candidate_name": candidate_name,
                    "position": invitation.get("position", ""),
                    "department": invitation.get("department", ""),
                    "company": invitation.get("requester", ""),
                    "db_question_type": q_type_raw,
                    "question_order_db": row.get("question_order"),
                    "question_type_name": _format_question_type(q_type_raw),
                    "question_text": row.get("question_text", ""),
                    # 这里用真题的参考答案，而不是候选人作答
                    "standard_answer": row.get("standard_answer") or "",
                    # 评估要点同样从 interview_questions 表读取
                    "evaluation_points_str": _format_evaluation_points(eval_raw),
                }
            )

        logger.info(f"共找到 {len(data)} 条题目记录")

        # 3. 转 DataFrame，并生成“基础题 1-N / 专业题 1-M”的题目顺序
        df = pd.DataFrame(data)

        # 保证排序：先基础题，再专业题，每类内部按 question_order_db 升序
        def _type_sort_key(t: str) -> int:
            if t in ("BASIC", "BASIC_INFO"):
                return 1
            if t in ("SPECIALTY", "PROFESSIONAL"):
                return 2
            return 3

        df["__type_order"] = df["db_question_type"].apply(_type_sort_key)
        df = df.sort_values(by=["__type_order", "question_order_db", "question_text"])

        # 在各自类型内重新编号：基础题 1-N / 专业题 1-M
        df["question_order_in_type"] = 0
        for t_group, idx in (
            (("BASIC", "BASIC_INFO"), (df["db_question_type"].isin(["BASIC", "BASIC_INFO"]))),
            (("SPECIALTY", "PROFESSIONAL"), (df["db_question_type"].isin(["SPECIALTY", "PROFESSIONAL"]))),
        ):
            mask = idx
            if mask.any():
                df.loc[mask, "question_order_in_type"] = range(1, mask.sum() + 1)

        column_mapping = {
            "invitation_id": "邀请ID",
            "candidate_name": "评估人姓名",
            "company": "公司",
            "department": "部门",
            "position": "岗位",
            # 题目顺序：按类型内重新编号（基础题 1-N，专业题 1-M）
            "question_order_in_type": "题目顺序",
            "question_text": "题目",
            "question_type_name": "题目类型",
            # 使用真题参考答案，而不是候选人答案
            "standard_answer": "题目参考答案",
            "evaluation_points_str": "评估要点",
        }

        selected_cols = [c for c in column_mapping.keys() if c in df.columns]
        df_final = df[selected_cols].copy()
        df_final.rename(columns=column_mapping, inplace=True)

        # 4. 生成输出文件名
        if not output_file:
            safe_id = invitation_id.replace(" ", "_").replace("/", "_").replace("\\", "_")
            today = datetime.now().strftime("%Y%m%d")
            output_file = f"{safe_id}-题目明细-{today}.xlsx"

        logger.info(f"正在导出数据到: {output_file}")
        df_final.to_excel(output_file, index=False, engine="openpyxl")
        logger.info(f"✅ 导出成功，记录数: {len(df_final)}")
        logger.info(f"文件保存位置: {os.path.abspath(output_file)}")
        return True

    except Exception as e:
        logger.error(f"导出 invitation 题目明细时出错: {e}", exc_info=True)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="按 invitation_id 导出面试题目明细到 Excel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python export_invitation_questions.py INV_20260209135052_B
  python export_invitation_questions.py INV_20260209135052_B -o output.xlsx
        """,
    )

    parser.add_argument(
        "invitation_id",
        type=str,
        help="面试邀请 ID（invitation_id）",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="输出 Excel 文件路径（可选）",
    )

    args = parser.parse_args()
    success = export_invitation_questions(args.invitation_id, args.output)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()


