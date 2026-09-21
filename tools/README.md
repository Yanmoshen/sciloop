# tools/ — SciLoop 的 MCP 工具通道

> **是什么**：SciLoop 通过**规范 MCP 协议**给自家 agent 提供工具。不是"开发这个仓库用的脚本"，
> 而是产品能力的一部分。
>
> **代码位置**：`server/mcp_server/`（导入名 `mcp_server`）。本目录只放索引与契约文档。
>
> **状态（2026-09-21）**：自建 MCP server + 2 个工具 + 四道边界门，已通过 19 条契约测试
> （含**走真 stdio 子进程**的端到端）。**尚未接进任何 API / 研究节点**。

---

## 一、为什么代码在 `server/mcp_server/` 而不是这里

硬约束，不是审美：后端镜像构建上下文是 `./server`（`docker-compose.yml:33`），
容器内 `WORKDIR=/app/server`、`PYTHONPATH=/app/server` → **仓库根的 `tools/` 进不了镜像**，
`server/` 就 import 不到。而把上下文改成仓库根会把 `web/node_modules`（约 1.9 万文件）塞进构建上下文。
所以代码放后端包内，本目录负责对外说明。

---

## 二、架构（三条要求同时成立的形态）

```
SciLoop agent（后端）
  └─ MCP client（mcp_server/client.py，走 stdio 标准协议）
       └─ SciLoop 自建 MCP server（mcp_server/server.py）   ← 边界全在这层
            ├─ query_library  只读业务工具（查论文库/对话链），自动放行
            └─ run_command    执行工具，**必须带研究者批准令牌**
            └─ （后续）代理 → 官方 server（filesystem / fetch / git）
                            它们的工具也经我们登记 → 审批 → 审计，不直连 agent
```

三条要求：**工具自己提供**、**通用能力借官方**、**权限仍由我们把关** ——
官方 server 挂在聚合层后面只当能力来源，所以"用官方全套"和"边界在我们手里"不冲突。

传输：现在 `stdio`（同机）。将来要对外暴露给远程/网页端，只需把
`MCPServer.run(transport="stdio")` 换成 `"streamable-http"` —— 工具定义与边界一行不用改。

---

## 三、四道边界门（授权来自**进程启动配置**，不来自调用方）

stdio 下调用方就是我们自己拉起的后端进程，如果让**调用方**在参数里声明"我允许自己写盘"，
等于没有边界。所以授权（`Grant`）在启动时由环境变量读入，调用方只能在已授予范围内请求。

| 门 | 规则 | 违反时的错误码 |
|---|---|---|
| ① 能力 | 工具声明的 `read/write/exec/net` 必须在授权里出现 | `tool_denied` |
| ② 路径 | **先 `resolve()` 再比对授权根**，挡 `..` 穿越与软链逃逸；凭据类名字永不可写 | `tool_denied` |
| ③ 审批 | 写盘/执行类必须带**研究者批准令牌** | 没给 `approval_required` / 给错 `approval_invalid` |
| ④ 审计 | 每次调用落一条，**含被拒的**；只记参数键名与长度，**不记原文** | — |

启动环境变量：`SCILOOP_MCP_WORKSPACE` / `SCILOOP_MCP_READ_ROOTS` / `SCILOOP_MCP_ALLOW_EXEC` /
`SCILOOP_MCP_APPROVAL_TOKENS` / `SCILOOP_MCP_ACTOR`。

### 错误码（可区分，"没跑成"与"跑了但没通过"必须分开）

`tool_denied` · `approval_required` · `approval_invalid` · `tool_limit` ·
`tool_timeout` · `tool_bad_params` · `tool_failed`

> `run_command` 的结果里 `exit_code` 与 `ok` **分开给**：命令跑成了、只是返回非零，
> 不是工具失败。把两者混为一谈，是安全类检查长期"假装通过"的同一种病。

---

## 四、怎么跑

```bash
# 契约测试（19 条：Guard 四道门 + 走真 stdio 子进程的端到端）
# 需要 mcp SDK；宿主隔离 venv 即可，不动运行中的容器
cd server && ../.learnbuddy/tmp/mcpenv/Scripts/python.exe -m pytest mcp_server/tests -q

# 静态检查（两个版本都要过 —— 宿主 0.15.13 / 镜像 0.16.8，两者会不一致）
cd server && ruff check mcp_server
docker run --rm -v "D:/aicoding竞赛/server:/src" -w /src sciloop-backend:latest ruff check mcp_server

# 手工试一次（stdio）
cd server && SCILOOP_MCP_WORKSPACE=$PWD SCILOOP_MCP_ALLOW_EXEC=1 python -m mcp_server.server
```

---

## 五、踩到的 SDK 坑（mcp 2.x，网上教程大多是 v1，照抄会挂）

| 现象 | 真相 |
|---|---|
| `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` | 2.x 里 **`FastMCP` 已改名为 `MCPServer`**（`from mcp.server.mcpserver import MCPServer`） |
| `'CallToolResult' object has no attribute 'isError'` | 2.x 是 Pydantic 模型，字段蛇形：**`is_error`** |
| 我们的拒绝被抛成异常、客户端拿不到结构化错误 | 普通异常 → `UnexpectedToolError` **往客户端抛异常**；必须转成 SDK 的 **`ToolError`** 才会变成 `is_error` 结果 |
| 客户端解析不到错误码 | SDK 会在错误前面加 `Error executing tool <name>: ` 前缀 → 取码要**全文搜** `[...]`，不能只判开头 |
| 工具参数类型错 | **协议层按 schema 先拒**（`Tool 'run_command' rejected arguments`），我们的 `tool_bad_params` 收不到 —— 留作纵深防御 |

---

## 六、未完成（如实列出）

1. **尚未接进产品**：没有任何 API / 研究节点调用它（`server/` 里引用 0 处）。接线位置（哪个节点、
   谁批、审计落哪张表）需要先定。
2. **运行中的容器看不到它**：`server/mcp_server/` 是新增顶层包，不在 `docker-compose.override.yml`
   挂载清单里；且 `mcp` 是新依赖 → 需一次 `docker compose build backend`（**不是** `down`）。
3. **官方 server 还没接**（filesystem / fetch / git）：需要给镜像装 Node（Debian 13 的 apt 够用），
   且要**构建期预装**官方包 —— 运行时 `npx -y` 会联网下载，评测环境断网就挂。
4. ~~旧的手写工具层 `server/tools/` 待删除~~ —— **已删除（2026-09-21）**。
   注：`safe-delete` 钩子会拦截 `rm` 与 `shutil.rmtree`（D: 盘回收站不可用 → **失败关闭**），
   agent 侧删不掉，最终由人工在终端执行。绕过那道护栏是错的，遇到时如实上报即可。
5. **审计还没有落点**：`Guard.audit` 现在只进内存列表（可注入 sink），没接数据库表。
