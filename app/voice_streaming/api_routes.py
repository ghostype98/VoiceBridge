# -*- coding: utf-8 -*-
"""
语音面试流式转写API路由
提供语音面试会话管理、统计信息等功能
"""

import sys
import os
import bcrypt
import random
import string
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from loguru import logger
from config.settings import settings
from app.database.connection import DatabaseManager
# LLM服务已在dependencies中处理

from .websocket_server import VoiceInterviewWebSocketServer
from .service_launcher import ConfigWrapper

# 使用loguru logger

router = APIRouter(prefix="/api/v1/voice-interview", tags=["voice_interview_streaming"])

# 全局服务实例
_websocket_server: Optional[VoiceInterviewWebSocketServer] = None


# ==================== 数据模型 ====================

class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    invitation_id: str
    question_id: str


class CreateSessionResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    websocket_url: str


class SessionInfo(BaseModel):
    """会话信息"""
    session_id: str
    question_id: str
    start_time: str
    status: str  # active, completed, error


class EvaluationInfo(BaseModel):
    """评价信息"""
    id: str
    session_id: str
    question_id: str
    answer_text: str
    score: float
    reason: str
    evaluation_details: Dict[str, Any]
    need_follow_up: bool
    follow_up_question: Optional[str]
    created_at: str


class SessionStatusResponse(BaseModel):
    """会话状态响应"""
    session: SessionInfo
    evaluations: List[EvaluationInfo]


class StatsResponse(BaseModel):
    """统计信息响应"""
    total_sessions: int
    active_sessions: int
    total_evaluations: int
    avg_score: float
    follow_up_count: int


class WebSocketConfigResponse(BaseModel):
    """WebSocket配置响应"""
    websocket_url: str
    ping_interval: int
    max_reconnect_attempts: int


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str  # healthy, unhealthy
    services: Dict[str, bool]
    timestamp: str


# ==================== 路由处理 ====================

