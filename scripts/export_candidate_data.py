#!/usr/bin/env python3
"""
导出候选人答案数据脚本
根据候选人姓名导出 candidate_answers 表的所有数据及 interview_session 表的 audio_duration 字段
关联 interview_question 表获取题目信息
关联 interview_evaluation_record 表获取评估得分和评估详情
"""

import sys
import os
import argparse
from datetime import datetime

# 检查依赖
try:
    import pandas as pd
except ImportError:
    print("错误: 缺少 pandas 库，请运行: pip install pandas openpyxl")
    sys.exit(1)

try:
    import openpyxl
except ImportError:
    print("错误: 缺少 openpyxl 库，请运行: pip install openpyxl")
    sys.exit(1)

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import get_db_manager
from loguru import logger
import json


def export_candidate_data(candidate_name: str, output_file: str = None):
    """
    导出指定候选人的所有答案数据
    
    Args:
        candidate_name: 候选人姓名
        output_file: 输出文件路径（可选，默认使用姓名）
    """
    db_manager = get_db_manager()
    
    if not db_manager:
        logger.error("数据库连接失败")
        return False
    
    try:
        # 1. 根据姓名查询 invitation_id 和公司信息
        logger.info(f"正在查询候选人: {candidate_name}")
        invitation_query = """
            SELECT invitation_id, candidate_name, position, department, requester
            FROM interview_invitation
            WHERE candidate_name = %s
            ORDER BY created_time DESC
        """
        
        invitations = db_manager.execute_query(invitation_query, (candidate_name,))
        
        if not invitations:
            logger.warning(f"未找到姓名为 '{candidate_name}' 的候选人")
            return False
        
        logger.info(f"找到 {len(invitations)} 个邀请记录")
        
        # 2. 查询所有相关的 candidate_answers 数据，并关联题目、会话和评估记录信息
        all_data = []
        
        for inv in invitations:
            invitation_id = inv['invitation_id']
            logger.info(f"处理邀请: {invitation_id}")
            
            # 获取评估记录信息
            evaluation_query = """
                SELECT 
                    overall_score,
                    question_score,
                    dimension_details,
                    evaluation_summary,
                    evaluation_suggestions
                FROM interview_evaluation_record
                WHERE invitation_id = %s
                LIMIT 1
            """
            evaluation_record = db_manager.execute_one(evaluation_query, (invitation_id,))
            
            # 先获取该邀请下的所有 session_id
            session_query = """
                SELECT DISTINCT session_id, audio_duration
                FROM interview_session
                WHERE invitation_id = %s
            """
            sessions = db_manager.execute_query(session_query, (invitation_id,))
            session_duration_map = {s['session_id']: s.get('audio_duration') for s in sessions}
            
            if not session_duration_map:
                logger.warning(f"邀请 {invitation_id} 下没有找到 session 记录")
                continue
            
            # 基于 interview_question 表获取所有题目，左连接 candidate_answers 获取答案
            # 这样即使没有答案的题目也会被导出
            session_ids = list(session_duration_map.keys())
            placeholders = ','.join(['%s'] * len(session_ids))
            
            # 使用 DISTINCT ON 获取每个题目的最新答案（PostgreSQL特有）
            # 如果没有答案，题目仍然会被导出
            query = f"""
                SELECT DISTINCT ON (iq.question_id)
                    iq.question_id,
                    COALESCE(iqs.content, '') as question_text,
                    iq.question_order,
                    iq.question_type,
                    ca.id as answer_id,
                    ca.session_id,
                    ca.answer_text,
                    ca.is_follow_up,
                    ca.parent_answer_id,
                    ca.status,
                    ca.final_score,
                    ca.comprehensive_score,
                    ca.point_evaluations,
                    ca.evaluation_result,
                    ca.need_follow_up,
                    ca.follow_up_question,
                    ca.follow_up_evaluation_points,
                    ca.follow_up_answer_text,
                    ca.follow_up_evaluation,
                    COALESCE(ca.create_time, iq.create_time) as create_time,
                    ca.update_time
                FROM interview_question iq
                LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
                LEFT JOIN candidate_answers ca ON iq.question_id = ca.question_id
                    AND ca.session_id IN ({placeholders})
                    AND (ca.is_follow_up = FALSE OR ca.is_follow_up IS NULL)
                WHERE iq.invitation_id = %s
                ORDER BY iq.question_id, iq.question_order ASC, ca.create_time DESC NULLS LAST
            """
            
            # 构建参数：先放 session_ids，再放 invitation_id
            params = list(session_ids) + [invitation_id]
            results = db_manager.execute_query(query, tuple(params))
            
            # 合并数据
            for row in results:
                row_dict = dict(row)
                question_id = row_dict.get('question_id')
                session_id = row_dict.get('session_id')
                
                # 如果该题目没有答案，仍然需要创建记录
                if not session_id:
                    # 没有答案的题目，使用第一个 session_id 来获取 audio_duration
                    session_id = session_ids[0] if session_ids else None
                
                # 添加 audio_duration（题目用时，单位：分钟）
                duration = session_duration_map.get(session_id) if session_id else None
                if duration is not None:
                    row_dict['question_duration'] = round(float(duration), 2)
                else:
                    row_dict['question_duration'] = None
                
                # 添加评估记录信息
                if evaluation_record:
                    row_dict['interview_score'] = evaluation_record.get('overall_score')
                    row_dict['question_avg_score'] = evaluation_record.get('question_score')
                    row_dict['dimension_details'] = evaluation_record.get('dimension_details')
                    row_dict['evaluation_summary'] = evaluation_record.get('evaluation_summary')
                    row_dict['evaluation_suggestions'] = evaluation_record.get('evaluation_suggestions')
                else:
                    row_dict['interview_score'] = None
                    row_dict['question_avg_score'] = None
                    row_dict['dimension_details'] = None
                    row_dict['evaluation_summary'] = None
                    row_dict['evaluation_suggestions'] = None
                
                # 添加邀请信息
                row_dict['invitation_id'] = invitation_id
                row_dict['candidate_name'] = inv['candidate_name']
                row_dict['position'] = inv.get('position', '')
                row_dict['department'] = inv.get('department', '')
                row_dict['company'] = inv.get('requester', '')
                
                # 题目得分（当前答案的得分，如果没有答案则为空）
                row_dict['question_score'] = row_dict.get('comprehensive_score') or row_dict.get('final_score')
                
                # 如果没有答案，确保 answer_text 为空字符串而不是 None
                if not row_dict.get('answer_text'):
                    row_dict['answer_text'] = ''
                
                all_data.append(row_dict)
        
        if not all_data:
            logger.warning(f"候选人 '{candidate_name}' 没有答案数据")
            return False
        
        logger.info(f"共找到 {len(all_data)} 条答案记录")
        
        # 3. 处理 JSON 字段，转换为字符串以便在 Excel 中查看
        json_fields = ['point_evaluations', 'evaluation_result', 'follow_up_evaluation_points', 
                      'follow_up_evaluation', 'dimension_details']
        for row in all_data:
            for field in json_fields:
                if field in row and row[field] is not None:
                    if isinstance(row[field], (dict, list)):
                        row[field] = json.dumps(row[field], ensure_ascii=False, indent=2)
                    elif isinstance(row[field], str):
                        # 如果已经是字符串，尝试解析后再格式化（如果是有效的 JSON）
                        try:
                            parsed = json.loads(row[field])
                            row[field] = json.dumps(parsed, ensure_ascii=False, indent=2)
                        except (json.JSONDecodeError, TypeError):
                            pass  # 保持原样
        
        # 4. 转换为 DataFrame
        df = pd.DataFrame(all_data)
        
        # 5. 选择需要的列并重命名为中文
        column_mapping = {
            'candidate_name': '姓名',
            'company': '公司',
            'department': '部门',
            'position': '岗位',
            'interview_score': '面试得分',
            'question_avg_score': '题目平均分',
            'question_duration': '题目用时',
            'question_text': '题目',
            'answer_text': '题目回答',
            'question_score': '题目得分',
            'evaluation_result': '评估结果',
            'dimension_details': '维度详情',
            'evaluation_summary': '评估总结',
            'evaluation_suggestions': '评估建议',
            'create_time': '时间'
        }
        
        # 选择需要的列
        selected_columns = []
        for col in column_mapping.keys():
            if col in df.columns:
                selected_columns.append(col)
        
        # 选择列
        df_final = df[selected_columns].copy()
        
        # 重命名列
        df_final.rename(columns=column_mapping, inplace=True)
        
        # 6. 生成输出文件名（使用姓名-面试详情）
        if not output_file:
            safe_name = candidate_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
            output_file = f"{safe_name}-面试详情.xlsx"
        
        # 7. 导出到 Excel
        logger.info(f"正在导出数据到: {output_file}")
        df_final.to_excel(output_file, index=False, engine='openpyxl')
        
        logger.info(f"✅ 导出成功！共 {len(df_final)} 条记录")
        logger.info(f"文件保存位置: {os.path.abspath(output_file)}")
        
        return True
        
    except Exception as e:
        logger.error(f"导出数据时发生错误: {str(e)}", exc_info=True)
        return False


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='导出候选人答案数据到Excel',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python export_candidate_data.py 张三
  python export_candidate_data.py "商洁璇" -o output.xlsx
        """
    )
    
    parser.add_argument(
        'name',
        type=str,
        help='候选人姓名'
    )
    
    parser.add_argument(
        '-o', '--output',
        type=str,
        default=None,
        help='输出Excel文件路径（可选，默认使用姓名）'
    )
    
    args = parser.parse_args()
    
    # 执行导出
    success = export_candidate_data(args.name, args.output)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
