"""
面试会话管理路由API
处理面试会话的创建、管理、状态控制等功能
"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from loguru import logger

from app.services.interview_session_service import interview_session_service
from app.database.service import database_service


# Pydantic模型定义
class CreateInterviewSessionRequest(BaseModel):
    """创建面试会话请求模型"""
    invitation_id: str = Field(..., description="面试邀请ID（必填）")


class InterviewSessionResponse(BaseModel):
    """面试会话响应模型"""
    session_id: str
    status: str
    position: str
    total_questions: int
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    current_question_index: int = 0


class QuestionResponse(BaseModel):
    """问题响应模型"""
    question_id: str
    type: str
    text: str
    order: int
    created_at: str


class AddAnswerRequest(BaseModel):
    """添加回答请求模型"""
    question_id: str
    answer_text: str
    audio_duration: Optional[float] = Field(default=None, description="音频时长（秒）")


class StartInterviewRequest(BaseModel):
    """开始面试请求模型"""
    invitation_id: str = Field(..., description="面试邀请ID")


class ConversationHistoryItem(BaseModel):
    """对话历史项模型"""
    order: int
    question: QuestionResponse
    answer: Optional[dict] = None


# 创建路由器
router = APIRouter(prefix="/api/v1/interview-sessions", tags=["面试会话管理"])


@router.post("/", response_model=InterviewSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_interview_session(
    request: CreateInterviewSessionRequest,
):
    """
    创建新的面试会话

    - **position**: 面试职位（可选）

    返回创建的会话信息，包含会话ID和初始状态
    """
    try:
        logger.info(f"创建面试会话请求: 邀请ID {request.invitation_id}")

        # 使用邀请ID创建会话，服务会验证邀请并获取相关信息
        session_data = interview_session_service.create_interview_session(
            invitation_id=request.invitation_id
        )

        return InterviewSessionResponse(
            session_id=session_data["session_id"],
            status=session_data["status"],
            position=session_data["position"],
            total_questions=session_data["total_questions"],
            created_at=session_data["created_at"]
        )

    except Exception as e:
        logger.error(f"创建面试会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建面试会话失败: {str(e)}"
        )


@router.post("/start-interview", response_model=dict)
async def start_interview(
    request: StartInterviewRequest,
):
    """
    开始面试 - 检查状态并创建/恢复会话

    - **invitation_id**: 面试邀请ID

    检查interview_status是否为"进行中"，检查interview_session表记录，
    如果不存在则创建新会话，返回第一个问题
    """
    try:
        logger.info(f"开始面试请求: 邀请ID {request.invitation_id}")

        # 1. 检查邀请状态
        invitation = database_service.get_invitation_by_id(request.invitation_id)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试邀请不存在"
            )

        status_val = (invitation.get("interview_status") or "").strip()
        if status_val not in ("CONFIRMED", "IN_PROGRESS", "进行中"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"邀请状态不正确，无法开始面试。当前状态: {status_val}"
            )

        # 2. 检查interview_session表中是否存在记录
        existing_sessions = interview_session_service._check_existing_sessions(request.invitation_id)

        if existing_sessions:
            # 如果存在记录，使用第一个会话的session_id
            session_id = existing_sessions[0]["session_id"]
            logger.info(f"找到现有会话: {session_id}")

            # 恢复现有会话到内存
            restored_session = interview_session_service.restore_session_from_database(session_id, request.invitation_id)
            if not restored_session:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="恢复会话失败，会话数据不存在"
                )

            # 对于恢复的会话，直接设置第一个问题，不改变状态
            interview_session_service.get_next_question(session_id)
        else:
            # 如果不存在记录，创建新会话
            logger.info("未找到现有会话，创建新会话")
            session_data = interview_session_service.create_interview_session(
                invitation_id=request.invitation_id
            )
            session_id = session_data["session_id"]

            # 开始新创建的会话
            if not interview_session_service.start_session(session_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="开始新会话失败"
                )

        # 获取当前问题
        current_question = interview_session_service.get_current_question(session_id)

        logger.info(f"面试开始成功: 会话ID {session_id}")

        return {
            "message": "面试已开始",
            "session_id": session_id,
            "invitation_id": request.invitation_id,
            "current_question": current_question,
            "total_questions": interview_session_service.get_session(session_id)["total_questions"]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"开始面试失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"开始面试失败: {str(e)}"
        )


@router.get("/{session_id}", response_model=InterviewSessionResponse)
async def get_interview_session(
    session_id: str,
):
    """
    获取面试会话信息
    
    - **session_id**: 会话ID
    
    返回指定会话的详细信息
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        # 检查用户权限
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此面试会话"
            )
        
        return InterviewSessionResponse(
            session_id=session["session_id"],
            status=session["status"],
            position=session.get("position", ""),
            total_questions=len(session.get("questions", [])),
            created_at=session["created_at"],
            started_at=session.get("started_at"),
            completed_at=session.get("completed_at"),
            current_question_index=session.get("current_question_index", 0)
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取面试会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取面试会话失败: {str(e)}"
        )


