"""
用户认证路由API
处理用户登录等认证功能
与旧代码（sdhd voice_interview_streaming api_routes）对齐：支持 candidate_username 或 invitation_id 作为账号，
支持 CONFIRMED/IN_PROGRESS/进行中 状态登录，密码支持明文或 bcrypt 哈希。
"""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from loguru import logger

from app.database.service import database_service
from fastapi import Request

router = APIRouter(prefix="/api/v1/auth", tags=["用户认证"])

# 允许登录的邀请状态：只有CONFIRMED状态可以登录，登录后更新为IN_PROGRESS
ALLOWED_LOGIN_STATUSES = ("CONFIRMED",)


class LoginRequest(BaseModel):
    """登录请求模型"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class LoginResponse(BaseModel):
    """登录响应模型"""
    success: bool
    message: str
    invitation_data: Dict[str, Any] = None


class SessionCheckRequest(BaseModel):
    """会话检查请求模型"""
    invitation_id: str = Field(..., description="邀请ID")


class SessionCheckResponse(BaseModel):
    """会话检查响应模型"""
    valid: bool
    message: str
    user_data: Optional[Dict[str, Any]] = None

def _check_password(stored: Optional[str], plain: str) -> bool:
    """验证密码：支持明文一致或 bcrypt 哈希（与旧代码一致）"""
    if not stored:
        return False
    if stored.startswith("$2") or stored.startswith("$2a") or stored.startswith("$2b"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    return stored.strip() == plain.strip()


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(request: LoginRequest):
    """
    候选人登录

    - **username**: 候选人用户名 (candidate_username) 或邀请ID (invitation_id，如 INV_xxx)
    - **password**: 候选人密码 (candidate_password)，支持明文或 bcrypt 存储
    - 允许状态：只有CONFIRMED状态可以登录，登录成功后自动更新为IN_PROGRESS
    """
    try:
        logger.info(f"候选人登录请求: {request.username}")

        # 先按 candidate_username 查；若未找到且账号形如 INV_xxx，再按 invitation_id 查
        invitation = database_service.get_invitation_by_candidate_username(request.username)
        if not invitation and request.username.strip().upper().startswith("INV_"):
            invitation = database_service.get_invitation_by_id(request.username.strip())
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户不存在或邀请不存在"
            )

        # 验证密码（明文或 bcrypt）
        if not _check_password(invitation.get("candidate_password"), request.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="密码错误"
            )

        # 检查邀请状态：只有CONFIRMED状态可以登录
        status_val = (invitation.get("interview_status") or "").strip()
        if status_val not in ALLOWED_LOGIN_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"邀请状态不正确，无法登录（当前状态: {status_val}）。只有已确认(CONFIRMED)状态的邀请可以登录。"
            )

        # 登录成功后，更新邀请状态为IN_PROGRESS
        try:
            database_service.update_invitation_status(
                invitation_id=invitation["invitation_id"],
                status="IN_PROGRESS",
                start_time=datetime.now()
            )
            logger.info(f"登录成功，已更新邀请状态为IN_PROGRESS: invitation_id={invitation['invitation_id']}")
        except Exception as e:
            logger.error(f"更新邀请状态失败: {e}")
            # 状态更新失败不影响登录，但记录错误

        # 构建邀请数据
        invitation_data = {
            "invitation_id": invitation["invitation_id"],
            'evaluation_id': invitation.get("evaluation_id", ""),
            "candidate_name": invitation.get("candidate_name", ""),
            "requester": invitation.get("requester", ""),  # 求职公司
            "position": invitation.get("position", ""),     # 求职岗位
            "department": invitation.get("department", ""),
            "basic_info_duration": invitation.get("basic_info_duration"),
            "basic_info_focus": invitation.get("basic_info_focus"),
            "professional_duration": invitation.get("professional_duration"),
            "professional_focus": invitation.get("professional_focus"),
            "interview_status": invitation.get("interview_status"),
            "created_time": invitation.get("created_time"),
            "updated_time": invitation.get("updated_time")
        }

        logger.info(f"候选人登录成功 - 用户名: {request.username}, invitation_data: {invitation_data}")

        return LoginResponse(
            success=True,
            message="登录成功",
            invitation_data=invitation_data
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"候选人登录失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"登录失败: {str(e)}"
        )


@router.post("/check-session", response_model=SessionCheckResponse, status_code=status.HTTP_200_OK)
async def check_session(request: SessionCheckRequest):
    """
    检查用户会话是否有效

    - **invitation_id**: 邀请ID，用于验证会话
    """
    try:
        logger.info(f"检查会话: {request.invitation_id}")

        # 从数据库验证邀请ID是否存在且状态正常
        invitation = database_service.get_invitation_by_id(request.invitation_id)
        if not invitation:
            logger.warning(f"邀请ID不存在: {request.invitation_id}")
            return SessionCheckResponse(
                valid=False,
                message="邀请不存在或已过期"
            )

        # 检查邀请状态（CONFIRMED/IN_PROGRESS 均视为有效，允许继续面试）
        status_val = (invitation.get("interview_status") or "").strip()
        if status_val not in ("CONFIRMED", "IN_PROGRESS", "进行中"):
            logger.warning(f"邀请状态异常: {request.invitation_id}, 状态: {status_val}")
            return SessionCheckResponse(
                valid=False,
                message="面试状态异常，无法继续"
            )

        # 构建用户数据（不包含敏感信息）
        user_data = {
            "invitation_id": invitation["invitation_id"],
            "candidate_name": invitation.get("candidate_name", ""),
            "position": invitation.get("position", ""),
            "requester": invitation.get("requester", ""),
            "interview_status": invitation.get("interview_status")
        }

        logger.info(f"会话验证成功: {request.invitation_id}")
        return SessionCheckResponse(
            valid=True,
            message="会话有效",
            user_data=user_data
        )

    except Exception as e:
        logger.error(f"会话检查失败: {e}")
        return SessionCheckResponse(
            valid=False,
            message="会话验证失败"
        )

