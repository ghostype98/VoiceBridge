# 面试 21 维度评估 — 提示词与输出契约（v2.1）

运行时**单维度阶段**使用 `interview_evaluation_system_prompt.md`；**聚合叙事**使用 `interview_evaluation_narrative_prompt.md`。

## 单维度阶段
- `dimension_scores` / `dimension_details`：`score` + `reasoning`（宜短，避免大段回答引用）。

## 聚合叙事 JSON（LLM 输出，映射报告模板）
- `conclusion` ← **总体建议**
- `highlights` ← **主要优势**
- `risks` ← **主要劣势 / 风险**
- `follow_up_items` ← **复试重点考察方向**（推荐 `{ "theme", "suggestion" }`，兼容 `verify_item` / `reason` / `follow_up_direction`）

## 核心能力总结（第二次聚合 LLM）
- 独立提示词：`interview_evaluation_core_summary_prompt.md`
- 落库字段：`evaluation_structured.core_competency_summary`（`title` + `sections[].heading` + `sections[].entries[]`）

## evaluation_structured（落库，由 `_post_aggregate_enrich` 组装）
- `core_competency_summary`：见上节。
- `basic_avg_score` / `pro_avg_score`：**整数**，优先取 `evaluation_result` 中 `template_basic_score`、`template_specialty_score`（或 `basic_question_score` / `professional_question_score`）；否则按题目类型 **BASIC/SPECIALTY** 均分四舍五入。
- `unexamined_dimensions`：`dimension_scores` 中无有效数值的维度列表。
- `follow_up_items`：叙事结果规范化后，**自动追加**未考察维度的补测建议（条数多时每批合并一条）。

## dimension_details 展示字段（后端写入）
- `table_category`：可选归并标签（技术面 / 基础素质 / …），当前前端表格不单独展示该列
- `exam_status`：`已考察` | `未考察`
- `brief`：表格用简评（弱化原文引用）

## 话术
- 禁止工程模板句；面向业务面试官。
