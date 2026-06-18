"""
数据库服务类
提供用户、面试题、面试记录等数据的CRUD操作
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID
from loguru import logger

from app.database.connection import get_db_manager
from app.database.models import InterviewStatus, QuestionType


class DatabaseService:
    """数据库服务类"""
    
    def __init__(self):
        self.db = get_db_manager()
    
    # ==================== 用户相关操作 ====================
    
    def create_user(
        self,
        username: str,
        password_hash: str,
        full_name: str = "",
        email: str = ""
    ) -> Dict[str, Any]:
        """创建用户"""
        query = """
            INSERT INTO users (username, password_hash, full_name, email)
            VALUES (%s, %s, %s, %s)
            RETURNING id, username, full_name, email, created_at
        """
        result = self.db.execute_one(query, (username, password_hash, full_name, email))
        if result:
            user_dict = dict(result)
            # 确保ID是字符串格式
            if user_dict.get("id"):
                user_dict["id"] = str(user_dict["id"])
            return user_dict
        raise Exception("创建用户失败")
    
    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """根据用户名获取用户"""
        query = """
            SELECT id, username, password_hash, full_name, email, 
                   is_active, created_at, last_login
            FROM users
            WHERE username = %s
        """
        result = self.db.execute_one(query, (username,))
        if result:
            user_dict = dict(result)
            # 确保ID是字符串格式
            if user_dict.get("id"):
                user_dict["id"] = str(user_dict["id"])
            return user_dict
        return None
    
    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取用户"""
        query = """
            SELECT id, username, full_name, email, is_active, created_at, last_login
            FROM users
            WHERE id = %s
        """
        result = self.db.execute_one(query, (user_id,))
        return dict(result) if result else None
    
    def update_user_login_time(self, user_id: str):
        """更新用户最后登录时间"""
        query = """
            UPDATE users
            SET last_login = CURRENT_TIMESTAMP
            WHERE id = %s
        """
        self.db.execute_update(query, (user_id,))
    
    def update_user_info(self, user_id: str, **kwargs) -> bool:
        """更新用户信息"""
        allowed_fields = ["full_name", "email", "is_active"]
        updates = []
        params = []
        
        for field, value in kwargs.items():
            if field in allowed_fields:
                updates.append(f"{field} = %s")
                params.append(value)
        
        if not updates:
            return False
        
        params.append(user_id)
        query = f"""
            UPDATE users
            SET {', '.join(updates)}
            WHERE id = %s
        """
        self.db.execute_update(query, tuple(params))
        return True
    
    def change_user_password(self, user_id: str, password_hash: str) -> bool:
        """修改用户密码"""
        query = """
            UPDATE users
            SET password_hash = %s
            WHERE id = %s
        """
        self.db.execute_update(query, (password_hash, user_id))
        return True
    
    def get_all_users(self) -> List[Dict[str, Any]]:
        """获取所有用户"""
        query = """
            SELECT id, username, full_name, email, is_active, created_at, last_login
            FROM users
            ORDER BY created_at DESC
        """
        results = self.db.execute_query(query)
        return [dict(r) for r in results]
    
    # ==================== 面试题相关操作 ====================
    
    def create_question(
        self,
        question_text: str,
        question_type: str,
        position: str = "",
        order_index: int = 0
    ) -> Dict[str, Any]:
        """创建面试题"""
        query = """
            INSERT INTO interview_questions
            (question_text, question_type, position, order_index)
            VALUES (%s, %s, %s, %s)
            RETURNING id, question_text, question_type, position, order_index, created_at
        """
        result = self.db.execute_one(
            query, (question_text, question_type, position, order_index)
        )
        if result:
            return dict(result)
        raise Exception("创建面试题失败")
    
    def get_question_by_id(self, question_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取面试题"""
        query = """
            SELECT id, question_text, question_type, position, order_index, created_at
            FROM interview_questions
            WHERE id = %s AND is_active = TRUE
        """
        result = self.db.execute_one(query, (question_id,))
        return dict(result) if result else None
    
    def get_questions_by_type(
        self,
        question_type: str = None,
        position: str = None,
        limit: int = None
    ) -> List[Dict[str, Any]]:
        """根据条件获取面试题列表"""
        conditions = ["is_active = TRUE"]
        params = []

        if question_type:
            conditions.append("question_type = %s")
            params.append(question_type)
        if position:
            conditions.append("position = %s")
            params.append(position)

        query = f"""
            SELECT id, question_text, question_type, position, order_index
            FROM interview_questions
            WHERE {' AND '.join(conditions)}
            ORDER BY order_index, created_at
        """
        
        if limit:
            query += f" LIMIT {limit}"
        
        results = self.db.execute_query(query, tuple(params) if params else None)
        return [dict(r) for r in results]
    
    def update_question(
        self,
        question_id: str,
        question_text: str = None,
        question_type: str = None,
        is_active: bool = None
    ) -> bool:
        """更新面试题"""
        updates = []
        params = []

        if question_text:
            updates.append("question_text = %s")
            params.append(question_text)
        if question_type:
            updates.append("question_type = %s")
            params.append(question_type)
        if is_active is not None:
            updates.append("is_active = %s")
            params.append(is_active)
        
        if not updates:
            return False
        
        params.append(question_id)
        query = f"""
            UPDATE interview_questions
            SET {', '.join(updates)}
            WHERE id = %s
        """
        self.db.execute_update(query, tuple(params))
        return True
    
    # ==================== 面试邀请相关操作（基于新表结构） ====================
    
    def get_invitation_by_id(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        """根据邀请ID获取面试邀请信息"""
        query = """
            SELECT invitation_id, evaluation_id, candidate_name, position, department,
                   basic_info_duration, basic_info_focus, professional_duration, professional_focus,
                   interview_status, interview_scheduled_time, interview_actual_start_time,
                   interview_actual_end_time, candidate_username, candidate_password,
                   created_time, updated_time, requester
            FROM interview_invitation
            WHERE invitation_id = %s
        """
        result = self.db.execute_one(query, (invitation_id,))
        return dict(result) if result else None
    
    def get_job_description_by_company_department_position(
        self, 
        company: str, 
        department: str, 
        position: str
    ) -> Optional[Dict[str, Any]]:
        """根据公司、部门、职位查询岗位JD的core_requirements字段"""
        query = """
            SELECT core_requirements
            FROM job_description_base
            WHERE requester = %s AND department = %s AND position = %s
            LIMIT 1
        """
        result = self.db.execute_one(query, (company, department, position))
        return dict(result) if result else None

    def get_invitation_by_candidate_username(self, candidate_username: str) -> Optional[Dict[str, Any]]:
        """根据候选人用户名获取面试邀请信息"""
        query = """
            SELECT invitation_id, evaluation_id, candidate_name, position,
                   basic_info_duration, basic_info_focus, professional_duration, professional_focus,
                   interview_status, interview_scheduled_time, interview_actual_start_time,
                   interview_actual_end_time, candidate_username, candidate_password,
                   created_time, updated_time, requester
            FROM interview_invitation
            WHERE candidate_username = %s
            ORDER BY created_time DESC
            LIMIT 1
        """
        result = self.db.execute_one(query, (candidate_username,))
        return dict(result) if result else None
    
    # 已取消：根据候选人用户名获取面试邀请列表的方法
    # def get_invitations_by_candidate_username(
    #     self,
    #     candidate_username: str,
    #     status: str = None,
    #     limit: int = 50
    # ) -> List[Dict[str, Any]]:
    #     """根据候选人用户名获取面试邀请列表"""
    #     conditions = ["candidate_username = %s"]
    #     params = [candidate_username]
    #
    #     if status:
    #         conditions.append("interview_status = %s")
    #         params.append(status)
    #
    #     query = f"""
    #         SELECT invitation_id, evaluation_id, candidate_name, position, department,
    #            interview_status, interview_scheduled_time, interview_actual_start_time,
    #            interview_actual_end_time, created_time
    #         FROM interview_invitation
    #         WHERE {' AND '.join(conditions)}
    #         ORDER BY created_time DESC
    #         LIMIT %s
    #     """
    #     params.append(limit)
    #
    #     results = self.db.execute_query(query, tuple(params))
    #     return [dict(r) for r in results]
    
    def update_invitation_status(
        self,
        invitation_id: str,
        status: str,
        start_time: datetime = None,
        end_time: datetime = None
    ) -> bool:
        """更新面试邀请状态"""
        updates = ["interview_status = %s"]
        params = [status]
        
        if start_time:
            updates.append("interview_actual_start_time = %s")
            params.append(start_time)
        if end_time:
            updates.append("interview_actual_end_time = %s")
            params.append(end_time)
        
        if status == "IN_PROGRESS" and start_time is None:
            updates.append("interview_actual_start_time = CURRENT_TIMESTAMP")
        elif status == "COMPLETED" and end_time is None:
            updates.append("interview_actual_end_time = CURRENT_TIMESTAMP")
        
        params.append(invitation_id)
        query = f"""
            UPDATE interview_invitation
            SET {', '.join(updates)}, updated_time = CURRENT_TIMESTAMP
            WHERE invitation_id = %s
        """
        rows_affected = self.db.execute_update(query, tuple(params))
        return rows_affected > 0
    
    # ==================== 面试题相关操作（基于新表结构） ====================
    
    def get_invitation_questions(
        self,
        invitation_id: str
    ) -> List[Dict[str, Any]]:
        """获取面试邀请的所有题目（JOIN知识库表获取题目内容）"""
        try:
            query = """
                SELECT
                    iq.question_id,
                    iq.invitation_id,
                    iq.atomic_question_id,
                    iq.question_type,
                    iq.question_category,
                    iq.question_order,
                    iq.evaluation_points,
                    iq.estimated_duration,  -- 默认120秒
                    iq.difficulty,  -- 默认难度等级
                    COALESCE(iqs.content, iq.question_text, '题目内容暂无') as question_text
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
            results = self.db.execute_query(query, (invitation_id,))
            questions = [dict(r) for r in results]

            # 如果没有找到问题，返回默认问题
            if not questions:
                logger.info(f"邀请 {invitation_id} 没有预设问题，使用默认问题")
                return [
                    {
                        "question_id": f"default_1",
                        "invitation_id": invitation_id,
                        "atomic_question_id": "1",
                        "question_type": "BASIC_INFO",
                        "question_category": "background",
                        "question_order": 1,
                        "evaluation_points": 5,
                        "estimated_duration": 120,
                        "difficulty": "中等",
                        "question_text": "请介绍一下您的基本情况和工作经历"
                    },
                    {
                        "question_id": f"default_2",
                        "invitation_id": invitation_id,
                        "atomic_question_id": "2",
                        "question_type": "PROFESSIONAL",
                        "question_category": "technical",
                        "question_order": 2,
                        "evaluation_points": 8,
                        "estimated_duration": 180,
                        "difficulty": "困难",
                        "question_text": "请描述您在项目中遇到的技术挑战及解决方案"
                    },
                    {
                        "question_id": f"default_3",
                        "invitation_id": invitation_id,
                        "atomic_question_id": "3",
                        "question_type": "PROFESSIONAL",
                        "question_category": "teamwork",
                        "question_order": 3,
                        "evaluation_points": 6,
                        "estimated_duration": 150,
                        "difficulty": "中等",
                        "question_text": "请分享一次团队协作的经历"
                    }
                ]

            # 记录问题内容到日志
            for i, q in enumerate(questions, 1):
                logger.info(f"题目{i}: {q.get('question_text', '无内容')} (类型:{q.get('question_type')}, 难度:{q.get('difficulty')}, 评分:{q.get('evaluation_points')})")

            return questions
        except Exception as e:
            # 如果查询失败，返回默认问题
            logger.warning(f"获取邀请问题失败，使用默认问题: {e}")
            return [
                {
                    "question_id": f"default_1",
                    "invitation_id": invitation_id,
                    "atomic_question_id": "1",
                    "question_type": "BASIC_INFO",
                    "question_category": "background",
                    "question_order": 1,
                    "evaluation_points": 5,
                    "estimated_duration": 120,
                    "difficulty": "中等",
                    "question_text": "请介绍一下您的基本情况和工作经历"
                },
                {
                    "question_id": f"default_2",
                    "invitation_id": invitation_id,
                    "atomic_question_id": "2",
                    "question_type": "PROFESSIONAL",
                    "question_category": "technical",
                    "question_order": 2,
                    "evaluation_points": 8,
                    "estimated_duration": 180,
                    "difficulty": "困难",
                    "question_text": "请描述您在项目中遇到的技术挑战及解决方案"
                },
                {
                    "question_id": f"default_3",
                    "invitation_id": invitation_id,
                    "atomic_question_id": "3",
                    "question_type": "PROFESSIONAL",
                    "question_category": "teamwork",
                    "question_order": 3,
                    "evaluation_points": 6,
                    "estimated_duration": 150,
                    "difficulty": "中等",
                    "question_text": "请分享一次团队协作的经历"
                }
            ]

    def get_invitation_basic_questions(
        self,
        invitation_id: str
    ) -> List[str]:
        """获取面试邀请的基本信息题目question_id列表"""
        query = """
            SELECT iq.question_id
            FROM interview_question iq
            WHERE iq.invitation_id = %s AND iq.question_type = 'BASIC'
            ORDER BY iq.question_order ASC
        """
        results = self.db.execute_query(query, (invitation_id,))
        return [row['question_id'] for row in results]

    def get_invitation_professional_questions(
        self,
        invitation_id: str
    ) -> List[str]:
        """获取面试邀请的专业能力题目question_id列表"""
        query = """
            SELECT iq.question_id
            FROM interview_question iq
            WHERE iq.invitation_id = %s AND iq.question_type = 'SPECIALTY'
            ORDER BY iq.question_order ASC
        """
        results = self.db.execute_query(query, (invitation_id,))
        return [row['question_id'] for row in results]
    
    def get_question_by_id(
        self,
        question_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据题目ID获取题目信息（JOIN知识库表）"""
        query = """
            SELECT
                iq.question_id,
                iq.invitation_id,
                iq.atomic_question_id,
                iq.question_type,
                iq.question_category,
                iq.question_order,
                iq.evaluation_points,
                iqs.content as question_text,
                iqs.standard_answer
            FROM interview_question iq
            LEFT JOIN interview_questions iqs ON iq.atomic_question_id = iqs.id
            WHERE iq.question_id = %s
        """
        result = self.db.execute_one(query, (question_id,))
        return dict(result) if result else None
    
    # ==================== 面试会话记录相关操作（基于新表结构） ====================
    
    def create_interview_session_record(
        self,
        invitation_id: str,
        question_id: str = None,
        question_text: str = None,
        candidate_answer: str = None,
        session_status: str = "IN_PROGRESS",
        audio_duration: float = None,
        follow_up_used: int = 0,
        follow_up_limit: int = 3,
        session_id: str = None,
    ) -> Dict[str, Any]:
        """创建面试会话记录（问答记录）
        注意：question_order字段不存在于interview_session表中，
        如需题目顺序请通过JOIN interview_question表查询。
        若传入 session_id，则使用该 ID 写入一条记录（与前端/语音流共用同一会话 ID，避免 current-question 404）。
        """
        import uuid
        if not session_id:
            session_id = str(uuid.uuid4())

        query = """
            INSERT INTO interview_session
            (session_id, invitation_id, question_id, question_text, candidate_answer,
             session_status, audio_duration, follow_up_used, follow_up_limit,
             start_time, create_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING session_id, invitation_id, question_id, question_text, candidate_answer,
                      session_status, start_time, end_time, create_time,
                      audio_duration, follow_up_used, follow_up_limit
        """
        result = self.db.execute_one(
            query,
            (session_id, invitation_id, question_id, question_text, candidate_answer,
             session_status, audio_duration, follow_up_used, follow_up_limit)
        )
        if result:
            return dict(result)
        raise Exception("创建面试会话记录失败")
    
    def update_interview_session_record(
        self,
        session_id: str,
        candidate_answer: str = None,
        session_status: str = None,
        end_time: datetime = None,
        audio_duration: float = None,
        follow_up_used: int = None
    ) -> bool:
        """更新面试会话记录"""
        updates = []
        params = []
        
        if candidate_answer is not None:
            updates.append("candidate_answer = %s")
            params.append(candidate_answer)
        if session_status:
            updates.append("session_status = %s")
            params.append(session_status)
        if end_time:
            updates.append("end_time = %s")
            params.append(end_time)
        elif session_status == "COMPLETED":
            updates.append("end_time = CURRENT_TIMESTAMP")
        if audio_duration is not None:
            updates.append("audio_duration = %s")
            params.append(audio_duration)
        if follow_up_used is not None:
            updates.append("follow_up_used = %s")
            params.append(follow_up_used)
        
        if not updates:
            return False
        
        params.append(session_id)
        query = f"""
            UPDATE interview_session
            SET {', '.join(updates)}
            WHERE session_id = %s
        """
        rows_affected = self.db.execute_update(query, tuple(params))
        return rows_affected > 0
    
    def get_session_invitation(self, session_id: str) -> Optional[Dict[str, Any]]:
        """根据 session_id 查一条 interview_session 记录，用于恢复会话时拿到 invitation_id"""
        query = """
            SELECT session_id, invitation_id FROM interview_session WHERE session_id = %s LIMIT 1
        """
        result = self.db.execute_one(query, (session_id,))
        return dict(result) if result else None

    def get_session_invitation_latest_for_invitation(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        """按 invitation_id 取该邀请下最新一条 interview_session（与语音流写库时查「当前会话」逻辑一致）"""
        query = """
            SELECT session_id, invitation_id FROM interview_session
            WHERE invitation_id = %s
            ORDER BY create_time DESC
            LIMIT 1
        """
        result = self.db.execute_one(query, (invitation_id,))
        return dict(result) if result else None

    def get_invitation_sessions(
        self,
        invitation_id: str,
        limit: int = None
    ) -> List[Dict[str, Any]]:
        """获取面试邀请的所有会话记录"""
        query = """
            SELECT session_id, invitation_id, question_id, question_text, candidate_answer,
                   session_status, start_time, end_time, create_time, question_order,
                   audio_duration, follow_up_used, follow_up_limit
            FROM interview_session
            WHERE invitation_id = %s
            ORDER BY question_order ASC, create_time ASC
        """
        if limit:
            query += f" LIMIT {limit}"
        
        results = self.db.execute_query(query, (invitation_id,))
        return [dict(r) for r in results]
    
    def get_invitation_session_count(self, invitation_id: str) -> int:
        """获取面试邀请的会话记录数量"""
        query = """
            SELECT COUNT(*) as count
            FROM interview_session
            WHERE invitation_id = %s
        """
        result = self.db.execute_one(query, (invitation_id,))
        return result["count"] if result else 0
    
    # ==================== candidate_answers表操作 ====================

    def create_candidate_answer(
        self,
        session_id: str,
        question_id: str,
        answer_text: str,
        is_follow_up: bool = False,
        parent_answer_id: str = None,
        status: str = 'recorded'
    ) -> Dict[str, Any]:
        """创建候选人答案记录"""

        import uuid
        from datetime import datetime

        answer_id = f"ANS_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"

        query = """
        INSERT INTO candidate_answers (
            id, session_id, question_id, answer_text,
            is_follow_up, parent_answer_id, status, create_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        RETURNING id, session_id, question_id, answer_text, is_follow_up,
                  parent_answer_id, status, create_time
        """

        result = self.db.execute_one(
            query,
            (answer_id, session_id, question_id, answer_text,
             is_follow_up, parent_answer_id, status)
        )

        if result:
            return dict(result)
        raise Exception("创建候选人答案记录失败")

    def update_candidate_answer_evaluation(
        self,
        answer_id: str,
        evaluation_result: dict = None,
        point_evaluations: list = None,
        final_score: float = None,
        need_follow_up: bool = False,
        follow_up_question: str = None,
        follow_up_evaluation_points: list = None,
        follow_up_answer_text: str = None,
        follow_up_evaluation: dict = None,
        comprehensive_score: float = None,
        status: str = 'evaluated'
    ):
        """更新候选人答案的评估结果"""

        query = """
        UPDATE candidate_answers SET
            evaluation_result = %s,
            point_evaluations = %s,
            final_score = %s,
            need_follow_up = %s,
            follow_up_question = %s,
            follow_up_evaluation_points = %s,
            follow_up_answer_text = %s,
            follow_up_evaluation = %s,
            comprehensive_score = %s,
            status = %s,
            update_time = CURRENT_TIMESTAMP
        WHERE id = %s
        """

        import json
        affected_rows = self.db.execute_update(
            query,
            (json.dumps(evaluation_result, ensure_ascii=False) if evaluation_result else None,
             json.dumps(point_evaluations, ensure_ascii=False) if point_evaluations else None,
             final_score,
             need_follow_up,
             follow_up_question,
             json.dumps(follow_up_evaluation_points, ensure_ascii=False) if follow_up_evaluation_points else None,
             follow_up_answer_text,
             json.dumps(follow_up_evaluation, ensure_ascii=False) if follow_up_evaluation else None,
             comprehensive_score,
             status,
             answer_id)
        )

        return affected_rows > 0

    def get_candidate_answer(self, answer_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取候选人答案记录"""

        query = "SELECT * FROM candidate_answers WHERE id = %s"
        result = self.db.execute_one(query, (answer_id,))
        return dict(result) if result else None

    def get_session_candidate_answers(self, session_id: str) -> List[Dict[str, Any]]:
        """获取会话的所有候选人答案记录
        按照题目的真实顺序（question_order）排序，而不是答案创建时间
        """

        query = """
        SELECT ca.* 
        FROM candidate_answers ca
        LEFT JOIN interview_question iq ON ca.question_id = iq.question_id
        WHERE ca.session_id = %s
        ORDER BY COALESCE(iq.question_order, 999999) ASC, ca.create_time ASC
        """

        results = self.db.execute_query(query, (session_id,))
        return [dict(r) for r in results]

    def get_answer_by_question(self, session_id: str, question_id: str) -> Optional[Dict[str, Any]]:
        """获取会话中特定问题的答案记录"""

        query = """
        SELECT * FROM candidate_answers
        WHERE session_id = %s AND question_id = %s
        ORDER BY create_time DESC
        LIMIT 1
        """

        result = self.db.execute_one(query, (session_id, question_id))
        return dict(result) if result else None

    def get_current_question_order(self, invitation_id: str) -> int:
        """获取当前问题序号（已回答的问题数）"""
        query = """
            SELECT COALESCE(COUNT(*), 0) as next_order
            FROM candidate_answers ca
            JOIN interview_session s ON ca.session_id = s.session_id
            WHERE s.invitation_id = %s
              AND (ca.is_follow_up = FALSE OR ca.is_follow_up IS NULL)
        """
        result = self.db.execute_one(query, (invitation_id,))
        return result["next_order"] if result else 0

    # ==================== 面试评估记录操作 ====================

    async def create_interview_evaluation_record(
        self,
        invitation_id: str,
        overall_score: float = None,
        dimension_scores: dict = None,
        dimension_details: dict = None,
        evaluation_summary: str = None,
        evaluation_suggestions: str = None,
        is_passed: int = 0,
        evaluator_type: str = 'AGENT',
        question_score: float = None,
        evaluation_structured: dict = None,
    ) -> bool:
        """
        创建或更新面试评估记录（如果invitation_id已存在则更新）

        Args:
            invitation_id: 面试邀请ID
            overall_score: 总体分数
            dimension_scores: 维度分数字典
            dimension_details: 维度详情字典
            evaluation_summary: 评估总结
            evaluation_suggestions: 评估建议
            is_passed: 录用结论（0=未通过，1=通过，2=待定）
            evaluator_type: 评估类型
            question_score: 题目分数（总分/总题数，未作答按0分）
            evaluation_structured: 叙事结构（结论/亮点/风险/复试项/分类均分等）JSON

        Returns:
            是否创建/更新成功
        """
        try:
            import json
            import uuid
            from datetime import datetime

            evaluation_record_id = f"EVAL_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8].upper()}"

            # 使用 UPSERT (INSERT ... ON CONFLICT ... UPDATE) 语法
            # PostgreSQL 使用 ON CONFLICT，MySQL/SQLite 使用 ON DUPLICATE KEY UPDATE
            if self.db.db_type == 'postgresql':
                query = """
                INSERT INTO interview_evaluation_record (
                    evaluation_record_id, invitation_id, overall_score, dimension_scores,
                    dimension_details, evaluation_summary, evaluation_suggestions,
                    is_passed, evaluator_type, question_score, evaluation_structured, create_time
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (invitation_id) 
                DO UPDATE SET
                    evaluation_record_id = EXCLUDED.evaluation_record_id,
                    overall_score = EXCLUDED.overall_score,
                    dimension_scores = EXCLUDED.dimension_scores,
                    dimension_details = EXCLUDED.dimension_details,
                    evaluation_summary = EXCLUDED.evaluation_summary,
                    evaluation_suggestions = EXCLUDED.evaluation_suggestions,
                    is_passed = EXCLUDED.is_passed,
                    evaluator_type = EXCLUDED.evaluator_type,
                    question_score = EXCLUDED.question_score,
                    evaluation_structured = EXCLUDED.evaluation_structured,
                    update_time = CURRENT_TIMESTAMP
                """
            else:
                # MySQL/SQLite 使用 ON DUPLICATE KEY UPDATE
                query = """
                INSERT INTO interview_evaluation_record (
                    evaluation_record_id, invitation_id, overall_score, dimension_scores,
                    dimension_details, evaluation_summary, evaluation_suggestions,
                    is_passed, evaluator_type, question_score, evaluation_structured, create_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE
                    evaluation_record_id = VALUES(evaluation_record_id),
                    overall_score = VALUES(overall_score),
                    dimension_scores = VALUES(dimension_scores),
                    dimension_details = VALUES(dimension_details),
                    evaluation_summary = VALUES(evaluation_summary),
                    evaluation_suggestions = VALUES(evaluation_suggestions),
                    is_passed = VALUES(is_passed),
                    evaluator_type = VALUES(evaluator_type),
                    question_score = VALUES(question_score),
                    evaluation_structured = VALUES(evaluation_structured),
                    update_time = CURRENT_TIMESTAMP
                """
                # 将 %s 替换为 ?
                query = query.replace('%s', '?')

            # 准备JSON数据
            dimension_scores_json = json.dumps(dimension_scores, ensure_ascii=False) if dimension_scores else None
            dimension_details_json = json.dumps(dimension_details, ensure_ascii=False) if dimension_details else None
            evaluation_structured_json = (
                json.dumps(evaluation_structured, ensure_ascii=False) if evaluation_structured else None
            )

            # 使用execute_update执行INSERT/UPDATE并检查影响行数
            affected_rows = self.db.execute_update(
                query,
                (evaluation_record_id, invitation_id, overall_score,
                 dimension_scores_json,
                 dimension_details_json,
                 evaluation_summary, evaluation_suggestions, is_passed, evaluator_type, question_score,
                 evaluation_structured_json)
            )

            success = affected_rows > 0
            if success:
                logger.info(f"面试评估记录创建/更新成功: {evaluation_record_id}, invitation_id={invitation_id}")
                logger.debug(f"保存的数据: overall_score={overall_score}, dimension_scores长度={len(dimension_scores) if dimension_scores else 0}, dimension_details长度={len(dimension_details) if dimension_details else 0}, evaluation_summary长度={len(evaluation_summary) if evaluation_summary else 0}")
            else:
                logger.warning(f"面试评估记录创建/更新失败: 影响行数为0")
            return success

        except Exception as e:
            logger.error(f"创建面试评估记录失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def get_interview_evaluation_record(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        """
        获取面试评估记录

        Args:
            invitation_id: 面试邀请ID

        Returns:
            评估记录字典或None
        """
        try:
            query = """
            SELECT * FROM interview_evaluation_record
            WHERE invitation_id = %s
            ORDER BY create_time DESC
            LIMIT 1
            """

            result = self.db.execute_one(query, (invitation_id,))
            if result:
                # 解析JSON字段
                import json
                record = dict(result)

                # 解析JSON字符串
                for field in ['dimension_scores', 'dimension_details', 'evaluation_structured']:
                    if record.get(field) and isinstance(record[field], str):
                        try:
                            record[field] = json.loads(record[field])
                        except Exception:
                            record[field] = None

                return record
            return None

        except Exception as e:
            logger.error(f"获取面试评估记录失败: {e}")
            return None

    async def update_interview_evaluation_record(
        self,
        invitation_id: str,
        overall_score: float = None,
        dimension_scores: dict = None,
        dimension_details: dict = None,
        evaluation_summary: str = None,
        evaluation_suggestions: str = None,
        is_passed: int = None,
        question_score: float = None,
        manual_override: int = None,
        manual_override_by: str = None,
        manual_override_reason: str = None,
        evaluation_structured: dict = None,
    ) -> bool:
        """
        更新面试评估记录

        Args:
            invitation_id: 面试邀请ID
            overall_score: 总体分数
            dimension_scores: 维度分数字典
            dimension_details: 维度详情字典
            evaluation_summary: 评估总结
            evaluation_suggestions: 评估建议
            is_passed: 录用结论（0=未通过，1=通过，2=待定）
            question_score: 题目分数（总分/总题数，未作答按0分）
            manual_override: 手动覆盖状态
            manual_override_by: 手动覆盖操作人
            manual_override_reason: 手动覆盖原因

        Returns:
            是否更新成功
        """
        try:
            import json

            # 构建更新字段
            update_fields = []
            update_values = []

            fields_mapping = {
                'overall_score': overall_score,
                'dimension_scores': json.dumps(dimension_scores, ensure_ascii=False) if dimension_scores else None,
                'dimension_details': json.dumps(dimension_details, ensure_ascii=False) if dimension_details else None,
                'evaluation_summary': evaluation_summary,
                'evaluation_suggestions': evaluation_suggestions,
                'is_passed': is_passed,
                'question_score': question_score,
                'manual_override': manual_override,
                'manual_override_by': manual_override_by,
                'manual_override_reason': manual_override_reason,
                'evaluation_structured': (
                    json.dumps(evaluation_structured, ensure_ascii=False) if evaluation_structured else None
                ),
            }

            for field, value in fields_mapping.items():
                if value is not None:
                    update_fields.append(f"{field} = %s")
                    update_values.append(value)

            if not update_fields:
                return True  # 没有需要更新的字段

            # 添加更新时间
            update_fields.append("update_time = CURRENT_TIMESTAMP")

            query = f"""
            UPDATE interview_evaluation_record
            SET {', '.join(update_fields)}
            WHERE invitation_id = %s
            """

            update_values.append(invitation_id)
            affected_rows = self.db.execute_update(query, tuple(update_values))

            success = affected_rows > 0
            if success:
                logger.info(f"面试评估记录更新成功: invitation_id={invitation_id}")
            return success

        except Exception as e:
            logger.error(f"更新面试评估记录失败: {e}")
            return False

    async def get_evaluation_records_by_status(self, is_passed: int = None, limit: int = 100) -> List[Dict[str, Any]]:
        """
        根据状态获取评估记录

        Args:
            is_passed: 录用结论 (0/1/2，2=待定)
            limit: 限制数量

        Returns:
            评估记录列表
        """
        try:
            if is_passed is not None:
                query = """
                SELECT * FROM interview_evaluation_record
                WHERE is_passed = %s
                ORDER BY create_time DESC
                LIMIT %s
                """
                results = self.db.execute_query(query, (is_passed, limit))
            else:
                query = """
                SELECT * FROM interview_evaluation_record
                ORDER BY create_time DESC
                LIMIT %s
                """
                results = self.db.execute_query(query, (limit,))

            # 解析JSON字段
            import json
            records = []
            for result in results:
                record = dict(result)
                for field in ['dimension_scores', 'dimension_details', 'evaluation_structured']:
                    if record.get(field) and isinstance(record[field], str):
                        try:
                            record[field] = json.loads(record[field])
                        except Exception:
                            record[field] = None
                records.append(record)

            return records

        except Exception as e:
            logger.error(f"获取评估记录列表失败: {e}")
            return []


# 全局数据库服务实例
database_service = DatabaseService()

