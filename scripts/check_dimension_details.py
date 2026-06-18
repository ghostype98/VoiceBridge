#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.connection import DatabaseManager
import json

db_manager = DatabaseManager()
query = '''
SELECT dimension_details 
FROM interview_evaluation_record 
WHERE invitation_id = %s 
ORDER BY create_time DESC
LIMIT 3
'''
try:
    results = db_manager.execute_query(query, ('INV_20260209135052_BC113F8F',))
    for idx, row in enumerate(results, 1):
        print(f'=== 记录 {idx} ===')
        dim_details = row.get('dimension_details', {})
        if isinstance(dim_details, str):
            dim_details = json.loads(dim_details)
        for dim_name, details in list(dim_details.items())[:5]:  # 显示前5个维度
            reasoning = details.get('reasoning', '')
            print(f'\n{dim_name}:')
            print(f'reasoning: {reasoning}')
            print('-' * 80)
except Exception as e:
    print(f'查询失败: {e}')
    import traceback
    traceback.print_exc()
finally:
    if hasattr(db_manager, '_connection_pool') and db_manager._connection_pool:
        db_manager._connection_pool.closeall()



