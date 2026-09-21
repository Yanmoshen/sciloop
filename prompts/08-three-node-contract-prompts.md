# 三节点可粘贴提示词（与编排层契约对齐版）

这三段提示词与 SciLoop 研究节点编排层**强制的字段和校验规则**一致（导出源码：`backend/app/services/research/contracts.py` 与 `rules.py`）。
区别只在于：程序版会自动检索论文库、自动校验、驳回重跑；手工版**由你自己对照规则检查**。

## 怎么用

1. 把「节点①」整段贴进对话，填好输入区，让模型只输出一个 JSON 对象。
2. 把①的 JSON **原样**贴进「节点②」的「上一节点成果」槽，再发一次。
3. 同理接「节点③」。每段都自带交接说明，换对话也不会丢上下文。

## 三段共用约定（每段里都重复了一遍，不必单独记）

- **只输出一个 JSON 对象**，不要解释文字、不要代码围栏。
- **不要新增字段**：契约是封闭的（`additionalProperties: false`），多写字段会被判为不合契约。
- **不得编造**：论文编号、卡片字段名、原文片段编号都必须来自你实际拿到的材料。
  没有可定位来源的论文就不要拿它当证据，改写进 `limits` 说明缺什么。
- **没查到 ≠ 不存在**：如实写覆盖范围与盲区，并给出后续检索建议。
- **负结果和无法判定都是有效产出**，不需要包装成成功。

---

## 节点① 文献调研

```text
你是我的科研文献调研执行者。本节点的产出会被逐条校验，不满足就会被驳回重跑。

【输入】
- 研究问题或初步想法：<填>
- 入口模式：paper_driven（读新论文找灵感）/ idea_driven（为已有想法找相关工作）：<填>
- 我给你的材料：<粘贴论文列表/摘要/解析卡片/片段；没有就写「无」>
- 检索范围与预算：<时间范围、学科、语言、最多读几篇>

【硬性要求】
1. 只输出一个 JSON 对象，不要解释文字、不要用代码围栏包裹，不要新增契约外的字段。
2. 每条证据必须能回到原文，二选一：
   ① 填 card_field —— 只能填该论文解析卡片里**真实存在**的字段名；
   ② 填 paper_span_id —— 只能从我给的片段列表里取，不得自己编号。
   我提供的材料里没有可定位来源的论文，不要拿它当证据；改写进 limits。
3. 必须给出最接近的工作，并同时写明「重合点」与「仍存差异」。
4. 不得编造论文编号。

【输出契约】（字段名与取值必须完全一致）
{
  "research_question": "本次调研要回答的问题",
  "entry_mode": "paper_driven 或 idea_driven",
  "queries": [
    {"query_text": "检索式", "source": "来源（本地库/arXiv/…）", "result_count": 0, "searched_at": null}
  ],
  "evidence": [
    {
      "evidence_type": "method | result | limitation | gap",
      "paper_id": 0,
      "card_field": "解析卡片字段名，或 null",
      "paper_span_id": null,
      "quote_text": "原文引文，可 null",
      "relation": "support | refute | neutral",
      "verification": "verified | abstract_only | inferred"
    }
  ],
  "closest_work": [
    {"paper_id": 0, "overlap": "重合在什么地方", "remaining_difference": "仍存差异", "worth_continuing": true}
  ],
  "coverage_note": "检索覆盖范围与盲区（提到研究空白时必须写清）",
  "gaps": [
    {"gap_text": "空白描述", "raised_by_paper_ids": [0], "novelty_hint": "可 null"}
  ],
  "recommended_queries": ["后续建议的检索式"],
  "limits": ["仅摘要/未解析/无法访问的清单"]
}

【会被校验的规则（不满足即驳回）】
- R1 格式｜研究问题不能为空
- R2 质量｜证据至少 3 条，且每条都要带 card_field 或 paper_span_id
- R3 质量｜至少 1 条最接近的工作，且同时说明重合点与仍存差异
- R4 质量｜证据不能全部标为 inferred
- R5 质量｜提到研究空白时必须说明检索覆盖范围与盲区
- R6 格式｜引用的论文编号必须真实存在，不得编造

【执行步骤】
1. 先复述研究问题与入口模式，列出中英文关键词、同义词、易混淆概念。
2. 逐篇读材料：每条证据记清「支持/反驳/中立」「已核验/仅摘要/推断」。
3. 找出最接近的工作——这是判断差异与空白的锚点，不要跳过。
4. 收尾时如实写 limits：哪些只有摘要、哪些没解析、哪些根本拿不到。
```

---

## 节点② Idea 与可行性

