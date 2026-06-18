"""
面试会话管理服务
结合内存管理和数据库同步：
- 邀请验证和数据获取
- 内存中的会话实例管理
- 数据库状态同步
- 问题加载和会话恢复
- 对话上下文维护
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import uuid
from loguru import logger

from app.database.service import database_service


class InterviewSessionService:
    """面试会话服务类 - 内存会话管理"""

    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}  # 活跃会话存储
        self.session_timeout_minutes = 30  # 会话超时时间30分钟
        self.default_question_count = 10  # 默认问题数量

        # 面试状态机定义
        self.state_transitions = {
            "CREATED": ["STARTED"],
            "STARTED": ["IN_PROGRESS", "PAUSED", "COMPLETED", "CANCELLED"],
            "IN_PROGRESS": ["PAUSED", "COMPLETED", "CANCELLED"],
            "PAUSED": ["IN_PROGRESS", "COMPLETED", "CANCELLED"],
            "COMPLETED": ["CLOSED"],
            "CANCELLED": ["CLOSED"]
        }
    
    def create_interview_session(
        self,
        invitation_id: str
    ) -> Dict[str, Any]:
        """
        验证邀请ID并创建面试会话实例

        Args:
            invitation_id: 面试邀请ID（必填）

        Returns:
            创建的会话信息

        Raises:
            Exception: 邀请ID不存在或状态无效
        """
        # 验证邀请ID存在且状态正确
        invitation = self._validate_invitation(invitation_id)
        if not invitation:
            raise Exception(f"邀请ID不存在或状态无效: {invitation_id}")

        # 检查interview_session表中是否已存在记录
        existing_sessions = self._check_existing_sessions(invitation_id)
        if existing_sessions:
            raise Exception(f"邀请ID {invitation_id} 已有进行中的会话记录")

        session_id = str(uuid.uuid4())

        # 从interview_question表获取题目信息
        question_data = self._load_questions_for_session(invitation_id)

        # 从邀请数据创建会话实例
        session = {
            "session_id": session_id,
            "invitation_id": invitation_id,
            "username": invitation["candidate_username"],
            "candidate_name": invitation["candidate_name"],
            "position": invitation["position"] or "未指定",
            "status": "CREATED",
            "created_at": datetime.utcnow(),
            "started_at": None,
            "completed_at": None,
            "current_question_index": 0,
            "total_questions": question_data["total_questions"],
            "conversation_history": [],
            "current_question": None,
            "question_history": [],
            "questions": question_data["questions"],  # 所有题目
            "basic_questions": question_data["basic_questions"],  # 基本信息题目ID列表
            "professional_questions": question_data["professional_questions"]  # 专业能力题目ID列表
        }

        # 存储到内存
        self.active_sessions[session_id] = session

        # 同步会话信息到数据库
        self._sync_session_to_database(session)

        logger.info(f"创建面试会话: {session_id}, 邀请ID: {invitation_id}, 用户: {invitation['candidate_name']}")

        return {
            "session_id": session_id,
            "status": "CREATED",
            "position": session["position"],
            "total_questions": session["total_questions"],
            "created_at": session["created_at"].isoformat()
        }

    def _check_existing_sessions(self, invitation_id: str) -> List[Dict[str, Any]]:
        """
        检查interview_session表中是否已存在该邀请的会话记录

        Args:
            invitation_id: 邀请ID

        Returns:
            存在的会话记录列表
        """
        try:
            query = """
                SELECT session_id, invitation_id, session_status, start_time, end_time
                FROM interview_session
                WHERE invitation_id = %s AND session_status = 'IN_PROGRESS'
            """
            results = database_service.db.execute_query(query, (invitation_id,))
            return [dict(row) for row in results]
        except Exception as e:
            logger.error(f"检查现有会话失败: {e}")
            return []

    def _load_questions_for_session(self, invitation_id: str) -> Dict[str, Any]:
        """
        从interview_question表加载题目信息

        Args:
            invitation_id: 邀请ID

        Returns:
            包含题目信息的字典
        """
        try:
            # 获取所有题目
            questions = database_service.get_invitation_questions(invitation_id)

            # 按类型分类
            basic_questions = []
            professional_questions = []

            for question in questions:
                question_type = question.get("question_type", "")
                question_id = question.get("question_id", "")

                if question_type == "BASIC_INFO":
                    basic_questions.append(question_id)
                elif question_type == "PROFESSIONAL":
                    professional_questions.append(question_id)

            return {
                "questions": questions,
                "basic_questions": basic_questions,
                "professional_questions": professional_questions,
                "total_questions": len(questions)
            }

        except Exception as e:
            logger.error(f"加载会话题目失败: {e}")
            return {
                "questions": [],
                "basic_questions": [],
                "professional_questions": [],
                "total_questions": 0
            }

    def _validate_invitation(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        """
        验证邀请ID是否存在且状态正确

        Args:
            invitation_id: 邀请ID

        Returns:
            邀请数据或None
        """
        try:
            invitation = database_service.get_invitation_by_id(invitation_id)
            if not invitation:
                return None

            # 检查邀请状态（CONFIRMED/IN_PROGRESS/进行中 均可）
            if (invitation.get("interview_status") or "").strip() not in ("CONFIRMED", "IN_PROGRESS", "进行中"):
                logger.warning(f"邀请状态无效: {invitation_id}, 状态: {invitation.get('interview_status')}")
                return None

            return invitation
        except Exception as e:
            logger.error(f"验证邀请失败: {e}")
            return None

    def _sync_session_to_database(self, session: Dict[str, Any]):
        """
        同步会话信息到数据库 - 为该邀请创建一条 interview_session 记录，使用内存中的 session_id。
        这样前端拿到的 session_id 会落在表中，get_current_question 按 invitation_id 或 session_id 都能查到，
        避免切换下一题时 404「面试会话不存在」；语音流也会按 invitation_id 找到同一条记录并更新。
        """
        try:
            questions = session.get("questions", [])
            first_question = questions[0] if questions else {}
            question_id = first_question.get("question_id", "q_1")
            question_text = first_question.get("text", first_question.get("question_text", ""))

            database_service.create_interview_session_record(
                invitation_id=session["invitation_id"],
                question_id=question_id,
                question_text=question_text,
                session_status="IN_PROGRESS",
                follow_up_used=0,
                follow_up_limit=3,
                session_id=session["session_id"],
            )

            logger.info(f"同步会话到数据库: {session['session_id']}, 邀请 {session['invitation_id']} 一条记录")

        except Exception as e:
            logger.error(f"同步会话到数据库失败: {e}")
            raise

    def transition_session_state(self, session_id: str, new_state: str) -> bool:
        """
        会话状态机转换

        Args:
            session_id: 会话ID
            new_state: 目标状态

        Returns:
            是否转换成功
        """
        if session_id not in self.active_sessions:
            logger.warning(f"会话不存在: {session_id}")
            return False

        session = self.active_sessions[session_id]
        current_state = session["status"]

        # 重复完成面试：幂等返回成功，避免前端重试或双路径调用返回 400
        if new_state == "COMPLETED" and current_state == "COMPLETED":
            logger.info(f"会话已是 COMPLETED，幂等返回成功: {session_id}")
            return True

        # 检查状态转换是否有效
        if new_state not in self.state_transitions.get(current_state, []):
            logger.warning(f"无效的状态转换: {current_state} -> {new_state}")
            return False

        # 执行状态转换
        session["status"] = new_state

        # 状态变更时的处理逻辑
        if new_state == "STARTED":
            session["started_at"] = datetime.utcnow()
        elif new_state == "COMPLETED":
            session["completed_at"] = datetime.utcnow()
        elif new_state == "CLOSED":
            # 可以在这里添加清理逻辑
            pass

        # 同步状态变更到数据库
        self._sync_session_status_to_database(session_id, new_state)

        logger.info(f"会话状态转换: {session_id}, {current_state} -> {new_state}")
        return True

    def _sync_session_status_to_database(self, session_id: str, status: str):
        """
        同步会话状态到数据库

        Args:
            session_id: 会话ID
            status: 新状态
        """
        try:
            # 这里应该调用数据库服务更新会话状态
            # 暂时记录日志，后续实现数据库同步
            logger.debug(f"同步会话状态到数据库: {session_id} -> {status}")

            # 如果是COMPLETED状态，还需要更新邀请状态
            if status == "COMPLETED":
                session = self.get_session(session_id)
                if session and session.get("invitation_id"):
                    invitation_id = session["invitation_id"]
                    # 更新邀请状态为COMPLETED
                    try:
                        from datetime import datetime
                        success = database_service.update_invitation_status(
                            invitation_id=invitation_id,
                            status="COMPLETED",
                            end_time=datetime.now()
                        )
                        if success:
                            logger.info(f"成功更新邀请状态为COMPLETED: invitation_id={invitation_id}")
                        else:
                            logger.warning(f"更新邀请状态失败: invitation_id={invitation_id}")
                    except Exception as e:
                        logger.error(f"更新邀请状态时发生错误: invitation_id={invitation_id}, error={e}")

        except Exception as e:
            logger.error(f"同步会话状态到数据库失败: {e}")

    def load_questions_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """
        为会话加载问题列表

        Args:
            session_id: 会话ID

        Returns:
            问题列表
        """
        try:
            session = self.get_session(session_id)
            if not session or not session.get("invitation_id"):
                return []

            invitation_id = session["invitation_id"]

            # 从数据库加载问题
            questions = database_service.get_invitation_questions(invitation_id)

            if not questions:
                # 如果没有预设问题，生成默认问题
                questions = self._generate_default_questions(session["position"])

            # 更新会话中的问题列表
            session["questions"] = questions
            session["total_questions"] = len(questions)

            return questions

        except Exception as e:
            logger.error(f"加载会话问题失败: {e}")
            return []

    def _generate_default_questions(self, position: str) -> List[Dict[str, Any]]:
        """
        生成默认问题列表

        Args:
            position: 职位名称

        Returns:
            默认问题列表
        """
        default_questions = [
            {
                "question_id": f"default_{i+1}",
                "type": "text",
                "text": f"请介绍一下你对{position}职位的理解",
                "order": i+1,
                "created_at": datetime.utcnow()
            } for i in range(5)  # 生成5个默认问题
        ]

        return default_questions

    def get_next_question(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        获取下一个问题

        Args:
            session_id: 会话ID

        Returns:
            下一个问题或None
        """
        try:
            session = self.get_session(session_id)
            if not session:
                return None

            # 确保问题已加载
            if not session.get("questions"):
                self.load_questions_for_session(session_id)

            questions = session.get("questions", [])
            current_index = session.get("current_question_index", 0)

            if current_index < len(questions):
                question = questions[current_index]
                session["current_question"] = question
                session["current_question_index"] = current_index + 1
                return question

            return None  # 没有更多问题

        except Exception as e:
            logger.error(f"获取下一个问题失败: {e}")
            return None

    def restore_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        从数据库恢复会话

        Args:
            session_id: 会话ID

        Returns:
            恢复的会话数据或None
        """
        try:
            # 从数据库加载会话数据
            # 这里应该调用数据库服务获取会话信息
            # 暂时返回None，表示不支持恢复
            logger.info(f"尝试恢复会话: {session_id}")
            return None

        except Exception as e:
            logger.error(f"恢复会话失败: {e}")
            return None

    def restore_session_from_database(self, session_id: str, invitation_id: str) -> Optional[Dict[str, Any]]:
        """
        从interview_session表恢复会话到内存

        Args:
            session_id: 会话ID
            invitation_id: 邀请ID

        Returns:
            恢复的会话数据
        """
        try:
            # 从interview_session表加载会话记录
            query = """
                SELECT session_id, invitation_id, question_id, question_text,
                       candidate_answer, session_content, session_status,
                       start_time, end_time, audio_duration, question_order,
                       follow_up_used, follow_up_limit
                FROM interview_session
                WHERE invitation_id = %s
                ORDER BY question_order ASC
            """
            results = database_service.db.execute_query(query, (invitation_id,))
            session_records = [dict(row) for row in results]

            if not session_records:
                logger.warning(f"未找到会话记录: {invitation_id}")
                return None

            # 获取邀请信息
            invitation = database_service.get_invitation_by_id(invitation_id)
            if not invitation:
                logger.error(f"邀请不存在: {invitation_id}")
                return None

            # 创建会话对象（使用标准的问题加载逻辑）
            session = {
                "session_id": session_id,
                "invitation_id": invitation_id,
                "username": invitation["candidate_username"],
                "candidate_name": invitation["candidate_name"],
                "position": invitation["position"] or "未指定",
                "status": "IN_PROGRESS",  # 恢复的会话设为进行中
                "created_at": session_records[0]["start_time"] or datetime.utcnow(),
                "started_at": session_records[0]["start_time"],
                "completed_at": None,
                "current_question_index": 0,  # 从第一个问题开始
                "total_questions": 0,  # 稍后通过load_questions_for_session设置
                "conversation_history": [],
                "current_question": None,
                "question_history": [],
                "questions": [],  # 稍后通过load_questions_for_session设置
                "basic_questions": [],
                "professional_questions": []
            }

            # 存储到内存
            self.active_sessions[session_id] = session

            # 重新加载问题数据（这会获取完整的题目信息）
            question_data = self._load_questions_for_session(invitation_id)
            session["questions"] = question_data["questions"]
            session["basic_questions"] = question_data["basic_questions"]
            session["professional_questions"] = question_data["professional_questions"]
            session["total_questions"] = question_data["total_questions"]

            logger.info(f"从数据库恢复会话: {session_id}, 邀请ID: {invitation_id}")
            return session

        except Exception as e:
            logger.error(f"从数据库恢复会话失败: {e}")
            return None

    def get_session_history(self, invitation_id: str) -> List[Dict[str, Any]]:
        """
        获取邀请的所有历史会话

        Args:
            invitation_id: 邀请ID

        Returns:
            会话历史列表
        """
        try:
            # 从数据库获取会话历史
            # 这里应该调用数据库服务
            sessions = []

            # 临时实现：返回内存中的活跃会话
            for session_id, session in self.active_sessions.items():
                if session.get("invitation_id") == invitation_id:
                    sessions.append({
                        "session_id": session_id,
                        "status": session["status"],
                        "created_at": session["created_at"],
                        "started_at": session.get("started_at"),
                        "completed_at": session.get("completed_at"),
                        "current_question_index": session["current_question_index"],
                        "total_questions": session["total_questions"]
                    })

            return sessions

        except Exception as e:
            logger.error(f"获取会话历史失败: {e}")
            return []

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话信息"""
        return self.active_sessions.get(session_id)

    def destroy_session(self, session_id: str) -> bool:
        """销毁会话实例"""
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
            logger.info(f"销毁会话: {session_id}")
            return True
        return False

    def add_conversation_message(self, session_id: str, message_type: str, content: str,
                               audio_url: Optional[str] = None) -> bool:
        """
        添加对话消息到上下文

        Args:
            session_id: 会话ID
            message_type: 消息类型 ('question', 'answer', 'system')
            content: 消息内容
            audio_url: 音频URL（可选）
        """
        if session_id not in self.active_sessions:
            return False

        message = {
            "type": message_type,
            "content": content,
            "timestamp": datetime.utcnow(),
            "audio_url": audio_url
        }

        self.active_sessions[session_id]["conversation_history"].append(message)
        return True

    def get_conversation_history(self, session_id: str) -> List[Dict[str, Any]]:
        """获取对话历史"""
        if session_id not in self.active_sessions:
            return []
        return self.active_sessions[session_id]["conversation_history"]

    def update_current_question(self, session_id: str, question: Dict[str, Any]) -> bool:
        """更新当前问题"""
        if session_id not in self.active_sessions:
            return False

        session = self.active_sessions[session_id]
        session["current_question"] = question
        session["current_question_index"] += 1
        session["question_history"].append(question)

        return True
    

    def start_session(self, session_id: str) -> bool:
        """开始面试会话"""
        if self.transition_session_state(session_id, "STARTED"):
            # 开始会话后，设置第一个问题为当前问题
            self.get_next_question(session_id)
            return True
        return False

    def start_interview(self, session_id: str) -> bool:
        """开始面试流程"""
        return self.transition_session_state(session_id, "STARTED")
    
    def pause_interview(self, session_id: str) -> bool:
        """暂停面试"""
        return self.transition_session_state(session_id, "PAUSED")
    
    def resume_interview(self, session_id: str) -> bool:
        """恢复面试"""
        return self.transition_session_state(session_id, "IN_PROGRESS")
    
    def complete_interview(self, session_id: str) -> bool:
        """完成面试"""
        return self.transition_session_state(session_id, "COMPLETED")

    def complete_session(
        self,
        session_id: str,
        username: str = "",
        is_external_user: bool = False
    ) -> bool:
        """完成面试会话（与 complete_interview 一致，供路由层调用）"""
        return self.transition_session_state(session_id, "COMPLETED")

    def register_session_from_flow(self, session_id: str, invitation_id: str) -> None:
        """
        由 interview_flow.start_interview 创建 DB 会话后，在内存中注册会话，
        以便 get_session / complete 等接口能查到（避免 404）。
        """
        if session_id in self.active_sessions:
            return
        self.active_sessions[session_id] = {
            "session_id": session_id,
            "invitation_id": invitation_id,
            "user_id": invitation_id,  # 候选人场景用 invitation_id 标识，便于 complete 校验
            "username": "",
            "candidate_name": "",
            "position": "",
            "status": "IN_PROGRESS",
            "created_at": datetime.utcnow(),
            "started_at": datetime.utcnow(),
            "completed_at": None,
            "current_question_index": 0,
            "total_questions": 0,
            "conversation_history": [],
            "current_question": None,
            "question_history": [],
            "questions": [],
        }
        logger.info(f"已注册会话到内存: session_id={session_id}, invitation_id={invitation_id}")
    
    def cancel_interview(self, session_id: str) -> bool:
        """取消面试"""
        return self.transition_session_state(session_id, "CANCELLED")
    
    def get_current_question(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取当前问题"""
        session = self.get_session(session_id)
        return session.get("current_question") if session else None
    
    def get_session_status(self, session_id: str) -> str:
        """获取会话状态"""
        session = self.get_session(session_id)
        return session.get("status", "UNKNOWN") if session else "NOT_FOUND"
    
    def get_session_progress(self, session_id: str) -> Dict[str, Any]:
        """获取会话进度"""
        session = self.get_session(session_id)
        if not session:
            return {"current": 0, "total": 0, "percentage": 0}
        
        current = session.get("current_question_index", 0)
        total = session.get("total_questions", 10)
        
        return {
            "current": current,
            "total": total,
            "percentage": (current / total * 100) if total > 0 else 0
        }
    
    def cleanup_expired_sessions(self):
        """清理过期的会话"""
        current_time = datetime.utcnow()
        expired_sessions = []
        
        for session_id, session in self.active_sessions.items():
            created_at = session.get("created_at")
            if created_at and (current_time - created_at).seconds > self.session_timeout_minutes * 60:
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            self.destroy_session(session_id)
        
        if expired_sessions:
            logger.info(f"清理了 {len(expired_sessions)} 个过期会话")


# 创建全局服务实例
interview_session_service = InterviewSessionService()
