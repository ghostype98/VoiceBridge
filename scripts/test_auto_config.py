#!/usr/bin/env python3
"""
测试自动配置获取功能
验证ConfigManager是否能正确从不同源获取配置
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import ConfigManager, Settings


def test_config_manager():
    """测试ConfigManager基本功能"""
    print("🔧 测试ConfigManager基本功能...")

    # 创建配置管理器
    config_manager = ConfigManager()

    # 测试获取不存在的配置
    value = config_manager.get_config_value("nonexistent.key", "default")
    assert value == "default", f"预期 'default'，实际 '{value}'"
    print("✅ 获取不存在配置的默认值测试通过")

    # 测试强制刷新
    result = config_manager.force_refresh()
    assert result == True, "强制刷新应该成功"
    print("✅ 强制刷新测试通过")


def test_settings_integration():
    """测试Settings与自动配置的集成"""
    print("\n🔧 测试Settings集成...")

    # 设置测试环境变量（Token获取只需要AK/SK）
    test_access_key = "test_access_key_123"
    test_secret = "test_secret_456"

    os.environ["ALIYUN_ACCESS_KEY_ID"] = test_access_key
    os.environ["ALIYUN_ACCESS_KEY_SECRET"] = test_secret

    try:
        # 创建Settings实例
        settings = Settings()

        # 验证配置是否正确获取
        assert settings.ALIYUN_ASR_ACCESS_KEY_ID == test_access_key, \
            f"Access Key ID不匹配: 预期 '{test_access_key}'，实际 '{settings.ALIYUN_ASR_ACCESS_KEY_ID}'"
        assert settings.ALIYUN_ASR_ACCESS_KEY_SECRET == test_secret, \
            f"Access Key Secret不匹配: 预期 '{test_secret}'，实际 '{settings.ALIYUN_ASR_ACCESS_KEY_SECRET}'"

        # AppKey应该从配置文件获取（不通过环境变量自动获取）
        expected_appkey = "your_aliyun_nls_appkey"  # config.example.yaml 中的占位符
        assert settings.ALIYUN_ASR_APPKEY == expected_appkey, \
            f"AppKey不匹配: 预期 '{expected_appkey}'，实际 '{settings.ALIYUN_ASR_APPKEY}'"

        print("✅ 环境变量配置获取测试通过")

        # 测试刷新功能
        result = settings.refresh_auto_config()
        assert result == True, "配置刷新应该成功"
        print("✅ 配置刷新测试通过")

    finally:
        # 清理环境变量
        del os.environ["ALIYUN_ACCESS_KEY_ID"]
        del os.environ["ALIYUN_ACCESS_KEY_SECRET"]


def test_credentials_file():
    """测试从credentials.yaml文件获取配置"""
    print("\n🔧 测试credentials.yaml文件配置...")

    # 创建测试credentials文件
    test_credentials = {
        "voice_streaming": {
            "asr": {
                "appkey": "file_appkey_123",
                "access_key_id": "file_access_key_456",
                "access_key_secret": "file_secret_789"
            }
        }
    }

    import yaml
    test_file = project_root / "config" / "test_credentials.yaml"
    with open(test_file, 'w', encoding='utf-8') as f:
        yaml.dump(test_credentials, f)

    try:
        # 修改配置管理器指向测试文件
        config_manager = ConfigManager()

        # 手动设置配置源为测试文件
        config_manager.config_sources = [{
            "type": "file",
            "enabled": True,
            "path": str(test_file),
            "format": "yaml"
        }]

        # 强制刷新以加载文件配置
        config_manager.force_refresh()

        # 测试获取配置
        appkey = config_manager.get_config_value("voice_streaming.asr.appkey")
        access_key = config_manager.get_config_value("voice_streaming.asr.access_key_id")
        secret = config_manager.get_config_value("voice_streaming.asr.access_key_secret")

        assert appkey == "file_appkey_123", f"文件配置获取失败: {appkey}"
        assert access_key == "file_access_key_456", f"文件配置获取失败: {access_key}"
        assert secret == "file_secret_789", f"文件配置获取失败: {secret}"

        print("✅ 文件配置获取测试通过")

    finally:
        # 清理测试文件
        if test_file.exists():
            test_file.unlink()


def main():
    """主测试函数"""
    print("🚀 开始测试自动配置获取功能...")
    print("=" * 50)

    try:
        test_config_manager()
        test_settings_integration()
        test_credentials_file()

        print("\n" + "=" * 50)
        print("🎉 所有测试通过！自动配置获取功能正常工作。")
        return 0

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())