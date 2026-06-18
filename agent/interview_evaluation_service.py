"""
面试评估服务
基于21维度对完整面试问答记录进行综合评估
包含问题内容、评估要点、候选人回答的全面分析
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from loguru import logger

from app.services.llm_service import LLMService
from app.database.service import database_service
from config.settings import settings

# 与 prompts/interview_evaluation_system_prompt.md 保持同步；文件不可读时保证与线上一致（无「三阶定级」松散 fallback）
_INTERVIEW_EVALUATION_SYSTEM_PROMPT_FALLBACK = """# 面试评估专家 (指令集)

## 受众与表达
- 推理与评分面向**业务面试官**：用语通俗、职业场景化（如：技术深度、沟通协作、项目落地），避免学术腔与空话。
- `reasoning` 应用**简短总结性表述**（建议 120～200 字内说清依据），不要展开长推理链；禁止把「思考过程」写进输出。

## 核心逻辑
1. **质量检测规则**: 
   - 若回答字数极少（<10字）或内容为空 -> 直接给20分，reasoning 中写「作答过短，难以判断」
   - 若摘要为空或所有题目均无评分 -> 直接0分
2. **按维度证据打分（不设「优/良/差」等硬档位）**:
   - 每一维度的得分必须可追溯到摘要中的**具体引语或事实**，禁止仅凭笼统印象或「态度积极」给中高分。
   - 若摘要中多处暴露 **TAR 缺失**（尤其缺少可核查的 **Action**：具体工具、步骤、数据、案例、报错与处置等），相关维度必须如实体现负面判断；**严禁在大报告中弱化、一笔带过或换辞藻「美化」单题已呈现的不足**。
   - 分数差异应来自证据强弱，而非无依据的随机扰动；避免连续多个维度无理由同分。

## 与单题评分一致（禁止「大报告圆场」）
- 摘要中若**多数专业题得分明显偏低**或大量「未展开 / 读题式」作答，相关维度（尤其专业深度、逻辑与沟通质量）**不得**用「整体尚可」「有一定潜力」等话术把分数抬到与单题证据不相称的区间。
- **禁止**为候选人「找补」：没有引用到具体作答证据的褒义结论一律视为无效，须改写为保守、可核验的表述并相应降分。
- 各维度 `score` 须能被本维度 `reasoning` 中的**短引语+分析**支撑；证据薄弱则宁可给低分，不可迁就总分观感。

## 严禁输出的模板句（违者不合格）
- 禁止出现「未找到与××维度相关的题目」「未找到任何题目」「未找到与某维度相关的题目」等**工程/筛选说明类**句式。
- 禁止出现「根据题目编号#1…」等**固定占位编号**（须用摘要中真实题号）。
- 信息不足时：用「本题作答中该维度可依据的信息较少，已保守给分」等**中性业务表述**，不要重复堆砌「信息不足」。

## 评估流程
通读面试记录 → 逐维度分析 → 提取证据 → 量化评分 → **简短**写明依据 → 输出 JSON。

## 证据与 reasoning 格式
reasoning 须包含：引用要点（可用双引号包裹短引语）+ 与维度的关系 + 给分理由；**篇幅宜短**，避免罗列多个无关点。

**格式示例**：#2+"引用片段"+简要分析+评分依据（题号与摘要一致）

