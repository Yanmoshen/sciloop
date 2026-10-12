# 内置技能包（第三方内容，来源与许可在此登记）



#### 本仓库只做了"适配字段"的改动（见下），**正文与脚本原样保留**。

##

每个 `SKILL.md` 的 frontmatter 里增加了：

| 字段 | 干什么 |
|---|---|
| `stage` | 归到哪个环节（文献调研 / 选题与假设 / 实验与统计 / 图表与可视化 / 写作与交付），界面按它分组 |
| `source` | 上游路径，例如 `K-Dense-AI/scientific-agent-skills/skills/literature-review` |
| `requires_env` | 上游声明的环境变量（缺 key 时界面要能提示"这个技能现在跑不了"） |
| `steps` + `steps_note` | **流程声明**：这一步跑哪些脚本、参数、产出。上游只有散文说明，所以这份是**用脚本的 argparse 自动推导**的草稿（标了 `"auto"`），演示要用的几个会手工校准 |

正文（`SKILL.md` 的 `---` 之后）与 `scripts/`、`references/`、`assets/` **未做任何改动**。

## 技能清单（42 个）

- **文献调研（8）**：`literature-review` `paper-lookup` `research-lookup` `bgpt-paper-search` `paperclip` `database-lookup` `citation-management` `pyzotero`
- **选题与假设（6）**：`hypothesis-generation` `scientific-brainstorming` `scientific-critical-thinking` `scholar-evaluation` `peer-review` `what-if-oracle`
- **实验与统计（11）**：`experimental-design` `statistical-power` `statistical-analysis` `exploratory-data-analysis` `uncertainty-and-units` `sympy` `shap` `scikit-learn` `statsmodels` `polars` `networkx`
- **图表与可视化（7）**：`scientific-visualization` `matplotlib` `seaborn` `scientific-schematics` `markdown-mermaid-writing` `infographics` `generate-image`
- **写作与交付（10）**：`scientific-writing` `scientific-slides` `pptx` `pptx-posters` `latex-posters` `venue-templates` `docx` `pdf` `xlsx` `markitdown`

其中 **30 个带脚本（能真跑）**，**12 个是说明书型**（上游本来就没有脚本，模型读说明自己写代码/自己算：
`networkx` `polars` `statsmodels` `sympy` `seaborn` `markdown-mermaid-writing` `scientific-critical-thinking`
`what-if-oracle` `bgpt-paper-search` `database-lookup` `paperclip` `pyzotero`）——
引擎对这两类是**分开对待**的（`mode: scripts | instructions`），说明书型不算"跑不了"。

⚠️ 需要 API key 的技能：`generate-image`（出图）等，其 `requires_env` 里写明了变量名。

## 新增一个技能（写给以后的我）

```
server/services/skills/packs/<技能名>/
├── SKILL.md          # frontmatter: name / description / stage / license / source
│                     #              steps: [{id, title, script, args, outputs}]
├── scripts/          # 真能跑的脚本（Python 为主）
└── references/       # 参考资料（可选）
```

- `name` 必须与目录名一致；`description` 是模型**唯一**用来挑技能的线索，要写清"什么时候该用我"；
- `steps` 里的 `args` 支持占位：`{{topic}}`、`{{in}}`、`{{out}}`、`{{skill_dir}}`；
- 扫描只读、不执行；坏技能只记 `problems`，不会影响其它技能。
