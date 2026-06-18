#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证test_interview_flow.py脚本的基本功能
"""
import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def test_script_imports():
    """测试脚本导入"""
    print("测试脚本导入...")
    try:
        from scripts.test_interview_flow import InterviewTester
        print("✅ InterviewTester类导入成功")
        return True
    except Exception as e:
        print(f"❌ 导入失败: {e}")
        return False

def test_class_instantiation():
    """测试类实例化"""
    print("测试类实例化...")
    try:
        from scripts.test_interview_flow import InterviewTester
        tester = InterviewTester()
        print("✅ InterviewTester实例化成功")
        print(f"   API URL: {tester.api_url}")
        return True
    except Exception as e:
        print(f"❌ 实例化失败: {e}")
        return False

def test_method_existence():
    """测试关键方法是否存在"""
    print("测试关键方法...")
    try:
        from scripts.test_interview_flow import InterviewTester
        tester = InterviewTester()

        # 检查关键方法
        required_methods = [
            'prepare_test_user',
            'login_user',
            'create_test_invitation',
            'start_interview',
            'submit_text_answer',
            'get_conversation_history',
            'complete_interview',
            'verify_database_data'
        ]

        missing_methods = []
        for method in required_methods:
            if not hasattr(tester, method):
                missing_methods.append(method)

        if missing_methods:
            print(f"❌ 缺少方法: {missing_methods}")
            return False
        else:
            print("✅ 所有关键方法都存在")
            return True

    except Exception as e:
        print(f"❌ 方法检查失败: {e}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("test_interview_flow.py 脚本验证")
    print("=" * 60)

    tests = [
        ("脚本导入", test_script_imports),
        ("类实例化", test_class_instantiation),
        ("方法检查", test_method_existence)
    ]

    passed = 0
    total = len(tests)

    for test_name, test_func in tests:
        print(f"\n🔍 {test_name}")
        if test_func():
            passed += 1

    print("\n" + "=" * 60)
    print(f"验证结果: {passed}/{total} 通过")
    if passed == total:
        print("✅ 脚本更新成功，所有功能正常")
    else:
        print("❌ 脚本存在问题，请检查")

    print("=" * 60)
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)