## 输出格式（单阶段仅维度时）
仅返回合法 JSON，格式如下：
{
  "dimension_scores": { "维度名": 分数 },
  "dimension_details": {
    "维度名": { "score": 分数, "reasoning": "题号+引用+简要分析+依据" }
  }
}
""" + "\n\n**重要**：reasoning 内双引号须正确转义为 " + r'\\"' + "，保证 JSON 可解析。\n"


class InterviewEvaluationService:
    """面试评估服务 - 21维度综合评估"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """初始化面试评估服务

        Args:
            config: 配置参数，包含面试评估阈值等
        """
        self.config = config or {}
        # 从配置中获取面试评估通过阈值，默认为60分
        _eval_cfg = self.config.get('evaluation') or {}
        self.interview_pass_threshold = _eval_cfg.get('interview_pass_threshold', 60)
        _mode = _eval_cfg.get('interview_pass_status_mode', 'threshold')
        self.interview_pass_status_mode = (
            _mode.strip().lower() if isinstance(_mode, str) else 'threshold'
        )
        if self.interview_pass_status_mode not in ('threshold', 'pending'):
            logger.warning(
                f"未知 interview_pass_status_mode={self.interview_pass_status_mode!r}，回退为 threshold"
            )
            self.interview_pass_status_mode = 'threshold'
        
        # 初始化LLM服务
        self.llm_service = LLMService()
        
        # 初始化维度配置（使用类属性）
        self.dimensions_config = self.INTERVIEW_EVALUATION_DIMENSIONS
        
        # 加载评估提示词模板
        self.prompt_template = self._load_evaluation_prompt()
        
        logger.info(
            f"面试评估服务初始化完成，通过阈值: {self.interview_pass_threshold}分，"
            f"录用结论模式: {self.interview_pass_status_mode}"
        )

    # 代码中的维度key到evaluation_dimension表中dimension_name的映射
    # 确保严格对应evaluation_dimension表中的dimension_name字段
    DIMENSION_NAME_MAPPING = {
        "learning_adaptability": "学习与适应能力",
        "communication_collaboration": "沟通与协作能力",
        "responsibility_commitment": "责任心与敬业度",
        "logical_thinking": "逻辑思维与问题解决能力",
        "stress_resistance": "抗压性与稳定性",
        "technical_depth": "技术栈掌握深度",
        "business_knowledge": "业务领域知识",
        "tool_proficiency": "专业工具/系统熟练度",
        "project_experience": "项目/工作经验",
        "education_match": "学历与专业匹配度",
        "experience_years": "工作经验年限符合度",
        "certification_match": "资格证书要求",
        "location_flexibility": "工作地点与出差接受度",
        "salary_expectation": "薪资期望匹配度",
        "self_motivation": "成就动机与自驱力",
        "innovation_consciousness": "创新与改进意识",
        "service_orientation": "客户服务导向",
        "leadership_potential": "团队领导潜力",
        "interview_performance": "简历质量与完整性",
        "career_development": "职业发展规划",
        "overall_fit": "综合匹配度",
    }
    
    # 维度到题目分类的映射关系（用于过滤相关题目）
    # 每个维度只评估与其相关的题目，避免LLM"复用式推理"
    DIMENSION_TO_QUESTION_CATEGORIES = {
        # 基础素质层面
        "learning_adaptability": ["自驱力与学习能力"],  # 学习与适应能力
        "communication_collaboration": ["沟通与协作能力"],  # 沟通与协作能力
        "responsibility_commitment": ["自驱力与学习能力"],  # 责任心与敬业度
        "logical_thinking": ["抗压与思维品质", "综合方案设计"],  # 逻辑思维与问题解决能力
        "stress_resistance": ["抗压与思维品质", "职业意愿与稳定性"],  # 抗压性与稳定性
        
        # 专业能力层面
        "technical_depth": ["技术栈深度", "综合方案设计"],  # 技术栈掌握深度
        "business_knowledge": ["业务与领域知识"],  # 业务领域知识
        "tool_proficiency": ["工具与系统操作"],  # 专业工具/系统熟练度
        "project_experience": ["项目实战经验"],  # 项目/工作经验
        
        # 岗位匹配层面
        "education_match": ["技术栈深度"],  # 学历与专业匹配度
        "experience_years": ["项目实战经验", "职业意愿与稳定性"],  # 工作经验年限符合度
        "certification_match": ["技术栈深度"],  # 资格证书要求
        "location_flexibility": ["职业意愿与稳定性"],  # 工作地点与出差接受度
        "salary_expectation": ["职业意愿与稳定性"],  # 薪资期望匹配度
        
        # 潜力适配层面
        "self_motivation": ["自驱力与学习能力"],  # 成就动机与自驱力
        "innovation_consciousness": ["综合方案设计"],  # 创新与改进意识
        "service_orientation": ["沟通与协作能力"],  # 客户服务导向
        "leadership_potential": ["团队领导力"],  # 团队领导潜力
        
        # 通用评估层面
        "interview_performance": [],  # 简历质量与完整性（基于所有题目综合评估）
        "career_development": ["职业意愿与稳定性"],  # 职业发展规划
        "overall_fit": ["职业意愿与稳定性", "故障排查", "架构设计"],  # 综合匹配度
    }
    
    # 21维度评估配置（按分组，7组，每组3个维度）
    INTERVIEW_EVALUATION_DIMENSIONS = {
        # 组1：基础素质（3个）
        "learning_adaptability": {
            "name": "学习与适应能力",
            "category": "基础素质层面",
            "description": "学习新知识、新技术的速度和适应变化的能力",
            "importance_level": "弹性",
            "group": 1
        },
        "communication_collaboration": {
            "name": "沟通与协作能力",
            "category": "基础素质层面",
            "description": "表达能力、倾听能力和团队协作能力",
            "importance_level": "弹性",
            "group": 1
        },
        "responsibility_commitment": {
            "name": "责任心与敬业度",
            "category": "基础素质层面",
            "description": "对工作的责任心和敬业态度",
            "importance_level": "弹性",
            "group": 1
        },
        
        # 组2：思维能力+技术深度（3个）
        "logical_thinking": {
            "name": "逻辑思维与问题解决能力",
            "category": "基础素质层面",
            "description": "分析问题、逻辑推理和解决问题的能力",
            "importance_level": "必备",
            "group": 2
        },
        "stress_resistance": {
            "name": "抗压性与稳定性",
            "category": "基础素质层面",
            "description": "面对压力时的应对能力和情绪稳定性",
            "importance_level": "弹性",
            "group": 2
        },
        "technical_depth": {
            "name": "技术栈掌握深度",
            "category": "专业能力层面",
            "description": "对核心技术的理解深度和应用能力",
            "importance_level": "必备",
            "group": 2
        },
        
        # 组3：专业能力（3个）
        "business_knowledge": {
            "name": "业务领域知识",
            "category": "专业能力层面",
            "description": "对业务领域的理解和专业知识掌握",
            "importance_level": "弹性",
            "group": 3
        },
        "tool_proficiency": {
            "name": "专业工具/系统熟练度",
            "category": "专业能力层面",
            "description": "对专业工具和系统的熟练程度",
            "importance_level": "弹性",
            "group": 3
        },
        "project_experience": {
            "name": "项目/工作经验",
            "category": "专业能力层面",
            "description": "实际项目经验的丰富程度和复杂度",
            "importance_level": "弹性",
            "group": 3
        },
        
        # 组4：岗位匹配基础（3个）
        "education_match": {
            "name": "学历与专业匹配度",
            "category": "岗位匹配层面",
            "description": "学历背景与岗位要求的匹配程度",
            "importance_level": "弹性",
            "group": 4
        },
        "experience_years": {
            "name": "工作经验年限符合度",
            "category": "岗位匹配层面",
            "description": "工作年限与岗位要求的匹配程度",
            "importance_level": "弹性",
            "group": 4
        },
        "certification_match": {
            "name": "资格证书要求",
            "category": "岗位匹配层面",
            "description": "相关证书与岗位要求的匹配程度",
            "importance_level": "不重要",
            "group": 4
        },
        
        # 组5：岗位匹配+动机（3个）
        "location_flexibility": {
            "name": "工作地点与出差接受度",
            "category": "岗位匹配层面",
            "description": "对工作地点和出差的接受程度",
            "importance_level": "不重要",
            "group": 5
        },
        "salary_expectation": {
            "name": "薪资期望匹配度",
            "category": "岗位匹配层面",
            "description": "薪资期望与市场水平和能力匹配的合理性",
            "importance_level": "不重要",
            "group": 5
        },
        "self_motivation": {
            "name": "成就动机与自驱力",
            "category": "潜力适配层面",
            "description": "内在动力和自我驱动力",
            "importance_level": "弹性",
            "group": 5
        },
        
        # 组6：潜力适配（3个）
        "innovation_consciousness": {
            "name": "创新与改进意识",
            "category": "潜力适配层面",
            "description": "创新思维和持续改进的意识",
            "importance_level": "弹性",
            "group": 6
        },
        "service_orientation": {
            "name": "客户服务导向",
            "category": "潜力适配层面",
            "description": "以客户为中心的服务意识",
            "importance_level": "弹性",
            "group": 6
        },
        "leadership_potential": {
            "name": "团队领导潜力",
            "category": "潜力适配层面",
            "description": "领导能力和团队管理潜力",
            "importance_level": "弹性",
            "group": 6
        },
        
        # 组7：通用评估+录用建议（3个）
        "interview_performance": {
            "name": "简历质量与完整性",
            "category": "通用评估层面",
            "description": "简历的完整性、规范性、信息真实性和表达清晰度",
            "importance_level": "弹性",
            "group": 7
        },
        "career_development": {
            "name": "职业发展规划",
            "category": "通用评估层面",
            "description": "职业规划的清晰度和合理性",
            "importance_level": "弹性",
            "group": 7
        },
        "overall_fit": {
            "name": "综合匹配度",
            "category": "通用评估层面",
            "description": "综合能力和岗位要求的匹配程度",
            "importance_level": "必备",
            "group": 7
        }
    }

    # 21 细维度 → 8 个「面试官友好」展示标签（归并展示，非改评分逻辑）
    INTERVIEWER_FRIENDLY_LABEL_BY_DIMENSION = {
        "学习与适应能力": "动机与发展",
        "责任心与敬业度": "动机与发展",
        "成就动机与自驱力": "动机与发展",
        "职业发展规划": "动机与发展",
        "沟通与协作能力": "协作与影响",
        "客户服务导向": "协作与影响",
        "团队领导潜力": "协作与影响",
        "逻辑思维与问题解决能力": "思维与稳定",
        "抗压性与稳定性": "思维与稳定",
        "技术栈掌握深度": "专业硬实力",
        "业务领域知识": "专业硬实力",
        "专业工具/系统熟练度": "专业硬实力",
        "项目/工作经验": "专业硬实力",
        "学历与专业匹配度": "岗位匹配",
        "工作经验年限符合度": "岗位匹配",
        "资格证书要求": "岗位匹配",
        "工作地点与出差接受度": "岗位匹配",
        "薪资期望匹配度": "岗位匹配",
        "创新与改进意识": "潜力与创新",
        "简历质量与完整性": "材料与表达",
        "综合匹配度": "综合匹配",
    }

    _INFO_INSUFFICIENT_KEYWORDS = (
        '信息不足', '信息不够', '未提供', '未涉及', '未提及',
        '无法判断', '无法准确判断', '无法直接判断', '无法明确',
        '缺乏相关信息', '缺乏信息', '没有相关信息', '没有信息',
        '无法从中获取', '无法获取', '无法得知', '无法了解',
        '未具体说明', '未详细说明', '未说明', '未描述',
        '信息量不足', '信息不完整', '信息缺失',
    )

    # 21 维 → 表格「归并分类」（与前端 el-table 列一致）
    DIMENSION_TABLE_CATEGORY_BY_DIMENSION = {
        "技术栈掌握深度": "技术面",
        "业务领域知识": "技术面",
        "专业工具/系统熟练度": "技术面",
        "项目/工作经验": "技术面",
        "学习与适应能力": "基础素质",
        "沟通与协作能力": "基础素质",
        "责任心与敬业度": "基础素质",
        "逻辑思维与问题解决能力": "基础素质",
        "抗压性与稳定性": "基础素质",
        "学历与专业匹配度": "岗位匹配",
        "工作经验年限符合度": "岗位匹配",
        "资格证书要求": "岗位匹配",
        "工作地点与出差接受度": "岗位匹配",
        "薪资期望匹配度": "岗位匹配",
        "成就动机与自驱力": "潜力与发展",
        "创新与改进意识": "潜力与发展",
        "客户服务导向": "潜力与发展",
        "团队领导潜力": "潜力与发展",
        "简历质量与完整性": "综合",
        "职业发展规划": "综合",
        "综合匹配度": "综合",
    }

    def _update_dimension_importance(self, dimension_importance: Dict[str, str], dimension_names_mapping: Dict[str, str] = None):
        """
        更新维度配置中的重要等级（从core_requirements中获取）
        同时更新维度名称以匹配core_requirements中的名称
        
        注意：为了避免线程安全问题，每次评估时应该deepcopy一份配置
        
        Args:
            dimension_importance: 维度名称 -> 重要等级的映射（使用代码中的维度名称）
            dimension_names_mapping: core_requirements中的维度名称 -> 代码中的维度名称映射
        """
        if not dimension_importance:
            logger.info("未提供维度重要等级信息，使用默认配置")
            return
        
        updated_count = 0
        # 构建反向映射：代码中的维度名称 -> core_requirements中的维度名称
        reverse_mapping = {}
        if dimension_names_mapping:
            reverse_mapping = {v: k for k, v in dimension_names_mapping.items()}
        
        # 注意：这里直接修改self.dimensions_config，在多线程/协程环境下可能有并发问题
        # 但考虑到评估是异步的，且每次评估都会重新获取配置，暂时保持现状
        # 如果后续需要真正的线程安全，可以在evaluate_interview开始时deepcopy整个配置
        for dim_key, dim_config in self.dimensions_config.items():
            dim_name = dim_config['name']
            original_importance = dim_config.get('importance_level', '弹性')
            
            # 尝试匹配维度名称
            if dim_name in dimension_importance:
                importance = dimension_importance[dim_name]
                # 验证重要等级值
                if importance in ['不重要', '弹性', '必备', '加分项']:
                    dim_config['importance_level'] = importance
                    # 如果core_requirements中有对应的维度名称，保存JD中的名称
                    if reverse_mapping and dim_name in reverse_mapping:
                        jd_dim_name = reverse_mapping[dim_name]
                        # 移除序号前缀，保存原始JD维度名称
                        dim_config['jd_dimension_name'] = jd_dim_name
                    
                    if importance != original_importance:
                        logger.info(f"✅ 更新维度 '{dim_name}' 的重要等级: {original_importance} -> {importance}")
                    else:
                        logger.debug(f"维度 '{dim_name}' 的重要等级保持不变: {importance}")
                    updated_count += 1
                else:
                    logger.warning(f"维度 '{dim_name}' 的重要等级值无效: {importance}，保持原值: {original_importance}")
            else:
                logger.debug(f"维度 '{dim_name}' 未在core_requirements中找到，使用默认重要等级: {original_importance}")
        
        logger.info(f"维度重要等级更新完成: 成功更新 {updated_count}/{len(self.dimensions_config)} 个维度")

    def _load_evaluation_prompt(self) -> str:
        """加载评估提示词模板（与 prompts/interview_evaluation_system_prompt.md 一致；失败时用内置副本，避免行为漂移）。"""
        prompt_path = Path(__file__).resolve().parent / "prompts" / "interview_evaluation_system_prompt.md"
        try:
            prompt_template = prompt_path.read_text(encoding="utf-8").strip()
            if not prompt_template:
                logger.warning(f"提示词文件为空: {prompt_path}，使用内置副本")
                return _INTERVIEW_EVALUATION_SYSTEM_PROMPT_FALLBACK
            logger.info(f"成功加载提示词模板: {prompt_path}")
            return prompt_template
        except OSError as e:
            logger.warning(f"无法读取提示词文件 {prompt_path}: {e}，使用与仓库同步的内置副本")
            return _INTERVIEW_EVALUATION_SYSTEM_PROMPT_FALLBACK
        except Exception as e:
            logger.error(f"加载提示词模板失败: {str(e)}，使用内置副本")
            return _INTERVIEW_EVALUATION_SYSTEM_PROMPT_FALLBACK

    async def evaluate_interview(self, session_id: str, invitation_id: str) -> Dict[str, Any]:
        """
        执行21维度面试评估

        Args:
            session_id: 会话ID
            invitation_id: 邀请ID

        Returns:
            评估结果字典
        """
        try:
            logger.info(f"开始21维度面试评估: session_id={session_id}, invitation_id={invitation_id}")

            # 1. 获取完整的面试问答数据（用于统计信息，不用于评估）
            # 注意：实际评估时会在_evaluate_in_stages中为每个维度重新获取过滤后的数据
            interview_data = await self._get_complete_interview_data(session_id, invitation_id)
            
            # 保存session_id和invitation_id到interview_data中，供后续使用
            interview_data['session_id'] = session_id
            interview_data['invitation_id'] = invitation_id
            
            # 检查是否有面试数据
            if not interview_data or not interview_data.get('interview_content') or len(interview_data.get('interview_content', [])) == 0:
                logger.warning(f"未获取到任何面试数据，返回默认评估结果: session_id={session_id}")
                return self._get_default_evaluation_result(
                    error_message="候选人未提供任何有效答案，无法进行评估",
                    session_id=session_id,
                    invitation_id=invitation_id
                )

            # 2. 获取岗位要求配置
            job_requirements = await self._get_job_requirements(invitation_id)
            
            # 2.1 更新维度配置中的重要等级和名称（从core_requirements中获取）
            self._update_dimension_importance(
                job_requirements.get('dimension_importance', {}),
                job_requirements.get('dimension_names_mapping', {})
            )

            # 3. 判断是否使用分阶段评估
            # 估算输入tokens，如果过长则使用分阶段评估
            system_content_length = len(self.prompt_template)
            report_content = interview_data.get('formatted_report', '')
            estimated_input_tokens = int((system_content_length + len(report_content)) * 1.5)
            max_context_length = getattr(self.llm_service, 'max_context_length', 4096)
            
            # 强制使用分阶段评估（7B模型即使不超3000 tokens，一次性处理21个维度也容易失败）
            # 为了确保评估质量和JSON解析成功率，强制分阶段评估
            use_staged_evaluation = True  # 强制分阶段评估
            
            if use_staged_evaluation:
                logger.info(f"📊 输入内容较长（估算{estimated_input_tokens} tokens），采用分阶段评估（Map-Reduce模式）")
                # 4. 分阶段评估（Map-Reduce模式）
                evaluation_result = await self._evaluate_in_stages(interview_data, job_requirements)
            else:
                logger.info("📊 输入内容适中，采用单次评估")
                # 4. 单次评估
                prompt = self._build_evaluation_prompt(interview_data, job_requirements)
                
                # 动态计算max_tokens
                user_content_length = len(prompt)
                estimated_input_tokens = int((system_content_length + user_content_length) * 1.5)
                safety_margin = 100
                available_max_tokens = max_context_length - estimated_input_tokens - safety_margin
                
                if available_max_tokens < 500:
                    logger.warning(f"⚠️ 输入内容过长，将尝试使用最小max_tokens=500")
                    max_tokens = 500
                else:
                    max_tokens = min(available_max_tokens, 2000)
                
                logger.info(f"📊 Token计算: 输入长度={system_content_length + user_content_length}字符, 估算输入tokens≈{estimated_input_tokens}, 设置max_tokens={max_tokens}")
                
                response = await self.llm_service.chat_completion(
                    messages=[
                        {"role": "system", "content": self.prompt_template},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=max_tokens
                )

                content = response.get("content", "").strip()
                logger.info(f"LLM评估响应长度: {len(content)} 字符")

                # 5. 解析评估结果
                evaluation_result = self._parse_evaluation_result(content)

            # 6. 添加元数据
            if not use_staged_evaluation:
                evaluation_result = self._add_evaluation_metadata(evaluation_result, response)
            else:
                evaluation_result = self._add_evaluation_metadata(evaluation_result, {"model": "staged_evaluation"})
            
            # 保存session_id到evaluation_result中，用于计算题目分数
            evaluation_result['session_id'] = session_id

            evaluation_result = await self._post_aggregate_enrich(
                evaluation_result, session_id, invitation_id, job_requirements
            )

            # 7. 保存评估结果到数据库
            await self._save_evaluation_result(invitation_id, evaluation_result)

            logger.info(f"面试评估完成: session_id={session_id}, 总体得分={evaluation_result.get('overall_score', 0)}")

            return evaluation_result

        except Exception as e:
            logger.error(f"面试评估异常: session_id={session_id}, invitation_id={invitation_id}, error={e}")
            import traceback
            logger.error(f"详细错误信息: {traceback.format_exc()}")

            # 返回默认评估结果
            return self._get_default_evaluation_result(
                error_message=f"评估系统异常: {str(e)}",
                session_id=session_id,
                invitation_id=invitation_id
            )

    async def _get_complete_interview_data(self, session_id: str, invitation_id: str, dimension_key: str = None) -> Dict[str, Any]:
        """
        获取完整的面试问答数据
        包含问题文本、评估要点、候选人回答、评分结果等
        
        Args:
            session_id: 会话ID
            invitation_id: 邀请ID
            dimension_key: 维度key（如"learning_adaptability"），如果提供则只返回与该维度相关的题目
        """
        try:
            # 获取所有答案记录
            answers = database_service.get_session_candidate_answers(session_id)
            logger.info(f"获取到 {len(answers)} 条答案记录")
            
            # 如果指定了维度，获取该维度对应的题目分类
            relevant_categories = None
            if dimension_key:
                relevant_categories = self.DIMENSION_TO_QUESTION_CATEGORIES.get(dimension_key, [])
                if relevant_categories:
                    logger.info(f"维度 {dimension_key} 对应的题目分类: {relevant_categories}")
                else:
                    # 如果维度没有对应的题目分类（如"面试表现质量"），使用所有题目
                    logger.info(f"维度 {dimension_key} 没有对应的题目分类，将使用所有题目进行综合评估")
                    relevant_categories = None  # None表示不过滤，使用所有题目
            
            # 检查是否有答案记录
            if not answers or len(answers) == 0:
                logger.warning(f"未获取到任何答案记录: session_id={session_id}")
                # 返回空的面试数据，让上层函数处理
                return {
                    'interview_content': [],
                    'session_stats': {
                        'total_questions': 0,
                        'followup_questions': 0,
                        'main_questions': 0,
                        'session_id': session_id,
                        'invitation_id': invitation_id
                    },
                    'formatted_content': '📋 面试问答记录\n\n未获取到任何答案记录。'
                }

            interview_content = []
            followup_count = 0

            for answer in answers:
                # 跳过追问答案，因为追问信息已经包含在主问题中
                if answer.get('is_follow_up', False):
                    followup_count += 1
                    continue

                # 获取问题详情
                question_detail = database_service.get_question_by_id(answer['question_id'])
                if not question_detail:
                    logger.warning(f"问题不存在: {answer['question_id']}")
                    continue

                # 从interview_question表获取content和question_type
                question_text = question_detail.get('question_text', '未知问题')
                question_type = question_detail.get('question_type', 'BASIC')
                question_category = question_detail.get('question_category', '')
                evaluation_points = question_detail.get('evaluation_points', [])
                # 使用题目的真实顺序编号（question_order），这是题目在整个面试中的真实编号
                question_order = question_detail.get('question_order', 0)
                
                # 如果指定了维度，过滤掉不相关的题目
                if dimension_key and relevant_categories:
                    # 如果题目分类不在相关分类列表中，跳过该题目
                    if question_category not in relevant_categories:
                        logger.debug(f"题目 {answer['question_id']} 的分类 {question_category} 不在维度 {dimension_key} 的相关分类中，跳过")
                        continue
                
                # 解析JSON字段
                import json
                point_evaluations = answer.get('point_evaluations')
                if isinstance(point_evaluations, str):
                    try:
                        point_evaluations = json.loads(point_evaluations)
                    except:
                        point_evaluations = None
                
                follow_up_evaluation_points = answer.get('follow_up_evaluation_points')
                if isinstance(follow_up_evaluation_points, str):
                    try:
                        follow_up_evaluation_points = json.loads(follow_up_evaluation_points)
                    except:
                        follow_up_evaluation_points = None
                
                evaluation_result = answer.get('evaluation_result')
                if isinstance(evaluation_result, str):
                    try:
                        evaluation_result = json.loads(evaluation_result)
                    except:
                        evaluation_result = None
                
                follow_up_evaluation = answer.get('follow_up_evaluation')
                if isinstance(follow_up_evaluation, str):
                    try:
                        follow_up_evaluation = json.loads(follow_up_evaluation)
                    except:
                        follow_up_evaluation = None

                # 构建问题块（包含所有需要的字段）
                # 使用question_order作为题目编号，这是题目在整个面试中的真实编号
                question_block = {
                    'question_number': question_order,  # 使用题目的真实顺序编号
                    'question_id': answer['question_id'],
                    'question_text': question_text,
                    'question_type': question_type,  # SPECIALTY=专业题，BASIC=基础题
                    'evaluation_points': evaluation_points,
                    'answer_text': answer.get('answer_text'),  # 主问题答案
                    'point_evaluations': point_evaluations,  # 主问题评估要点
                    'evaluation_result': evaluation_result,  # 主问题评分结果
                    'follow_up_question': answer.get('follow_up_question'),  # 追问问题（只有专业题可能有）
                    'follow_up_evaluation_points': follow_up_evaluation_points,  # 追问评估要点（只有专业题可能有）
                    'follow_up_answer_text': answer.get('follow_up_answer_text'),  # 追问答案（只有专业题可能有）
                    'follow_up_evaluation': follow_up_evaluation,  # 追问评价（只有专业题可能有）
                    'comprehensive_score': answer.get('comprehensive_score'),  # 主问题和追问综合评分（只有专业题可能有）
                    'is_follow_up': answer.get('is_follow_up', False)
                }

                interview_content.append(question_block)

            # 获取该邀请的总问题数（从interview_question表）
            total_questions_for_invitation = 0
            try:
                total_query = "SELECT COUNT(*) as total FROM interview_question WHERE invitation_id = %s"
                total_result = database_service.db.execute_one(total_query, (invitation_id,))
                if total_result:
                    total_questions_for_invitation = total_result.get('total', 0)
            except Exception as e:
                logger.warning(f"获取总问题数失败: {e}")
            
            # 获取会话统计信息
            answered_questions = len(interview_content)  # 已回答的主问题数
            completion_rate = (answered_questions / total_questions_for_invitation * 100) if total_questions_for_invitation > 0 else 0
            
            session_stats = {
                'total_questions': total_questions_for_invitation,  # 总问题数（15道）
                'answered_questions': answered_questions,  # 已回答的问题数
                'completion_rate': round(completion_rate, 1),  # 完成率
                'followup_questions': followup_count,
                'main_questions': answered_questions,
                'session_id': session_id,
                'invitation_id': invitation_id
            }
            
            logger.info(f"面试完成度: 总问题数={total_questions_for_invitation}, 已回答={answered_questions}, 完成率={completion_rate:.1f}%")

            return {
                'interview_content': interview_content,
                'session_stats': session_stats,
                'formatted_content': self._format_interview_content(interview_content),
                'formatted_report': self._format_interview_content_as_report(interview_content, max_chars=None)  # 结构化报表格式，不限制长度
            }

        except Exception as e:
            logger.error(f"获取面试数据异常: {e}")
            raise

    def _format_interview_content(self, interview_content: List[Dict]) -> str:
        """格式化面试内容为文本（详细版本，用于调试）"""
        formatted_text = "📋 面试问答记录\n\n"

        for i, item in enumerate(interview_content, 1):
            question_type_name = "专业题" if item.get('question_type') == 'SPECIALTY' else "基础题"
            if item.get('question_type') == 'BASIC':
                question_type_name = "基础题"
            
            # 使用题目的真实编号，如果不存在则使用枚举索引
            question_number = item.get('question_number', i)

            formatted_text += f"#{question_number} {question_type_name}\n"
            formatted_text += f"问题ID：{item.get('question_id', '未知')}\n"
            formatted_text += f"问题：{item['question_text']}\n"

            # 添加评估要点
            if item.get('evaluation_points'):
                formatted_text += "评估要点：\n"
                evaluation_points = item['evaluation_points']
                if isinstance(evaluation_points, str):
                    import json
                    try:
                        evaluation_points = json.loads(evaluation_points)
                    except:
                        evaluation_points = []
                
                if isinstance(evaluation_points, list):
                    for j, point in enumerate(evaluation_points, 1):
                        point_text = point.get('point', '') if isinstance(point, dict) else str(point)
                        weight = point.get('weight', '') if isinstance(point, dict) else ''
                        formatted_text += f"  {j}. {point_text}"
                        if weight:
                            formatted_text += f" (权重: {weight})"
                        formatted_text += "\n"
                formatted_text += "\n"

            formatted_text += f"回答：{item.get('answer_text', '')}\n"

            # 添加主问题评估要点评分
            if item.get('point_evaluations'):
                formatted_text += "主问题评估要点评分：\n"
                point_evals = item['point_evaluations']
                if isinstance(point_evals, list):
                    for j, point_eval in enumerate(point_evals, 1):
                        point_name = point_eval.get('point', '未知要点')
                        point_score = point_eval.get('score', 0)
                        point_weight = point_eval.get('weight', 0)
                        formatted_text += f"  {j}. {point_name}: {point_score} (权重: {point_weight})\n"
                formatted_text += "\n"

            # 添加主问题评分结果
            if item.get('evaluation_result'):
                eval_result = item['evaluation_result']
                if isinstance(eval_result, dict):
                    score = eval_result.get('score', 0)
                    reasoning = eval_result.get('reasoning', '')
                    grade = eval_result.get('grade', '')
                    formatted_text += f"主问题评分：{score}分 ({grade}) - {reasoning}\n"

            # 添加追问信息（只有专业题可能有）
            if item.get('follow_up_question'):
                formatted_text += f"\n追问问题：{item['follow_up_question']}\n"
                
                # 追问评估要点
                if item.get('follow_up_evaluation_points'):
                    formatted_text += "追问评估要点：\n"
                    follow_up_points = item['follow_up_evaluation_points']
                    if isinstance(follow_up_points, list):
                        for j, point in enumerate(follow_up_points, 1):
                            point_text = point.get('point', '') if isinstance(point, dict) else str(point)
                            weight = point.get('weight', '') if isinstance(point, dict) else ''
                            formatted_text += f"  {j}. {point_text}"
                            if weight:
                                formatted_text += f" (权重: {weight})"
                            formatted_text += "\n"
                
                # 追问答案
                if item.get('follow_up_answer_text'):
                    formatted_text += f"追问答案：{item['follow_up_answer_text']}\n"
                
                # 追问评价
                if item.get('follow_up_evaluation'):
                    follow_up_eval = item['follow_up_evaluation']
                    if isinstance(follow_up_eval, dict):
                        follow_up_score = follow_up_eval.get('score', 0)
                        follow_up_reasoning = follow_up_eval.get('reasoning', '')
                        formatted_text += f"追问评分：{follow_up_score}分 - {follow_up_reasoning}\n"
                
                # 综合评分（主问题和追问）
                if item.get('comprehensive_score') is not None:
                    formatted_text += f"综合评分（主问题+追问）：{item['comprehensive_score']}分\n"

            formatted_text += "\n" + "="*50 + "\n\n"

        return formatted_text
    
    def _format_interview_content_as_report(self, interview_content: List[Dict], max_chars: int = None) -> str:
        """
        格式化面试内容为结构化报表（摘要版本，用于21维度评估）
        优化：尽可能包含完整的候选人回答，但如果超过max_chars限制则截断
        注意：如果答案为空，不包含在输出中
        
        Args:
            interview_content: 面试内容列表
            max_chars: 最大字符数限制（用于控制token数量），如果为None则不限制
        """
        formatted_text = "[面试问答摘要]\n"
        
        item_index = 0
        current_length = len(formatted_text)
        
        for item in interview_content:
            # 检查是否有评分结果，如果没有则跳过
            eval_result = item.get('evaluation_result')
            if not eval_result:
                continue
            
            # 解析evaluation_result
            if isinstance(eval_result, str):
                import json
                try:
                    eval_result = json.loads(eval_result)
                except:
                    continue
            
            if not isinstance(eval_result, dict):
                continue
            
            item_index += 1
            question_type_name = "专业题" if item.get('question_type') == 'SPECIALTY' else "基础题"
            
            # 使用题目在整个面试中的真实编号，如果不存在则使用item_index
            question_number = item.get('question_number', item_index)
            
            # 传递：题目名 + 评分 + 评分理由 + 候选人回答（尽可能完整）
            question_text = item.get('question_text', '')
            score = eval_result.get('score', 0)
            reasoning = eval_result.get('reasoning', '')
            
            # 提取候选人回答，尽可能包含完整内容
            answer_text = item.get('answer_text', '')
            
            # 计算基础文本长度（题目+评分+格式）
            base_text = f"#{question_number} {question_type_name}：{question_text}\n候选人回答：\n评分：{score}分 - {reasoning}\n"
            base_length = len(base_text)
            
            # 如果设置了最大字符数限制，需要动态调整答案长度
            if max_chars is not None:
                remaining_chars = max_chars - current_length - base_length
                # 预留一些空间给追问和其他内容（约200字符）
                available_chars = max(0, remaining_chars - 200)
                
                if available_chars > 0:
                    # 尽可能包含完整回答，但不超过可用字符数
                    if len(answer_text) <= available_chars:
                        answer_snippet = answer_text
                    else:
                        # 截断到可用字符数，保留完整句子
                        answer_snippet = answer_text[:available_chars]
                        # 尝试在最后一个句号、问号或感叹号处截断
                        last_punct = max(
                            answer_snippet.rfind('。'),
                            answer_snippet.rfind('？'),
                            answer_snippet.rfind('！'),
                            answer_snippet.rfind('.'),
                            answer_snippet.rfind('?'),
                            answer_snippet.rfind('!')
                        )
                        if last_punct > available_chars * 0.7:  # 如果标点位置合理（在70%之后）
                            answer_snippet = answer_snippet[:last_punct + 1]
                        else:
                            answer_snippet += "..."
                else:
                    # 可用字符数不足，至少保留前100字符
                    answer_snippet = answer_text[:100] + "..." if len(answer_text) > 100 else answer_text
            else:
                # 没有限制，使用完整回答
                answer_snippet = answer_text if answer_text else "无回答"
            
            formatted_text += f"#{question_number} {question_type_name}：{question_text}\n"
            formatted_text += f"候选人回答：{answer_snippet}\n"
            formatted_text += f"评分：{score}分 - {reasoning}\n"
            
            # 如果有追问评分，也添加
            if item.get('follow_up_evaluation'):
                follow_up_eval = item['follow_up_evaluation']
                if isinstance(follow_up_eval, str):
                    import json
                    try:
                        follow_up_eval = json.loads(follow_up_eval)
                    except:
                        follow_up_eval = {}
                
                if isinstance(follow_up_eval, dict):
                    follow_up_score = follow_up_eval.get('score', 0)
                    follow_up_reasoning = follow_up_eval.get('reasoning', '')
                    if follow_up_reasoning:
                        formatted_text += f"追问评分：{follow_up_score}分 - {follow_up_reasoning}\n"
                    else:
                        formatted_text += f"追问评分：{follow_up_score}分\n"
            
            formatted_text += "\n"
            current_length = len(formatted_text)
            
            # 如果超过限制，停止添加更多内容
            if max_chars is not None and current_length >= max_chars:
                formatted_text += "...（内容已截断）\n"
                break
        
        return formatted_text

    async def _get_job_requirements(self, invitation_id: str) -> Dict[str, Any]:
        """
        获取岗位要求配置
        从job_description_base表查询core_requirements字段作为评估参考
        解析重要等级信息和维度名称映射
        """
        # 获取邀请信息
        invitation = database_service.get_invitation_by_id(invitation_id)
        
        if not invitation:
            logger.warning(f"未找到邀请信息: invitation_id={invitation_id}")
            return {
                'position': '未知职位',
                'company': '未知公司',
                'department': '未知部门',
                'description': '通用技术岗位要求：具备扎实的编程基础，良好的学习能力和团队协作精神',
                'core_requirements': None,
                'dimension_importance': {},  # 维度重要等级映射
                'dimension_names_mapping': {}  # core_requirements中的维度名称 -> 代码中的维度名称映射
            }
        
        company = invitation.get('requester', '')
        department = invitation.get('department', '')
        position = invitation.get('position', '未知职位')
        
        # 从job_description_base表查询core_requirements
        core_requirements = None
        dimension_importance = {}  # 维度名称 -> 重要等级的映射
        dimension_names_mapping = {}  # core_requirements中的维度名称 -> 代码中的维度名称映射
        
        if company and department and position:
            try:
                jd_info = database_service.get_job_description_by_company_department_position(
                    company=company,
                    department=department,
                    position=position
                )
                if jd_info:
                    core_requirements = jd_info.get('core_requirements', '')
                    logger.info(f"成功获取岗位JD: company={company}, department={department}, position={position}")
                    
                    # 解析core_requirements中的重要等级和维度名称
                    if core_requirements:
                        try:
                            import json
                            cr_data = json.loads(core_requirements)
                            jd_dimension_names = cr_data.get('21个具体维度', [])
                            importance_levels = cr_data.get('重要等级', [])
                            
                            # 构建维度名称映射和重要等级映射
                            # 建立core_requirements中的维度名称与代码中维度名称的映射关系
                            # 使用代码中定义的维度名称（从DIMENSION_NAME_MAPPING和dimensions_config中获取）
                            name_mapping = {}
                            # 从代码配置中构建映射表，确保使用正确的维度名称
                            for dim_key, dim_config in self.dimensions_config.items():
                                dim_name = dim_config['name']
                                # 支持多种可能的名称变体
                                name_mapping[dim_name] = dim_name
                                # 添加可能的别名映射
                                if dim_name == '专业工具/系统熟练度':
                                    name_mapping['专业工具熟练度'] = dim_name
                                elif dim_name == '项目/工作经验':
                                    name_mapping['项目经验丰富度'] = dim_name
                                elif dim_name == '资格证书要求':
                                    name_mapping['资格证书符合度'] = dim_name
                                elif dim_name == '工作地点与出差接受度':
                                    name_mapping['工作地点适应性'] = dim_name
                                elif dim_name == '薪资期望匹配度':
                                    name_mapping['薪资期望合理性'] = dim_name
                                elif dim_name == '简历质量与完整性':
                                    name_mapping['面试表现质量'] = dim_name
                            
                            # 构建维度名称到重要等级的映射
                            matched_count = 0
                            for i, jd_dim_name in enumerate(jd_dimension_names):
                                # 移除序号前缀（如"1."、"19."等）
                                clean_jd_name = jd_dim_name.split('.', 1)[-1].strip() if '.' in jd_dim_name else jd_dim_name.strip()
                                
                                # 映射到代码中的维度名称
                                code_dim_name = name_mapping.get(clean_jd_name, clean_jd_name)
                                dimension_names_mapping[clean_jd_name] = code_dim_name
                                
                                if i < len(importance_levels):
                                    importance_level = importance_levels[i]
                                    dimension_importance[code_dim_name] = importance_level
                                    # 验证重要等级是否有效
                                    if importance_level in ['不重要', '弹性', '必备', '加分项']:
                                        matched_count += 1
                                        logger.debug(f"维度映射: '{clean_jd_name}' -> '{code_dim_name}', 重要等级: {importance_level}")
                                    else:
                                        logger.warning(f"维度 '{code_dim_name}' 的重要等级无效: {importance_level}")
                            
                            logger.info(f"成功解析 {matched_count}/{len(importance_levels)} 个维度的重要等级和名称映射")
                            if matched_count < len(importance_levels):
                                logger.warning(f"部分维度的重要等级解析失败，已匹配: {matched_count}, 总数: {len(importance_levels)}")
                        except Exception as e:
                            logger.warning(f"解析core_requirements重要等级失败: {e}")
                else:
                    logger.warning(f"未找到岗位JD: company={company}, department={department}, position={position}")
            except Exception as e:
                logger.error(f"查询岗位JD失败: {e}")
        
        return {
            'position': position,
            'company': company,
            'department': department,
            'description': core_requirements if core_requirements else '通用技术岗位要求：具备扎实的编程基础，良好的学习能力和团队协作精神',
            'core_requirements': core_requirements,
            'dimension_importance': dimension_importance,  # 维度重要等级映射
            'dimension_names_mapping': dimension_names_mapping  # 维度名称映射
        }

    def _get_dimension_info_from_db(self, dimension_name: str) -> Optional[Dict[str, Any]]:
        """
        从evaluation_dimension表查询维度信息
        
        Args:
            dimension_name: 维度名称（必须严格匹配evaluation_dimension表中的dimension_name字段）
            
        Returns:
            维度信息字典，包含category、description、key_indicators等
        """
        try:
            query = """
                SELECT dimension_name, category, description, key_indicators, evaluation_method
                FROM evaluation_dimension
                WHERE dimension_name = %s AND is_active = true
                LIMIT 1
            """
            result = database_service.db.execute_one(query, (dimension_name,))
            if result:
                key_indicators = result.get('key_indicators')
                if isinstance(key_indicators, str):
                    try:
                        key_indicators = json.loads(key_indicators)
                    except:
                        key_indicators = []
                elif key_indicators is None:
                    key_indicators = []
                elif not isinstance(key_indicators, list):
                    # 如果key_indicators是其他类型（如dict），转换为list
                    key_indicators = [key_indicators] if key_indicators else []
                
                return {
                    'dimension_name': result['dimension_name'],
                    'category': result.get('category', ''),
                    'description': result.get('description', ''),
                    'key_indicators': key_indicators if isinstance(key_indicators, list) else [],
                    'evaluation_method': result.get('evaluation_method', '')
                }
            return None
        except Exception as e:
            logger.warning(f"查询维度信息失败: {dimension_name}, error={e}")
            return None

    def _build_evaluation_prompt(self, interview_data: Dict, job_requirements: Dict, dimension_key: str = None, max_chars: int = None) -> str:
        """
        构建评估提示词（单个维度评估）
        
        Args:
            interview_data: 面试数据（已按维度过滤）
            job_requirements: 岗位要求
            dimension_key: 维度key（如"learning_adaptability"），如果为None则评估所有维度
            max_chars: 最大字符数限制（用于控制token数量），如果为None则不限制
        """
        # 如果interview_data中有interview_content，重新格式化以包含完整回答
        if interview_data.get('interview_content') and max_chars is not None:
            # 重新格式化，包含完整回答但不超过限制
            formatted_report = self._format_interview_content_as_report(
                interview_data['interview_content'], 
                max_chars=max_chars
            )
        else:
            # 使用已有的格式化内容
            formatted_report = interview_data.get('formatted_report', interview_data.get('formatted_content', ''))
        session_stats = interview_data['session_stats']
        
        # 记录过滤后的题目数量
        filtered_questions_count = len(interview_data.get('interview_content', []))
        if dimension_key:
            dim_name = self.dimensions_config.get(dimension_key, {}).get('name', '未知维度')
            logger.debug(f"维度 {dim_name} 过滤后剩余 {filtered_questions_count} 道相关题目")
        
        # 构建岗位JD信息（已移除，因为已有评估维度信息）
        # jd_section = ""
        # if job_requirements.get('core_requirements'):
        #     core_req = job_requirements.get('core_requirements')
        #     jd_section = f"\n## 📋 岗位JD核心要求\n{core_req}\n"

        # 根据维度key确定要评估的维度（单个维度）
        if dimension_key and dimension_key in self.dimensions_config:
            dim_config = self.dimensions_config[dimension_key]
            # 使用映射表确保维度名称严格对应evaluation_dimension表
            dim_name = self.DIMENSION_NAME_MAPPING.get(dimension_key, dim_config['name'])
            
            # 从evaluation_dimension表查询维度详细信息
            dim_info = self._get_dimension_info_from_db(dim_name)
            if dim_info:
                dim_category = dim_info.get('category', '')
                dim_description = dim_info.get('description', dim_config.get('description', ''))
                key_indicators = dim_info.get('key_indicators', [])
                
                # 构建当前维度的详细信息
                dimension_info_section = f"""
