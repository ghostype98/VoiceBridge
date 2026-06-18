#!/usr/bin/env python3
"""
调试脚本：查看评估阶段的输入内容
用于分析为什么输入tokens会超过限制
"""

import asyncio
import sys
import os
from typing import Dict, Any

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.service import database_service
from agent.interview_evaluation_service import interview_evaluation_service


async def debug_prompt_content(session_id: str, invitation_id: str):
    """
    调试：查看评估阶段的输入内容
    
    Args:
        session_id: 面试会话ID
        invitation_id: 面试邀请ID
    """
    print("=" * 80)
    print("🔍 调试：查看评估阶段的输入内容")
    print("=" * 80)
    print(f"📋 参数:")
    print(f"   session_id: {session_id}")
    print(f"   invitation_id: {invitation_id}")
    print()
    
    try:
        # 1. 获取面试数据
        print("🔍 步骤1: 获取面试数据...")
        interview_data = await interview_evaluation_service._get_complete_interview_data(session_id, invitation_id)
        
        formatted_report = interview_data.get('formatted_report', '')
        print(f"✅ 面试数据获取成功")
        print(f"   formatted_report长度: {len(formatted_report)} 字符")
        print()
        
        # 2. 获取岗位要求
        print("🔍 步骤2: 获取岗位要求...")
        job_requirements = await interview_evaluation_service._get_job_requirements(invitation_id)
        
        core_req = job_requirements.get('core_requirements') or ''
        jd_length = len(core_req) if core_req else 0
        print(f"✅ 岗位要求获取成功")
        print(f"   JD核心要求长度: {jd_length} 字符")
        print()
        
        # 3. 获取系统prompt
        system_prompt = interview_evaluation_service.prompt_template
        system_prompt_length = len(system_prompt)
        print(f"✅ 系统Prompt长度: {system_prompt_length} 字符")
        print()
        
        # 4. 构建所有阶段的prompt并保存
        print("🔍 步骤3: 构建所有阶段的prompt...")
        from config.settings import settings
        debug_file = os.path.join(settings.LOG_DIR_DEBUG_PROMPT, "debug_prompt_content.txt")
        
        max_context_length = getattr(interview_evaluation_service.llm_service, 'max_context_length', 4096)
        
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write("=" * 100 + "\n")
            f.write("评估阶段输入内容调试信息\n")
            f.write("=" * 100 + "\n\n")
            
            f.write("基本信息:\n")
            f.write("-" * 100 + "\n")
            f.write(f"Session ID: {session_id}\n")
            f.write(f"Invitation ID: {invitation_id}\n")
            f.write(f"模型上限: {max_context_length}\n")
            f.write(f"80%限制: {max_context_length * 0.8}\n")
            f.write(f"System Prompt长度: {system_prompt_length} 字符\n")
            f.write(f"Formatted Report长度: {len(formatted_report)} 字符\n")
            f.write(f"JD核心要求长度: {jd_length} 字符\n")
            f.write("\n\n")
            
            # 保存formatted_report的完整内容
            f.write("=" * 100 + "\n")
            f.write("Formatted Report 完整内容\n")
            f.write("=" * 100 + "\n")
            f.write(formatted_report)
            f.write("\n\n")
            
            # 保存JD内容
            if job_requirements.get('core_requirements'):
                f.write("=" * 100 + "\n")
                f.write("岗位JD核心要求\n")
                f.write("=" * 100 + "\n")
                f.write(job_requirements.get('core_requirements', ''))
                f.write("\n\n")
            
            # 保存System Prompt
            f.write("=" * 100 + "\n")
            f.write("System Prompt 完整内容\n")
            f.write("=" * 100 + "\n")
            f.write(system_prompt)
            f.write("\n\n")
            
            # 构建并保存所有阶段的User Prompt
            all_dimensions = list(interview_evaluation_service.dimensions_config.items())
            
            f.write("=" * 100 + "\n")
            f.write("所有阶段的User Prompt\n")
            f.write("=" * 100 + "\n\n")
            
            for stage_num, (dim_key, dim_config) in enumerate(all_dimensions, 1):
                dim_name = dim_config['name']
                prompt = interview_evaluation_service._build_evaluation_prompt(
                    interview_data, 
                    job_requirements, 
                    dimension_key=dim_key
                )
                user_prompt_length = len(prompt)
                estimated_input_tokens = int((system_prompt_length + user_prompt_length) * 1.5)
                
                f.write("=" * 100 + "\n")
                f.write(f"第{stage_num}阶段 - {dim_name}\n")
                f.write("=" * 100 + "\n")
                f.write(f"User Prompt长度: {user_prompt_length} 字符\n")
                f.write(f"估算输入tokens: {estimated_input_tokens}\n")
                f.write(f"是否超限: {'是' if estimated_input_tokens > max_context_length * 0.8 else '否'}\n")
                f.write("-" * 100 + "\n")
                f.write(prompt)
                f.write("\n\n")
        
        print(f"✅ 所有输入内容已保存到: {debug_file}")
        print()
        
        # 5. 显示统计信息
        print("=" * 80)
        print("📊 统计信息")
        print("=" * 80)
        print(f"System Prompt: {system_prompt_length} 字符")
        print(f"Formatted Report: {len(formatted_report)} 字符")
        print(f"JD核心要求: {jd_length} 字符")
        print(f"模型上限: {max_context_length}")
        print(f"80%限制: {max_context_length * 0.8}")
        print()
        
        # 显示每个阶段的token估算
        print("各阶段token估算:")
        print(f"{'阶段':<5} {'维度名称':<25} {'User Prompt长度':<20} {'估算tokens':<15} {'是否超限':<10}")
        print("-" * 80)
        
        for stage_num, (dim_key, dim_config) in enumerate(all_dimensions, 1):
            dim_name = dim_config['name']
            prompt = interview_evaluation_service._build_evaluation_prompt(
                interview_data, 
                job_requirements, 
                dimension_key=dim_key
            )
            user_prompt_length = len(prompt)
            estimated_input_tokens = int((system_prompt_length + user_prompt_length) * 1.5)
            is_over = "是" if estimated_input_tokens > max_context_length * 0.8 else "否"
            
            print(f"{stage_num:<5} {dim_name:<25} {user_prompt_length:<20} {estimated_input_tokens:<15} {is_over:<10}")
        
        print()
        print("=" * 80)
        print("✅ 调试完成！")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        print(f"\n❌ 调试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主函数"""
    # 使用测试数据
    session_id = "4583a033-008d-4d56-acf1-34f2aca838f9"
    invitation_id = "INV_20260128105253_C4C97511"
    
    success = await debug_prompt_content(session_id, invitation_id)
    
    if success:
        print("\n✅ 调试完成！")
        return 0
    else:
        print("\n❌ 调试失败！")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

