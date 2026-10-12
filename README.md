# SciLoop

**面向科研工作的可审计 AI 工作台。**

SciLoop 把文献检索、全文阅读、研究构思、实验执行、证据整理和论文产出放进同一个本地工作区。每一步都有可追踪的输入、输出和状态；需要执行命令、写入文件或调用外部服务时，研究者可以查看并保留最终决定权。

> 当前项目处于持续开发阶段。它适合研究原型、课程项目和内部工作流，不会替研究者验证事实、数据或论文结论。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](server/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933.svg)](web/package.json)

## 能做什么

- **发现文献**：从多个公开来源检索、导入和整理论文，保留来源与抓取状态。
- **理解全文**：解析 PDF/HTML，提供阅读器、版本、批注和可定位的文本证据。
- **构思研究**：围绕研究问题生成候选方向、可行性分析和后续任务。
- **运行实验**：在项目工作区执行命令，记录 stdout、stderr、状态和产物。
- **管理知识**：统一保存上传资料、解析结果、翻译版本、实验文件和对话记录。
- **辅助写作**：把研究过程中的证据和产物组织成论文草稿，并支持 PDF 编译。
- **保留人工控制**：高风险命令、越界写入和需要授权的操作会进入审批流程。

## 快速开始

### 一键安装（推荐）

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Yanmoshen/sciloop/main/install.ps1 | iex"
```

Linux/macOS：

```bash
curl -fsSL https://raw.githubusercontent.com/Yanmoshen/sciloop/main/install.sh | sh
```

安装器会检查 Git、Python 3.11+、Node.js 18+ 和 npm，创建虚拟环境，安装后端依赖，构建前端并启动服务。默认安装目录是 `~/sciloop`；可通过 `SCILOOP_HOME` 和 `SCILOOP_REPO_URL` 覆盖。安装日志保存在安装目录的 `.data/install.log`（Windows 还会保留安装过程转录文件）。

启动完成后打开：<http://127.0.0.1:8000>

### 手动安装

需要：Python 3.11+、Node.js 18+、npm 和 Git。

```bash
git clone https://github.com/Yanmoshen/sciloop.git
cd sciloop
cp .env.example .env

python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ./server

cd web
npm ci
npm run build
cd ..

