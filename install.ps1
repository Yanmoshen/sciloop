$ErrorActionPreference = 'Stop'

# SciLoop host installer. It bootstraps the small set of host prerequisites,
# builds the single frontend bundle, and starts the one-process public entrypoint.
$repo = if ($env:SCILOOP_REPO_URL) { $env:SCILOOP_REPO_URL } else { 'https://github.com/Yanmoshen/sciloop.git' }
$installDir = if ($env:SCILOOP_HOME) { $env:SCILOOP_HOME } else { Join-Path $HOME 'sciloop' }
$transcriptPath = "$installDir.install.log"
$logPath = Join-Path (Join-Path $installDir '.data') 'install.log'
$repositoryReady = $false

New-Item -ItemType Directory -Force -Path (Split-Path $transcriptPath -Parent) | Out-Null
Start-Transcript -Path $transcriptPath -Append | Out-Null

function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = "$machine;$user"
}

function Has-Command([string]$name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Install-With-Winget([string]$id, [string]$label) {
  if (-not (Has-Command 'winget')) {
    throw "$label 未安装，且系统没有 winget。请先安装 $label，或从 https://learn.microsoft.com/windows/package-manager/winget/ 安装 App Installer。"
  }
  Write-Host "正在安装 $label ..."
  & winget install --id $id --exact --accept-source-agreements --accept-package-agreements --silent
  if ($LASTEXITCODE -ne 0) { throw "$label 安装失败，winget 返回码 $LASTEXITCODE。" }
  Refresh-Path
}

function Ensure-Prerequisite([string]$command, [string]$id, [string]$label) {
  if (-not (Has-Command $command)) { Install-With-Winget $id $label }
  if (-not (Has-Command $command)) { throw "$label 安装后仍无法在 PATH 中找到 $command。请重新打开 PowerShell 后重试。" }
}

try {
  Ensure-Prerequisite 'git' 'Git.Git' 'Git'
  Ensure-Prerequisite 'python' 'Python.Python.3.12' 'Python 3.11 或更高版本'
  Ensure-Prerequisite 'node' 'OpenJS.NodeJS.LTS' 'Node.js LTS'
  Ensure-Prerequisite 'npm' 'OpenJS.NodeJS.LTS' 'npm'

  $pythonVersion = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
  $parts = $pythonVersion.Trim().Split('.')
  if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    throw "Python 版本 $pythonVersion 过低，需要 Python 3.11 或更高版本。"
  }
  $nodeMajor = [int]((& node --version).Trim().TrimStart('v').Split('.')[0])
  if ($nodeMajor -lt 18) { throw "Node.js 版本过低，需要 18 或更高版本。" }

  if (Test-Path (Join-Path $installDir '.git')) {
    git -C $installDir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw '更新仓库失败。' }
  } else {
    if (Test-Path $installDir) { throw "$installDir 已存在但不是 SciLoop 仓库；请更换 SCILOOP_HOME 或手动处理该目录。" }
    git clone $repo $installDir
    if ($LASTEXITCODE -ne 0) { throw '克隆仓库失败。' }
  }
  $repositoryReady = $true
  Set-Location $installDir

  if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
  if ((Get-Content '.env' -Raw) -match 'OWNER_TOKEN=change_me') {
    $token = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    (Get-Content '.env' -Raw) -replace 'OWNER_TOKEN=change_me', "OWNER_TOKEN=$token" | Set-Content '.env' -Encoding utf8
  }

  if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败。' }
  }
  & '.venv\Scripts\python.exe' -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) { throw '升级 pip 失败。' }
  & '.venv\Scripts\python.exe' -m pip install -e '.\server'
  if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }

  Push-Location web
  if (Test-Path 'package-lock.json') { npm ci --no-audit --no-fund } else { npm install --no-audit --no-fund }
  if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
  Pop-Location
  if (-not (Test-Path 'web\dist\index.html')) { throw '前端构建完成但 web\dist\index.html 不存在。' }

  # Optional paper compilation never blocks normal deployment unless explicitly requested.
  if ($env:SCILOOP_BUILD_PAPER -eq '1') {
    & '.venv\Scripts\python.exe' tools/compile_paper.py --bootstrap
    if ($LASTEXITCODE -ne 0) { throw '论文 PDF 编译失败，请查看论文目录下 compile.log。' }
  }
  if ($env:SCILOOP_RUN_PAPER_ACCEPTANCE -eq '1') {
    & '.venv\Scripts\python.exe' tools/run.py paper-acceptance
    if ($LASTEXITCODE -ne 0) { throw '论文验收失败，请查看论文项目目录中的 paper_acceptance_report.json。' }
  }

  Write-Host ''
  Write-Host 'SciLoop 依赖、前端和 SQLite 已准备完成。'
  Write-Host '启动地址：http://127.0.0.1:8000'
  Write-Host "安装日志：$logPath"
  & '.venv\Scripts\python.exe' tools/run.py start
  if ($LASTEXITCODE -ne 0) { throw 'SciLoop 启动失败，请查看 .data/run 和安装日志。' }
} finally {
  Stop-Transcript | Out-Null
  if ($repositoryReady -and (Test-Path $installDir)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $logPath -Parent) | Out-Null
    Copy-Item $transcriptPath $logPath -Force
  }
}