# 已取消：获取用户面试邀请列表的接口
# @router.get("/", response_model=List[InterviewSessionResponse])
# async def get_user_sessions(
#     status: Optional[str] = Query(default=None, description="筛选状态"),
# ):
#     """
#     获取当前用户的所有面试会话
#
#     - **status**: 可选的状态筛选 (pending/active/paused/completed/cancelled/expired)
#
#     返回用户的面试会话列表，按创建时间倒序排列
#     """
#     try:
#
#         sessions = interview_session_service.get_user_sessions(
#             status=status
#         )
#
#         return [
#             InterviewSessionResponse(
#                 session_id=session["session_id"],
#                 status=session["status"],
#                 position=session.get("position", ""),
#                 total_questions=len(session.get("questions", [])),
#                 created_at=session["created_at"],
#                 started_at=session.get("started_at"),
#                 completed_at=session.get("completed_at"),
#                 current_question_index=session.get("current_question_index", 0)
#             )
#             for session in sessions
#         ]
#
#     except Exception as e:
#         logger.error(f"获取用户会话列表失败: {e}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail=f"获取会话列表失败: {str(e)}"
#         )


@router.post("/{session_id}/start", response_model=dict)
async def start_interview_session(
    session_id: str,
):
    """
    开始面试会话
    
    - **session_id**: 会话ID
    
    将会话状态从pending改为active
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权操作此面试会话"
            )
        
        if not interview_session_service.start_session(session_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确，无法开始"
            )
        
        logger.info(f"开始面试会话: {session_id}")
        
        return {
            "message": "面试会话已开始",
            "session_id": session_id,
            "status": "active",
            "current_question": interview_session_service.get_current_question(session_id)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"开始面试会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"开始面试会话失败: {str(e)}"
        )


@router.get("/{session_id}/current-question", response_model=QuestionResponse)
async def get_current_question(
    session_id: str,
    invitation_id: Optional[str] = Query(None, description="邀请ID，传入时按邀请维度的会话推算当前题（避免 path 的 session_id 与 DB 不一致导致 404）"),
):
    """
    获取当前问题（无状态：完全从 DB 推导，不依赖内存 active_sessions）

    - **session_id**: 会话ID（path，若未传 invitation_id 则用此查 interview_session）
    - **invitation_id**: 可选；传入时以 invitation 为准查「当前会话」并算当前题，推荐前端在能带上的时候都传
    """
    try:
        # 1. 确定 invitation_id 与用于计数的 session_id（以 invitation 为准时，避免 path 的 session_id 与 DB 不一致）
        invitation_id_resolved: Optional[str] = None
        answer_session_id: Optional[str] = None

        if invitation_id:
            # 以邀请为准：取该邀请下「当前会话」（与语音流写库逻辑一致：ORDER BY create_time DESC LIMIT 1）
            canonical = database_service.get_session_invitation_latest_for_invitation(invitation_id)
            invitation_id_resolved = invitation_id
            if canonical:
                # 用 canonical 的 session_id 数答案；空或缺失时用 path 的 session_id
                answer_session_id = canonical.get("session_id") or session_id
            else:
                # 邀请下暂无会话记录，用 path 的 session_id 数答案（可能为 0）
                answer_session_id = session_id
        else:
            # 未传 invitation_id：仅靠 path 的 session_id 查表
            row = database_service.get_session_invitation(session_id)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="面试会话不存在"
                )
            invitation_id_resolved = row.get("invitation_id")
            answer_session_id = session_id
        if not invitation_id_resolved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )

        # 2. 已答题目数（主答案，不含追问）——按实际写答案的 session 计
        answers = database_service.get_session_candidate_answers(answer_session_id)
        current_index = sum(1 for a in (answers or []) if not a.get("is_follow_up", False))

        # 3. 题目列表
        questions = database_service.get_invitation_questions(invitation_id_resolved)
        if not questions:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有找到面试题目"
            )

        # 4. 当前题 = questions[current_index]，无则面试已完成
        if current_index >= len(questions):
            try:
                interview_session_service.complete_session(answer_session_id or session_id)
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试已完成"
            )

        q = questions[current_index]
        question = {
            "question_id": q["question_id"],
            "question_text": q.get("question_text", ""),
            "text": q.get("question_text", ""),
            "question_order": q.get("question_order", 0),
            "order": q.get("question_order", 0),
            "question_type": q.get("question_type", "UNKNOWN"),
        }
        logger.info(f"[current-question 无状态] session_id={session_id}, invitation_id={invitation_id_resolved}, answer_session_id={answer_session_id}, index={current_index}, question_id={q['question_id']}")

        return QuestionResponse(
            question_id=question["question_id"],
            type=question.get("question_type", question.get("type", "UNKNOWN")),
            text=question.get("question_text", question.get("text", "")),
            order=question.get("question_order", question.get("order", 0)),
            created_at=datetime.utcnow().isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取当前问题失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取当前问题失败: {str(e)}"
        )


@router.post("/{session_id}/answers", response_model=dict)
async def add_answer(
    session_id: str,
    request: AddAnswerRequest,
):
    """
    添加用户回答
    
    - **session_id**: 会话ID
    - **question_id**: 问题ID
    - **answer_text**: 回答文本
    - **audio_duration**: 音频时长（秒）
    
    将用户回答保存到会话中
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权操作此面试会话"
            )
        
        if session["status"] != "IN_PROGRESS":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确"
            )
        
        if not interview_session_service.add_answer(
            session_id=session_id,
            question_id=request.question_id,
            answer_text=request.answer_text,
            audio_duration=request.audio_duration
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="添加回答失败"
            )
        
        # 获取下一题
        next_question = interview_session_service.get_current_question(session_id)
        is_completed = session["current_question_index"] + 1 >= len(session["questions"])
        
        logger.info(f"添加回答成功: 会话 {session_id}, 问题 {request.question_id}")
        
        return {
            "message": "回答已保存",
            "session_id": session_id,
            "next_question": next_question,
            "is_completed": is_completed,
            "current_question_index": session["current_question_index"] + 1
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加回答失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"添加回答失败: {str(e)}"
        )