python tools/run.py start
```

Windows 没有 `cp` 时，可以使用：

```powershell
Copy-Item .env.example .env
```

首次运行前，在 `.env` 中设置 `OWNER_TOKEN` 和要使用的模型供应商。也可以直接在网页的设置页面配置 OpenAI 兼容接口。启动器只管理当前 checkout 创建的服务，不会自动终止其他程序占用的端口。

## 日常运行

所有命令都在仓库根目录执行：

```bash
python tools/run.py start     # 启动
python tools/run.py status    # 查看状态和健康检查
python tools/run.py restart   # 重启
python tools/run.py stop      # 停止并回收本次启动的进程
```

服务和前端由同一个 FastAPI 进程提供，默认监听 `127.0.0.1:8000`。启动状态和日志位于 `.data/run/`：`state.json` 保存进程状态，`supervisor.log` 保存启动器日志，`backend.log` 保存 API 日志。

修改前端后重新构建：

```bash
cd web
npm run build
cd ..
python tools/run.py restart
```

## 配置

`.env.example` 是完整配置索引。复制后只修改 `.env`，不要把真实密钥写入源码或提交到 Git：

```bash
cp .env.example .env
```

最常用的配置项：

| 变量 | 作用 |
| --- | --- |
| `OWNER_TOKEN` | 本地管理员令牌，写入接口和高风险操作使用 |
| `DATABASE_URL` | SQLite 数据库地址，默认位于 `knowledge-base/sciloop.sqlite3` |
| `KNOWLEDGE_BASE_DIR` | 论文、上传资料、对话和产物的根目录 |
| `LLM_DEFAULT_BASE_URL` | 默认的 OpenAI 兼容 API 地址 |
| `LLM_DEFAULT_API_KEY` | 默认模型 API Key，仅保存在服务端配置中 |
| `LLM_DEFAULT_MODEL` | 默认模型名称 |
| `SCILOOP_HOST_RUNNER_URL` | 可选宿主执行器地址，默认端口为 `8766` |
| `HOST_EXECUTION_MODE` | 工作区权限模式：`workspace` 或 `full` |

SciLoop 使用 OpenAI 兼容的 `/v1/chat/completions` 协议。没有模型密钥时，离线回放或本地桩会明确标记结果来源，不会把模拟结果伪装成真实调用。

## 数据与项目工作区

默认数据目录是仓库根目录下的 `knowledge-base/`，该目录已被 `.gitignore` 排除：

| 路径 | 内容 |
| --- | --- |
| `literature/` | 外部文献和检索结果 |
| `uploads/` | 用户上传的 PDF、图片和多格式文件 |
| `parses/` | 全文解析、索引和缓存 |
| `translations/` | 翻译产物和清单 |
| `projects/` | 项目目录、实验文件和生成的论文 |
| `conversations/` | 对话记录 |
| `memories/` | Agent 记忆 |
| `exports/` | 导出的文件 |
| `sciloop.sqlite3` | SQLite 主数据库 |

创建项目时选择的文件夹是该项目的工作区路径。对话在创建时绑定项目，项目和对话名称不会自动追加日期时间；删除 `knowledge-base/` 会同时删除这些本地数据，请先备份。

## 论文与文件产物

论文源文件通常位于 `knowledge-base/projects/<项目目录>/`。编译前可以检查静态引用和文件结构：

```bash
python tools/compile_paper.py --check
python tools/compile_paper.py "knowledge-base/projects/<项目目录>" --bootstrap
```

编译器优先使用系统中的 `xelatex`；找不到时，`--bootstrap` 会把固定版本的 Tectonic 下载到本地 `.data/latex/`。编译输出 `main.pdf`、`compile.log` 和 `compile-result.json` 都留在论文项目目录中，下载的二进制文件不会进入 Git。

对话框支持粘贴图片和上传常见文本、代码、PDF、Office 文档及表格文件。上传内容写入知识库或当前项目工作区，原始文件不会被打包进前端构建产物。

## 开发

后端：

```bash
cd server
python -m pytest
ruff check .
```

前端：

```bash
cd web
npm run typecheck
npm run lint
npm run build
```

开发时也可以单独运行 API：

```bash
cd server
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

API 文档由 FastAPI 提供：<http://127.0.0.1:8000/docs>。生产式运行仍建议使用 `python tools/run.py start`，这样可以统一处理健康检查、日志和进程回收。

## 架构概览

```text
web/                    Vue 3 + Vite + TypeScript 前端
server/api/             FastAPI 路由和 WebSocket 接口
server/services/        检索、解析、研究编排、Agent、翻译和导出
server/db/              SQLAlchemy 模型与 SQLite 会话
server/llm/             OpenAI 兼容模型适配、路由和成本记录
server/contracts/       Agent v2 的事件与工具契约
prompts/                面向研究流程的提示词和设计文档
tools/                  启动器、论文编译器和宿主执行器
config/                 非机密静态配置
knowledge-base/         本地运行数据，不提交到仓库
```

搜索、解析、翻译和导出等能力在服务端统一编排；Agent v2 通过结构化事件连接前端，工具调用、审批和恢复状态会被保存并可追踪。

## 安全边界

- `OWNER_TOKEN` 和模型 API Key 只存在于服务端环境，不写入前端包、数据库明文或提交记录。
- `workspace` 模式只允许在选定工作区内写入和执行；越界路径、凭据访问和高风险命令需要额外裁决。
- 宿主执行器使用 argv 直启、输出大小限制和进程树回收，避免把用户命令拼接进 shell 字符串。
- 外部来源失败时返回来源和置信信息；系统不会用猜测内容填补缺失的实验数据或文献证据。
- 上传文件和生成产物默认保存在本机知识库，部署到共享机器前请配置访问控制和备份策略。

## 许可证

SciLoop 使用 [Apache License 2.0](LICENSE)。第三方依赖和内置技能包遵循各自的许可证与来源说明。
