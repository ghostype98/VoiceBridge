# -*- coding: utf-8 -*-
"""
Token管理器验证脚本
用于验证Token自动获取和刷新功能是否正常工作
"""

import time
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_interview_streaming.token_manager import ASRTokenManager
from shared.config.logging_config import get_logger

logger = get_logger(__name__)


def test_token_manager():
    """测试Token管理器"""
    
    # 从环境变量或配置文件读取AccessKey（这里使用示例值，实际使用时请替换）
    # 注意：实际使用时应该从配置文件读取
    access_key_id = os.getenv("ALIYUN_ACCESS_KEY_ID", "your_access_key_id")
    access_key_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "your_access_key_secret")
    region = os.getenv("ALIYUN_REGION", "cn-shanghai")
    
    if access_key_id == "your_access_key_id" or access_key_secret == "your_access_key_secret":
        logger.error("请设置环境变量 ALIYUN_ACCESS_KEY_ID 和 ALIYUN_ACCESS_KEY_SECRET")
        logger.info("或者修改此脚本，直接设置 access_key_id 和 access_key_secret")
        return False
    
    print("=" * 80)
    print("Token管理器验证测试")
    print("=" * 80)
    
    # 创建Token管理器
    print("\n1. 创建Token管理器...")
    token_manager = ASRTokenManager(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        region=region
    )
    
    # 测试1: 首次获取Token
    print("\n2. 测试1: 首次获取Token（应该看到获取新Token的日志）...")
    token1 = token_manager.get_token()
    if token1:
        print(f"✅ 成功获取Token: {token1[:30]}...")
    else:
        print("❌ 获取Token失败")
        return False
    
    # 测试2: 缓存验证（应该使用缓存的Token，不重新获取）
    print("\n3. 测试2: 缓存验证（应该使用缓存的Token，不重新获取）...")
    token2 = token_manager.get_token()
    if token2 == token1:
        print(f"✅ 使用缓存Token: {token2[:30]}...")
        print("✅ 缓存逻辑正常（第二次调用没有重新获取Token）")
    else:
        print("⚠️ Token不一致，可能触发了重新获取")
    
    # 测试3: 模拟过期（手动设置过期时间为过去）
    print("\n4. 测试3: 模拟Token过期（手动设置过期时间为过去）...")
    original_expire_time = token_manager.expire_time
    token_manager.expire_time = time.time() - 1  # 设置为1秒前过期
    
    print("   已手动设置Token为过期状态，再次获取Token...")
    token3 = token_manager.get_token()
    if token3 and token3 != token1:
        print(f"✅ Token已自动刷新: {token3[:30]}...")
        print("✅ 过期自动刷新逻辑正常")
    else:
        print("⚠️ Token未刷新，请检查过期判断逻辑")
    
    # 恢复原始过期时间（用于后续测试）
    token_manager.expire_time = original_expire_time
    
    # 测试4: 异常处理（使用错误的AccessKey）
    print("\n5. 测试4: 异常处理测试（使用错误的AccessKey）...")
    print("   创建使用错误AccessKey的Token管理器...")
    bad_token_manager = ASRTokenManager(
        access_key_id="wrong_key_id",
        access_key_secret="wrong_secret",
        region=region
    )
    bad_token = bad_token_manager.get_token()
    if bad_token is None:
        print("✅ 错误AccessKey正确处理，返回None")
    else:
        print("⚠️ 错误AccessKey未正确处理")
    
    print("\n" + "=" * 80)
    print("验证测试完成")
    print("=" * 80)
    
    return True


if __name__ == "__main__":
    try:
        success = test_token_manager()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        sys.exit(1)
    except Exception as e:
        logger.error(f"测试过程中发生错误: {str(e)}", exc_info=True)
        sys.exit(1)