@router.post("/{session_id}/complete", response_model=dict)
async def complete_interview_session(
    session_id: str,
):
    """
    完成面试会话
    
    - **session_id**: 会话ID
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权操作此面试会话"
            )
        
        if not interview_session_service.complete_session(session_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确，无法完成"
            )
        
        conversation_history = interview_session_service.get_conversation_history(session_id)
        
        logger.info(f"完成面试会话: {session_id}")
        
        return {
            "message": "面试会话已完成",
            "session_id": session_id,
            "status": "completed",
            "total_questions": len(conversation_history),
            "conversation_history": conversation_history
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"完成面试会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"完成面试会话失败: {str(e)}"
        )


@router.get("/{session_id}/history", response_model=List[ConversationHistoryItem])
async def get_conversation_history(
    session_id: str,
):
    """
    获取对话历史
    
    - **session_id**: 会话ID
    
    返回完整的问答历史记录
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此面试会话"
            )
        
        history = interview_session_service.get_conversation_history(session_id)
        
        return [
            ConversationHistoryItem(
                order=item["order"],
                question=item["question"],
                answer=item["answer"]
            )
            for item in history
        ]
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取对话历史失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取对话历史失败: {str(e)}"
        )


@router.get("/{invitation_id}/basic-questions", response_model=List[str])
async def get_basic_questions(
    invitation_id: str,
):
    """
    获取面试邀请的基本信息题目question_id列表

    - **invitation_id**: 面试邀请ID

    返回基本信息题目的question_id列表，按顺序排列
    """
    try:
        logger.info(f"获取基本题目列表: 邀请ID {invitation_id}")

        # 验证邀请ID存在
        invitation = database_service.get_invitation_by_id(invitation_id)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试邀请不存在"
            )

        basic_questions = database_service.get_invitation_basic_questions(invitation_id)

        logger.info(f"获取基本题目列表成功: {len(basic_questions)} 个题目")
        return basic_questions

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取基本题目列表失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取基本题目列表失败: {str(e)}"
        )


@router.get("/{invitation_id}/professional-questions", response_model=List[str])
async def get_professional_questions(
    invitation_id: str,
):
    """
    获取面试邀请的专业能力题目question_id列表

    - **invitation_id**: 面试邀请ID

    返回专业能力题目的question_id列表，按顺序排列
    """
    try:
        logger.info(f"获取专业题目列表: 邀请ID {invitation_id}")

        # 验证邀请ID存在
        invitation = database_service.get_invitation_by_id(invitation_id)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试邀请不存在"
            )

        professional_questions = database_service.get_invitation_professional_questions(invitation_id)

        logger.info(f"获取专业题目列表成功: {len(professional_questions)} 个题目")
        return professional_questions

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取专业题目列表失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取专业题目列表失败: {str(e)}"
        )


@router.delete("/{session_id}", response_model=dict)
async def cancel_interview_session(
    session_id: str,
):
    """
    取消面试会话
    
    - **session_id**: 会话ID
    """
    try:
        session = interview_session_service.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权操作此面试会话"
            )
        
        if not interview_session_service.cancel_session(session_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确，无法取消"
            )
        
        logger.info(f"取消面试会话: {session_id}")
        
        return {
            "message": "面试会话已取消",
            "session_id": session_id,
            "status": "cancelled"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"取消面试会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"取消面试会话失败: {str(e)}"
        )

