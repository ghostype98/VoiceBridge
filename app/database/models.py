"""
数据库模型定义（SQL表结构）
"""
from datetime import datetime
from enum import Enum


class InterviewStatus(str, Enum):
    """面试状态枚举"""
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class QuestionType(str, Enum):
    """问题类型枚举"""
    INTRODUCTION = "introduction"
    PROJECT = "project"
    STAR_SITUATION = "star_situation"
    STAR_TASK = "star_task"
    STAR_ACTION = "star_action"
    STAR_RESULT = "star_result"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    GENERAL = "general"


# SQL表结构定义
CREATE_TABLES_SQL = """
-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    email VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- 面试题表
CREATE TABLE IF NOT EXISTS interview_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_text TEXT NOT NULL,
    question_type VARCHAR(50) NOT NULL,
    position VARCHAR(100),
    order_index INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_questions_type ON interview_questions(question_type);
CREATE INDEX IF NOT EXISTS idx_questions_position ON interview_questions(position);

-- interview_session: 面试会话记录表
CREATE TABLE IF NOT EXISTS interview_session (
    session_id VARCHAR(36) PRIMARY KEY,
    invitation_id VARCHAR(50) NOT NULL,
    question_id VARCHAR(50),
    question_text TEXT,
    candidate_answer TEXT,
    session_content TEXT,
    session_status VARCHAR(20) DEFAULT 'IN_PROGRESS',
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    audio_duration FLOAT,
    question_order INTEGER,
    follow_up_used INTEGER DEFAULT 0,
    follow_up_limit INTEGER DEFAULT 3,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_session_invitation_id ON interview_session(invitation_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON interview_session(session_status);
CREATE INDEX IF NOT EXISTS idx_session_question_order ON interview_session(question_id, question_order);

-- 新表结构已存在，不需要在此定义
-- interview_invitation: 面试邀请主表
-- interview_question: 面试题附表
-- interview_session: 面试会话记录表
-- interview_questions: 题目知识库表
-- interview_evaluation_record: 评估结果表
-- candidate_answers: 候选人答案表
-- question_org_map: 组织层级映射表

-- 创建更新时间触发器函数
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- 为需要的表创建更新时间触发器（如果不存在）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_users_updated_at') THEN
        CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_questions_updated_at') THEN
        CREATE TRIGGER update_questions_updated_at BEFORE UPDATE ON interview_questions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;
"""