## 📊 评估维度信息

**维度名称**：{dim_name}
**分类**：{dim_category}
**描述**：{dim_description}"""
                
                if key_indicators:
                    indicators_text = "、".join(key_indicators) if isinstance(key_indicators, list) else str(key_indicators)
                    dimension_info_section += f"\n**关键指标**：{indicators_text}"
                
                dim_section = f"请评估以下维度：\n- {dim_name}：{dim_description}"
                group_name = dim_name
            else:
                # 如果查询失败，使用配置中的信息
                dim_description = dim_config['description']
                dimension_info_section = f"""
## 📊 评估维度信息

**维度名称**：{dim_name}
**描述**：{dim_description}"""
                dim_section = f"请评估以下维度：\n- {dim_name}：{dim_description}"
                group_name = dim_name
                logger.warning(f"未从evaluation_dimension表查询到维度信息: {dim_name}")
        else:
            dim_section = "请对所有21个维度进行评估"
            group_name = "全部维度"
            dim_name = "维度名"
            dimension_info_section = ""

        # 获取完成率信息（使用过滤后的题目数量）
        total_questions = session_stats.get('total_questions', 0)
        answered_questions = filtered_questions_count  # 使用过滤后的题目数量
        completion_rate = session_stats.get('completion_rate', 0)
        
        completion_info = ""
        if dimension_key:
            # 如果是单维度评估，显示过滤后的题目信息
            dim_name = self.dimensions_config.get(dimension_key, {}).get('name', '未知维度')
            completion_info = f"""
## 📋 相关题目筛选
**评估维度**：{dim_name}
**相关题目数量**：{answered_questions} 道（已过滤掉不相关题目）
**要求**：仅基于以下相关题目的回答进行评分，信息不足时标注"信息不足"并给出合理分数（10-30分），不要使用默认分数。
"""
        elif total_questions > 0:
            completion_info = f"""
## ⚠️ 面试完成度：{answered_questions}/{total_questions} ({completion_rate}%)
**要求**：根据实际回答评分，信息不足时标注"信息不足"并给出合理分数（10-30分），不要使用默认分数。
"""

        # 如果是单维度评估，添加相关题目说明
        relevant_questions_note = ""
        if dimension_key:
            relevant_categories = self.DIMENSION_TO_QUESTION_CATEGORIES.get(dimension_key, [])
            if relevant_categories:
                relevant_questions_note = f"""
**重要提示**：以下面试记录已经过筛选，仅包含与"{dim_name}"维度相关的题目。
请仅基于这些相关题目的回答进行评估，不要参考其他不相关的题目。
如果相关题目中没有足够信息，请标注"信息不足"并给出合理分数（10-30分）。
"""
            else:
                relevant_questions_note = f"""
**重要提示**："{dim_name}"维度需要综合评估所有题目，请基于全部面试记录进行评估。
如果面试记录中没有足够信息，请标注"信息不足"并给出合理分数（10-30分）。
"""
        
        prompt = f"""评估维度：{group_name}
岗位：{job_requirements.get('position', '未知')}
{completion_info}
{dimension_info_section}
{relevant_questions_note}
面试摘要（包含题目、候选人回答片段、评分）：
{formatted_report}

评估维度：{dim_section}

请基于以上面试摘要进行评估，reasoning必须引用候选人的具体回答内容。
**重要**：引用候选人回答时，请使用双引号包裹。reasoning格式：题目编号+"引用具体回答内容"+维度分析+评分依据（题目编号使用摘要中实际编号如#1、#2等，不要用固定的"#1"）

**JSON格式要求**：返回的JSON必须是有效的JSON格式。reasoning字段中的引号必须正确转义（使用\\"），例如：
- 正确：`"reasoning": "#1+\\"候选人回答内容\\"+维度分析+评分依据"`
- 错误：`"reasoning": "#1+\"候选人回答内容\"+维度分析+评分依据"`（会导致JSON解析失败）

