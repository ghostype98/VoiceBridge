# -*- coding: utf-8 -*-
"""
阿里云ASR Token管理器
实现Token的自动获取和刷新
"""

import time
import threading
from typing import Optional
from shared.config.logging_config import get_logger

logger = get_logger(__name__)


class ASRTokenManager:
    """阿里云ASR Token管理器
    
    自动管理Token的获取和刷新，确保Token始终有效
    """
    
    def __init__(self, access_key_id: str, access_key_secret: str, region: str = "cn-shanghai"):
        """
        初始化Token管理器
        
        参数:
            access_key_id: 阿里云AccessKey ID
            access_key_secret: 阿里云AccessKey Secret
            region: 服务地域，默认cn-shanghai
        """
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = region
        
        self.token: Optional[str] = None
        self.expire_time: int = 0  # 过期时间戳（秒）
        self._lock = threading.Lock()  # 线程锁，确保并发安全
        
        logger.info(f"ASR Token管理器初始化完成，地域: {region}")
    
    def get_token(self) -> Optional[str]:
        """
        获取有效的Token
        
        如果Token不存在或即将过期（提前10分钟），自动刷新
        返回:
            token: 有效的Token字符串，如果获取失败返回None
        """
        current_time = time.time()
        
        # 检查Token是否有效（提前10分钟刷新，确保安全）
        if self.token and current_time < (self.expire_time - 600):
            # 计算剩余有效时间
            remaining_seconds = self.expire_time - current_time
            remaining_hours = remaining_seconds / 3600
            logger.debug(f"使用缓存Token，剩余有效时间: {remaining_hours:.2f}小时 ({remaining_seconds:.0f}秒)")
            return self.token
        
        # 需要刷新Token
        logger.info("Token即将过期或不存在，开始获取新Token...")
        with self._lock:
            # 双重检查，避免并发时重复刷新
            if self.token and current_time < (self.expire_time - 600):
                remaining_seconds = self.expire_time - current_time
                remaining_hours = remaining_seconds / 3600
                logger.debug(f"其他线程已刷新Token，使用缓存Token，剩余有效时间: {remaining_hours:.2f}小时")
                return self.token
            
            return self._fetch_new_token()
    
    def _fetch_new_token(self) -> Optional[str]:
        """
        从阿里云获取新的Token
        
        返回:
            token: 新的Token字符串，如果获取失败返回None
        """
        try:
            # 尝试使用阿里云SDK
            try:
                from alibabacloud_nls_cloud_meta20190228.client import Client
                from alibabacloud_tea_openapi import models as open_api_models
                from alibabacloud_nls_cloud_meta20190228 import models as nls_models
                
                logger.info("🔑 使用阿里云SDK获取Token...")
                return self._fetch_token_with_sdk()
                
            except ImportError as e:
                # SDK未安装，使用HTTP请求方式
                logger.info("⚠️ SDK未安装，使用HTTP请求方式获取Token...")
                logger.debug(f"SDK导入错误: {str(e)}")
                return self._fetch_token_with_http()
                
        except Exception as e:
            logger.error(f"❌ 获取Token失败: {str(e)}", exc_info=True)
            return None
    
    def _fetch_token_with_sdk(self) -> Optional[str]:
        """使用阿里云官方SDK获取Token"""
        try:
            from alibabacloud_nls_cloud_meta20190228.client import Client as NlsClient
            from alibabacloud_tea_openapi import models as open_api_models
            
            # 1. 配置阿里云账号凭证
            config = open_api_models.Config(
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret
            )
            # 2. 设置访问域名（Endpoint）
            config.endpoint = f"nls-meta.{self.region}.aliyuncs.com"
            
            # 3. 初始化客户端
            client = NlsClient(config)
            
            # 4. 发送请求获取响应（create_token不需要Request参数）
            logger.info(f"正在通过 SDK 向阿里云 {config.endpoint} 发起 Token 请求...")
            response = client.create_token()
            
            # 6. 解析结果
            token_data = response.body.token
            self.token = token_data.id
            # expire_time 是秒级时间戳（10位数字，如1769545564）
            expire_time_value = token_data.expire_time
            
            # 阿里云返回的ExpireTime是秒级时间戳（10位）
            # 判断是否为毫秒级时间戳（13位或以上）
            if expire_time_value > 9999999999:  # 大于10位数字，可能是毫秒级
                # 毫秒级时间戳，转换为秒级
                self.expire_time = int(expire_time_value // 1000)
            else:
                # 秒级时间戳（10位），直接使用
                self.expire_time = int(expire_time_value)
            
            # 计算剩余有效时间
            current_time = time.time()
            remaining_seconds = self.expire_time - current_time
            remaining_hours = remaining_seconds / 3600
            
            # 使用datetime格式化时间，确保时区正确
            from datetime import datetime
            expire_date = datetime.fromtimestamp(self.expire_time).strftime('%Y-%m-%d %H:%M:%S')
            
            logger.info("=" * 60)
            logger.info("✅ 成功通过 SDK 获取 ASR Token")
            logger.info(f"Token: {self.token[:10]}...{self.token[-10:]}")
            logger.info(f"过期时间戳（秒）: {self.expire_time}")
            logger.info(f"过期时间: {expire_date}")
            logger.info(f"剩余有效时间: {remaining_hours:.2f}小时 ({remaining_seconds:.0f}秒)")
            logger.info("=" * 60)
            
            return self.token
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ SDK 获取 Token 失败: {error_msg}")
            
            # 提供详细的错误信息
            if "InvalidAccessKeyId" in error_msg or "access_key_id" in error_msg.lower():
                logger.error("可能的原因：AccessKey ID错误或不存在")
            elif "SignatureDoesNotMatch" in error_msg or "signature" in error_msg.lower():
                logger.error("可能的原因：AccessKey Secret错误")
            elif "Forbidden" in error_msg or "403" in error_msg:
                logger.error("可能的原因：AccessKey没有访问NLS服务的权限")
            else:
                logger.error("请检查：1. AccessKey是否正确 2. 是否有权限访问NLS服务 3. 网络连接是否正常")
            
            logger.debug("错误详情:", exc_info=True)
            # SDK失败时，尝试降级到HTTP方式
            logger.info("SDK获取失败，尝试使用HTTP请求方式...")
            return self._fetch_token_with_http()
    
    def _fetch_token_with_http(self) -> Optional[str]:
        """使用HTTP请求获取Token（备用方案）"""
        try:
            import requests
            import json
            import hmac
            import hashlib
            import base64
            from datetime import datetime
            from urllib.parse import quote
            
            logger.debug(f"正在连接阿里云Token服务: https://nls-meta.{self.region}.aliyuncs.com/")
            
            # 阿里云Token服务地址
            url = f"https://nls-meta.{self.region}.aliyuncs.com/"
            
            # 构建请求参数
            params = {
                "AccessKeyId": self.access_key_id,
                "Action": "CreateToken",
                "Format": "JSON",
                "RegionId": self.region,
                "SignatureMethod": "HMAC-SHA1",
                "SignatureNonce": str(int(time.time() * 1000)),
                "SignatureVersion": "1.0",
                "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Version": "2019-02-28"
            }
            
            # 构建签名字符串
            sorted_params = sorted(params.items())
            query_string = "&".join([f"{k}={quote(str(v), safe='')}" for k, v in sorted_params])
            string_to_sign = f"GET&%2F&{quote(query_string, safe='')}"
            
            # 计算签名
            secret = f"{self.access_key_secret}&"
            signature = base64.b64encode(
                hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
            ).decode()
            
            params["Signature"] = signature
            
            # 发送请求
            logger.debug("发送CreateToken HTTP请求...")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            result = response.json()
            logger.debug(f"收到Token响应: {json.dumps(result, ensure_ascii=False)[:200]}")
            
            if "Token" in result:
                token_info = result["Token"]
                self.token = token_info["Id"]
                # ExpireTime 是秒级时间戳（10位数字，如1769545564）
                expire_time_value = token_info.get("ExpireTime")
                if expire_time_value:
                    # 判断是否为毫秒级时间戳（13位或以上）
                    if expire_time_value > 9999999999:  # 大于10位数字，可能是毫秒级
                        # 毫秒级时间戳，转换为秒级
                        self.expire_time = int(expire_time_value // 1000)
                    else:
                        # 秒级时间戳（10位），直接使用
                        self.expire_time = int(expire_time_value)
                else:
                    # 默认1小时有效期
                    self.expire_time = int(time.time()) + 3600
                
                # 计算剩余有效时间
                current_time = time.time()
                remaining_seconds = self.expire_time - current_time
                remaining_hours = remaining_seconds / 3600
                
                # 使用datetime格式化时间，确保时区正确
                from datetime import datetime
                expire_date = datetime.fromtimestamp(self.expire_time).strftime('%Y-%m-%d %H:%M:%S')
                
                logger.info("=" * 60)
                logger.info("✅ 成功获取新ASR Token（使用HTTP请求）")
                logger.info(f"Token: {self.token[:20]}...{self.token[-10:]}")
                logger.info(f"过期时间戳（秒）: {self.expire_time}")
                logger.info(f"过期时间: {expire_date}")
                logger.info(f"剩余有效时间: {remaining_hours:.2f}小时 ({remaining_seconds:.0f}秒)")
                logger.info("=" * 60)
                
                return self.token
            else:
                logger.error(f"Token响应格式错误: {json.dumps(result, ensure_ascii=False)}")
                return None
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ 使用HTTP请求获取Token失败: {error_msg}")
            
            # 提供详细的错误信息
            if "InvalidAccessKeyId" in error_msg or "access_key_id" in error_msg.lower():
                logger.error("可能的原因：AccessKey ID错误或不存在")
            elif "SignatureDoesNotMatch" in error_msg or "signature" in error_msg.lower():
                logger.error("可能的原因：AccessKey Secret错误")
            elif "Forbidden" in error_msg or "403" in error_msg:
                logger.error("可能的原因：AccessKey没有访问NLS服务的权限")
            elif "timeout" in error_msg.lower():
                logger.error("可能的原因：网络连接超时，请检查网络")
            else:
                logger.error("请检查：1. AccessKey是否正确 2. 是否有权限访问NLS服务 3. 网络连接是否正常")
            
            logger.debug("错误详情:", exc_info=True)
            return None
    
    def is_token_valid(self) -> bool:
        """
        检查Token是否有效
        
        返回:
            bool: Token有效返回True，否则返回False
        """
        if not self.token:
            return False
        
        current_time = time.time()
        # 提前10分钟认为Token无效，需要刷新
        return current_time < (self.expire_time - 600)
    
    def force_refresh(self) -> Optional[str]:
        """
        强制刷新Token
        
        返回:
            token: 新的Token字符串，如果获取失败返回None
        """
        logger.info("强制刷新Token...")
        with self._lock:
            self.token = None
            self.expire_time = 0
            return self._fetch_new_token()
