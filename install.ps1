$ErrorActionPreference = 'Stop'
$repo = 'https://github.com/Yanmoshen/sciloop.git'
$installDir = Join-Path $HOME 'sciloop'

if (Test-Path (Join-Path $installDir '.git')) {
  git -C $installDir pull --ff-only
} else {
  git clone $repo $installDir
}
Set-Location $installDir

if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
if ((Get-Content '.env' -Raw) -match 'OWNER_TOKEN=change_me') {
  $token = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
  (Get-Content '.env' -Raw) -replace 'OWNER_TOKEN=change_me', "OWNER_TOKEN=$token" | Set-Content '.env' -Encoding utf8
}

if (-not (Test-Path '.venv\Scripts\python.exe')) { python -m venv .venv }
& '.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.venv\Scripts\python.exe' -m pip install -e '.\server'

if (Get-Command npm -ErrorAction SilentlyContinue) {
  Set-Location web
  if (Test-Path 'package-lock.json') { npm ci } else { npm install }
  npm run build
  Set-Location $installDir
}

# 可选论文 PDF 编译。设置 SCILOOP_BUILD_PAPER=1 后使用系统 XeLaTeX，
# 或下载并校验固定版本 Tectonic 到 .data/latex。
if ($env:SCILOOP_BUILD_PAPER -eq '1') {
  & '.venv\Scripts\python.exe' tools/compile_paper.py --bootstrap
  if ($LASTEXITCODE -ne 0) { throw '论文 PDF 编译失败，请查看论文目录下 compile.log。' }
}

if ($env:SCILOOP_RUN_PAPER_ACCEPTANCE -eq '1') {
  & '.venv\Scripts\python.exe' tools/run.py paper-acceptance
  if ($LASTEXITCODE -ne 0) { throw '论文验收失败，请查看论文项目目录中的 paper_acceptance_report.json。' }
}

# 正式运行只用 8000 提供页面和 API；执行器由后端负责启动与关闭。
# 重复安装不会再启动另一份 Vite/API/执行器，失败不自动换端口。
& '.venv\Scripts\python.exe' tools/run.py start
if ($LASTEXITCODE -ne 0) { throw 'SciLoop 启动失败，请查看上方提示。' }