{{
  "dimension_scores": {{"{dim_name}": 分数}},
  "dimension_details": {{"{dim_name}": {{"score": 分数, "reasoning": "题目编号+\\"引用具体回答内容\\"+维度分析+评分依据"}}}}
}}"""

        prompt += "\n只返回JSON。"
        return prompt.strip()

    async def _evaluate_in_stages(self, interview_data: Dict, job_requirements: Dict) -> Dict[str, Any]:
        """
        分阶段评估（Map-Reduce模式）
        将21个维度分成21个阶段，每个阶段只评估1个维度，最后合并结果
        
        Args:
            interview_data: 面试数据
            job_requirements: 岗位要求
            
        Returns:
            完整的评估结果
        """
        logger.info("开始分阶段评估（Map-Reduce模式，21个维度，21个阶段）...")
        
        all_dimension_scores = {}
        all_dimension_details = {}
        evaluation_summary = ""
        evaluation_suggestions = ""
        is_passed = 0
        
        max_context_length = getattr(self.llm_service, 'max_context_length', 4096)
        system_content_length = len(self.prompt_template)
        
        # 生成带时间戳的调试文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _session_stats = interview_data.get('session_stats') or {}
        session_id = interview_data.get('session_id') or _session_stats.get('session_id', 'unknown')
        invitation_id = interview_data.get('invitation_id') or _session_stats.get('invitation_id', 'unknown')
        debug_file = os.path.join(settings.LOG_DIR_DEBUG_PROMPT, f"debug_prompt_{timestamp}_{session_id[:8]}_{invitation_id[:20]}.txt")
        
        logger.info(f"📝 调试文件将保存到: {debug_file}")
        
        # 获取所有21个维度，按顺序评估
        all_dimensions = list(self.dimensions_config.items())
        
        # 循环评估21个维度（每个维度一个阶段）
        for stage_num, (dim_key, dim_config) in enumerate(all_dimensions, 1):
            dim_name = dim_config['name']
            logger.info(f"📊 第{stage_num}阶段（共21阶段）：评估维度 - {dim_name}...")
            try:
                # 为当前维度获取过滤后的面试数据（只包含相关题目）
                filtered_interview_data = await self._get_complete_interview_data(
                    session_id=interview_data.get('session_id') or _session_stats.get('session_id', ''),
                    invitation_id=interview_data.get('invitation_id') or _session_stats.get('invitation_id', ''),
                    dimension_key=dim_key
                )
                
                # 第19阶段（简历质量与完整性）特殊处理：从所有题目中选择最相关的3道题
                if dim_key == "interview_performance":
                    all_questions = interview_data.get('interview_content', [])
                    if not all_questions:
                        logger.warning(f"维度 {dim_name} 未找到任何题目，将返回保守分")
                        all_dimension_scores[dim_name] = 20
                        all_dimension_details[dim_name] = {
                            "score": 20,
                            "reasoning": "本题可用作答较少，该维度可依据信息有限，已按保守口径评分。"
                        }
                        continue
                    
                    # 定义简历质量与完整性相关的关键词
                    resume_keywords = [
                        '简历', '经历', '经验', '工作', '项目', '技能', '学习', '教育', '学历',
                        '证书', '资格', '认证', 'CPA', '会计证', '从业资格', '职业资格',
                        '自我介绍', '背景', '能力', '特长', '优势'
                    ]
                    
                    # 计算每道题的相关性得分
                    question_scores = []
                    for q in all_questions:
                        if not isinstance(q, dict):
                            continue
                        question_text = str(q.get('question_text') or '')
                        answer_text = str(q.get('answer_text') or '')
                        combined_text = f"{question_text} {answer_text}"
                        
                        # 计算关键词匹配得分
                        score = 0
                        for keyword in resume_keywords:
                            if keyword in combined_text:
                                score += 1
                        
                        # 如果有评分结果，说明题目有实际内容，增加相关性
                        if q.get('evaluation_result'):
                            score += 2
                        
                        question_scores.append((score, q))
                    
                    # 按相关性得分排序，选择得分最高的3道题
                    question_scores.sort(key=lambda x: x[0], reverse=True)
                    top_3_questions = [q for _, q in question_scores[:3]]
                    
                    logger.info(f"维度 {dim_name} 从 {len(all_questions)} 道题中选择了最相关的 {len(top_3_questions)} 道题进行评估")
                    filtered_interview_data = {
                        'interview_content': top_3_questions,
                        'session_stats': interview_data.get('session_stats') or {},
                        'formatted_report': self._format_interview_content_as_report(top_3_questions),
                        'formatted_content': self._format_interview_content(top_3_questions)
                    }
                # 如果过滤后没有相关题目，根据维度类型处理
                elif not filtered_interview_data.get('interview_content'):
                    relevant_categories = self.DIMENSION_TO_QUESTION_CATEGORIES.get(dim_key, [])
                    
                    if relevant_categories:
                        # 其他阶段：如果配置了相关分类但未找到题目，返回"信息不足"评分
                        logger.warning(f"维度 {dim_name} 预期分类 {relevant_categories} 下无可用题目，将返回保守分")
                        all_dimension_scores[dim_name] = 20
                        all_dimension_details[dim_name] = {
                            "score": 20,
                            "reasoning": f"与「{dim_name}」相关的本题作答信号较少，已按保守口径评分；建议在复试中结合岗位要点追问核实。"
                        }
                        continue
                    else:
                        # 配置为空列表的维度，使用所有题目（但第19阶段已在上面特殊处理）
                        logger.info(f"维度 {dim_name} 需要综合评估所有题目（无特定分类限制）")
                        filtered_interview_data = interview_data
                else:
                    logger.info(f"维度 {dim_name} 过滤后剩余 {len(filtered_interview_data['interview_content'])} 道相关题目")
                
                # 计算最大字符数限制（75% token限制对应的字符数，预留更多空间）
                # 估算：1 token ≈ 1.5字符，使用75%作为安全目标，预留更多空间给prompt其他部分
                target_tokens = int(max_context_length * 0.75)  # 使用75%作为安全目标
                target_total_chars = int(target_tokens * 1.5)  # token转字符数
                target_user_chars = max(2000, target_total_chars - system_content_length - 800)  # 预留800字符给prompt其他部分（维度信息、格式要求等）
                max_chars_limit = target_user_chars
                
                # 先构建一次prompt（带限制），检查是否超限
                prompt = self._build_evaluation_prompt(
                    filtered_interview_data, 
                    job_requirements, 
                    dimension_key=dim_key,
                    max_chars=max_chars_limit
                )
                user_content_length = len(prompt)
                total_chars = system_content_length + user_content_length
                estimated_input_tokens = int(total_chars / 1.5)  # 字符数转token：除以1.5
                
                # 如果超过80%限制，逐步截断答案内容直到在安全范围内
                max_chars_used = max_chars_limit
                if estimated_input_tokens > max_context_length * 0.8:
                    logger.warning(f"第{stage_num}阶段（{dim_name}）prompt超限（{estimated_input_tokens} tokens > {max_context_length * 0.8}），将截断答案内容")
                    
                    max_iterations = 15  # 增加迭代次数
                    for iteration in range(max_iterations):
                        prompt = self._build_evaluation_prompt(
                            filtered_interview_data, 
                            job_requirements, 
                            dimension_key=dim_key,
                            max_chars=max_chars_used
                        )
                        user_content_length = len(prompt)
                        total_chars = system_content_length + user_content_length
                        estimated_input_tokens = int(total_chars / 1.5)  # 字符数转token：除以1.5
                        
                        if estimated_input_tokens <= max_context_length * 0.8:
                            logger.info(f"第{stage_num}阶段（{dim_name}）截断后tokens: {estimated_input_tokens}，已降至安全范围")
                            break
                        
                        # 如果还超限，更激进地减少字符数
                        excess_tokens = estimated_input_tokens - int(max_context_length * 0.8)
                        excess_chars = int(excess_tokens * 1.5)  # token转字符数：乘以1.5
                        # 每次减少更多字符，至少保留1500字符
                        max_chars_used = max(1500, max_chars_used - excess_chars - 200)
                        
                        if iteration == max_iterations - 1:
                            logger.warning(f"第{stage_num}阶段（{dim_name}）经过{max_iterations}次截断后仍超限（{estimated_input_tokens} tokens），使用当前内容继续评估")
                
                # 保存所有阶段的prompt到文件（用于调试）
                try:
                    mode = 'a' if stage_num > 1 else 'w'  # 第1阶段创建新文件，后续追加
                    with open(debug_file, mode, encoding='utf-8') as f:
                        if stage_num == 1:
                            f.write("=" * 100 + "\n")
                            f.write(f"面试评估输入内容调试信息\n")
                            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                            f.write(f"Session ID: {session_id}\n")
                            f.write(f"Invitation ID: {invitation_id}\n")
                            f.write("=" * 100 + "\n\n")
                        f.write("=" * 100 + "\n")
                        f.write(f"第{stage_num}阶段 Prompt（{dim_name}）\n")
                        f.write("=" * 100 + "\n\n")
                        f.write("System Prompt:\n")
                        f.write("-" * 100 + "\n")
                        f.write(self.prompt_template)
                        f.write("\n\n")
                        f.write("User Prompt:\n")
                        f.write("-" * 100 + "\n")
                        f.write(prompt)
                        f.write("\n\n")
                        f.write("=" * 100 + "\n")
                        f.write(f"统计信息:\n")
                        f.write(f"System Prompt长度: {system_content_length} 字符\n")
                        f.write(f"User Prompt长度: {user_content_length} 字符\n")
                        f.write(f"总长度: {system_content_length + user_content_length} 字符\n")
                        f.write(f"估算输入tokens: {estimated_input_tokens}\n")
                        f.write(f"模型上限: {max_context_length}\n")
                        f.write(f"80%限制: {max_context_length * 0.8}\n")
                        f.write(f"是否超限: {'是' if estimated_input_tokens > max_context_length * 0.8 else '否'}\n")
                        f.write(f"过滤后题目数量: {len(filtered_interview_data.get('interview_content', []))}\n")
                        f.write("\n\n")
                    if stage_num == 1:
                        logger.info(f"✅ 已创建调试文件: {debug_file}")
                    logger.debug(f"✅ 已保存第{stage_num}阶段prompt到调试文件")
                except Exception as e:
                    logger.warning(f"保存prompt到文件失败: {e}")
                
                # 输入token校验：如果经过截断后仍然超过模型上限的80%，记录警告但继续评估
                if estimated_input_tokens > max_context_length * 0.8:
                    logger.warning(f"⚠️ 第{stage_num}阶段（{dim_name}）输入tokens({estimated_input_tokens})仍超过模型上限的80%({max_context_length * 0.8})，已尽力截断，继续评估")
                    # 不再抛出异常，而是继续评估
                
                available_max_tokens = max_context_length - estimated_input_tokens - 200  # 安全边距
                
                # 如果可用tokens不足，使用最小安全值而不是报错
                # 即使可用tokens很少，也尝试继续评估（LLM可能仍能生成简短但有效的JSON）
                min_safe_tokens = 300  # 降低最小token要求，允许更短的输出
                if available_max_tokens < min_safe_tokens:
                    logger.warning(f"⚠️ 第{stage_num}阶段（{dim_name}）可用tokens不足({available_max_tokens})，将使用最小安全值({min_safe_tokens})继续评估")
                    available_max_tokens = min_safe_tokens
                
                # 设置max_tokens：优先保证有足够的tokens输出完整JSON
                # 最小300，最大2000，如果可用tokens较少则使用可用值
                max_tokens = max(min_safe_tokens, min(available_max_tokens, 2000))
                logger.debug(f"第{stage_num}阶段（{dim_name}）: 输入tokens≈{estimated_input_tokens}, 可用={available_max_tokens}, 设置max_tokens={max_tokens}")
                
                response = await self.llm_service.chat_completion(
                    messages=[
                        {"role": "system", "content": self.prompt_template},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=max_tokens
                )
                
                content = response.get("content", "").strip()
                logger.debug(f"第{stage_num}阶段（{dim_name}）LLM响应长度: {len(content)} 字符")
                
                result = self._parse_evaluation_result(content, partial=True)
                all_dimension_scores.update(result.get('dimension_scores', {}))
                all_dimension_details.update(result.get('dimension_details', {}))
                
                logger.info(f"✅ 第{stage_num}阶段（{dim_name}）完成")
            except Exception as e:
                logger.error(f"❌ 第{stage_num}阶段（{dim_name}）评估失败: {e}")
                # 继续执行下一阶段，不中断整个评估流程；落一条保守结果避免后续聚合缺字段
                if dim_name not in all_dimension_scores:
                    all_dimension_scores[dim_name] = 20
                if dim_name not in all_dimension_details:
                    all_dimension_details[dim_name] = {
                        "score": 20,
                        "reasoning": "该维度评估阶段异常，已按保守口径临时记分，建议人工复核。"
                    }
        
        # 第21阶段（最后一个维度：综合匹配度）完成后，生成总体评估和录用建议
        logger.info("📊 生成总体评估和录用建议...")
        
        # 计算总体得分（基于重要等级的加权平均）
        # 重要等级权重映射：调整权重梯度，突出"必备"项的控制力
        # 必备=1.5（核心硬指标，具有一票否决权）
        # 加分项=0.8（重要但非必需）
        # 弹性=0.4（一般重要）
        # 不重要=0.1（几乎不影响总分）
        importance_weights = {
            '必备': 1.5,
            '加分项': 0.8,
            '弹性': 0.4,
            '不重要': 0.1
        }
        
        total_weighted_score = 0
        total_weight = 0
        evaluated_dimensions = 0  # 已评估的维度数量
        excluded_dimensions = []  # 被排除的维度（信息不足）
        
        for dim_name, dim_detail in all_dimension_details.items():
            if isinstance(dim_detail, dict):
                score = dim_detail.get('score')
                reasoning = dim_detail.get('reasoning', '')
                
                # 如果分数为None，说明未评估，跳过
                if score is None:
                    logger.warning(f"维度 '{dim_name}' 未评估，跳过计算")
                    continue
                
                if not isinstance(score, (int, float)):
                    logger.warning(f"维度 '{dim_name}' 的分数无效: {score}，跳过计算")
                    continue
                
                # 排除"信息不足"的维度（关键修改）
                # 如果 score <= 30 且 reasoning 中包含"信息不足"相关关键词，则不计入总分
                # 扩展关键词列表，覆盖LLM实际返回的各种表达方式
                info_insufficient_keywords = [
                    '信息不足', '信息不够', '未提供', '未涉及', '未提及',
                    '无法判断', '无法准确判断', '无法直接判断', '无法明确',
                    '缺乏相关信息', '缺乏信息', '没有相关信息', '没有信息',
                    '无法从中获取', '无法获取', '无法得知', '无法了解',
                    '未具体说明', '未详细说明', '未说明', '未描述',
                    '信息量不足', '信息不完整', '信息缺失'
                ]
                
                is_info_insufficient = (
                    score <= 30 and 
                    reasoning and 
                    any(keyword in reasoning for keyword in info_insufficient_keywords)
                )
                
                if is_info_insufficient:
                    excluded_dimensions.append({
                        'dimension': dim_name,
                        'score': score,
                        'reason': '信息不足，不计入总分'
                    })
                    logger.info(f"⚠️ 排除维度 '{dim_name}': 分数={score}, 原因=信息不足（reasoning中包含'信息不足'相关关键词）")
                    continue
                
                # 从配置中获取重要等级（从core_requirements动态获取，已更新到dimensions_config中）
                importance_level = '弹性'  # 默认值
                # 通过dimension_name反向查找dim_key
                for dim_key, mapped_name in self.DIMENSION_NAME_MAPPING.items():
                    if mapped_name == dim_name:
                        dim_config = self.dimensions_config.get(dim_key, {})
                        importance_level = dim_config.get('importance_level', '弹性')
                        break
                # 如果找不到，尝试直接匹配
                if importance_level == '弹性':
                    for dim_key, dim_config in self.dimensions_config.items():
                        if dim_config['name'] == dim_name or self.DIMENSION_NAME_MAPPING.get(dim_key) == dim_name:
                            importance_level = dim_config.get('importance_level', '弹性')
                            break
                
                weight = importance_weights.get(importance_level, 0.4)  # 默认弹性（权重已调整）
                total_weighted_score += score * weight
                total_weight += weight
                evaluated_dimensions += 1
                logger.debug(f"✅ 维度 '{dim_name}': 分数={score}, 重要等级={importance_level}, 权重={weight}, 加权分数={score * weight:.2f}")
        
        # 如果没有任何评估结果，返回None而不是默认分数
        if evaluated_dimensions == 0:
            logger.error("所有维度都未评估，无法计算总体得分")
            overall_score = None
        else:
            overall_score = total_weighted_score / total_weight if total_weight > 0 else None
            excluded_count = len(excluded_dimensions)
            logger.info(f"📊 总体得分计算完成:")
            logger.info(f"   - 参与计算的维度: {evaluated_dimensions} 个")
            logger.info(f"   - 排除的维度（信息不足）: {excluded_count} 个")
            if excluded_count > 0:
                excluded_names = [d['dimension'] for d in excluded_dimensions]
                logger.info(f"   - 排除的维度列表: {', '.join(excluded_names)}")
            logger.info(f"   - 总体得分: {overall_score:.2f}")
            logger.info(f"   - 总加权分数: {total_weighted_score:.2f}, 总权重: {total_weight:.2f}")
            
            # 将评分日志写入txt文件（仅在成功计算得分时）
            if overall_score is not None:
                try:
                    self._write_score_log_to_file(
                        session_id=interview_data.get('session_id') or _session_stats.get('session_id', 'unknown'),
                        invitation_id=interview_data.get('invitation_id') or _session_stats.get('invitation_id', 'unknown'),
                        overall_score=overall_score,
                        all_dimension_scores=all_dimension_scores,
                        all_dimension_details=all_dimension_details,
                        excluded_dimensions=excluded_dimensions,
                        evaluated_dimensions=evaluated_dimensions,
                        total_weighted_score=total_weighted_score,
                        total_weight=total_weight
                    )
                except Exception as e:
                    logger.warning(f"写入评分日志文件失败: {e}")
        
        # 生成详细的评估总结和建议（结合面试回答）
        try:
            # 获取完整的面试内容用于生成建议
            try:
                full_interview_data = await self._get_complete_interview_data(
                    session_id=interview_data.get('session_id') or _session_stats.get('session_id', ''),
                    invitation_id=interview_data.get('invitation_id') or _session_stats.get('invitation_id', ''),
                    dimension_key=None  # 获取所有题目
                )
            except Exception as e:
                logger.warning(f"获取完整面试数据失败，将使用已有数据: {e}")
                full_interview_data = interview_data
            
            evaluation_summary, evaluation_suggestions, is_passed = self._generate_evaluation_summary_and_suggestions(
                all_dimension_scores, all_dimension_details, overall_score, full_interview_data
            )
        except Exception as e:
            logger.error(f"❌ 生成总体评估失败: {e}")
            # 降级处理：使用简单的总结
            if getattr(self, "interview_pass_status_mode", "threshold") == "pending":
                evaluation_summary = (
                    f"候选人综合匹配度{overall_score:.1f}分（摘要生成降级）。综合录用结论：待定，请人工复核。"
                    if overall_score is not None
                    else "评估过程中部分维度评估失败，建议人工复核。综合录用结论：待定。"
                )
                evaluation_suggestions = "系统未能生成完整建议文案，当前为待定，请 HR 人工查看维度得分与答题记录。"
                is_passed = 2
            elif overall_score is not None and overall_score >= self.interview_pass_threshold:
                evaluation_summary = f"候选人综合匹配度{overall_score:.1f}分，达到通过标准。"
                evaluation_suggestions = "建议录用。"
                is_passed = 1
            else:
                evaluation_summary = f"候选人综合匹配度{overall_score:.1f}分，未达到通过标准。" if overall_score is not None else "评估过程中部分维度评估失败，建议人工复核。"
                evaluation_suggestions = "建议不录用或进一步考察。"
                is_passed = 0

        # threshold 模式下可按总分抬升通过；pending 模式不在此处改 is_passed（保持 2）
        if overall_score is not None:
            if (
                getattr(self, "interview_pass_status_mode", "threshold") == "threshold"
                and is_passed == 0
                and overall_score >= self.interview_pass_threshold
            ):
                is_passed = 1
            logger.info(f"✅ 分阶段评估完成，共评估 {len(all_dimension_scores)} 个维度，总体得分: {overall_score:.2f}")
        else:
            logger.error("⚠️ 分阶段评估完成，但总体得分计算失败")
            overall_score = 0  # 如果计算失败，设置为0
        
        return {
            'overall_score': overall_score if overall_score is not None else 0,
            'dimension_scores': all_dimension_scores,
            'dimension_details': all_dimension_details,
            'evaluation_summary': evaluation_summary if evaluation_summary else '评估过程中部分维度评估失败，建议人工复核',
            'evaluation_suggestions': evaluation_suggestions if evaluation_suggestions else '由于部分维度评估失败，建议人力资源部门进行人工评估',
            'is_passed': is_passed
        }

    def _generate_evaluation_summary_and_suggestions(
        self, 
        dimension_scores: Dict[str, Any], 
        dimension_details: Dict[str, Any], 
        overall_score: float,
        interview_data: Dict[str, Any] = None
    ) -> tuple:
        """
        生成详细的评估总结和建议（结合面试回答）
        
        Args:
            dimension_scores: 各维度得分
            dimension_details: 各维度详情
            overall_score: 总体得分
            interview_data: 完整的面试数据（用于生成针对性建议）
            
        Returns:
            (evaluation_summary, evaluation_suggestions, is_passed)
        """
        # 容错兜底，避免上游异常数据导致总体总结阶段报错
        if not isinstance(dimension_scores, dict):
            dimension_scores = {}
        if not isinstance(dimension_details, dict):
            dimension_details = {}
        if not isinstance(interview_data, dict):
            interview_data = {}
        if not isinstance(overall_score, (int, float)):
            overall_score = 0.0

        # 分析各维度得分情况
        excellent_dimensions = []  # 优秀维度（≥85分）
        good_dimensions = []  # 良好维度（70-84分）
        average_dimensions = []  # 一般维度（60-69分）
        poor_dimensions = []  # 待提升维度（<60分）
        
        # 收集各维度的推理说明
        dimension_reasonings = {}
        for dim_name, dim_detail in dimension_details.items():
            if isinstance(dim_detail, dict):
                reasoning = dim_detail.get('reasoning', '')
                score = dim_detail.get('score')
                if reasoning and score is not None:
                    dimension_reasonings[dim_name] = {
                        'score': score,
                        'reasoning': reasoning
                    }
        
        for dim_name, score in dimension_scores.items():
            if not isinstance(score, (int, float)):
                continue
            
            if score >= 85:
                excellent_dimensions.append(dim_name)
            elif score >= 70:
                good_dimensions.append(dim_name)
            elif score >= 60:
                average_dimensions.append(dim_name)
            else:
                poor_dimensions.append(dim_name)
        
        # 提取面试回答的关键信息（用于生成针对性建议）
        interview_content = interview_data.get('interview_content', []) if interview_data else []
        if not isinstance(interview_content, list):
            interview_content = []
        interview_summary_text = ""
        if interview_content:
            # 提取主要问题和回答
            key_answers = []
            for item in interview_content[:5]:  # 只取前5个问题
                if not isinstance(item, dict):
                    continue
                question = str(item.get('question_text') or '')
                answer = str(item.get('answer_text') or '')
                if answer and len(answer) > 10:  # 只取有实质内容的回答
                    key_answers.append(f"问题：{question[:50]}... 回答：{answer[:100]}...")
            if key_answers:
                interview_summary_text = "\n".join(key_answers)
        
        # 生成评估总结（200字左右，结合面试回答）
        summary_parts = []
        pending_mode = getattr(self, "interview_pass_status_mode", "threshold") == "pending"

        # 总体评价（pending 模式下不写「达到/未达到通过标准」，避免与待定结论矛盾）
        if pending_mode:
            if overall_score >= 85:
                summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，整体表现优秀（得分仅供参考）。")
            elif overall_score >= 70:
                summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，整体表现良好（得分仅供参考）。")
            elif overall_score >= 60:
                summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，整体表现一般（得分仅供参考）。")
            else:
                summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，部分维度表现不足（得分仅供参考）。")
            summary_parts.append("综合录用结论：待定，系统未按分数线自动判定通过或未通过。")
        elif overall_score >= 85:
            summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，表现优秀，达到通过标准。")
        elif overall_score >= 70:
            summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，表现良好，基本达到通过标准。")
        elif overall_score >= 60:
            summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，表现一般，勉强达到通过标准。")
        else:
            summary_parts.append(f"候选人综合匹配度{overall_score:.1f}分，表现不足，未达到通过标准。")
        
        # 优势维度
        if excellent_dimensions:
            if len(excellent_dimensions) == 1:
                summary_parts.append(f"在{excellent_dimensions[0]}方面表现突出，")
            elif len(excellent_dimensions) == 2:
                summary_parts.append(f"在{excellent_dimensions[0]}、{excellent_dimensions[1]}等方面表现突出，")
            elif len(excellent_dimensions) <= 5:
                # 5个以内，全部列出
                excellent_dimensions_text = "、".join(excellent_dimensions)
                summary_parts.append(f"在{excellent_dimensions_text}等方面表现突出，")
            else:
                # 超过5个，列出前5个，其余用"等"表示
                excellent_dimensions_text = "、".join(excellent_dimensions[:5])
                summary_parts.append(f"在{len(excellent_dimensions)}个维度表现优秀，包括{excellent_dimensions_text}等，")
        
        # 待改进维度
        if poor_dimensions:
            if len(poor_dimensions) == 1:
                summary_parts.append(f"但在{poor_dimensions[0]}方面需要重点关注和提升。")
            elif len(poor_dimensions) == 2:
                summary_parts.append(f"但在{poor_dimensions[0]}、{poor_dimensions[1]}方面需要重点关注和提升。")
            else:
                # 清晰列出所有表现不足的维度
                poor_dimensions_text = "、".join(poor_dimensions)
                summary_parts.append(f"但在{len(poor_dimensions)}个维度表现不足，包括{poor_dimensions_text}，需要重点关注。")
        elif average_dimensions:
            if len(average_dimensions) == 1:
                summary_parts.append(f"但部分维度如{average_dimensions[0]}等仍有改进空间。")
            elif len(average_dimensions) <= 5:
                # 5个以内，全部列出
                average_dimensions_text = "、".join(average_dimensions)
                summary_parts.append(f"但部分维度如{average_dimensions_text}等仍有改进空间。")
            else:
                # 超过5个，列出前5个，其余用"等"表示
                average_dimensions_text = "、".join(average_dimensions[:5])
                summary_parts.append(f"但部分维度如{average_dimensions_text}等（共{len(average_dimensions)}个维度）仍有改进空间。")
        elif excellent_dimensions:
            summary_parts.append("整体表现较为均衡。")
        
        evaluation_summary = "".join(summary_parts)

        # 生成建议（300字左右，结合面试回答）
        suggestion_parts: List[str] = []
        if pending_mode:
            suggestion_parts.append(
                "系统不按总分自动给出「通过」或「未通过」，当前为「待定」。"
                "请 HR 或用人部门结合岗位要求、维度评语与业务情况人工裁定。"
            )
            if poor_dimensions:
                more = "等" if len(poor_dimensions) > 8 else ""
                suggestion_parts.append(
                    "相对需重点关注的维度包括："
                    + "、".join(poor_dimensions[:8])
                    + more
                    + "。"
                )
            elif average_dimensions:
                suggestion_parts.append(
                    "部分维度仍有提升空间，例如："
                    + "、".join(average_dimensions[:5])
                    + ("等。" if len(average_dimensions) > 5 else "。")
                )
            if excellent_dimensions:
                suggestion_parts.append(
                    "可作为讨论参考的优势维度包括："
                    + "、".join(excellent_dimensions[:5])
                    + ("等。" if len(excellent_dimensions) > 5 else "。")
                )
        elif overall_score >= self.interview_pass_threshold:
            suggestion_parts.append("建议录用。")

            # 结合面试回答的针对性建议
            if interview_summary_text:
                suggestion_parts.append("基于面试表现分析：")
                # 分析回答质量
                if poor_dimensions:
                    poor_reasonings = []
                    for dim_name in poor_dimensions[:3]:  # 最多3个
                        if dim_name in dimension_reasonings:
                            reasoning = dimension_reasonings[dim_name]['reasoning']
                            if reasoning and len(reasoning) > 20:
                                poor_reasonings.append(f"{dim_name}方面：{reasoning[:80]}...")
                    if poor_reasonings:
                        suggestion_parts.append(" ".join(poor_reasonings))

            # 针对性的培养建议
            if poor_dimensions:
                if len(poor_dimensions) == 1:
                    dim_reasoning = dimension_reasonings.get(poor_dimensions[0], {}).get('reasoning', '')
                    if dim_reasoning:
                        suggestion_parts.append(f"针对{poor_dimensions[0]}的不足（{dim_reasoning[:100]}），建议入职后通过专业培训、实践项目和导师指导等方式重点提升。")
                    else:
                        suggestion_parts.append(f"建议入职后重点关注{poor_dimensions[0]}的培养，可通过专业培训、实践项目和导师指导等方式提升该方面的能力。")
                else:
                    suggestion_parts.append(f"建议入职后重点关注{poor_dimensions[0]}、{poor_dimensions[1]}等方面的培养，可通过专业培训、实践项目和导师指导等方式提升相关能力。")
            elif average_dimensions:
                if len(average_dimensions) == 1:
                    suggestion_parts.append(f"建议在{average_dimensions[0]}等方面提供更多指导和支持，帮助候选人快速成长，可通过定期反馈和针对性培训来提升。")
                else:
                    suggestion_parts.append(f"建议在{average_dimensions[0]}、{average_dimensions[1]}等方面提供更多指导和支持，帮助候选人快速成长。")

            # 优势发挥建议
            if excellent_dimensions:
                if len(excellent_dimensions) == 1:
                    dim_reasoning = dimension_reasonings.get(excellent_dimensions[0], {}).get('reasoning', '')
                    if dim_reasoning:
                        suggestion_parts.append(f"候选人在{excellent_dimensions[0]}方面表现突出（{dim_reasoning[:80]}），可安排相应的工作任务，充分发挥其优势。")
                    else:
                        suggestion_parts.append(f"可充分发挥候选人在{excellent_dimensions[0]}方面的优势，安排相应的工作任务，让其在该领域发挥更大价值。")
                else:
                    suggestion_parts.append(f"可充分发挥候选人在{excellent_dimensions[0]}、{excellent_dimensions[1]}等方面的优势，安排相应的工作任务。")
        else:
            if overall_score >= 60:
                suggestion_parts.append("建议不录用或进一步考察。")
                if interview_summary_text:
                    suggestion_parts.append("基于面试表现，候选人部分维度表现不足。")
                suggestion_parts.append("如考虑录用，建议在入职后重点关注能力提升，特别是")
                if poor_dimensions:
                    if len(poor_dimensions) == 1:
                        dim_reasoning = dimension_reasonings.get(poor_dimensions[0], {}).get('reasoning', '')
                        if dim_reasoning:
                            suggestion_parts.append(f"{poor_dimensions[0]}方面（{dim_reasoning[:80]}）。")
                        else:
                            suggestion_parts.append(f"{poor_dimensions[0]}方面。")
                    else:
                        suggestion_parts.append(f"{poor_dimensions[0]}、{poor_dimensions[1]}等方面。")
                else:
                    suggestion_parts.append(f"{average_dimensions[0]}等方面。")
                suggestion_parts.append("建议制定详细的培养计划，包括培训课程、实践项目和定期评估，确保改进效果。")
            else:
                suggestion_parts.append("建议不录用。")
                if interview_summary_text:
                    suggestion_parts.append("基于面试表现分析，候选人整体能力与岗位要求存在较大差距。")
                suggestion_parts.append("多个维度表现不足，建议考虑其他更符合要求的候选人。")

        evaluation_suggestions = "".join(suggestion_parts)

        if pending_mode:
            is_passed = 2
        else:
            is_passed = 1 if overall_score >= self.interview_pass_threshold else 0

        return evaluation_summary, evaluation_suggestions, is_passed

    def _parse_evaluation_result(self, content: str, partial: bool = False) -> Dict[str, Any]:
        """
        解析LLM返回的评估结果（增强版，支持多种格式和截断情况）

        Args:
            content: LLM返回的原始内容
            partial: 是否为部分评估结果（分阶段评估时使用，不需要overall_score等字段）

        Returns:
            解析后的评估结果
        """
        try:
            import re
            
            # 1. 清理内容
            original_content = content
            content = content.strip()
            
            # 2. 尝试直接解析
            try:
                evaluation_result = json.loads(content)
                logger.debug("✅ JSON直接解析成功")
                return self._process_evaluation_result(evaluation_result, partial)
            except json.JSONDecodeError:
                pass
            
            # 3. 尝试提取 ```json { ... } ``` 中的内容
            json_match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
            if json_match:
                try:
                    evaluation_result = json.loads(json_match.group(1))
                    logger.debug("✅ 从Markdown代码块中提取JSON成功")
                    return self._process_evaluation_result(evaluation_result, partial)
                except json.JSONDecodeError:
                    pass
            
            # 4. 尝试提取任何 { ... } 结构（最外层的大括号）
            json_match = re.search(r"(\{.*\})", content, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                try:
                    evaluation_result = json.loads(json_str)
                    logger.debug("✅ 使用正则表达式提取JSON成功")
                    return self._process_evaluation_result(evaluation_result, partial)
                except json.JSONDecodeError as e:
                    # 如果还是失败，尝试修复未转义的引号
                    logger.warning(f"JSON解析失败，尝试修复未转义引号: {str(e)}")
                    try:
                        # 修复reasoning字段中未转义的引号
                        # 使用逐字符解析的方法，更可靠
                        def fix_reasoning_field(text):
                            # 找到 "reasoning" 字段的位置
                            reasoning_pos = text.find('"reasoning"')
                            if reasoning_pos == -1:
                                return text
                            
                            # 找到值的开始引号位置
                            colon_pos = text.find(':', reasoning_pos)
                            if colon_pos == -1:
                                return text
                            
                            # 跳过空白字符，找到第一个引号
                            value_start = colon_pos + 1
                            while value_start < len(text) and text[value_start] in ' \t\n\r':
                                value_start += 1
                            
                            if value_start >= len(text) or text[value_start] != '"':
                                return text
                            
                            # 从第一个引号后开始，找到值的结束位置
                            # 需要找到最后一个引号（在遇到逗号、}或换行之前）
                            value_content_start = value_start + 1
                            value_content_end = value_content_start
                            
                            # 找到reasoning值的结束位置
                            # 查找下一个未转义的引号，后面跟着逗号、}或换行
                            i = value_content_start
                            while i < len(text):
                                if text[i] == '\\' and i + 1 < len(text):
                                    i += 2  # 跳过转义字符
                                    continue
                                elif text[i] == '"':
                                    # 检查这个引号后面是否是字段结束
                                    j = i + 1
                                    while j < len(text) and text[j] in ' \t\n\r':
                                        j += 1
                                    if j >= len(text) or text[j] in ',}':
                                        value_content_end = i
                                        break
                                i += 1
                            
                            if value_content_end == value_content_start:
                                # 没找到结束引号，尝试找到最后一个引号
                                last_quote = text.rfind('"', value_content_start)
                                if last_quote > value_content_start:
                                    value_content_end = last_quote
                                else:
                                    return text
                            
                            # 提取值内容并转义其中的引号
                            value_content = text[value_content_start:value_content_end]
                            fixed_content = ""
                            i = 0
                            while i < len(value_content):
                                if value_content[i] == '\\' and i + 1 < len(value_content):
                                    # 保留已转义的字符
                                    fixed_content += value_content[i:i+2]
                                    i += 2
                                elif value_content[i] == '"':
                                    # 转义未转义的引号
                                    fixed_content += '\\"'
                                    i += 1
                                else:
                                    fixed_content += value_content[i]
                                    i += 1
                            
                            # 重构JSON字符串
                            return (text[:value_content_start] + fixed_content + 
                                   text[value_content_end:])
                        
                        fixed_json = fix_reasoning_field(json_str)
                        
                        # 如果修复后不同，尝试解析
                        if fixed_json != json_str:
                            try:
                                evaluation_result = json.loads(fixed_json)
                                logger.info("✅ 修复未转义引号后JSON解析成功")
                                return self._process_evaluation_result(evaluation_result, partial)
                            except json.JSONDecodeError as e2:
                                logger.debug(f"修复引号后仍解析失败: {e2}")
                        
                        # 如果引号修复失败，尝试截断到最后一个完整的}
                        last_brace = json_str.rfind('}')
                        if last_brace > 0:
                            truncated_content = json_str[:last_brace + 1]
                            # 尝试补全可能缺失的闭合括号
                            open_count = truncated_content.count('{')
                            close_count = truncated_content.count('}')
                            if open_count > close_count:
                                truncated_content += '}' * (open_count - close_count)
                            try:
                                evaluation_result = json.loads(truncated_content)
                                logger.info(f"✅ 截断后的JSON解析成功 (长度: {len(truncated_content)})")
                                return self._process_evaluation_result(evaluation_result, partial)
                            except json.JSONDecodeError:
                                pass
                    except Exception as fix_error:
                        logger.debug(f"修复JSON时出错: {fix_error}")
                        pass
            
            # 5. 如果所有方法都失败，分阶段模式降级返回，避免中断整轮评估
            if partial:
                logger.warning(f"JSON解析失败（partial模式），降级返回空结果。原始文本前1000字符：\n{original_content[:1000]}")
                return {"dimension_scores": {}, "dimension_details": {}}

            # 非partial模式保持抛错，让上层感知异常
            logger.error(f"JSON解析完全失败，原始文本内容如下（前1000字符）：\n{original_content[:1000]}")
            raise ValueError("LLM返回格式错误且无法修复")
            
        except Exception as e:
            logger.error(f"解析评估结果失败: {e}, 原始内容: {content[:500]}...")
            raise Exception(f"评估结果解析失败: {str(e)}")
    
    def _process_evaluation_result(self, evaluation_result: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
        """
        处理解析后的评估结果（验证、转换、补全）
        
        Args:
            evaluation_result: 解析后的JSON结果
            partial: 是否为部分评估结果
            
        Returns:
            处理后的评估结果
        """
        try:

            # 验证必需字段（分阶段评估时不需要overall_score和evaluation_summary）
            if not partial:
                required_fields = ['overall_score', 'dimension_scores', 'evaluation_summary']
                for field in required_fields:
                    if field not in evaluation_result:
                        raise ValueError(f"缺少必需字段: {field}")
                # 验证分数范围
                if not isinstance(evaluation_result.get('overall_score'), (int, float)):
                    evaluation_result['overall_score'] = 75.0
                evaluation_result['overall_score'] = max(0, min(100, evaluation_result['overall_score']))
            else:
                # 部分评估只需要dimension_scores
                if 'dimension_scores' not in evaluation_result:
                    raise ValueError("缺少必需字段: dimension_scores")

            # 验证维度分数
            dimension_scores = evaluation_result.get('dimension_scores', {})
            if not isinstance(dimension_scores, dict):
                dimension_scores = {}

            # 转换维度分数为中文键名（使用evaluation_dimension表中的dimension_name）
            chinese_dimension_scores = {}

            # 处理LLM返回的维度分数（可能包含中英文混合）
            for key, score in dimension_scores.items():
                # 如果是英文键名，转换为evaluation_dimension表中的dimension_name
                if key in self.dimensions_config:
                    # 使用映射表确保维度名称严格对应evaluation_dimension表
                    chinese_name = self.DIMENSION_NAME_MAPPING.get(key, self.dimensions_config[key]['name'])
                    chinese_dimension_scores[chinese_name] = score
                # 如果已经是中文键名，检查是否在evaluation_dimension表中
                elif key in self.DIMENSION_NAME_MAPPING.values():
                    chinese_dimension_scores[key] = score
                # 如果是未知键名，尝试匹配
                else:
                    # 查找最相似的中文名称（使用evaluation_dimension表中的名称）
                    matched = False
                    for dim_key, dim_config in self.dimensions_config.items():
                        mapped_name = self.DIMENSION_NAME_MAPPING.get(dim_key, dim_config['name'])
                        if key.lower() in mapped_name.lower() or mapped_name.lower() in key.lower():
                            chinese_dimension_scores[mapped_name] = score
                            matched = True
                            break
                    if not matched:
                        # 如果找不到匹配，保持原样
                        chinese_dimension_scores[key] = score

            # 处理dimension_scores：分阶段评估时只保留返回的维度，非分阶段评估时验证所有维度
            if partial:
                # 分阶段评估：只保留LLM返回的维度，确保分数在合理范围内
                for dim_name, score in list(chinese_dimension_scores.items()):
                    if not isinstance(score, (int, float)):
                        # 如果分数不是数字，移除该维度
                        logger.warning(f"维度 '{dim_name}' 的分数不是数字: {score}，将移除")
                        del chinese_dimension_scores[dim_name]
                    else:
                        chinese_dimension_scores[dim_name] = max(0, min(100, score))
            else:
                # 非分阶段评估：确保所有维度都有分数（使用evaluation_dimension表中的dimension_name）
                # 注意：不要为未评估的维度设置默认分数，应该基于实际回答进行评估
                for dim_key, dim_config in self.dimensions_config.items():
                    # 使用映射表确保维度名称严格对应evaluation_dimension表
                    dim_name = self.DIMENSION_NAME_MAPPING.get(dim_key, dim_config['name'])
                    if dim_name not in chinese_dimension_scores:
                        # 如果LLM没有评估该维度，不设置默认分数
                        logger.warning(f"维度 '{dim_name}' 未在LLM返回结果中")
                        # 不设置默认分数，保持为None，后续处理会标记为未评估

                    # 确保分数在合理范围内
                    if dim_name in chinese_dimension_scores:
                        score = chinese_dimension_scores[dim_name]
                        if not isinstance(score, (int, float)):
                            # 如果分数不是数字，移除该维度
                            logger.warning(f"维度 '{dim_name}' 的分数不是数字: {score}，将移除")
                            del chinese_dimension_scores[dim_name]
                        else:
                            chinese_dimension_scores[dim_name] = max(0, min(100, score))

            evaluation_result['dimension_scores'] = chinese_dimension_scores

            # 生成dimension_details（使用中文键名）
            dimension_details = evaluation_result.get('dimension_details', {})
            if not isinstance(dimension_details, dict):
                dimension_details = {}

            chinese_dimension_details = {}

            # 处理LLM返回的维度详情（使用evaluation_dimension表中的dimension_name）
            for key, detail in dimension_details.items():
                # 如果是英文键名，转换为evaluation_dimension表中的dimension_name
                if key in self.dimensions_config:
                    # 使用映射表确保维度名称严格对应evaluation_dimension表
                    chinese_name = self.DIMENSION_NAME_MAPPING.get(key, self.dimensions_config[key]['name'])
                    chinese_dimension_details[chinese_name] = detail
                # 如果已经是中文键名，检查是否在evaluation_dimension表中
                elif key in self.DIMENSION_NAME_MAPPING.values():
                    chinese_dimension_details[key] = detail
                else:
                    # 尝试匹配中文名称（使用evaluation_dimension表中的名称）
                    matched = False
                    for dim_key, dim_config in self.dimensions_config.items():
                        mapped_name = self.DIMENSION_NAME_MAPPING.get(dim_key, dim_config['name'])
                        if key.lower() in mapped_name.lower() or mapped_name.lower() in key.lower():
                            chinese_dimension_details[mapped_name] = detail
                            matched = True
                            break
                    if not matched:
                        chinese_dimension_details[key] = detail

            # 处理dimension_details：分阶段评估时只保留返回的维度，非分阶段评估时为所有维度创建条目
            if partial:
                # 分阶段评估：只保留LLM返回的维度，不为其他维度创建条目
                # 移除importance_level字段（如果存在）
                for dim_name, detail in chinese_dimension_details.items():
                    if isinstance(detail, dict):
                        if 'importance_level' in detail:
                            del detail['importance_level']
                            logger.debug(f"移除维度 '{dim_name}' 的importance_level字段")
                        # 确保有score字段（从dimension_scores中获取）
                        if 'score' not in detail and dim_name in chinese_dimension_scores:
                            detail['score'] = chinese_dimension_scores[dim_name]
            else:
                # 非分阶段评估：确保所有维度都有详情（不包含importance_level）
                # 注意：不要为未评估的维度设置默认分数，应该基于实际回答进行评估
                for dim_key, dim_config in self.dimensions_config.items():
                    # 使用映射表确保维度名称严格对应evaluation_dimension表
                    dim_name = self.DIMENSION_NAME_MAPPING.get(dim_key, dim_config['name'])
                    if dim_name not in chinese_dimension_details:
                        # 如果LLM没有评估该维度，尝试从dimension_scores中获取分数
                        score = chinese_dimension_scores.get(dim_name)
                        if score is None:
                            # 如果完全没有评估结果，标记为未评估
                            logger.warning(f"维度 '{dim_name}' 未在LLM返回结果中，标记为未评估")
                            chinese_dimension_details[dim_name] = {
                                'score': None,  # 标记为未评估
                                'reasoning': f'该维度未在评估结果中，可能因为信息不足或评估失败'
                            }
                        else:
                            # 如果有分数但没有详情，创建详情
                            chinese_dimension_details[dim_name] = {
                                'score': score,
                                'reasoning': f'基于面试表现的综合评估（该维度详情缺失，使用默认推理）'
                            }
                    else:
                        # 移除importance_level字段（如果存在）
                        detail = chinese_dimension_details[dim_name]
                        if isinstance(detail, dict):
                            # 移除importance_level字段
                            if 'importance_level' in detail:
                                del detail['importance_level']
                                logger.debug(f"移除维度 '{dim_name}' 的importance_level字段")

            evaluation_result['dimension_details'] = chinese_dimension_details

            # 设置是否通过（分阶段评估时在第3阶段设置；单次评估路径会用到）
            if not partial:
                overall_score = evaluation_result.get('overall_score', 0)
                if getattr(self, "interview_pass_status_mode", "threshold") == "pending":
                    evaluation_result["is_passed"] = 2
                else:
                    evaluation_result["is_passed"] = 1 if overall_score >= self.interview_pass_threshold else 0
            else:
                # 部分评估时不设置is_passed
                if 'is_passed' not in evaluation_result:
                    evaluation_result['is_passed'] = 0

            return evaluation_result

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"处理评估结果失败: {e}")
            raise Exception(f"评估结果处理失败: {str(e)}")

    def _add_evaluation_metadata(self, evaluation_result: Dict, llm_response: Dict) -> Dict[str, Any]:
        """添加评估元数据"""
        evaluation_result['evaluation_metadata'] = {
            'evaluator_type': 'AGENT',
            'evaluation_time': datetime.now().isoformat(),
            'llm_model': llm_response.get('model', 'unknown'),
            'dimensions_count': len(self.dimensions_config),
            'evaluation_version': '2.0'
        }

        return evaluation_result

    def _write_score_log_to_file(
        self,
        session_id: str,
        invitation_id: str,
        overall_score: float,
        all_dimension_scores: Dict[str, Any],
        all_dimension_details: Dict[str, Any],
        excluded_dimensions: List[Dict[str, Any]],
        evaluated_dimensions: int,
        total_weighted_score: float,
        total_weight: float
    ) -> None:
        """
        将最终评分日志写入txt文件
        
        Args:
            session_id: 会话ID
            invitation_id: 邀请ID
            overall_score: 总体得分
            all_dimension_scores: 所有维度分数
            all_dimension_details: 所有维度详情
            excluded_dimensions: 被排除的维度列表
            evaluated_dimensions: 参与计算的维度数量
            total_weighted_score: 总加权分数
            total_weight: 总权重
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = os.path.join(settings.LOG_DIR_EVALUATION, f"evaluation_score_log_{timestamp}_{session_id[:8]}_{invitation_id[:20]}.txt")
            
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write("=" * 100 + "\n")
                f.write("面试评估最终评分日志\n")
                f.write("=" * 100 + "\n")
                f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Session ID: {session_id}\n")
                f.write(f"Invitation ID: {invitation_id}\n")
                f.write("=" * 100 + "\n\n")
                
                # 总体得分信息
                f.write("📊 总体得分计算\n")
                f.write("-" * 100 + "\n")
                f.write(f"总体得分: {overall_score:.2f} 分\n")
                f.write(f"参与计算的维度: {evaluated_dimensions} 个\n")
                f.write(f"排除的维度（信息不足）: {len(excluded_dimensions)} 个\n")
                f.write(f"总加权分数: {total_weighted_score:.2f}\n")
                f.write(f"总权重: {total_weight:.2f}\n\n")
                
                # 权重配置
                f.write("⚙️ 权重配置\n")
                f.write("-" * 100 + "\n")
                f.write("重要等级权重映射:\n")
                f.write("  - 必备: 1.5（核心硬指标，具有一票否决权）\n")
                f.write("  - 加分项: 0.8（重要但非必需）\n")
                f.write("  - 弹性: 0.4（一般重要）\n")
                f.write("  - 不重要: 0.1（几乎不影响总分）\n\n")
                
                # 排除的维度详情
                if excluded_dimensions:
                    f.write("⚠️ 排除的维度（信息不足，不计入总分）\n")
                    f.write("-" * 100 + "\n")
                    for i, excluded in enumerate(excluded_dimensions, 1):
                        f.write(f"{i}. {excluded['dimension']}\n")
                        f.write(f"   分数: {excluded['score']} 分\n")
                        f.write(f"   原因: {excluded['reason']}\n")
                        # 获取reasoning
                        dim_name = excluded['dimension']
                        if dim_name in all_dimension_details:
                            reasoning = all_dimension_details[dim_name].get('reasoning', '')
                            if reasoning:
                                # 截断过长的reasoning，避免文件过大
                                reasoning_preview = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
                                f.write(f"   Reasoning: {reasoning_preview}\n")
                        f.write("\n")
                    f.write("\n")
                
                # 参与计算的维度详情
                f.write("✅ 参与计算的维度详情\n")
                f.write("-" * 100 + "\n")
                f.write(f"{'维度名称':<30} {'分数':<10} {'重要等级':<12} {'权重':<10} {'加权分数':<12}\n")
                f.write("-" * 100 + "\n")
                
                # 按维度名称排序
                sorted_dimensions = sorted(all_dimension_scores.items())
                for dim_name, score in sorted_dimensions:
                    # 跳过被排除的维度
                    if any(d['dimension'] == dim_name for d in excluded_dimensions):
                        continue
                    
                    # 获取重要等级和权重
                    importance_level = '弹性'
                    weight = 0.4
                    for dim_key, mapped_name in self.DIMENSION_NAME_MAPPING.items():
                        if mapped_name == dim_name:
                            dim_config = self.dimensions_config.get(dim_key, {})
                            importance_level = dim_config.get('importance_level', '弹性')
                            break
                    
                    importance_weights = {
                        '必备': 1.5,
                        '加分项': 0.8,
                        '弹性': 0.4,
                        '不重要': 0.1
                    }
                    weight = importance_weights.get(importance_level, 0.4)
                    weighted_score = score * weight
                    
                    f.write(f"{dim_name:<30} {score:<10.1f} {importance_level:<12} {weight:<10.2f} {weighted_score:<12.2f}\n")
                
                f.write("\n")
                
                # 所有维度的详细评分（包含被排除的）
                f.write("📋 所有维度详细评分\n")
                f.write("-" * 100 + "\n")
                sorted_all_dimensions = sorted(all_dimension_details.items())
                for dim_name, dim_detail in sorted_all_dimensions:
                    if isinstance(dim_detail, dict):
                        score = dim_detail.get('score', 'N/A')
                        reasoning = dim_detail.get('reasoning', '')
                        
                        # 标记是否被排除
                        is_excluded = any(d['dimension'] == dim_name for d in excluded_dimensions)
                        excluded_mark = " [已排除]" if is_excluded else ""
                        
                        f.write(f"\n【{dim_name}】{excluded_mark}\n")
                        f.write(f"分数: {score}\n")
                        if reasoning:
                            f.write(f"Reasoning: {reasoning}\n")
                        f.write("-" * 100 + "\n")
                
                f.write("\n")
                f.write("=" * 100 + "\n")
                f.write("日志结束\n")
                f.write("=" * 100 + "\n")
            
            logger.info(f"📝 评分日志已保存到: {log_file}")
            
        except Exception as e:
            logger.error(f"写入评分日志文件失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _save_evaluation_result(self, invitation_id: str, evaluation_result: Dict[str, Any]) -> bool:
        """
        保存评估结果到数据库

        Args:
            invitation_id: 邀请ID
            evaluation_result: 评估结果

        Returns:
            保存是否成功
        """
        try:
            # 准备数据
            overall_score = evaluation_result.get('overall_score', 0)
            dimension_scores = evaluation_result.get('dimension_scores', {})
            dimension_details = evaluation_result.get('dimension_details', {})
            evaluation_summary = evaluation_result.get('evaluation_summary', '')
            evaluation_suggestions = evaluation_result.get('evaluation_suggestions', '')
            is_passed = evaluation_result.get('is_passed', 0)
            evaluation_structured = evaluation_result.get('evaluation_structured')

            # 保存到知识库前统一取整，避免落库出现小数分数
            if overall_score is not None:
                overall_score = int(round(float(overall_score)))
                evaluation_result['overall_score'] = overall_score
            
            # 计算题目分数：总分 / 总题数。总题数来自该面试邀请的题目数，未作答题目按0分计入分母
            question_score = None
            try:
                # 获取该面试邀请的总题数（不限于15，以实际题目数为准）
                invitation_questions = database_service.get_invitation_questions(invitation_id)
                total_questions = len(invitation_questions) if invitation_questions else 0
                if total_questions == 0:
                    logger.warning("未找到面试题目，无法计算题目平均分")
                else:
                    # 从evaluation_result中获取session_id（如果存在）
                    session_id = evaluation_result.get('session_id')
                    if not session_id:
                        sessions = database_service.get_invitation_sessions(invitation_id, limit=1)
                        if sessions:
                            session_id = sessions[0].get('session_id')
                    
                    total_score = 0.0
                    answered_count = 0
                    if session_id:
                        answers = database_service.get_session_candidate_answers(session_id)
                        main_answers = [ans for ans in answers if not ans.get('is_follow_up', False)]
                        for ans in main_answers:
                            final_score = ans.get('final_score')
                            if final_score is not None and isinstance(final_score, (int, float)):
                                total_score += float(final_score)
                                answered_count += 1
                    
                    # 题目平均分 = 总分 / 总题数（未作答题目等价于0分）
                    question_score = total_score / total_questions
                    question_score = int(round(question_score))
                    evaluation_result['question_score'] = question_score
                    logger.info(f"计算题目分数: 总题数={total_questions}, 已作答={answered_count}, 总分={total_score:.2f}, 平均分={question_score}")
            except Exception as e:
                logger.warning(f"计算题目分数失败: {e}，将继续保存其他数据")

            # 记录详细日志
            logger.info(f"准备保存评估结果到数据库: invitation_id={invitation_id}")
            logger.debug(f"评估结果数据: overall_score={overall_score}, question_score={question_score}, dimension_scores数量={len(dimension_scores)}, dimension_details数量={len(dimension_details)}")
            logger.debug(f"evaluation_summary长度={len(evaluation_summary) if evaluation_summary else 0}, evaluation_suggestions长度={len(evaluation_suggestions) if evaluation_suggestions else 0}")
            
            # 验证数据完整性
            if not dimension_scores:
                logger.warning(f"⚠️ dimension_scores为空，可能评估失败")
            if not dimension_details:
                logger.warning(f"⚠️ dimension_details为空，可能评估失败")
            if not evaluation_summary:
                logger.warning(f"⚠️ evaluation_summary为空")

            # 补充reasoning中的题目类型信息（将#1替换为#1 基础题或#1 专业题）
            dimension_details = self._enrich_reasoning_with_question_types(
                invitation_id, dimension_details, evaluation_result.get('session_id')
            )

            # 保存到数据库
            success = await database_service.create_interview_evaluation_record(
                invitation_id=invitation_id,
                overall_score=overall_score,
                dimension_scores=dimension_scores,
                dimension_details=dimension_details,
                evaluation_summary=evaluation_summary,
                evaluation_suggestions=evaluation_suggestions,
                is_passed=is_passed,
                evaluator_type='AGENT',
                question_score=question_score,
                evaluation_structured=evaluation_structured,
            )

            if success:
                logger.info(f"✅ 评估结果保存成功: invitation_id={invitation_id}, overall_score={overall_score}, question_score={question_score}, 维度数量={len(dimension_scores)}")
            else:
                logger.error(f"❌ 评估结果保存失败: invitation_id={invitation_id}")

            return success

        except Exception as e:
            logger.error(f"保存评估结果异常: invitation_id={invitation_id}, error={e}")
            return False

    def _enrich_reasoning_with_question_types(
        self, invitation_id: str, dimension_details: Dict[str, Any], session_id: str = None
    ) -> Dict[str, Any]:
        """
        补充reasoning中的题目类型信息
        将reasoning中的题目编号（如#1、#2）替换为包含题目类型的格式（如#1 基础题、#2 专业题）
        
        Args:
            invitation_id: 邀请ID
            dimension_details: 维度详情字典
            session_id: 会话ID（可选）
            
        Returns:
            更新后的维度详情字典
        """
        try:
            import re
            
            # 获取题目编号到题目类型的映射
            question_type_map = {}  # {question_number: question_type_name}
            
            # 从数据库查询该invitation的所有题目
            try:
                query = """
                    SELECT question_order, question_type
                    FROM interview_question
                    WHERE invitation_id = %s
                    ORDER BY question_order ASC
                """
                results = database_service.db.execute_query(query, (invitation_id,))
                
                for row in results:
                    question_order = row.get('question_order')
                    question_type = row.get('question_type', 'BASIC')
                    question_type_name = "专业题" if question_type == 'SPECIALTY' else "基础题"
                    if question_order:
                        question_type_map[question_order] = question_type_name
                
                logger.debug(f"获取到 {len(question_type_map)} 个题目的类型映射")
            except Exception as e:
                logger.warning(f"查询题目类型映射失败: {e}")
                return dimension_details  # 如果查询失败，返回原始数据
            
            # 更新每个维度的reasoning
            enriched_details = {}
            for dim_name, dim_detail in dimension_details.items():
                if not isinstance(dim_detail, dict):
                    enriched_details[dim_name] = dim_detail
                    continue
                
                reasoning = dim_detail.get('reasoning', '')
                if not reasoning:
                    enriched_details[dim_name] = dim_detail
                    continue
                
                # 查找reasoning中的所有题目编号（格式：#数字）
                updated_reasoning = reasoning
                pattern = r'#(\d+)'
                
                def replace_with_type(match):
                    question_num = int(match.group(1))
                    question_type_name = question_type_map.get(question_num, '')
                    if question_type_name:
                        return f"#{question_num} {question_type_name}"
                    return match.group(0)  # 如果找不到类型，保持原样
                
                # 替换所有题目编号
                updated_reasoning = re.sub(pattern, replace_with_type, updated_reasoning)
                
                # 创建更新后的详情
                updated_detail = dim_detail.copy()
                updated_detail['reasoning'] = updated_reasoning
                enriched_details[dim_name] = updated_detail
                
                # 如果reasoning有变化，记录日志
                if updated_reasoning != reasoning:
                    logger.debug(f"维度 {dim_name} 的reasoning已补充题目类型信息")
            
            return enriched_details
            
        except Exception as e:
            logger.warning(f"补充reasoning题目类型信息失败: {e}")
            return dimension_details  # 如果处理失败，返回原始数据

    def _load_narrative_prompt(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "prompts", "interview_evaluation_narrative_prompt.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"叙事提示词加载失败，使用内置兜底: {e}")
            return "你是招聘助手，仅输出合法 JSON，包含 conclusion、highlights、risks、follow_up_items。"

    def _reasoning_looks_insufficient(self, reasoning: str, score: Any) -> bool:
        if not reasoning or not isinstance(reasoning, str):
            return False
        if not isinstance(score, (int, float)):
            return False
        if score > 30:
            return False
        return any(kw in reasoning for kw in self._INFO_INSUFFICIENT_KEYWORDS)

    def _collapse_sparse_insufficient_reasonings(self, dimension_details: Dict[str, Any]) -> Dict[str, Any]:
        """多条重复「信息不足」话术时折叠为统一说明。"""
        if not isinstance(dimension_details, dict):
            return dimension_details
        sparse_names: List[str] = []
        for dim_name, detail in dimension_details.items():
            if not isinstance(detail, dict):
                continue
            if self._reasoning_looks_insufficient(detail.get("reasoning", ""), detail.get("score")):
                sparse_names.append(dim_name)
        if len(sparse_names) < 2:
            return dimension_details
        unified = (
            "多个维度在本次面试中可依据的作答信号较少，已在各维度采用保守评分；"
            "建议在复试中就岗位关键能力做针对性核实与追问。"
        )
        out = dict(dimension_details)
        for dim_name in sparse_names:
            d = out.get(dim_name)
            if isinstance(d, dict):
                new_d = dict(d)
                new_d["reasoning"] = unified
                out[dim_name] = new_d
        return out

    def _sanitize_brief_for_table(self, reasoning: str) -> str:
        """简评：弱化回答原文引用，偏评分逻辑总结（供表格展示）。"""
        if not reasoning or not isinstance(reasoning, str):
            return ""
        import re
        t = reasoning.strip()
        t = re.sub(r'"[^"]{6,}"', "（作答节选略）", t)
        t = re.sub(r"＂[^＂]{6,}＂", "（作答节选略）", t)
        t = re.sub(r"#\d+\s*\+?", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 100:
            t = t[:100].rstrip() + "…"
        return t

    def _annotate_interviewer_dimension_details(
        self,
        dimension_details: Dict[str, Any],
        dimension_scores: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """写入 interviewer_label、table_category、brief；未考察维度打标。"""
        if not isinstance(dimension_details, dict):
            return dimension_details
        scores = dimension_scores if isinstance(dimension_scores, dict) else {}
        out = {}
        for dim_name, detail in dimension_details.items():
            if not isinstance(detail, dict):
                out[dim_name] = detail
                continue
            nd = dict(detail)
            label = self.INTERVIEWER_FRIENDLY_LABEL_BY_DIMENSION.get(dim_name, "综合")
            nd["interviewer_label"] = label
            nd["table_category"] = self.DIMENSION_TABLE_CATEGORY_BY_DIMENSION.get(dim_name, "综合")
            raw_score = scores.get(dim_name, nd.get("score"))
            if not isinstance(raw_score, (int, float)):
                nd["exam_status"] = "未考察"
                nd["brief"] = "未考察"
            else:
                r = nd.get("reasoning") or ""
                sanitized = self._sanitize_brief_for_table(r) if isinstance(r, str) else ""
                nd["brief"] = sanitized if sanitized else (r[:100] + "…" if isinstance(r, str) and len(r) > 100 else (r or "—"))
                nd["exam_status"] = "已考察"
            out[dim_name] = nd
        return out

    def _resolve_basic_pro_scores_for_structured(
        self,
        evaluation_result: Dict[str, Any],
        invitation_id: str,
        session_id: str,
    ):
        """
        evaluation_structured 中的基础题/专业题得分：优先取 evaluation_result 内显式字段（模板对齐），
        否则按题目类型均分计算并四舍五入为整数（与报告模板「78 分」样式一致）。
        """
        basic_keys = ("template_basic_score", "basic_question_score", "report_basic_score")
        pro_keys = ("template_specialty_score", "specialty_question_score", "professional_question_score", "report_specialty_score")

        basic_v = None
        for k in basic_keys:
            v = evaluation_result.get(k)
            if isinstance(v, (int, float)):
                basic_v = int(round(float(v)))
                break
        pro_v = None
        for k in pro_keys:
            v = evaluation_result.get(k)
            if isinstance(v, (int, float)):
                pro_v = int(round(float(v)))
                break

        cb, cp = self._compute_basic_pro_avg_scores(invitation_id, session_id)
        if basic_v is None and cb is not None:
            basic_v = int(round(cb))
        if pro_v is None and cp is not None:
            pro_v = int(round(cp))
        return basic_v, pro_v

    def _compute_basic_pro_avg_scores(self, invitation_id: str, session_id: str):
        """按题目元数据 BASIC / SPECIALTY 分别求均分，未作答题目计 0 分。"""
        try:
            invitation_questions = database_service.get_invitation_questions(invitation_id) or []
            answers = database_service.get_session_candidate_answers(session_id) or []
            main_answers = [a for a in answers if not a.get("is_follow_up", False)]
            last_by_qid: Dict[str, Dict[str, Any]] = {}
            for row in main_answers:
                qid = row.get("question_id")
                if qid:
                    last_by_qid[qid] = row
            basic_sum, basic_n, pro_sum, pro_n = 0.0, 0, 0.0, 0
            for q in sorted(invitation_questions, key=lambda x: (x.get("question_order") is None, x.get("question_order") or 0)):
                qid = q.get("question_id")
                qtype = (q.get("question_type") or "BASIC").upper()
                is_pro = qtype == "SPECIALTY"
                if is_pro:
                    pro_n += 1
                else:
                    basic_n += 1
                ans = last_by_qid.get(qid) if qid else None
                fs = ans.get("final_score") if ans else None
                val = float(fs) if isinstance(fs, (int, float)) else 0.0
                if is_pro:
                    pro_sum += val
                else:
                    basic_sum += val
            basic_avg = round(basic_sum / basic_n, 2) if basic_n else None
            pro_avg = round(pro_sum / pro_n, 2) if pro_n else None

            return basic_avg, pro_avg
        except Exception as e:
            logger.warning(f"计算基础/专业均分失败: {e}")
            return None, None

    def _fallback_narrative_payload(self, evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
        overall = evaluation_result.get("overall_score")
        if not isinstance(overall, (int, float)):
            overall = 0
        summary = (evaluation_result.get("evaluation_summary") or "").strip()
        conclusion = summary[:100] + ("…" if len(summary) > 100 else "") if summary else f"综合得分约 {overall:.0f} 分，建议结合岗位要求人工复核是否进入复试。"
        scores = [
            (k, float(v))
            for k, v in (evaluation_result.get("dimension_scores") or {}).items()
            if isinstance(v, (int, float))
        ]
        scores.sort(key=lambda x: -x[1])
        highlights = [f"「{d}」相对突出（约 {s:.0f} 分）。" for d, s in scores[:3]]
        low = sorted(scores, key=lambda x: x[1])[:3]
        risks = [f"「{d}」得分偏低（约 {s:.0f} 分），建议复试核实。" for d, s in low if s < 70]
        if not risks and scores:
            risks = [f"整体约 {overall:.0f} 分，建议对关键能力点做情景追问。"]
        follow = [
            {
                "theme": "能力核实",
                "suggestion": "结合真实业务场景追问设计与排障经历，核实岗位核心技能深度",
                "verify_item": "能力核实",
                "follow_up_direction": "结合真实业务场景追问设计与排障经历，核实岗位核心技能深度",
            }
        ]
        return {
            "conclusion": conclusion,
            "highlights": highlights[:4] or ["面试记录可支持基础判断。"],
            "risks": risks[:4] or ["建议业务面试官关注与 JD 强相关的短板。"],
            "follow_up_items": follow,
        }

    async def _generate_narrative_with_llm(
        self, evaluation_result: Dict[str, Any], job_requirements: Dict[str, Any]
    ) -> Dict[str, Any]:
        system_prompt = self._load_narrative_prompt()
        dd = evaluation_result.get("dimension_details") or {}
        dims_payload = []
        for name, score in (evaluation_result.get("dimension_scores") or {}).items():
            if not isinstance(score, (int, float)):
                continue
            det = dd.get(name) if isinstance(dd, dict) else None
            snippet = ""
            if isinstance(det, dict):
                snippet = (det.get("brief") or det.get("reasoning") or "")[:200]
            dims_payload.append({"name": name, "score": score, "snippet": snippet})
        cb, cp = self._resolve_basic_pro_scores_for_structured(
            evaluation_result,
            evaluation_result.get("invitation_id") or job_requirements.get("invitation_id") or "",
            evaluation_result.get("session_id") or "",
        )
        user_obj = {
            "position": job_requirements.get("position", "未知岗位"),
            "overall_score": evaluation_result.get("overall_score"),
            "基础题得分": cb,
            "专业题得分": cp,
            "summary_hint": (evaluation_result.get("evaluation_summary") or "")[:300],
            "dimensions": dims_payload,
        }
        user_text = json.dumps(user_obj, ensure_ascii=False)
        try:
            response = await self.llm_service.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.2,
                max_tokens=1200,
            )
            content = (response.get("content") or "").strip()
            parsed = self._parse_narrative_json(content)
            if parsed:
                return parsed
        except Exception as e:
            logger.warning(f"叙事 LLM 调用失败: {e}")
        return self._fallback_narrative_payload(evaluation_result)

    def _normalize_narrative_object(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """兼容模板字段名：总体建议、主要优势、主要劣势、复试重点考察方向。"""
        if not isinstance(obj, dict):
            return obj
        if obj.get("总体建议") and not obj.get("conclusion"):
            obj["conclusion"] = str(obj["总体建议"]).strip()
        if obj.get("主要优势") is not None and not obj.get("highlights"):
            v = obj["主要优势"]
            obj["highlights"] = v if isinstance(v, list) else [str(v).strip()]
        if obj.get("主要劣势") is not None and not obj.get("risks"):
            v = obj["主要劣势"]
            obj["risks"] = v if isinstance(v, list) else [str(v).strip()]
        if obj.get("复试重点考察方向") is not None and not obj.get("follow_up_items"):
            v = obj["复试重点考察方向"]
            obj["follow_up_items"] = v if isinstance(v, list) else [{"suggestion": str(v).strip()}]
        return obj

    def _parse_narrative_json(self, content: str) -> Optional[Dict[str, Any]]:
        import re
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                self._normalize_narrative_object(obj)
                if obj.get("conclusion") or obj.get("highlights") or obj.get("risks") or obj.get("follow_up_items"):
                    return obj
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}\s*$", text)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    self._normalize_narrative_object(obj)
                    if obj.get("conclusion") or obj.get("highlights") or obj.get("risks") or obj.get("follow_up_items"):
                        return obj
            except json.JSONDecodeError:
                pass
        return None

    def _load_core_summary_prompt(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "prompts", "interview_evaluation_core_summary_prompt.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"核心总结提示词加载失败: {e}")
            return "你是招聘助手，仅输出合法 JSON，含 title 与 sections 数组。"

    def _get_strict_jd_selected_dimensions(self, job_requirements: Dict[str, Any]) -> set[str]:
        """严格模式：仅保留 JD 明确勾选且非「不重要」的维度。"""
        selected: set[str] = set()
        dim_importance = job_requirements.get("dimension_importance") or {}
        if not isinstance(dim_importance, dict):
            return selected
        for dim_name, imp in dim_importance.items():
            if imp in ("必备", "加分项", "弹性") and isinstance(dim_name, str) and dim_name.strip():
                selected.add(dim_name.strip())
        return selected

    def _build_jd_dimensions_for_core_summary(self, strict_selected_dims: Optional[set[str]] = None) -> List[Dict[str, Any]]:
        """构造核心总结维度：严格模式下仅保留 JD 明确勾选的维度。"""
        rows: List[Dict[str, Any]] = []
        for dim_key, cfg in self.dimensions_config.items():
            name = self.DIMENSION_NAME_MAPPING.get(dim_key, cfg["name"])
            if strict_selected_dims is not None and name not in strict_selected_dims:
                continue
            imp = cfg.get("importance_level", "弹性")
            if imp not in ("必备", "加分项", "弹性", "不重要"):
                imp = "弹性"
            if imp == "不重要":
                continue
            rows.append({
                "dimension_name": name,
                "importance_level": imp,
                "jd_layer": cfg.get("category", ""),
            })
        return rows

    def _build_core_summary_evidence_payload(
        self,
        evaluation_result: Dict[str, Any],
        strict_selected_dims: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        dd = evaluation_result.get("dimension_details") or {}
        ds = evaluation_result.get("dimension_scores") or {}
        if not isinstance(ds, dict):
            return []
        out: List[Dict[str, Any]] = []
        for dim_name, sc in ds.items():
            if strict_selected_dims is not None and dim_name not in strict_selected_dims:
                continue
            det = dd.get(dim_name, {}) if isinstance(dd, dict) else {}
            if not isinstance(det, dict):
                det = {}
            out.append({
                "dimension_name": dim_name,
                "score": sc,
                "brief": det.get("brief", ""),
                "exam_status": det.get("exam_status", ""),
            })
        return out

    def _parse_core_summary_json(self, content: str) -> Optional[Dict[str, Any]]:
        import re
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("sections"), list) and obj["sections"]:
                return obj
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}\s*$", text)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and isinstance(obj.get("sections"), list) and obj["sections"]:
                    return obj
            except json.JSONDecodeError:
                pass
        return None

    def _fallback_core_competency_summary(self, evaluation_result: Dict[str, Any]) -> Dict[str, Any]:
        """LLM 失败时按重要等级分组生成简要总结。"""
        title = "核心能力总结（以JD需求为纲，目前不展示不重要的维度）"
        strict_selected_dims = set(
            k for k in (evaluation_result.get("strict_selected_dimensions") or []) if isinstance(k, str)
        )
        if not strict_selected_dims:
            return {
                "title": title,
                "sections": [{
                    "heading": "核心能力",
                    "entries": [{
                        "dimension_name": "JD配置",
                        "importance_level": "说明",
                        "analysis": "当前 JD 未明确勾选可展示维度，核心能力总结不展示维度项。",
                    }],
                }],
            }
        jd_rows = self._build_jd_dimensions_for_core_summary(
            strict_selected_dims if strict_selected_dims else None
        )
        dd = evaluation_result.get("dimension_details") or {}
        ds = evaluation_result.get("dimension_scores") or {}
        if not isinstance(dd, dict):
            dd = {}
        if not isinstance(ds, dict):
            ds = {}

        def analysis_for(dim_name: str, imp: str) -> str:
            det = dd.get(dim_name, {})
            if not isinstance(det, dict):
                det = {}
            sc = ds.get(dim_name)
            brief = (det.get("brief") or "").strip()
            if not isinstance(sc, (int, float)):
                return (brief + "。" if brief and brief != "—" else "") + "本次缺乏有效打分依据，建议复试针对性补测。"
            s = int(round(float(sc)))
            base = f"得分约{s}分。"
            if brief and brief != "—" and brief != "未考察":
                return f"{base}{brief}"
            return f"{base}请结合岗位 JD 判断是否满足要求。"

        heading_by_imp = {
            "必备": "（一）岗位必备项（核心技术能力与关键硬性要求）",
            "加分项": "（二）岗位加分项（业务与综合适配）",
            "弹性": "（三）岗位弹性项（扩展能力）",
        }
        sections: List[Dict[str, Any]] = []
        for imp, heading in heading_by_imp.items():
            dims = [r for r in jd_rows if r["importance_level"] == imp]
            if not dims:
                continue
            entries = []
            for r in dims:
                dn = r["dimension_name"]
                entries.append({
                    "dimension_name": dn,
                    "importance_level": imp,
                    "analysis": analysis_for(dn, imp),
                })
            sections.append({"heading": heading, "entries": entries})

        if not sections:
            sections.append({
                "heading": "核心能力",
                "entries": [{
                    "dimension_name": "JD配置",
                    "importance_level": "说明",
                    "analysis": "JD 已勾选维度暂无有效评估内容，建议人工复核。",
                }],
            })
        return {"title": title, "sections": sections}

    async def _generate_core_competency_summary_llm(
        self,
        evaluation_result: Dict[str, Any],
        job_requirements: Dict[str, Any],
    ) -> Dict[str, Any]:
        system_prompt = self._load_core_summary_prompt()
        strict_selected_dims = self._get_strict_jd_selected_dimensions(job_requirements)
        evaluation_result["strict_selected_dimensions"] = sorted(strict_selected_dims)
        if not strict_selected_dims:
            return {
                "title": "核心能力总结（以JD需求为纲，目前不展示不重要的维度）",
                "sections": [{
                    "heading": "核心能力",
                    "entries": [{
                        "dimension_name": "JD配置",
                        "importance_level": "说明",
                        "analysis": "当前 JD 未明确勾选可展示维度，核心能力总结不展示维度项。",
                    }],
                }],
            }
        jd_dims = self._build_jd_dimensions_for_core_summary(strict_selected_dims)
        evidence = self._build_core_summary_evidence_payload(evaluation_result, strict_selected_dims)
        cr = job_requirements.get("core_requirements") or ""
        if isinstance(cr, str) and len(cr) > 1500:
            cr = cr[:1500] + "…"
        user_obj = {
            "position": job_requirements.get("position", "未知岗位"),
            "jd_dimensions_with_importance": jd_dims,
            "evaluation_per_dimension": evidence,
            "core_requirements_json_excerpt": cr,
        }
        user_text = json.dumps(user_obj, ensure_ascii=False)
        try:
            response = await self.llm_service.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.25,
                max_tokens=3500,
            )
            content = (response.get("content") or "").strip()
            parsed = self._parse_core_summary_json(content)
            if parsed:
                if not parsed.get("title"):
                    parsed["title"] = "核心能力总结（以JD需求为纲，目前不展示不重要的维度）"
                # 规范化 entries
                clean_sections = []
                for sec in parsed.get("sections", []):
                    if not isinstance(sec, dict):
                        continue
                    h = str(sec.get("heading", "")).strip()
                    ents = []
                    for e in sec.get("entries", []) or []:
                        if not isinstance(e, dict):
                            continue
                        dn = str(e.get("dimension_name", "")).strip()
                        il = str(e.get("importance_level", "")).strip()
                        an = str(e.get("analysis", "")).strip()
                        if il == "不重要":
                            continue
                        if dn and strict_selected_dims and dn not in strict_selected_dims:
                            continue
                        if dn and an:
                            ents.append({
                                "dimension_name": dn,
                                "importance_level": il or "弹性",
                                "analysis": an,
                            })
                    if h and ents:
                        clean_sections.append({"heading": h, "entries": ents})
                if clean_sections:
                    return {"title": parsed.get("title"), "sections": clean_sections}
        except Exception as e:
            logger.warning(f"核心能力总结 LLM 失败: {e}")
        return self._fallback_core_competency_summary(evaluation_result)

    async def _post_aggregate_enrich(
        self,
        evaluation_result: Dict[str, Any],
        session_id: str,
        invitation_id: str,
        job_requirements: Dict[str, Any],
    ) -> Dict[str, Any]:
        """聚合后：话术兜底、表格分类/未考察打标、模板化分数与 narrative 写入 evaluation_structured。"""
        try:
            evaluation_result.setdefault("invitation_id", invitation_id)
            evaluation_result.setdefault("session_id", session_id)

            dd = evaluation_result.get("dimension_details")
            dim_scores = evaluation_result.get("dimension_scores") or {}
            if isinstance(dd, dict):
                dd = self._collapse_sparse_insufficient_reasonings(dict(dd))
                dd = self._annotate_interviewer_dimension_details(dd, dim_scores)
                evaluation_result["dimension_details"] = dd

            unexamined_dimensions = [
                name for name, d in (dd or {}).items()
                if isinstance(d, dict) and d.get("exam_status") == "未考察"
            ]

            basic_avg, pro_avg = self._resolve_basic_pro_scores_for_structured(
                evaluation_result, invitation_id, session_id
            )
            narrative = await self._generate_narrative_with_llm(evaluation_result, job_requirements)
            core_summary = await self._generate_core_competency_summary_llm(evaluation_result, job_requirements)

            def _as_list(_narrative, key):
                v = _narrative.get(key)
                if isinstance(v, list):
                    return [str(i).strip() for i in v if str(i).strip()]
                return []

            structured = {
                "basic_avg_score": basic_avg,
                "pro_avg_score": pro_avg,
                "conclusion": (narrative.get("conclusion") or "").strip(),
                "highlights": _as_list(narrative, "highlights"),
                "risks": _as_list(narrative, "risks"),
                "follow_up_items": narrative.get("follow_up_items") if isinstance(narrative.get("follow_up_items"), list) else [],
                "unexamined_dimensions": unexamined_dimensions,
                "core_competency_summary": core_summary,
            }

            # 报告展示层：隐藏 JD 中标记为「不重要」的维度，并统一标题为「维度评分」
            all_dim_scores = dim_scores if isinstance(dim_scores, dict) else {}
            all_dim_details = dd if isinstance(dd, dict) else {}
            strict_selected_dims = self._get_strict_jd_selected_dimensions(job_requirements)
            report_dimension_scores = {
                k: v for k, v in all_dim_scores.items() if k in strict_selected_dims
            }
            report_dimension_details = {
                k: v for k, v in all_dim_details.items() if k in strict_selected_dims
            }
            structured["dimension_score_title"] = "维度评分"
            structured["dimension_scores"] = report_dimension_scores
            structured["dimension_details"] = report_dimension_details
            structured["core_summary_title"] = "核心能力总结（以JD需求为纲，目前不展示不重要的维度）"
            # 规范化 follow_up_items（支持 theme + suggestion 复试重点考察方向）
            norm_items = []
            for it in structured["follow_up_items"]:
                if isinstance(it, dict):
                    theme = str(it.get("theme", "")).strip()
                    sug = str(it.get("suggestion", "")).strip()
                    vi = str(it.get("verify_item", "")).strip()
                    reason = str(it.get("reason", "")).strip()
                    direction = str(it.get("follow_up_direction", "")).strip()
                    if theme and sug:
                        disp = f"{theme}：{sug}"
                        norm_items.append({
                            "theme": theme,
                            "suggestion": sug,
                            "verify_item": theme,
                            "reason": reason or "复试重点考察方向",
                            "follow_up_direction": sug,
                            "display_line": disp,
                        })
                    elif vi or direction or reason:
                        disp = "：".join([p for p in (vi, direction) if p])
                        norm_items.append({
                            "theme": vi or "考察建议",
                            "suggestion": direction or reason or "",
                            "verify_item": vi or "考察建议",
                            "reason": reason,
                            "follow_up_direction": direction,
                            "display_line": disp or vi or reason or direction,
                        })
            structured["follow_up_items"] = [x for x in norm_items if x.get("follow_up_direction") or x.get("suggestion")]

            # 未考察维度 → 自动并入复试建议（建议考察点）
            if len(unexamined_dimensions) <= 8:
                for dim_name in unexamined_dimensions:
                    structured["follow_up_items"].append({
                        "theme": "维度补测",
                        "suggestion": f"「{dim_name}」本次信号不足或未考察，复试中建议设计情景题或追问补测",
                        "verify_item": "维度补测",
                        "reason": "该维度缺乏有效作答信号",
                        "follow_up_direction": f"针对性考察「{dim_name}」",
                        "display_line": f"维度补测：「{dim_name}」建议复试补测",
                    })
            elif unexamined_dimensions:
                joined = "、".join(unexamined_dimensions[:12])
                more = f"等共{len(unexamined_dimensions)}项" if len(unexamined_dimensions) > 12 else ""
                structured["follow_up_items"].append({
                    "theme": "多维补测",
                    "suggestion": f"以下能力维度本次缺乏有效信号，建议复试分轮设计情景题补测：{joined}{more}",
                    "verify_item": "多维补测",
                    "reason": "多维度未考察或信号不足",
                    "follow_up_direction": "按岗位优先级安排补测与追问",
                    "display_line": f"多维补测：{joined}{more}",
                })

            if not structured["conclusion"] or not structured["highlights"]:
                fb = self._fallback_narrative_payload(evaluation_result)
                if not structured["conclusion"]:
                    structured["conclusion"] = fb.get("conclusion", "")
                if not structured["highlights"]:
                    structured["highlights"] = fb.get("highlights", [])
                if not structured["risks"]:
                    structured["risks"] = fb.get("risks", [])
                if not structured["follow_up_items"]:
                    structured["follow_up_items"] = fb.get("follow_up_items", [])

            evaluation_result["basic_avg_score"] = basic_avg
            evaluation_result["pro_avg_score"] = pro_avg
            evaluation_result["conclusion"] = structured["conclusion"]
            evaluation_result["highlights"] = structured["highlights"]
            evaluation_result["risks"] = structured["risks"]
            evaluation_result["follow_up_items"] = structured["follow_up_items"]
            evaluation_result["core_competency_summary"] = core_summary
            # 保留完整维度用于审计/分析，同时将默认展示维度切换为过滤后的报告维度
            evaluation_result["all_dimension_scores"] = all_dim_scores
            evaluation_result["all_dimension_details"] = all_dim_details
            evaluation_result["dimension_scores"] = report_dimension_scores
            evaluation_result["dimension_details"] = report_dimension_details
            evaluation_result["dimension_score_title"] = "维度评分"
            evaluation_result["evaluation_structured"] = structured
            evaluation_result["unexamined_dimensions"] = unexamined_dimensions
        except Exception as e:
            logger.warning(f"_post_aggregate_enrich 失败，跳过部分字段: {e}")
        return evaluation_result

    def _get_default_evaluation_result(self, error_message: str, session_id: str, invitation_id: str) -> Dict[str, Any]:
        """
        获取默认评估结果（异常情况下的降级处理）

        Args:
            error_message: 错误信息
            session_id: 会话ID
            invitation_id: 邀请ID

        Returns:
            默认评估结果
        """
        # 生成默认维度分数（仅在异常情况下使用）
        # 注意：不要使用默认分数，应该标记为评估失败
        default_dimension_scores = {}
        default_dimension_details = {}

        for dim_key, dim_config in self.dimensions_config.items():
            dim_name = dim_config['name']
            # 不设置默认分数，标记为未评估
            default_dimension_scores[dim_name] = None
            default_dimension_details[dim_name] = {
                'score': None,
                'reasoning': f'评估异常，无法评估该维度。错误信息：{error_message}。建议人工复核。'
            }

        return {
            'overall_score': 0.0,  # 评估失败，设置为0
            'dimension_scores': default_dimension_scores,
            'dimension_details': default_dimension_details,
            'evaluation_summary': f'评估过程中发生异常：{error_message}。所有维度均未成功评估，强烈建议人工复核。',
            'evaluation_suggestions': f'由于系统评估异常（{error_message}），所有维度评估失败。建议人力资源部门立即进行人工评估和面试复核，不要依赖此次自动评估结果。',
            'is_passed': 0,
            'conclusion': '',
            'highlights': [],
            'risks': [],
            'follow_up_items': [],
            'core_competency_summary': None,
            'basic_avg_score': None,
            'pro_avg_score': None,
            'evaluation_structured': None,
            'evaluation_metadata': {
                'evaluator_type': 'AGENT',
                'evaluation_time': datetime.now().isoformat(),
                'error_occurred': True,
                'error_message': error_message,
                'dimensions_count': len(self.dimensions_config),
                'evaluation_version': '2.0'
            }
        }



# 创建全局服务实例
try:
    from config.settings import settings
    # 修复：get_config() 需要参数
    scoring_config = settings.get_config("scoring_thresholds")
    config = {
        'evaluation': {
            'interview_pass_threshold': scoring_config.get('interview_pass_threshold', 60),
            'interview_pass_status_mode': scoring_config.get(
                'interview_pass_status_mode',
                getattr(settings, 'INTERVIEW_PASS_STATUS_MODE', 'threshold'),
            ),
        }
    }
    interview_evaluation_service = InterviewEvaluationService(config)
    logger.info(
        f"面试评估服务全局实例创建成功，通过阈值: {config['evaluation']['interview_pass_threshold']}分，"
        f"录用结论模式: {config['evaluation']['interview_pass_status_mode']}"
    )
except ImportError as e:
    # 如果无法导入配置，使用默认配置
    logger.warning(f"无法导入配置: {e}，使用默认面试评估阈值60分")
    interview_evaluation_service = InterviewEvaluationService()
except Exception as e:
    # 如果配置加载失败，使用默认配置
    logger.warning(f"加载配置失败: {e}，使用默认面试评估阈值60分")
    interview_evaluation_service = InterviewEvaluationService()
