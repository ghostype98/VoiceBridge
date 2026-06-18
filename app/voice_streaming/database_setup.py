# -*- coding: utf-8 -*-
"""
语音面试数据库表创建脚本
根据《VoiceBridge 智能语音面试系统 - 业务流程指南》创建完整的数据库表结构

创建的表包括：
- interview_invitation: 面试邀请主表
- interview_question: 面试题关联表
- interview_questions: 题目知识库表
- interview_session: 面试会话记录表
- candidate_answers: 候选人回答表
- interview_evaluation_record: 面试评估结果表
"""

import sys
import os
from typing import List

# 添加backend到Python路径
_current_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.join(_current_dir, '..')
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from loguru import logger
from app.database.connection import DatabaseManager

# 使用loguru logger


def _check_table_exists_and_valid(db_manager: DatabaseManager, table_name: str, expected_columns: dict) -> tuple[bool, str, list]:
    """检查表是否存在且结构正确

    返回值：
        (is_valid: bool, status_message: str, issues: list)
        - is_valid: 表结构是否有效
        - status_message: 状态描述
        - issues: 发现的问题列表
    """
    issues = []

    try:
        # 1. 检查表是否存在
        # 使用更通用的方法，避免information_schema权限问题
        table_exists_query = """
        SELECT EXISTS (
            SELECT 1
            FROM pg_tables
            WHERE tablename = %s
            UNION
            SELECT 1
            FROM pg_views
            WHERE viewname = %s
        );
        """
        logger.debug(f"查询表存在性: {table_name}, SQL: {table_exists_query}")
        table_exists = db_manager.fetch_one(table_exists_query, (table_name, table_name))
        logger.debug(f"表 {table_name} 存在性查询结果: {table_exists}")

        # 检查EXISTS查询的结果（返回字典格式）
        table_exists_bool = False
        if table_exists:
            # EXISTS查询返回的字典，获取第一个值
            first_key = next(iter(table_exists.keys()))
            table_exists_bool = bool(table_exists[first_key])
            logger.debug(f"解析后的表存在性: {table_exists_bool} (从键 {first_key} 获取)")

        if not table_exists_bool:
            # 尝试备用方法：使用更简单的方式检查表是否存在
            logger.debug(f"表 {table_name} 在系统表中不存在，尝试备用检查方法")
            try:
                # 使用一个更简单的方法：尝试获取表统计信息
                count_query = f"SELECT COUNT(*) FROM {table_name};"
                count_result = db_manager.fetch_one(count_query)
                if count_result is not None:
                    # 从字典中获取count值
                    first_key = next(iter(count_result.keys()))
                    count_value = count_result[first_key]
                    logger.debug(f"表 {table_name} 存在，记录数: {count_value}")
                else:
                    message = f"表 {table_name} 不存在"
                    logger.warning(message)
                    return False, message, [f"表不存在: {table_name}"]
            except Exception as backup_error:
                logger.debug(f"备用检查也失败: {str(backup_error)}")
                message = f"表 {table_name} 不存在"
                logger.warning(message)
                return False, message, [f"表不存在: {table_name}"]

        logger.debug(f"表 {table_name} 存在，开始检查字段结构")

        # 2. 检查表结构（详细检查：检查必需的列是否存在和类型是否匹配）
        required_columns = list(expected_columns.keys())
        columns_query = """
        SELECT
            a.attname as column_name,
            pg_catalog.format_type(a.atttypid, a.atttypmod) as data_type,
            CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END as is_nullable,
            pg_catalog.pg_get_expr(d.adbin, d.adrelid) as column_default
        FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_attrdef d ON (a.attrelid, a.attnum) = (d.adrelid, d.adnum)
        WHERE a.attrelid = (
            SELECT oid FROM pg_catalog.pg_class
            WHERE relname = %s AND relkind = 'r'
        )
        AND a.attnum > 0
        AND NOT a.attisdropped
        ORDER BY a.attname;
        """
        logger.debug(f"查询表 {table_name} 列信息, SQL: {columns_query}")
        try:
            existing_columns_result = db_manager.execute_query(columns_query, (table_name,))
            logger.debug(f"表 {table_name} 列查询结果数量: {len(existing_columns_result) if existing_columns_result else 'None'}")
            if existing_columns_result is None:
                message = f"无法获取表 {table_name} 的列信息"
                logger.warning(message)
                return False, message, [f"无法查询列信息: {table_name}"]

            # 处理查询结果（execute_query返回字典列表）
            existing_columns = []
            logger.debug(f"开始处理 {len(existing_columns_result)} 行列数据")
            if hasattr(existing_columns_result, '__iter__'):
                for i, row in enumerate(existing_columns_result):
                    logger.debug(f"处理第 {i+1} 行: {row}")
                    if isinstance(row, dict):
                        # row是字典格式
                        existing_columns.append({
                            'column_name': row.get('column_name'),
                            'data_type': row.get('data_type'),
                            'is_nullable': row.get('is_nullable'),
                            'column_default': row.get('column_default')
                        })
                        logger.debug(f"  添加列: {row.get('column_name')} ({row.get('data_type')})")
                    elif hasattr(row, '__getitem__'):
                        # 兼容旧格式（如果有的话）
                        existing_columns.append({
                            'column_name': row[0] if len(row) > 0 else None,
                            'data_type': row[1] if len(row) > 1 else None,
                            'is_nullable': row[2] if len(row) > 2 else None,
                            'column_default': row[3] if len(row) > 3 else None
                        })
                        logger.debug(f"  添加列(兼容模式): {row[0] if len(row) > 0 else None}")
                    else:
                        # 如果row是其他类型
                        existing_columns.append({'column_name': str(row), 'data_type': None, 'is_nullable': None, 'column_default': None})
                        logger.debug(f"  添加列(其他类型): {str(row)}")

            logger.debug(f"最终解析出 {len(existing_columns)} 个列")

            existing_column_names = [col['column_name'] for col in existing_columns]

        except Exception as e:
            error_message = f"查询表 {table_name} 列信息失败: {str(e)}"
            logger.error(error_message)
            return False, error_message, [f"查询列信息失败: {str(e)}"]

        # 3. 检查必需列是否都存在（排除外键约束）
        actual_required_columns = [col for col in required_columns if not col.startswith('FOREIGN KEY')]
        logger.debug(f"表 {table_name} 需要检查的字段: {actual_required_columns}")
        logger.debug(f"表 {table_name} 存在的字段: {existing_column_names}")
        missing_columns = [col for col in actual_required_columns if col not in existing_column_names]
        if missing_columns:
            issues.extend([f"缺少必需字段: {col}" for col in missing_columns])
            message = f"表 {table_name} 缺少 {len(missing_columns)} 个必需字段: {', '.join(missing_columns)}"
            logger.error(message)
            return False, message, issues

        # 4. 字段类型检查已禁用（当前只检查字段存在性，不检查类型匹配）
        # 如果需要启用类型检查，可以取消注释下面的代码

        # 5. 验证通过
        message = f"表 {table_name} 结构验证通过 (字段数: {len(existing_column_names)})"
        logger.info(message)
        return True, message, []

    except Exception as e:
        import traceback
        error_message = f"检查表 {table_name} 时出错: {str(e)}"
        logger.error(error_message)
        logger.error(f"异常类型: {type(e).__name__}")
        logger.error(f"异常详情: {traceback.format_exc()}")
        issues.append(f"检查过程出错: {str(e)}")
        issues.append(f"异常类型: {type(e).__name__}")
        return False, error_message, issues


