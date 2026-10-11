# 论文验收入口

`tools/paper_acceptance.py` 是项目内的单入口验收命令。它在 `knowledge-base/projects/` 中选择最新的包含 `main.tex` 的研究项目，也可以用 `--paper-dir` 指定目录，随后执行：

1. 论文静态一致性检查（引用键、交叉引用、图文件和实验数值）。
2. LaTeX 编译。调用项目内 `tools/compile_paper.py --bootstrap`，下载并校验固定版本 Tectonic 到 `.data/latex/`（不把可执行文件提交到 Git）。Docker 仅在明确传入 `--docker-fallback` 时作为兼容路径使用。
3. 知识库隔离检查：自生成论文必须位于 `knowledge-base/projects/<project>/`，外部资料只从 `knowledge-base/literature/` 或 `knowledge-base/uploads/` 统计，不会被复制到论文目录。

在仓库根目录运行：

```powershell
python tools/paper_acceptance.py
```

指定论文项目：

```powershell
python tools/paper_acceptance.py --paper-dir "knowledge-base/projects/复杂度优化论文验收 20261011__2026101108"
```

统一运行器也提供同一入口：

```powershell
python tools/run.py paper-acceptance
```

安装脚本默认只安装并启动服务；需要在安装后自动验收论文时，在命令前设置
`SCILOOP_RUN_PAPER_ACCEPTANCE=1`。例如 PowerShell 中运行
`$env:SCILOOP_RUN_PAPER_ACCEPTANCE='1'; irm https://raw.githubusercontent.com/Yanmoshen/sciloop/main/install.ps1 | iex`，
Unix shell 中运行 `SCILOOP_RUN_PAPER_ACCEPTANCE=1 curl -fsSL https://raw.githubusercontent.com/Yanmoshen/sciloop/main/install.sh | sh`。

若只需检查静态一致性和目录隔离，不触发编译：

```powershell
python tools/paper_acceptance.py --no-compile
```

需要显式允许 Docker 兼容回退时：

```powershell
python tools/paper_acceptance.py --docker-fallback
```

报告会保存到论文项目目录的 `paper_acceptance_report.json`，编译器自身还会生成 `compile-result.json` 与 `compile.log`。成功条件是静态检查通过、目录隔离通过并且生成 `main.pdf`；固定 Tectonic 下载、哈希校验或编译失败时，报告会保留失败原因，不会伪称 PDF 已生成。

外部文献和上传材料不得放入 `projects/` 项目目录。项目生成的正文、实验脚本、图表、检查报告和 PDF 均放在同一个项目目录下，便于备份和删除。
