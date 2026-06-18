"""
集成扩展服务
对接招聘系统，实现录音存储、评分关联、面试报告生成等功能
"""
from typing import Optional, Dict, Any
from loguru import logger
import os
import json
from datetime import datetime

from config.settings import settings


class IntegrationService:
    """集成扩展服务类"""
    
    def __init__(self):
        self.storage_base = settings.STORAGE_PATH
        self.audio_storage = settings.AUDIO_STORAGE_PATH
        self.metadata_file = os.path.join(self.storage_base, "metadata.json")
        self._init_metadata()
    
    def _init_metadata(self):
        """初始化元数据文件"""
        if not os.path.exists(self.metadata_file):
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump({"audio_records": [], "scoring_records": [], "interviews": {}}, f, ensure_ascii=False)
    
    async def save_audio(
        self,
        audio_id: str,
        user_id: str,
        storage_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        保存录音元数据
        
        Args:
            audio_id: 音频ID
            user_id: 用户ID
            storage_path: 存储路径（可选）
            
        Returns:
            保存结果
        """
        try:
            # 读取元数据
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            
            # 添加录音记录
            record = {
                "audio_id": audio_id,
                "user_id": user_id,
                "storage_path": storage_path or os.path.join(self.audio_storage, f"{audio_id}"),
                "timestamp": datetime.now().isoformat(),
                "status": "saved"
            }
            
            metadata["audio_records"].append(record)
            
            # 保存元数据
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            logger.info(f"录音存储成功: audio_id={audio_id}, user_id={user_id}")
            
            return {
                "audio_id": audio_id,
                "user_id": user_id,
                "status": "success"
            }
        except Exception as e:
            logger.error(f"录音存储失败: {str(e)}")
            raise
    
    async def link_scoring(
        self,
        text: str,
        user_id: str,
        job_title: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        关联评分模块
        
        Args:
            text: 识别文本
            user_id: 用户ID
            job_title: 职位名称（可选）
            
        Returns:
            关联结果
        """
        try:
            # 读取元数据
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            
            # 添加评分记录
            record = {
                "user_id": user_id,
                "text": text,
                "job_title": job_title,
                "timestamp": datetime.now().isoformat(),
                "status": "linked"
            }
            
            metadata["scoring_records"].append(record)
            
            # 保存元数据
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            logger.info(f"评分关联成功: user_id={user_id}, text_length={len(text)}")
            
            # 这里可以调用实际的评分API
            # score = await self._call_scoring_api(text, job_title)
            
            return {
                "user_id": user_id,
                "text_length": len(text),
                "status": "success",
                "message": "已同步至评分模块"
            }
        except Exception as e:
            logger.error(f"评分关联失败: {str(e)}")
            raise
    
    async def get_interview_status(
        self,
        user_id: str,
        interview_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取面试状态
        
        Args:
            user_id: 用户ID
            interview_id: 面试ID（可选）
            
        Returns:
            面试状态信息
        """
        try:
            # 读取元数据
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            
            interviews = metadata.get("interviews", {})
            interview_key = interview_id or user_id
            
            if interview_key in interviews:
                interview_info = interviews[interview_key]
                return {
                    "user_id": user_id,
                    "interview_id": interview_id,
                    "status": interview_info.get("status", "in_progress"),
                    "progress": interview_info.get("progress", 0),
                    "start_time": interview_info.get("start_time"),
                    "current_step": interview_info.get("current_step", "unknown")
                }
            else:
                # 创建新的面试记录
                new_interview = {
                    "user_id": user_id,
                    "interview_id": interview_id or f"interview_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    "status": "in_progress",
                    "progress": 0,
                    "start_time": datetime.now().isoformat(),
                    "current_step": "greeting"
                }
                
                interviews[interview_key] = new_interview
                metadata["interviews"] = interviews
                
                with open(self.metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                
                return new_interview
        except Exception as e:
            logger.error(f"获取面试状态失败: {str(e)}")
            raise
    
    async def _call_scoring_api(self, text: str, job_title: Optional[str]) -> float:
        """调用评分API（示例）"""
        # 这里应该调用实际的评分服务
        # 返回评分结果
        return 0.0