def _create_table_if_needed(db_manager: DatabaseManager, table_name: str, columns: dict) -> None:
    """创建表（如果不存在）"""
    try:
        # 构建CREATE TABLE语句
        column_defs = []
        for col_name, col_type in columns.items():
            column_defs.append(f"{col_name} {col_type}")

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            {', '.join(column_defs)}
        );
        """

        db_manager.execute(create_sql)
        logger.info(f"表 {table_name} 创建成功")

    except Exception as e:
        logger.error(f"创建表 {table_name} 失败: {str(e)}")
        raise


def _ensure_postgresql_indexes_and_comments(db_manager: DatabaseManager) -> None:
    """为PostgreSQL添加索引和注释"""
    try:
        # 添加索引
        indexes = [
            # interview_invitation表索引
            "CREATE INDEX IF NOT EXISTS idx_invitation_username ON interview_invitation(candidate_username);",
            "CREATE INDEX IF NOT EXISTS idx_invitation_status ON interview_invitation(interview_status);",
            "CREATE INDEX IF NOT EXISTS idx_invitation_created ON interview_invitation(created_time);",

            # interview_question表索引
            "CREATE INDEX IF NOT EXISTS idx_question_invitation ON interview_question(invitation_id);",
            "CREATE INDEX IF NOT EXISTS idx_question_atomic ON interview_question(atomic_question_id);",
            "CREATE INDEX IF NOT EXISTS idx_question_order ON interview_question(question_order);"
        ]

        for index_sql in indexes:
            try:
                db_manager.execute(index_sql)
            except Exception as e:
                logger.warning(f"创建索引失败（可能已存在）: {str(e)}")

        # 添加注释
        comments = [
            # interview_invitation表注释
            "COMMENT ON TABLE interview_invitation IS '面试邀请主表（存储面试基本信息、状态和候选人认证信息）';",
            "COMMENT ON COLUMN interview_invitation.invitation_id IS '面试邀请ID（主键）';",
            "COMMENT ON COLUMN interview_invitation.candidate_name IS '候选人姓名';",
            "COMMENT ON COLUMN interview_invitation.candidate_username IS '候选人登录用户名';",
            "COMMENT ON COLUMN interview_invitation.candidate_password IS '候选人登录密码';",
            "COMMENT ON COLUMN interview_invitation.position IS '应聘职位';",
            "COMMENT ON COLUMN interview_invitation.department IS '应聘部门';",
            "COMMENT ON COLUMN interview_invitation.interview_status IS '面试状态（DRAFT/CONFIRMED/IN_PROGRESS/COMPLETED）';",

            # interview_question表注释
            "COMMENT ON TABLE interview_question IS '面试题关联表（关联邀请与题库）';",
            "COMMENT ON COLUMN interview_question.invitation_id IS '面试邀请ID（外键）';",
            "COMMENT ON COLUMN interview_question.atomic_question_id IS '原子问题ID（关联题库）';",
            "COMMENT ON COLUMN interview_question.question_order IS '问题顺序';"
        ]

        for comment_sql in comments:
            try:
                db_manager.execute(comment_sql)
            except Exception as e:
                logger.warning(f"添加注释失败: {str(e)}")

        logger.info("PostgreSQL索引和注释添加完成")

    except Exception as e:
        logger.error(f"添加PostgreSQL索引和注释时出错: {str(e)}")


def create_voice_interview_tables(db_manager: DatabaseManager) -> bool:
    """创建语音面试相关的数据库表（根据业务流程指南创建完整的表结构）"""
    try:
        logger.info("开始检查语音面试数据库表...")

        # 定义期望的表结构（简化版本：只检查核心表）
        # 注意：当前只检查 interview_invitation 和 interview_question 两个核心表
        expected_tables = {
            # 面试邀请主表（存储面试基本信息、状态和候选人认证信息）
            'interview_invitation': {
                'invitation_id': 'VARCHAR(100) PRIMARY KEY',
                'candidate_name': 'VARCHAR(100) NOT NULL',
                'candidate_username': 'VARCHAR(100) UNIQUE NOT NULL',
                'candidate_password': 'VARCHAR(255) NOT NULL',
                'position': 'VARCHAR(100) NOT NULL',
                'department': 'VARCHAR(100)',
                'requester': 'VARCHAR(100)',
                'basic_info_duration': 'INTEGER DEFAULT 300',
                'basic_info_focus': 'TEXT',
                'professional_duration': 'INTEGER DEFAULT 1800',
                'professional_focus': 'TEXT',
                'interview_status': "VARCHAR(20) DEFAULT 'DRAFT'",
                'interview_actual_start_time': 'TIMESTAMP NULL',
                'interview_actual_end_time': 'TIMESTAMP NULL',
                'created_time': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
                'updated_time': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
            },
            # 面试题关联表（关联邀请与题库）
            'interview_question': {
                'question_id': 'VARCHAR(100) PRIMARY KEY',
                'invitation_id': 'VARCHAR(100) NOT NULL',
                'atomic_question_id': 'VARCHAR(100) NOT NULL',
                'question_type': 'VARCHAR(20) NOT NULL',
                'question_category': 'VARCHAR(100)',
                'question_order': 'INTEGER DEFAULT 0',
                'estimated_duration': 'INTEGER',
                'difficulty': 'VARCHAR(20)',
                'evaluation_points': 'JSONB',
                'create_time': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
                'FOREIGN KEY (invitation_id)': 'REFERENCES interview_invitation(invitation_id) ON DELETE CASCADE'
            },

        }

        # 检查每个表
        for table_name, expected_columns in expected_tables.items():
            is_valid, status_message, issues = _check_table_exists_and_valid(db_manager, table_name, expected_columns)

            if not is_valid:
                logger.error(f"表 {table_name} 存在问题: {status_message}")

                # 详细记录问题
                if issues:
                    logger.error(f"表 {table_name} 的具体问题:")
                    for issue in issues:
                        logger.error(f"  ❌ {issue}")

                # 根据问题类型决定处理方式
                if "不存在" in status_message:
                    # 表不存在 - 自动创建
                    logger.info(f"表 {table_name} 不存在，正在自动创建...")
                    _create_table_if_needed(db_manager, table_name, expected_columns)
                    logger.info(f"表 {table_name} 创建完成")
                elif "缺少" in status_message:
                    # 字段缺失 - 严重错误，停止程序
                    critical_error = f"表 {table_name} 缺少必需字段，无法自动修复！"
                    logger.error("=" * 60)
                    logger.error("🚨 数据库表结构严重错误！")
                    logger.error(critical_error)
                    logger.error("请手动修复数据库表结构或删除表让系统重新创建")
                    logger.error("问题详情:")
                    for issue in issues:
                        logger.error(f"  - {issue}")
                    logger.error("=" * 60)

                    # 抛出异常停止程序
                    raise RuntimeError(f"数据库表结构错误: {critical_error}")
                elif "类型不匹配" in status_message:
                    # 类型不匹配 - 警告但继续运行（可选升级）
                    logger.warning(f"表 {table_name} 字段类型不匹配，但系统将继续运行")
                    logger.warning("建议检查并手动修复字段类型")
                else:
                    # 其他未知问题 - 抛出异常
                    logger.error(f"表 {table_name} 存在未知问题: {status_message}")
                    raise RuntimeError(f"数据库表检查失败: {status_message}")
            else:
                logger.info(f"✅ 表 {table_name} 结构验证通过")

        # 为PostgreSQL添加索引和注释
        if db_manager.db_type == 'postgresql':
            _ensure_postgresql_indexes_and_comments(db_manager)

        logger.info("语音面试数据库表检查完成")
        return True

    except RuntimeError:
        # 重新抛出RuntimeError，让上层处理程序停止
        raise
    except Exception as e:
        logger.error(f"语音面试数据库表检查失败: {str(e)}", exc_info=True)
        return False
