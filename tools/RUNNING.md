# 宿主机运行

首次安装仍可使用仓库根目录的 `install.ps1` / `install.sh`。
安装构建前端后调用统一启动器，页面与 API 共用 http://127.0.0.1:8000。
正式运行不需要 Node/Vite 常驻，唯一公开入口是 `http://127.0.0.1:8000`。

在项目根目录运行（Windows 推荐用 `.venv\Scripts\python.exe` 替换 `python`）：

```text
python tools/run.py start
python tools/run.py status
python tools/run.py stop
python tools/run.py restart
```

修改前端后，在 `web/` 目录重新运行 `npm run build`，再执行 `python tools/run.py restart`。
页面和 API 始终由同一个 8000 端口提供。

执行器使用 `.env` 中 `SCILOOP_HOST_RUNNER_URL` 指定的本机端口。
它提供独立进程执行能力，由后端自动启动。
统一停止会回收所启动的后端、Vite 与执行器进程树；独立启动的外部执行器不会被误杀。
直接运行后端并正常退出时，也会回收由该后端启动的执行器。

同一 checkout 重复启动会复用已运行实例，多个 checkout 也不能抢占固定端口。
端口被未知程序占用时启动失败并提示冲突，不强杀其他程序、不自动递增端口。
启动器不会启动 Vite，也不会自动递增或占用额外前端端口。

状态与日志位于 `.data/run/`（本地运行数据，已有忽略规则）：
`state.json`、`supervisor.log`、`backend.log`、`frontend.log`。
停止请求采用每次启动唯一标识，停止操作不根据历史 PID 直接结束进程。

这些命令管理通过统一启动器启动的服务。
遗留的手动启动实例必须先停止；启动器不会根据端口猜测并杀死未知服务。
强制结束监督进程或断电不保证执行中子进程正常收尾，应优先使用 `stop`。