@router.post("/session", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest):
    """创建语音面试会话"""
    try:
        # 生成会话ID
        session_id = f"VOICE_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex().upper()}"

        # 验证邀请ID和题目ID是否存在
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 验证邀请
        invitation_sql = "SELECT id FROM interview_invitation WHERE id = %s"
        if db_manager.db_type != 'postgresql':
            invitation_sql = invitation_sql.replace('%s', '?')
        invitation = db_manager.fetch_one(invitation_sql, (request.invitation_id,))
        if not invitation:
            raise HTTPException(status_code=404, detail="面试邀请不存在")

        # 验证题目
        question_sql = "SELECT question_id FROM interview_question WHERE question_id = %s"
        if db_manager.db_type != 'postgresql':
            question_sql = question_sql.replace('%s', '?')
        question = db_manager.fetch_one(question_sql, (request.question_id,))
        if not question:
            raise HTTPException(status_code=404, detail="面试题目不存在")

        # 创建会话记录
        insert_sql = """
        INSERT INTO interview_session (
            session_id, invitation_id, session_status, created_at
        ) VALUES (%s, %s, %s, %s)
        """
        if db_manager.db_type != 'postgresql':
            insert_sql = insert_sql.replace('%s', '?')

        db_manager.execute(insert_sql, (
            session_id,
            request.invitation_id,
            'CREATED',
            datetime.now()
        ))

        # 获取WebSocket配置
        config_manager = get_config_manager()
        voice_config = config_manager.get_config('voice_interview_streaming')
        websocket_config = voice_config['websocket']

        websocket_url = f"ws://{websocket_config['host']}:{websocket_config['port']}"

        logger.info(f"创建语音面试会话: {session_id}")

        return CreateSessionResponse(
            session_id=session_id,
            websocket_url=websocket_url
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"创建会话失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"创建会话失败: {str(e)}")


@router.get("/session/{session_id}", response_model=SessionStatusResponse)
async def get_session_status(session_id: str):
    """获取会话状态"""
    try:
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 获取会话信息
        session_sql = """
        SELECT session_id, invitation_id, created_at, session_status
        FROM interview_session
        WHERE session_id = %s
        """
        if db_manager.db_type != 'postgresql':
            session_sql = session_sql.replace('%s', '?')

        session_data = db_manager.fetch_one(session_sql, (session_id,))
        if not session_data:
            raise HTTPException(status_code=404, detail="会话不存在")

        session = SessionInfo(
            session_id=session_data['session_id'],
            question_id=session_data['invitation_id'],  # 临时使用invitation_id作为question_id
            start_time=session_data['created_at'].isoformat() if hasattr(session_data['created_at'], 'isoformat') else str(session_data['created_at']),
            status=session_data['session_status']
        )

        # 获取评价历史
        evaluations_sql = """
        SELECT id, session_id, question_id, answer_text, evaluation_score as score,
               evaluation_result->>'reason' as reason, evaluation_result as evaluation_details,
               (evaluation_result->>'need_follow_up')::boolean as need_follow_up,
               evaluation_result->>'follow_up_question' as follow_up_question, create_time as created_at
        FROM candidate_answers
        WHERE session_id = %s
        ORDER BY create_time DESC
        """
        if db_manager.db_type != 'postgresql':
            evaluations_sql = evaluations_sql.replace('%s', '?')

        evaluations_data = db_manager.execute_query(evaluations_sql, (session_id,))

        evaluations = []
        for eval_data in evaluations_data:
            evaluations.append(EvaluationInfo(
                id=eval_data['id'],
                session_id=eval_data['session_id'],
                question_id=eval_data['question_id'],
                answer_text=eval_data['answer_text'],
                score=float(eval_data['score']),
                reason=eval_data['reason'],
                evaluation_details=eval_data['evaluation_details'] if eval_data['evaluation_details'] else {},
                need_follow_up=bool(eval_data['need_follow_up']),
                follow_up_question=eval_data['follow_up_question'],
                created_at=eval_data['created_at'].isoformat() if hasattr(eval_data['created_at'], 'isoformat') else str(eval_data['created_at'])
            ))

        return SessionStatusResponse(
            session=session,
            evaluations=evaluations
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取会话状态失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取会话状态失败: {str(e)}")


@router.post("/session/{session_id}/end")
async def end_session(session_id: str):
    """结束会话"""
    try:
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 更新会话状态
        update_sql = """
        UPDATE interview_session
        SET session_status = 'COMPLETED', updated_at = %s
        WHERE session_id = %s AND session_status = 'CREATED'
        """
        if db_manager.db_type != 'postgresql':
            update_sql = update_sql.replace('%s', '?')

        affected_rows = db_manager.execute(update_sql, (datetime.now(), session_id))

        if affected_rows == 0:
            raise HTTPException(status_code=404, detail="会话不存在或已结束")

        logger.info(f"结束语音面试会话: {session_id}")

        return {"message": "会话已结束", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"结束会话失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"结束会话失败: {str(e)}")


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """获取统计信息"""
    try:
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 获取会话统计
        session_stats_sql = """
        SELECT
            COUNT(*) as total_sessions,
            COUNT(CASE WHEN session_status = 'CREATED' THEN 1 END) as active_sessions
        FROM interview_session
        WHERE DATE(created_at) = CURRENT_DATE
        """

        if db_manager.db_type != 'postgresql':
            session_stats_sql = session_stats_sql.replace("CURRENT_DATE", "DATE('now')")

        session_stats = db_manager.fetch_one(session_stats_sql)

        # 获取评价统计
        eval_stats_sql = """
        SELECT
            COUNT(*) as total_evaluations,
            AVG(evaluation_score) as avg_score,
            SUM(CASE WHEN (evaluation_result->>'need_follow_up')::boolean THEN 1 ELSE 0 END) as follow_up_count
        FROM candidate_answers
        WHERE DATE(create_time) = CURRENT_DATE
        """

        if db_manager.db_type != 'postgresql':
            eval_stats_sql = eval_stats_sql.replace("CURRENT_DATE", "DATE('now')")

        eval_stats = db_manager.fetch_one(eval_stats_sql)

        return StatsResponse(
            total_sessions=session_stats['total_sessions'] or 0,
            active_sessions=session_stats['active_sessions'] or 0,
            total_evaluations=eval_stats['total_evaluations'] or 0,
            avg_score=round(float(eval_stats['avg_score'] or 0), 2),
            follow_up_count=eval_stats['follow_up_count'] or 0
        )

    except Exception as e:
        logger.error(f"获取统计信息失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")


@router.get("/config/websocket", response_model=WebSocketConfigResponse)
async def get_websocket_config():
    """获取WebSocket配置"""
    try:
        config_manager = get_config_manager()
        voice_config = config_manager.get_config('voice_interview_streaming')
        websocket_config = voice_config['websocket']

        websocket_url = f"ws://{websocket_config['host']}:{websocket_config['port']}"

        return WebSocketConfigResponse(
            websocket_url=websocket_url,
            ping_interval=websocket_config['ping_interval'],
            max_reconnect_attempts=5  # 默认最大重连次数
        )

    except Exception as e:
        logger.error(f"获取WebSocket配置失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取WebSocket配置失败: {str(e)}")


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    try:
        services_status = {
            'websocket': False,
            'asr': False,
            'llm': False,
            'database': False
        }

        # 检查WebSocket服务器
        global _websocket_server
        if _websocket_server and _websocket_server.is_running:
            services_status['websocket'] = True

        # 检查数据库连接
        db_manager = get_db_manager()
        if db_manager:
            try:
                # 执行简单的查询来测试连接
                test_sql = "SELECT 1"
                db_manager.fetch_one(test_sql)
                services_status['database'] = True
            except:
                pass

        # 检查LLM服务
        try:
            llm_wrapper = get_llm()
            if llm_wrapper:
                # 简单的LLM测试（可选，避免过度调用）
                services_status['llm'] = True
        except:
            pass

        # 检查ASR服务（通过WebSocket服务器状态间接判断）
        if services_status['websocket']:
            services_status['asr'] = True

        # 整体状态
        overall_status = "healthy" if all(services_status.values()) else "unhealthy"

        return HealthResponse(
            status=overall_status,
            services=services_status,
            timestamp=datetime.now().isoformat()
        )

    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}", exc_info=True)
        return HealthResponse(
            status="unhealthy",
            services={
                'websocket': False,
                'asr': False,
                'llm': False,
                'database': False
            },
            timestamp=datetime.now().isoformat()
        )


@router.post("/auth/candidate/login")
async def candidate_login(request: Request):
    """候选人登录验证"""
    try:
        data = await request.json()
        username = data.get('username')
        password = data.get('password')

        logger.info(f"候选人登录请求: username={username}, password_length={len(password) if password else 0}")

        if not username or not password:
            logger.warning("用户名或密码为空")
            raise HTTPException(status_code=400, detail="用户名和密码不能为空")

        db_manager = get_db_manager()
        if not db_manager:
            logger.error("数据库连接失败")
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 验证账号密码
        sql = """
        SELECT invitation_id, candidate_name, position, department,
               interview_status, interview_form, candidate_password
        FROM interview_invitation
        WHERE candidate_username = %s AND interview_status = 'CONFIRMED'
        """

        if db_manager.db_type != 'postgresql':
            sql = sql.replace('%s', '?')

        invitation_data = db_manager.fetch_one(sql, (username,))
        if not invitation_data:
            logger.warning(f"用户不存在或面试未确认: {username}")
            raise HTTPException(status_code=401, detail="用户名不存在或面试未确认")

        logger.info(f"找到用户记录: invitation_id={invitation_data.get('invitation_id')}")

        # 验证密码
        stored_password_hash = invitation_data.get('candidate_password')
        logger.info(f"存储的密码哈希: {stored_password_hash}")

        if not stored_password_hash:
            logger.error("密码哈希不存在")
            raise HTTPException(status_code=500, detail="系统错误")

        try:
            password_valid = bcrypt.checkpw(password.encode('utf-8'), stored_password_hash.encode('utf-8'))
            logger.info(f"密码验证结果: {password_valid}")

            if not password_valid:
                logger.warning("密码验证失败")
                raise HTTPException(status_code=401, detail="密码错误")
        except Exception as e:
            logger.error(f"密码验证异常: {str(e)}")
            raise HTTPException(status_code=500, detail="密码验证失败")

        logger.info(f"候选人登录成功: {username}")

        return {
            'success': True,
            'invitation_id': invitation_data['invitation_id'],
            'candidate_name': invitation_data['candidate_name'],
            'position': invitation_data['position'],
            'department': invitation_data['department'],
            'interview_form': invitation_data['interview_form']
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"候选人登录失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"登录失败: {str(e)}")


@router.get("/interview/{invitation_id}/questions")
async def get_interview_questions(invitation_id: str):
    """获取面试题目列表"""
    try:
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 验证邀请ID
        inv_sql = "SELECT invitation_id FROM interview_invitation WHERE invitation_id = %s AND interview_status = 'CONFIRMED'"
        if db_manager.db_type != 'postgresql':
            inv_sql = inv_sql.replace('%s', '?')

        invitation = db_manager.fetch_one(inv_sql, (invitation_id,))
        if not invitation:
            raise HTTPException(status_code=404, detail="面试邀请不存在或未确认")

        # 获取面试题目：先基础题（BASIC_INFO），再专业题（PROFESSIONAL），都按question_order排序
        # 注意：每个invitation_id对应标准的5个基础题+10个专业题，共15道题
        # 时长从 interview_questions.estimated_duration 读取（单位：秒）
        questions_sql = """
        SELECT
            iq.question_id,
            iq.question_text,
            iq.question_type,
            iq.question_category,
            iq.question_order,
            COALESCE(iqs.estimated_duration, iq.estimated_duration) as estimated_duration,
            iq.difficulty,
            COALESCE(iss.session_status, 'NOT_STARTED') as session_status,
            COALESCE(iss.candidate_answer, '') as candidate_answer,
            COALESCE(iss.follow_up_used, 0) as follow_up_used,
            COALESCE(iss.follow_up_limit, 2) as follow_up_limit
        FROM interview_question iq
        LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
        LEFT JOIN interview_session iss ON iq.invitation_id = iss.invitation_id
            AND iq.question_id = iss.question_id
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
            iq.question_id ASC  -- 当question_order相同时，按ID排序确保稳定顺序
        """

        if db_manager.db_type != 'postgresql':
            questions_sql = questions_sql.replace('%s', '?')

        questions = db_manager.execute_query(questions_sql, (invitation_id,))

        # 格式化返回数据
        formatted_questions = []
        for q in questions:
            formatted_questions.append({
                'question_id': q['question_id'],
                'question_text': q['question_text'],
                'question_type': q['question_type'],
                'question_category': q['question_category'],
                'question_order': q['question_order'],
                'estimated_duration': q['estimated_duration'],
                'difficulty': q['difficulty'],
                'session_status': q['session_status'],
                'has_answer': bool(q['candidate_answer']),
                'follow_up_used': q['follow_up_used'],
                'follow_up_limit': q['follow_up_limit']
            })

        return {
            'success': True,
            'invitation_id': invitation_id,
            'questions': formatted_questions,
            'total_questions': len(formatted_questions)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取面试题目失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取面试题目失败: {str(e)}")


@router.get("/interview/{invitation_id}/question/{question_id}")
async def get_question_detail(invitation_id: str, question_id: str):
    """获取题目详情和会话信息"""
    try:
        db_manager = get_db_manager()
        if not db_manager:
            raise HTTPException(status_code=500, detail="数据库连接失败")

        # 获取题目信息，时长从 interview_questions.estimated_duration 读取
        question_sql = """
        SELECT
            iq.question_id,
            iq.question_text,
            iq.question_type,
            iq.question_category,
            iq.question_order,
            COALESCE(iqs.estimated_duration, iq.estimated_duration) as estimated_duration,
            iq.difficulty,
            iq.evaluation_points
        FROM interview_question iq
        LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
        WHERE iq.invitation_id = %s AND iq.question_id = %s
        """

        if db_manager.db_type != 'postgresql':
            question_sql = question_sql.replace('%s', '?')

        question = db_manager.fetch_one(question_sql, (invitation_id, question_id))
        if not question:
            raise HTTPException(status_code=404, detail="题目不存在")

        # 获取会话信息
        session_sql = """
        SELECT
            session_id,
            candidate_answer,
            session_content,
            session_status,
            start_time,
            end_time,
            audio_duration,
            follow_up_used,
            follow_up_limit
        FROM interview_session
        WHERE invitation_id = %s AND question_id = %s
        """

        if db_manager.db_type != 'postgresql':
            session_sql = session_sql.replace('%s', '?')

        session = db_manager.fetch_one(session_sql, (invitation_id, question_id))

        return {
            'success': True,
            'question': {
                'question_id': question['question_id'],
                'question_text': question['question_text'],
                'question_type': question['question_type'],
                'question_category': question['question_category'],
                'question_order': question['question_order'],
                'estimated_duration': question['estimated_duration'],
                'difficulty': question['difficulty'],
                'evaluation_points': question['evaluation_points']
            },
            'session': session or None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取题目详情失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取题目详情失败: {str(e)}")


    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}", exc_info=True)
        return HealthResponse(
            status="unhealthy",
            services={
                'websocket': False,
                'asr': False,
                'llm': False,
                'database': False
            },
            timestamp=datetime.now().isoformat()
        )


# ==================== 服务管理函数 ====================

def get_config_manager():
    """获取配置管理器"""
    # 使用config.settings
    from config.settings import settings
    return ConfigWrapper(settings)


def get_db_manager() -> Optional[DatabaseManager]:
    """获取数据库管理器"""
    # 直接创建数据库管理器实例
    from app.database.connection import DatabaseManager
    return DatabaseManager()


def get_llm():
    """获取LLM服务"""
    from app.services.llm_service import LLMService
    return LLMService()


def init_websocket_server(config_manager, db_manager: DatabaseManager) -> VoiceInterviewWebSocketServer:
    """初始化WebSocket服务器"""
    global _websocket_server

    if _websocket_server is None:
        _websocket_server = VoiceInterviewWebSocketServer(config_manager, db_manager)

    return _websocket_server


def get_websocket_server() -> Optional[VoiceInterviewWebSocketServer]:
    """获取WebSocket服务器实例"""
    return _websocket_server


async def start_websocket_server():
    """启动WebSocket服务器"""
    global _websocket_server

    if _websocket_server:
        await _websocket_server.start_server()
        logger.info("语音面试WebSocket服务器已启动")


async def stop_websocket_server():
    """停止WebSocket服务器"""
    global _websocket_server

    if _websocket_server:
        await _websocket_server.stop_server()
        logger.info("语音面试WebSocket服务器已停止")