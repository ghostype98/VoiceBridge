"""
面试流程API
整合ASR、TTS、大模型和对话管理的核心面试流程
"""
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pathlib import Path
import tempfile
import uuid
import json
from datetime import datetime
from loguru import logger
import aiofiles

# TTS和ASR服务已迁移到语音流式服务
# from app.services.asr_service import ASRService
# TTS功能已屏蔽
# from app.services.tts_service import TTSService
from app.services.dialogue_service import DialogueService
from app.services.llm_service import LLMService
from app.services.interview_session_service import interview_session_service
from app.database.service import database_service
from app.dependencies import get_dialogue_service, get_llm_service
from agent.evaluation_service import evaluation_service
from config.settings import settings


# Pydantic模型定义
class StartInterviewRequest(BaseModel):
    """开始面试请求"""
    session_id: str = Field(..., description="面试会话ID")


class NextQuestionRequest(BaseModel):
    """下一问题请求"""
    session_id: str = Field(..., description="面试会话ID")


class SubmitAnswerRequest(BaseModel):
    """提交回答请求"""
    session_id: str = Field(..., description="面试会话ID")
    question_id: str = Field(..., description="问题ID")


class VoiceAnswerRequest(BaseModel):
    """语音回答请求"""
    session_id: str = Field(..., description="面试会话ID")
    question_id: str = Field(..., description="问题ID")
    answer_text: str = Field(..., description="语音识别后的文本内容")
    confidence: Optional[float] = Field(0.0, description="语音识别置信度")


