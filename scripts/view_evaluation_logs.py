#!/usr/bin/env python3
"""
查看面试评估API的日志
用于调试和查看评估过程的详细信息
"""

import sys
import os
from datetime import datetime

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.service import database_service


def view_evaluation_record(invitation_id: str):
    """查看评估记录"""
    print("=" * 80)
    print(f"📋 查看评估记录: invitation_id={invitation_id}")
    print("=" * 80)
    
    # 查询评估记录
    query = """
        SELECT 
            evaluation_record_id,
            invitation_id,
            overall_score,
            dimension_scores,
            dimension_details,
            evaluation_summary,
            evaluation_suggestions,
            is_passed,
            evaluator_type,
            create_time,
            update_time
        FROM interview_evaluation_record
        WHERE invitation_id = %s
        ORDER BY create_time DESC
        LIMIT 1
    """
    
    record = database_service.db.execute_one(query, (invitation_id,))
    
    if not record:
        print(f"❌ 未找到评估记录: invitation_id={invitation_id}")
        return
    
    print(f"\n✅ 找到评估记录:")
    print(f"   记录ID: {record.get('evaluation_record_id')}")
    print(f"   邀请ID: {record.get('invitation_id')}")
    print(f"   总体得分: {record.get('overall_score')}")
    print(f"   是否通过: {'是' if record.get('is_passed') == 1 else '否'}")
    print(f"   评估类型: {record.get('evaluator_type')}")
    print(f"   创建时间: {record.get('create_time')}")
    print(f"   更新时间: {record.get('update_time')}")
    
    # 解析JSON字段
    import json
    
    dimension_scores = record.get('dimension_scores')
    if isinstance(dimension_scores, str):
        try:
            dimension_scores = json.loads(dimension_scores)
        except:
            dimension_scores = {}
    
    dimension_details = record.get('dimension_details')
    if isinstance(dimension_details, str):
        try:
            dimension_details = json.loads(dimension_details)
        except:
            dimension_details = {}
    
    print(f"\n📊 维度评分 ({len(dimension_scores) if dimension_scores else 0}个维度):")
    if dimension_scores:
        for dim_name, score in sorted(dimension_scores.items()):
            print(f"   {dim_name}: {score}分")
    else:
        print("   ⚠️  dimension_scores为空")
    
    print(f"\n📝 维度详情 ({len(dimension_details) if dimension_details else 0}个维度):")
    if dimension_details:
        for dim_name, detail in list(dimension_details.items())[:5]:  # 只显示前5个
            if isinstance(detail, dict):
                score = detail.get('score', '未知')
                importance = detail.get('importance_level', '未知')
                reasoning = detail.get('reasoning', '')[:50] + "..." if len(detail.get('reasoning', '')) > 50 else detail.get('reasoning', '')
                print(f"   {dim_name}: 得分={score}, 重要等级={importance}, 推理={reasoning}")
        if len(dimension_details) > 5:
            print(f"   ... 还有 {len(dimension_details) - 5} 个维度")
    else:
        print("   ⚠️  dimension_details为空")
    
    evaluation_summary = record.get('evaluation_summary', '')
    if evaluation_summary:
        print(f"\n📋 评估总结:")
        print(f"   {evaluation_summary}")
    else:
        print(f"\n⚠️  评估总结为空")
    
    evaluation_suggestions = record.get('evaluation_suggestions', '')
    if evaluation_suggestions:
        print(f"\n💡 评估建议:")
        print(f"   {evaluation_suggestions}")
    else:
        print(f"\n⚠️  评估建议为空")
    
    print("\n" + "=" * 80)


def view_recent_logs():
    """查看最近的日志文件"""
    print("=" * 80)
    print("📋 查看最近的日志文件")
    print("=" * 80)
    
    project_root = os.path.dirname(os.path.dirname(__file__))
    logs_dir = os.path.join(project_root, "logs", "app")
    if not os.path.exists(logs_dir):
        print(f"❌ 日志目录不存在: {logs_dir}")
        return

    # 查找最新的应用日志文件
    log_files = [f for f in os.listdir(logs_dir) if f.endswith('.log')]
    if not log_files:
        print(f"❌ 未找到日志文件")
        return
    
    log_files.sort(reverse=True)
    latest_log = os.path.join(logs_dir, log_files[0])
    
    print(f"\n📄 最新日志文件: {log_files[0]}")
    print(f"   路径: {latest_log}")
    
    # 读取最后100行
    try:
        with open(latest_log, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            print(f"\n📝 最后50行日志:")
            print("-" * 80)
            for line in lines[-50:]:
                print(line.rstrip())
    except Exception as e:
        print(f"❌ 读取日志文件失败: {e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='查看面试评估API的日志')
    parser.add_argument('--invitation-id', type=str, help='邀请ID，查看评估记录')
    parser.add_argument('--logs', action='store_true', help='查看最近的日志文件')
    
    args = parser.parse_args()
    
    if args.invitation_id:
        view_evaluation_record(args.invitation_id)
    elif args.logs:
        view_recent_logs()
    else:
        # 默认查看指定邀请ID的记录
        view_evaluation_record("INV_20260128105253_C4C97511")

