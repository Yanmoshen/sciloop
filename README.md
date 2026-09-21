# SciLoop

**可审计的科研流水线：文献空白 → 可复现实验 → 风险自适应。**

把「读了很多论文，还是不知道做什么」拆成六个可检查、可回退、可取证的环境节，并让**人在任何一步都保有否决权**。

## 快速启动

前置：Docker Desktop（含 compose v2）。

```bash
cp .env.example .env          # 至少填 OPENALEX_MAILTO；禁止提交 .env
docker compose up -d --build   # db → backend（启动时自动 alembic upgrade head）→ frontend
docker compose ps              # 三个服务都应为 healthy
curl http://localhost:8000/api/v1/health
```

| 入口 | 地址 |
|---|---|
| 前端 | http://localhost:8080 |
| 后端 API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/v1/health |
| 数据源健康 | http://localhost:8000/api/v1/sources/health |
| 数据库 | `docker compose exec db psql -U sciloop -d sciloop` |

迁移（幂等）：

```bash
docker compose exec backend alembic upgrade head       # 建 / 升级到最新
docker compose exec backend alembic downgrade base     # 回滚
```

迁移链：`0001_initial_schema` → `0002_passport_immutable_guard` → `0003_reader_library`。

## 目录

| 目录 | 职责 |
|---|---|
| `server/` | FastAPI + SQLAlchemy 2.x + Alembic。顶层按层分目录：`api/`（路由）、`services/`（领域服务：ingest / parsing / aggregation / ideation / feasibility / experiment / writing / review / translate / reader / export / research）、`db/`（会话与 ORM 模型）、`schemas/`（Pydantic DTO）、`core/`（配置与安全）、`llm/`（OpenAI 兼容适配与按环节路由）、`executor/`（进程内受限执行器）、`tasks/`（后台作业）、`migrations/`（迁移链） |
| `web/` | Vue 3 + Vite + TypeScript + Pinia + Element Plus + ECharts（`api/` `views/` `components/` `stores/` `router/` `layouts/` `styles/` `utils/`） |
| `prompts/` | 研究工作流长文档提示词（程序侧契约在 `server/services/research/`） |
| `config/` | 非机密静态配置（`app.yaml`、`venue_whitelist.json`） |
| `docker-compose.yml` | 三服务编排：`db`（PostgreSQL 16）/ `backend`（server/ 镜像）/ `frontend`（web/ 镜像，nginx） |

## 环境变量

完整清单见 `.env.example`（每一项都有注释）。

- **红线**：`OWNER_TOKEN` 只允许存在于服务端环境变量，禁止写入前端包或数据库；API Key 不明文落库（`model_configs.api_key_enc` 用 fernet 加密）。
- 默认 `APP_ACCESS_MODE=public_demo`：全站**只读**，需要写入的请求要带请求头 `X-Owner-Token`（匿名写返回 403）。
- LLM 走 **OpenAI 兼容协议**（`/v1/chat/completions`），自带 Base URL 与 Key；无 Key 时走本地桩/回放并**如实标记**，不会静默降级成假结果。

## 核心能力（后端 API）

| 模块 | 端点 |
|---|---|
| 论文导入 | `POST /api/v1/papers/import`（PDF ≤20MB，单次 ≤20）、`POST /api/v1/papers/import/identifiers`（DOI / arXiv ID 批量）、`GET /api/v1/papers/import-jobs/{task_id}` |
| 论文翻译 | `POST /api/v1/translate/jobs`（`mode=translate\|simplify`）、`GET /api/v1/translate/jobs/{task_id}/pdf?format=mono\|dual` |
| 全文阅读器 | `POST /api/v1/reader/documents`、`GET /api/v1/reader/documents/{id}/versions`、`/state/`、`/annotations/` |
| 多格式导出 | `GET /api/v1/exports/{json,bibtex,csl-json,obsidian,csv}`、`GET /api/v1/exports/paper/{paper_id}` |
| 论文流与检索 | `GET /api/v1/papers/feed`、`/papers/search`、`/papers/overview` |
| 流水线与决策 | `GET /api/v1/pipelines/{project_id}/stages`、`POST /api/v1/decisions/{id}/approve` |
| 实验与 Passport | `GET /api/v1/passports/{id}`、`POST /api/v1/passports/{id}/replay` |

## 数据与持久化

运行时数据落在 `./.data/`（**已被 `.gitignore` 忽略，不入库**）：

| 目录 | 内容 |
|---|---|
| `fulltext-cache/` | 归一全文文本缓存（容器重建后 span 定位仍可校验） |
| `uploads/` | 导入的原始 PDF |
| `artifacts/` | 翻译产物与 `manifest.json` |
| `reader-library/` | 阅读器不可变版本 PDF、文本索引、批注归档 |

删除 `.data/` 会丢失已解析全文、翻译产物与阅读器版本。

## 许可与合规

- 许可证：**Apache License 2.0**（见 `LICENSE`）
- 第三方依赖：FastAPI / SQLAlchemy / Alembic / Pydantic / httpx / asyncpg / psycopg / PyMuPDF / Vue 3 / Vite / Element Plus / ECharts / Pinia，均遵循各自许可证
- 产出物一律标注「**本内容由 AI 辅助生成，需研究者自行核验**」；本工具定位为科研辅助工作台（教学用途），产出物为研究草稿，**不是**「可自动投稿的论文生成器」
- 演示叙事强调「AI 执行、人保有否决权与最终判断」；回放结果必须标 `is_replay=true`
- 数据源失败一律**降级并披露**（`null` + `source` + `confidence`），**禁止编造数据**