class InterviewResponse(BaseModel):
    """面试响应"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None


class QuestionWithAudio(BaseModel):
    """带音频的问题"""
    question_id: str
    question_text: str
    audio_path: str
    audio_url: str
    order: int
    estimated_duration: int = 180  # 预计回答时间（秒），默认3分钟
    evaluation_points: Optional[List[Dict]] = None


# 创建路由器
router = APIRouter(prefix="/api/v1/interview", tags=["面试流程"])


@router.get("/ui-config")
async def get_interview_ui_config():
    """面试页公开 UI 开关（不含敏感信息），供前端拉取。"""
    return {"show_asr_text": bool(settings.INTERVIEW_UI_SHOW_ASR_TEXT)}


@router.post("/start", response_model=InterviewResponse)
async def start_interview(
    request: StartInterviewRequest
):
    """
    开始面试

    - **session_id**: 面试邀请ID（用作会话ID）

    1. 创建会话：检查interview_status是进行中，创建interview_session表记录
    2. 从interview_question表获取题目，按question_type分组
    返回第一个问题
    """
    try:
        logger.info(f"开始面试: 邀请 {request.session_id}")

        # 1. 验证邀请存在且状态正确
        invitation = database_service.get_invitation_by_id(request.session_id)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试邀请不存在"
            )

        # 检查邀请状态（与登录一致：CONFIRMED/IN_PROGRESS/进行中）
        if (invitation.get("interview_status") or "").strip() not in ("CONFIRMED", "IN_PROGRESS", "进行中"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邀请状态不正确，无法开始面试"
            )

        # 2. 创建面试会话记录（如果不存在）
        try:
            session_record = database_service.create_interview_session_record(
                invitation_id=request.session_id,
                session_status="IN_PROGRESS",
                follow_up_limit=2  # 追问次数限制为2
            )
            session_id = session_record["session_id"]
            logger.info(f"创建面试会话记录: {session_id}")
        except Exception as e:
            logger.warning(f"创建会话记录失败，可能已存在: {e}")
            # 如果已存在，获取现有记录
            existing_records = database_service.get_interview_sessions_by_invitation(request.session_id)
            if existing_records:
                session_id = existing_records[0]["session_id"]
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="创建会话记录失败"
                )
        # 在内存中注册会话，以便 complete 等接口能通过 get_session 查到（避免 404）
        interview_session_service.register_session_from_flow(session_id, request.session_id)

        # 3. 从interview_question表获取题目，按question_type分组
        questions = database_service.get_invitation_questions(request.session_id)
        if not questions:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有找到面试题目"
            )

        # 按question_type分组题目
        basic_questions = []
        professional_questions = []

        for q in questions:
            question_type = q.get("question_type", "")
            # 统一问题类型：PROFESSIONAL->SPECIALTY, BASIC_INFO->BASIC
            if question_type == "PROFESSIONAL":
                question_type = "SPECIALTY"
            elif question_type == "BASIC_INFO":
                question_type = "BASIC"
            
            if question_type == "BASIC":
                basic_questions.append(q["question_id"])
            elif question_type == "SPECIALTY":
                professional_questions.append(q["question_id"])

        total_questions = len(questions)

        # 4. 获取第一个问题
        if questions:
            first_question = questions[0]

            # 处理evaluation_points格式
            evaluation_points = first_question.get("evaluation_points")
            if isinstance(evaluation_points, (int, float)):
                # 如果是整数（默认问题），根据问题类型转换为标准JSON格式
                question_type = first_question.get("question_type", "")
                question_text = first_question.get("question_text", "")

                # 统一问题类型
                if question_type == "PROFESSIONAL":
                    question_type = "SPECIALTY"
                elif question_type == "BASIC_INFO":
                    question_type = "BASIC"
                
                if question_type == "BASIC" or "基本情况" in question_text:
                    evaluation_points = [
                        {"point": "了解公司业务和文化", "weight": 0.4},
                        {"point": "个人职业目标与公司匹配", "weight": 0.4},
                        {"point": "表达对岗位的热情", "weight": 0.2}
                    ]
                elif question_type == "SPECIALTY" or any(keyword in question_text for keyword in ["技术", "项目", "挑战", "解决方案"]):
                    evaluation_points = [
                        {"point": "清晰描述问题和解决方案", "weight": 0.3},
                        {"point": "体现团队协作精神", "weight": 0.3},
                        {"point": "展现沟通和协调能力", "weight": 0.4}
                    ]
                else:
                    evaluation_points = [
                        {"point": "回答完整性", "weight": 0.4},
                        {"point": "逻辑清晰度", "weight": 0.3},
                        {"point": "专业深度", "weight": 0.3}
                    ]
            elif isinstance(evaluation_points, str):
                # 如果是字符串，尝试解析JSON
                import json
                try:
                    evaluation_points = json.loads(evaluation_points)
                except:
                    evaluation_points = [
                        {"point": "回答完整性", "weight": 0.4},
                        {"point": "逻辑清晰度", "weight": 0.3},
                        {"point": "专业深度", "weight": 0.3}
                    ]

            # 获取estimated_duration，如果没有则使用默认值
            try:
                estimated_duration = first_question.get("estimated_duration", 180)  # 默认3分钟
                if isinstance(estimated_duration, str):
                    estimated_duration = int(estimated_duration)
                elif not isinstance(estimated_duration, (int, float)):
                    raise ValueError(f"estimated_duration类型错误: {type(estimated_duration)}")

                # 确保是正整数且在合理范围内
                estimated_duration = max(30, min(1800, int(estimated_duration)))  # 30秒到30分钟

            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"获取estimated_duration失败，使用默认值180秒: {str(e)}")
                estimated_duration = 180  # 默认3分钟

            current_question = {
                "question_id": first_question["question_id"],
                "text": first_question["question_text"],
                "order": first_question["question_order"],
                "evaluation_points": evaluation_points
            }

            # TTS功能已屏蔽，直接返回空音频信息
            audio_path = ""
            duration = 0.0
            audio_url = ""

            response = QuestionWithAudio(
                question_id=current_question["question_id"],
                question_text=current_question["text"],
                audio_path=audio_path,
                audio_url=audio_url,
                order=current_question["order"],
                estimated_duration=estimated_duration,
                evaluation_points=current_question["evaluation_points"]
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有可用的问题"
            )

        return InterviewResponse(
            success=True,
            message="面试已开始",
            data={
                "session_id": session_id,
                "invitation_id": request.session_id,
                "status": "IN_PROGRESS",
                "current_question": response.dict(),
                "total_questions": total_questions,
                "current_index": 1,
                "basic_questions": basic_questions,  # 基本信息题目ID列表
                "professional_questions": professional_questions  # 专业能力题目ID列表
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"开始面试失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"开始面试失败: {str(e)}"
        )


@router.post("/next-question", response_model=InterviewResponse)
async def get_next_question(
    request: NextQuestionRequest
):
    """
    获取下一问题
    
    - **session_id**: 面试会话ID
    
    获取面试中的下一个问题并生成语音
    """
    try:
        # 验证会话
        session = interview_session_service.get_session(request.session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )

        if session["status"] not in ["active", "paused"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确"
            )
        
        # 获取下一问题
        current_question = interview_session_service.get_current_question(request.session_id)
        if not current_question:
            # 没有更多问题，完成面试
            interview_session_service.complete_session(request.session_id)
            return InterviewResponse(
                success=True,
                message="面试已完成",
                data={
                    "session_id": request.session_id,
                    "status": "completed",
                    "is_completed": True,
                    "total_questions": len(session["questions"])
                }
            )

        # 获取问题的evaluation_points
        question_id = current_question["question_id"]
        evaluation_points = current_question.get("evaluation_points")

        # 如果current_question中没有evaluation_points，从数据库查询
        if not evaluation_points:
            try:
                question_detail = database_service.get_question_by_id(question_id)
                if question_detail:
                    evaluation_points = question_detail.get("evaluation_points")
            except Exception as e:
                logger.warning(f"获取问题评估要点失败: {e}")

        # 处理evaluation_points格式
        if isinstance(evaluation_points, (int, float)):
            # 如果是整数（默认问题），根据问题类型转换为标准JSON格式
            question_type = current_question.get("question_type", "")
            question_text = current_question.get("text", "")

            # 统一问题类型
            if question_type == "PROFESSIONAL":
                question_type = "SPECIALTY"
            elif question_type == "BASIC_INFO":
                question_type = "BASIC"
            
            if question_type == "BASIC" or "基本情况" in question_text:
                evaluation_points = [
                    {"point": "了解公司业务和文化", "weight": 0.4},
                    {"point": "个人职业目标与公司匹配", "weight": 0.4},
                    {"point": "表达对岗位的热情", "weight": 0.2}
                ]
            elif question_type == "SPECIALTY" or any(keyword in question_text for keyword in ["技术", "项目", "挑战", "解决方案"]):
                evaluation_points = [
                    {"point": "清晰描述问题和解决方案", "weight": 0.3},
                    {"point": "体现团队协作精神", "weight": 0.3},
                    {"point": "展现沟通和协调能力", "weight": 0.4}
                ]
            else:
                evaluation_points = [
                    {"point": "回答完整性", "weight": 0.4},
                    {"point": "逻辑清晰度", "weight": 0.3},
                    {"point": "专业深度", "weight": 0.3}
                ]
        elif isinstance(evaluation_points, str):
            # 如果是字符串，尝试解析JSON
            import json
            try:
                evaluation_points = json.loads(evaluation_points)
            except:
                evaluation_points = [
                    {"point": "回答完整性", "weight": 0.4},
                    {"point": "逻辑清晰度", "weight": 0.3},
                    {"point": "专业深度", "weight": 0.3}
                ]

        # 获取estimated_duration，如果没有则使用默认值
        try:
            estimated_duration = current_question.get("estimated_duration", 180)  # 默认3分钟
            if isinstance(estimated_duration, str):
                estimated_duration = int(estimated_duration)
            elif not isinstance(estimated_duration, (int, float)):
                raise ValueError(f"estimated_duration类型错误: {type(estimated_duration)}")

            # 确保是正整数且在合理范围内
            estimated_duration = max(30, min(1800, int(estimated_duration)))  # 30秒到30分钟

        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"获取estimated_duration失败，使用默认值180秒: {str(e)}")
            estimated_duration = 180  # 默认3分钟

        # TTS功能已屏蔽，直接返回空音频信息
        audio_path = ""
        duration = 0.0
        audio_url = ""

        response = QuestionWithAudio(
            question_id=current_question["question_id"],
            question_text=current_question["text"],
            audio_path=audio_path,
            audio_url=audio_url,
            order=current_question["order"],
            estimated_duration=estimated_duration,
            evaluation_points=evaluation_points
        )
        
        return InterviewResponse(
            success=True,
            message="获取下一问题成功",
            data={
                "session_id": request.session_id,
                "current_question": response.dict(),
                "current_index": session["current_question_index"] + 1,
                "total_questions": len(session["questions"])
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取下一问题失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取下一问题失败: {str(e)}"
        )


@router.post("/voice-answer", response_model=InterviewResponse)
async def submit_voice_answer(
    request: VoiceAnswerRequest,
    dialogue_service: DialogueService = Depends(get_dialogue_service),
    llm_service: LLMService = Depends(get_llm_service)
):
    """
    提交语音回答

    - **session_id**: 面试会话ID
    - **question_id**: 问题ID
    - **answer_text**: 语音识别后的文本内容
    - **confidence**: 语音识别置信度（可选）

    处理用户语音回答文本，进行智能评分和追问判断
    """
    try:
        # 验证会话（基于新表结构）
        invitation = database_service.get_invitation_by_id(request.session_id)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试邀请不存在"
            )
        
        
        if (invitation.get("interview_status") or "").strip() not in ("CONFIRMED", "IN_PROGRESS", "进行中"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邀请状态不正确，无法提交回答"
            )
        
        # 使用语音流式服务已处理的ASR结果
        answer_text = request.answer_text.strip()
        confidence = request.confidence or 0.0

        logger.info(f"语音回答: 文本='{answer_text}', 置信度={confidence}")

        # 音频处理相关（可选，用于保存音频时长等信息）
        audio_duration = None  # 语音流式服务可能提供时长信息

        # 检查是否为追问回答
        is_follow_up_answer = "_followup_" in request.question_id
        parent_question_id = None
        parent_answer_id = None

        if is_follow_up_answer:
            # 解析原始问题ID
            parent_question_id = request.question_id.split("_followup_")[0]
            logger.info(f"识别为追问回答，原始问题ID: {parent_question_id}")

            # 查找父答案记录
            parent_answer_record = database_service.get_answer_by_question(
                request.session_id, parent_question_id
            )
            if parent_answer_record:
                parent_answer_id = parent_answer_record["id"]
                logger.info(f"找到父答案记录: {parent_answer_id}")

        # 保存回答到candidate_answers表

        if is_follow_up_answer:
            # 处理追问回答：更新父答案记录
            if parent_answer_id:
                # 对追问回答进行评估
                follow_up_evaluation_result = await _evaluate_answer_with_llm(
                    question_text=followup_question if 'followup_question' in locals() else "请详细说明",
                    candidate_answer=answer_text,
                    evaluation_points=[],  # 追问通常不需要复杂的评估要点
                    question_type="FOLLOW_UP",
                    question_id=request.question_id
                )

                # 更新父答案记录的追问信息
                database_service.update_candidate_answer_evaluation(
                    answer_id=parent_answer_id,
                    follow_up_answer_text=answer_text,
                    follow_up_evaluation=json.dumps(follow_up_evaluation_result, ensure_ascii=False),
                    comprehensive_score=follow_up_evaluation_result.get('score', 0)
                )

                logger.info(f"追问回答已更新到父答案记录: {parent_answer_id}")
                answer_id = parent_answer_id
                session_id = request.session_id  # 使用传递的session_id
            else:
                logger.error("未找到父答案记录，追问回答处理失败")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="追问回答处理失败：未找到原始问题记录"
                )
        else:
            # 处理普通回答：创建新的答案记录
            # 获取当前问题序号（question_order）
            current_order = database_service.get_current_question_order(request.session_id) + 1

            # 步骤1: 插入到interview_session表（保持向后兼容）
            try:
                session_record = database_service.create_interview_session_record(
                    invitation_id=request.session_id,
                    question_id=request.question_id,
                    candidate_answer=answer_text,
                    session_status="IN_PROGRESS"
                )
                session_id = session_record["session_id"]
                logger.info(f"创建面试会话记录: {session_id}, 问题ID: {request.question_id}, 序号: {current_order}")
            except Exception as e:
                logger.warning(f"创建会话记录失败，可能已存在: {e}")
                # 如果已存在，获取现有记录
                existing_records = database_service.get_invitation_sessions(request.session_id)
                if existing_records:
                    # 查找对应问题的记录
                    for record in existing_records:
                        if record["question_id"] == request.question_id:
                            session_id = record["session_id"]
                            # 更新候选人回答
                            database_service.update_interview_session_record(
                                session_id=session_id,
                                candidate_answer=answer_text
                            )
                            break
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="找不到对应问题的会话记录"
                        )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="创建会话记录失败"
                    )

            # 创建candidate_answers记录
            answer_record = database_service.create_candidate_answer(
                session_id=session_id,
                question_id=request.question_id,
                answer_text=answer_text,
                is_follow_up=False,
                status='recorded'
            )
            answer_id = answer_record["id"]
            logger.info(f"创建候选人答案记录: {answer_id}")

        # 步骤2: 获取evaluation_points并调用LLM评分（仅对普通回答）
        evaluation_result = None
        if not is_follow_up_answer:
            try:
                # 获取问题详情（包含evaluation_points）
                question_detail = database_service.get_question_by_id(request.question_id)
                if question_detail and question_detail.get("evaluation_points"):
                    evaluation_points = question_detail["evaluation_points"]
                    logger.info(f"获取到评估要点: {len(evaluation_points)} 个")

                    # 调用LLM进行评分
                    # 统一问题类型：PROFESSIONAL->SPECIALTY, BASIC_INFO->BASIC
                    question_type = question_detail.get("question_type", "BASIC")
                    if question_type == "PROFESSIONAL":
                        question_type = "SPECIALTY"
                    elif question_type == "BASIC_INFO":
                        question_type = "BASIC"
                    
                    evaluation_result = await _evaluate_answer_with_llm(
                        question_text=question_detail.get("question_text", ""),
                        candidate_answer=answer_text,
                        evaluation_points=evaluation_points,
                        question_type=question_type,
                        question_id=request.question_id,
                        standard_answer=question_detail.get("standard_answer")  # 专业题参考答案
                    )
                    logger.info(f"LLM评分结果: 得分={evaluation_result.get('score', 0)}")

                    # 更新candidate_answers表的评估结果
                    database_service.update_candidate_answer_evaluation(
                        answer_id=answer_id,
                        evaluation_result=evaluation_result,
                        point_evaluations=evaluation_result.get('point_scores', []),
                        final_score=evaluation_result.get('score', 0),
                        need_follow_up=evaluation_result.get('need_follow_up', False),
                        follow_up_question=evaluation_result.get('follow_up_question'),
                        status='evaluated'
                    )
                    logger.info(f"评估结果已存储到candidate_answers表: {answer_id}")
                else:
                    logger.warning(f"问题 {request.question_id} 没有评估要点，跳过评分")

            except Exception as e:
                logger.error(f"评分过程失败: {e}")
                # 评分失败不影响面试流程继续

        # 准备对话上下文
        conversation_history = interview_session_service.get_conversation_history(request.session_id)
        
        # 构建对话状态
        conversation_state = {
            "invitation_id": request.session_id,
            "session_id": request.session_id,  # 兼容性保留
            "user_id": current_user["id"],
            "current_step": "general",
            "slot": {
                "user_answer": answer_text,
                "confidence": confidence,
                "position": invitation.get("position", ""),
                "question_type": question_detail.get("question_type", "BASIC"),  # 添加问题类型（统一为BASIC/SPECIALTY）
                "current_question_id": request.question_id  # 添加当前问题ID
            }
        }
        
        # 使用STAR追问服务处理回答
        dialogue_result = await dialogue_service.process(
            interview_id=request.session_id,
            question_id=request.question_id,
            answer_text=answer_text,
            evaluation_result=evaluation_result,  # 传递评分结果用于智能追问
            conversation_state=conversation_state
        )

        # 根据STAR追问结果决定后续流程
        action = dialogue_result.get("action")
        followup_question = dialogue_result.get("followup_question")
        missing_dimension = dialogue_result.get("missing_dimension")

        if action == "follow_up":
            # 触发STAR追问，返回追问问题
            logger.info(f"STAR追问触发: 维度={missing_dimension}, 问题='{followup_question}'")

            # 更新当前答案记录，将追问信息存储到candidate_answers表
            follow_up_evaluation_points = dialogue_result.get("follow_up_evaluation_points", [])
            if answer_id:
                database_service.update_candidate_answer_evaluation(
                    answer_id=answer_id,
                    need_follow_up=True,
                    follow_up_question=followup_question,
                    follow_up_evaluation_points=follow_up_evaluation_points
                )
                logger.info(f"追问评估要点已存储: {len(follow_up_evaluation_points)} 个要点")

            question_response = QuestionWithAudio(
                question_id=f"{request.question_id}_followup_{missing_dimension}",
                question_text=followup_question,
                audio_path="",  # STAR追问不生成音频
                audio_url="",
                order=999,  # 追问问题序号设为较大值
                evaluation_points=follow_up_evaluation_points  # 返回追问的评估要点
            )
            is_completed = False

        elif action == "next_question":
            # 进入下一题
            logger.info(f"进入下一题: {dialogue_result.get('reasoning', '')}")

            next_question = interview_session_service.get_current_question(request.session_id)
            is_completed = next_question is None

            if not is_completed:
                # TTS功能已屏蔽，直接返回空音频信息
                question_response = QuestionWithAudio(
                    question_id=next_question["question_id"],
                    question_text=next_question["text"],
                    audio_path="",
                    audio_url="",
                    order=next_question["order"]
                )
            else:
                # 完成面试
                interview_session_service.complete_session(request.session_id)
                question_response = None
        else:
            # 异常情况，默认进入下一题
            logger.warning(f"未知的STAR追问动作: {action}")
            next_question = interview_session_service.get_current_question(request.session_id)
            is_completed = next_question is None
            question_response = None if is_completed else QuestionWithAudio(
                question_id=next_question["question_id"],
                question_text=next_question["text"],
                audio_path="",
                audio_url="",
                order=next_question["order"]
            )
        
        return InterviewResponse(
            success=True,
            message="回答提交成功",
            data={
                "session_id": session_id,
                "invitation_id": request.session_id,  # 新字段
                "question_id": request.question_id,
                "answer_id": answer_id,  # 新增答案记录ID
                "answer_saved": True,
                "is_follow_up": is_follow_up_answer,  # 新增是否为追问回答
                "asr_result": {
                    "text": answer_text,
                    "confidence": confidence
                },
                "evaluation_result": evaluation_result,
                "star_followup": {
                    "action": action,
                    "followup_question": followup_question,
                    "missing_dimension": missing_dimension,
                    "reasoning": dialogue_result.get("reasoning", "")
                },
                "next_question": question_response.dict() if question_response else None,
                "is_completed": is_completed,
                "current_index": invitation["current_question_index"] + 1,
                "total_questions": invitation["total_questions"],
                "fallback_triggered": dialogue_result.get("fallback", False)
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交语音回答失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"提交语音回答失败: {str(e)}"
        )


@router.post("/text-answer", response_model=InterviewResponse)
async def submit_text_answer(
    request: SubmitAnswerRequest,
    text: str = Form(..., description="文本回答")
):
    """
    提交文本回答
    
    - **session_id**: 面试会话ID
    - **question_id**: 问题ID
    - **text**: 文本回答内容
    
    处理用户文本回答，生成下一个问题
    """
    try:
        # 验证会话
        session = interview_session_service.get_session(request.session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在"
            )
        
        if session["user_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此面试会话"
            )
        
        if session["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确"
            )
        
        answer_text = text.strip()
        if not answer_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="回答内容不能为空"
            )
        
        # 检查是否为外部系统用户
        is_external_user = current_user.get("external_system", False)
        username = current_user.get("username", "")
        
        # 保存回答
        success = interview_session_service.add_answer(
            session_id=request.session_id,
            question_id=request.question_id,
            answer_text=answer_text,
            username=username,
            is_external_user=is_external_user
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="保存回答失败"
            )
        
        # 获取下一问题
        next_question = interview_session_service.get_current_question(request.session_id)
        is_completed = next_question is None
        
        if not is_completed:
            # TTS功能已屏蔽，直接返回空音频信息
            audio_path = ""
            duration = 0.0
            audio_url = ""
            
            question_response = QuestionWithAudio(
                question_id=next_question["question_id"],
                question_text=next_question["text"],
                audio_path=audio_path,
                audio_url=audio_url,
                order=next_question["order"]
            )
        else:
            # 完成面试
            interview_session_service.complete_session(request.session_id)
            question_response = None
        
        return InterviewResponse(
            success=True,
            message="回答提交成功",
            data={
                "session_id": request.session_id,
                "answer_saved": True,
                "next_question": question_response.dict() if question_response else None,
                "is_completed": is_completed,
                "current_index": session["current_question_index"] + 1,
                "total_questions": len(session["questions"])
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交文本回答失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"提交文本回答失败: {str(e)}"
        )


@router.get("/audio/{audio_filename}")
async def get_audio_file(audio_filename: str):
    """
    获取生成的音频文件
    
    - **audio_filename**: 音频文件名
    """
    try:
        # 构建音频文件路径（题目音频）
        audio_path = Path("storage/question_audio") / audio_filename
        
        if not audio_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="音频文件不存在"
            )
        
        return FileResponse(
            path=str(audio_path),
            media_type="audio/wav",
            filename=audio_filename
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取音频文件失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取音频文件失败: {str(e)}"
        )


@router.get("/answer-audio/{audio_filename}")
async def get_answer_audio_file(audio_filename: str):
    """
    获取候选人完整作答录音文件
    
    - **audio_filename**: 录音文件名
    """
    try:
        # 构建录音文件路径（完整作答录音）
        audio_path = Path("storage/answer_audio") / audio_filename

        if not audio_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="录音文件不存在"
            )

        return FileResponse(
            path=str(audio_path),
            media_type="audio/wav",
            filename=audio_filename
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取录音文件失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取录音文件失败: {str(e)}"
        )



async def _run_interview_evaluation_background(session_id: str, invitation_id: str):
    """后台生成面试总评，候选人端完成接口不再等待该耗时流程。"""
    try:
        logger.info(f"[后台总评] 开始21维度面试评估: session_id={session_id}, invitation_id={invitation_id}")
        from agent.interview_evaluation_service import interview_evaluation_service

        evaluation_result = await interview_evaluation_service.evaluate_interview(
            session_id=session_id,
            invitation_id=invitation_id,
        )
        is_passed = evaluation_result.get("is_passed", 0)
        is_passed_text = {0: "未通过", 1: "通过", 2: "待定"}.get(is_passed, str(is_passed))
        logger.info(
            "[后台总评] 面试评估完成: "
            f"session_id={session_id}, invitation_id={invitation_id}, "
            f"总体得分={evaluation_result.get('overall_score', 0)}, 录用结论={is_passed_text}({is_passed})"
        )
        from app.services.dsw_evaluation_callbacks import schedule_baidu_asr_report_after_main_eval

        await schedule_baidu_asr_report_after_main_eval(invitation_id)
    except Exception as e:
        logger.error(f"[后台总评] 面试评估失败: session_id={session_id}, invitation_id={invitation_id}, error={e}", exc_info=True)


@router.post("/complete", response_model=InterviewResponse)
async def complete_interview(
    background_tasks: BackgroundTasks,
    session_id: str = Form(..., description="面试会话ID")
):
    """
    完成面试（候选人流程不依赖 current_user，仅凭 session_id 完成）
    
    - **session_id**: 面试会话ID
    """
    try:
        session = interview_session_service.get_session(session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="面试会话不存在或无权访问"
            )

        invitation_id = session.get("invitation_id") or ""

        # 已完成的会话：幂等响应，避免重复更新邀请与触发 21 维评估
        if session.get("status") == "COMPLETED":
            logger.info(f"会话已是 COMPLETED，跳过重复完成逻辑: session_id={session_id}")
            conversation_history = interview_session_service.get_conversation_history(session_id)
            return InterviewResponse(
                success=True,
                message="面试已完成",
                data={
                    "session_id": session_id,
                    "status": "completed",
                    "conversation_history": conversation_history,
                    "total_questions": len(conversation_history),
                    "evaluation_result": None,
                },
            )

        username = session.get("username") or session.get("invitation_id") or ""
        is_external_user = False
        
        if not interview_session_service.complete_session(
            session_id,
            username=username,
            is_external_user=is_external_user
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="会话状态不正确"
            )

        # 更新邀请状态为COMPLETED
        if invitation_id:
            try:
                from datetime import datetime
                from app.database.service import database_service
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

        # 21 维度总评耗时较长，放到响应后的后台任务，避免候选人端显示“保存中”等待几十秒。
        evaluation_result = None
        if invitation_id:
            background_tasks.add_task(_run_interview_evaluation_background, session_id, invitation_id)
            logger.info(f"已提交后台21维度面试评估任务: session_id={session_id}, invitation_id={invitation_id}")

        conversation_history = interview_session_service.get_conversation_history(session_id)
        
        return InterviewResponse(
            success=True,
            message="面试已完成",
            data={
                "session_id": session_id,
                "status": "completed",
                "conversation_history": conversation_history,
                "total_questions": len(conversation_history),
                "evaluation_result": evaluation_result
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"完成面试失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"完成面试失败: {str(e)}"
        )


# TTS功能已完全屏蔽，所有相关辅助函数已删除


async def _save_base64_audio(audio_data: str, audio_id: str) -> str:
    """保存Base64音频数据"""
    try:
        import base64
        
        # 创建音频存储目录
        audio_dir = Path("storage/answer_audio")
        audio_dir.mkdir(parents=True, exist_ok=True)
        
        # 解码Base64数据
        audio_bytes = base64.b64decode(audio_data)
        
        # 保存音频文件
        audio_path = audio_dir / f"{audio_id}.wav"
        async with aiofiles.open(audio_path, 'wb') as f:
            await f.write(audio_bytes)
        
        return str(audio_path)
    
    except Exception as e:
        logger.error(f"保存音频文件失败: {e}")
        raise


# TTS相关辅助函数已删除


async def _evaluate_answer_with_llm(
    question_text: str,
    candidate_answer: str,
    evaluation_points: Any,
    question_type: str = "BASIC",
    question_id: Optional[str] = None,
    difficulty: Optional[str] = None,
    standard_answer: Optional[str] = None
) -> dict:
    """
    使用LLM对候选人回答进行评分

    Args:
        question_text: 问题文本
        candidate_answer: 候选人回答
        evaluation_points: 评估要点（支持多种格式）
        question_type: 问题类型 ('BASIC', 'SPECIALTY')
        question_id: 问题ID（用于追溯）
        difficulty: 难度等级（预留扩展字段）
        standard_answer: 参考答案（仅专业题使用）

    Returns:
        评分结果字典
    """
    try:
        # 调用独立的评分服务
        evaluation_result = await evaluation_service.evaluate_answer(
            question_text=question_text,
            candidate_answer=candidate_answer,
            evaluation_points=evaluation_points,
            question_type=question_type,
            question_id=question_id,
            difficulty=difficulty,
            standard_answer=standard_answer
        )

        logger.info(f"LLM评分成功: question_id={question_id}, type={question_type}, "
                   f"得分={evaluation_result.get('score', 0)}, 等级={evaluation_result.get('grade', '未知')}")
        return evaluation_result

    except Exception as e:
        logger.error(f"LLM评分异常: question_id={question_id}, type={question_type}, error={e}")
        # 返回默认评分结果
        return evaluation_service._get_default_evaluation_result(
            error_message=f"评分系统异常: {str(e)}",
            question_id=question_id,
            question_type=question_type
        )

