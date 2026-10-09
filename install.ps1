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

$env:PYTHONPATH = Join-Path $installDir 'server'
Start-Process -WindowStyle Hidden -FilePath (Join-Path $installDir '.venv\Scripts\python.exe') -ArgumentList 'tools\host-runner\host_runner.py' -WorkingDirectory $installDir
if (Get-Command npm -ErrorAction SilentlyContinue) { Start-Process -WindowStyle Hidden -FilePath 'npm.cmd' -ArgumentList 'run','dev','--','--host','127.0.0.1','--port','5173' -WorkingDirectory (Join-Path $installDir 'web') }
& '.venv\Scripts\python.exe' -m uvicorn main:app --app-dir server --host 127.0.0.1 --port 8000
