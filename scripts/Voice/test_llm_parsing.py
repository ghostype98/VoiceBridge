#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试LLM响应解析
"""

import json
from typing import Dict, Any, Union

def parse_llm_response(response: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """测试解析函数"""
    try:
        if not response:
            return {'score': 60, 'reason': 'LLM响应为空', 'details': {}}
        
        # 如果response是字典，提取content字段
        if isinstance(response, dict):
            if 'content' in response:
                content = response['content']
                print(f"提取content: {content[:100]}...")
                
                if isinstance(content, str):
                    content = content.strip()
                    if content.startswith('```json'):
                        content = content[7:]
                    if content.startswith('```'):
                        content = content[3:]
                    if content.endswith('```'):
                        content = content[:-3]
                    content = content.strip()
                    
                    result = json.loads(content)
                elif isinstance(content, dict):
                    result = content
                else:
                    raise ValueError(f"content类型异常: {type(content)}")
                
                score = result.get('score', 0)
                if not isinstance(score, (int, float)):
                    score = float(score)
                
                if score == 0:
                    reason = result.get('reason', '')
                    if not reason or len(reason) < 10:
                        score = 60
                
                return {
                    'score': score,
                    'reason': result.get('reason', ''),
                    'details': result.get('dimensions', {})
                }
        
        return {'score': 60, 'reason': '解析失败', 'details': {}}
        
    except Exception as e:
        print(f"解析失败: {e}")
        return {'score': 60, 'reason': f'解析失败: {e}', 'details': {}}

# 测试用例
test_cases = [
    # 测试1: LLM服务返回的标准格式
    {
        "name": "标准LLM服务响应",
        "input": {
            "content": '{"score": 85, "reason": "回答完整", "dimensions": {"content": 20}}',
            "usage": {"total_tokens": 100},
            "model": "qwen",
            "finish_reason": "stop"
        },
        "expected_score": 85
    },
    # 测试2: content是字典
    {
        "name": "content是字典",
        "input": {
            "content": {"score": 75, "reason": "回答良好", "dimensions": {}},
            "usage": {},
            "model": "qwen"
        },
        "expected_score": 75
    },
    # 测试3: 评分为0的情况
    {
        "name": "评分为0",
        "input": {
            "content": '{"score": 0, "reason": "", "dimensions": {}}',
            "usage": {},
            "model": "qwen"
        },
        "expected_score": 60  # 应该使用默认值
    },
    # 测试4: 字符串响应
    {
        "name": "字符串响应",
        "input": '{"score": 90, "reason": "优秀", "dimensions": {}}',
        "expected_score": 90
    }
]

print("="*80)
print("测试LLM响应解析")
print("="*80)

for i, test_case in enumerate(test_cases, 1):
    print(f"\n测试 {i}: {test_case['name']}")
    result = parse_llm_response(test_case['input'])
    print(f"输入: {str(test_case['input'])[:100]}...")
    print(f"输出: {result}")
    print(f"期望分数: {test_case['expected_score']}, 实际分数: {result['score']}")
    
    if result['score'] == test_case['expected_score']:
        print("✅ 通过")
    else:
        print("❌ 失败")

print("\n" + "="*80)
print("测试完成")
print("="*80)