```text
你是我的研究构想与可行性分析执行者。产出会被逐条校验。

【输入】
- 节点①的成果（原样粘贴）：<粘贴节点①的 JSON>
- 可用数据、代码、算力、时间与费用上限：<填>

【硬性要求】
1. 只输出一个 JSON 对象，不要解释文字、不要代码围栏，不要新增契约外字段。
2. 可证伪条件**必须带数值阈值或明确比较关系**；「效果不好就不成立」这类没有判据的写法会被驳回。
3. 效果大小没有依据时写「待确定」，不要承诺百分比提升。
4. 与最接近工作的差异必须逐条给出，**重合与差异都要写**。
5. 如果相关工作仍不足，可以在 revert_request 里建议退回文献调研，并带齐三项：
   重合证据、仍存差异、是否值得继续（缺一项会被闸门拒绝）。

【输出契约】
{
  "hypothesis": {
    "statement": "在<限定条件>下，相比<明确基线>，通过<核心机制>，预期<主指标>发生<方向性变化>",
    "scope": "限定条件",
    "baseline": "明确基线",
    "mechanism": "核心机制",
    "expected_direction": "主要指标或可观察现象的方向性变化",
    "falsification_condition": "可检验的否定条件（须含数值阈值或比较关系）",
    "effect_size": "无依据时写「待确定」"
  },
  "novelty_delta": [
    {"paper_id": 0, "overlap": "重合点", "difference": "差异", "still_novel": true}
  ],
  "feasibility": {
    "data_availability": {}, "compute_cost": {}, "method_maturity": {},
    "novelty_gap": {}, "risk_list": [], "mve_plan": {}, "total_score": 0
  },
  "recommendation": "continue | narrow | more_reading | stop",
  "revert_request": null
}

【会被校验的规则】
- R7 质量｜必须有可证伪条件，且含数值阈值或明确比较关系
- R8 质量｜至少 1 条与最接近工作的差异，需同时写重合与差异
- R6 格式｜引用的论文编号必须真实存在

【执行步骤】
1. 把想法写成可检验假设（用上面的句式），先写可证伪条件再写期待结果。
2. 逐条对照最接近的工作，写清「重合在哪」「差异在哪」「差异是否足以支撑新意」。
3. 可行性至少覆盖：数据可得性、算力/费用、方法成熟度、风险与最小验证方案。
4. 给出 recommendation，并说明为什么不是其他三个选项。
```

---

## 节点③ 实验与数据准备

```text
你是我的实验设计与数据准备执行者。产出会被逐条校验。

【输入】
- 节点②的成果（原样粘贴）：<粘贴节点②的 JSON>
- 数据来源、代码仓库、运行环境：<填>
- 时间、费用、算力预算：<填>

【硬性要求】
1. 只输出一个 JSON 对象，不要解释文字、不要代码围栏，不要新增契约外字段。
2. **预检必须真实**：先在环境里跑一次小规模检查（例如 `python -m pytest --version`
   或你自己的 smoke 脚本），把**真实退出码**填进 preflight.exit_code。
   没跑就别写 0 —— 这一项由程序核验，伪造会被直接判为未完成。
3. 数据集不可访问就如实写 access_ok=false，不要假设它能用。
4. 主指标必须写单位与方向（越大越好 / 越小越好）。
5. 成功判据与失败判据都要写 —— 什么结果算假设被否定。

【输出契约】
{
  "protocol": {
    "unit_of_analysis": "分析单位", "independent_vars": [], "dependent_vars": [],
    "controls": [], "confounders": []
  },
  "datasets": [
    {"name": "", "version": "", "location": "可定位来源", "access_ok": true, "split": "划分方式"}
  ],
  "baselines": [{"name": "", "runnable": true, "source": ""}],
  "metrics": [
    {"name": "", "formula": "", "unit": "", "direction": "higher_better | lower_better", "is_primary": true}
  ],
  "falsification": {"success_criteria": "", "failure_criteria": ""},
  "resources": {"compute_budget": "", "estimated_hours": 0, "storage": ""},
  "preflight": {
    "level": "template_smoke | researcher_script | isolated_runner",
    "command": "实际执行的命令", "exit_code": 0, "duration_ms": 0,
    "artifact_path": null, "log_path": "", "note": ""
  },
  "revert_request": null
}

【会被校验的规则】
- R9  质量｜每个数据集都要带版本且可访问
- R10 质量｜至少 1 条可运行的基线
- R11 质量｜主指标必须写单位与方向
- R12 质量｜必须同时给出成功判据与失败判据
- R13 质量｜小规模预检必须真实跑通（退出码 0）
- R14 质量｜必须给出预算

【执行步骤】
1. 先写协议再看数据：分析单位、自变量、因变量、控制变量、可能的混杂因素。
2. 列数据集时逐个核实：版本是哪个、路径能不能读到、划分方式是什么。
3. 定主指标与辅助指标，写清公式、单位、方向。
4. 写成功/失败判据，再跑预检，最后填资源预算。
```

---

## 自检清单（贴完后照这个看）

| 检查项 | 怎么看 |
|---|---|
| 是不是干净 JSON | 只有 `{` 开头 `}` 结尾，没有解释文字、没有 ``` 围栏 |
| 有没有多写字段 | 逐个对照契约里的键名，多一个都不行 |
| 证据有没有来源 | 每条 evidence 至少有一个 `card_field` **或** `paper_span_id` |
| 编号真不真 | `paper_id` / `paper_span_id` / `card_field` 是否都来自我给你的材料 |
| 可证伪条件 | 有没有数值阈值或明确比较关系（不是「效果不好」这种） |
| 预检 | `exit_code` 是不是我**真的跑过**的那个值 |

## 与程序版的差别

| | 程序版（SciLoop 编排层） | 手工版（本文件） |
|---|---|---|
| 检索 | 自动在论文库检索，并把可用卡片字段与片段 id 列给你 | 你自己贴材料 |
| 校验 | 程序逐条跑 R1–R14，不通过自动驳回重跑（上限 2 次） | 你自己对照规则 |
| 预检 | 程序真实执行并**覆盖**模型声明 | 你自己跑、自己如实填 |
| 回退 | 三类闸门（必带信息 / 次数上限 / 留痕）自动校验并记录 | 你自己检查三项是否齐 |
| 留痕 | `research_node_runs` + `research_node_transitions` 落库 | 无 |

---

_本内容由 AI 辅助生成，需研究者自行核验。_
