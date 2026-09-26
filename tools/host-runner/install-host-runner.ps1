# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
#
# 把「本机执行环境」装成开机自启 —— **跑一次，以后不用管**。
#
# 为什么需要它：SciLoop 的后端跑在容器里，容器看不见宿主机的进程；
# 想让命令**在你自己的 Windows 上**执行（能碰这台机器的任何目录、用你装的 Python），
# 就需要一个跑在 Windows 上的小程序一直待着（tools/host-runner/host_runner.py）。
#
# 不装也能用：`docker compose up` 自带一个容器执行环境，只是它只能碰挂进去的目录。
# 装了这个之后，后端会**自动优先用你本机**（见 services/agent/host_runner.py 的候选顺序）。
#
# 用法（在仓库根目录，用 PowerShell 跑）：
#     powershell -ExecutionPolicy Bypass -File tools\host-runner\install-host-runner.ps1
# 卸载：
#     powershell -ExecutionPolicy Bypass -File tools\host-runner\install-host-runner.ps1 -Uninstall
#
# ⚠️ 本文件**不写死任何绝对路径**（仓库路径可能含中文，写死容易踩编码坑）：
# 一切从 $PSScriptRoot 推出来。

param(
    [switch]$Uninstall,
    [string]$TaskName = 'SciLoopHostRunner',
    #: 指定用哪个 python 跑执行器（不指定就从 PATH 找）。
    #: 为什么要能指定：PATH 上可能有多个 python，而执行器用哪个解释器会被
    #: 「技能脚本用哪个 python 跑」继承（见 services/skills/runner.py）。
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

# 仓库根：本脚本可能被放在 tools\host-runner\ 下，也可能被复制到 scripts\ 下，
# 所以**不能**只按「上一层」推。直接向上找那个真的含 tools\host-runner\host_runner.py 的目录。
# 2026-09-26 修：原来写死「上一层」，脚本在 tools\host-runner\ 下时推成 ...\tools，
# 于是拼出 tools\tools\host-runner\host_runner.py，必然报「找不到执行器脚本」。
$repoRoot = $PSScriptRoot
$runner = Join-Path $repoRoot 'tools\host-runner\host_runner.py'
while (-not (Test-Path -LiteralPath $runner)) {
    $parent = Split-Path -Parent $repoRoot
    if (-not $parent -or $parent -eq $repoRoot) { break }
    $repoRoot = $parent
    $runner = Join-Path $repoRoot 'tools\host-runner\host_runner.py'
}

if ($Uninstall) {
    Write-Host "正在卸载开机自启（$TaskName）…"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除计划任务。当前正在跑的那一个不会被杀掉，重启后就不会再起了。"
    }
    else {
        Write-Host "没有找到这个计划任务，无需卸载。"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $runner)) {
    Write-Error "找不到执行器脚本：$runner（请在仓库里运行本脚本）"
    exit 1
}

if ($Python) {
    $python = $Python
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Error "指定的 python 不存在：$python"
        exit 1
    }
}
else {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Error "没找到 python。请先装 Python 3，并确认它在 PATH 里（python --version 能用），或用 -Python 指定绝对路径。"
    exit 1
}

Write-Host "仓库：$repoRoot"
Write-Host "执行器：$runner"
Write-Host "解释器：$python"

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$runner`"" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# ⚠️ 登记可能失败（实测：非管理员/受限环境下 `Register-ScheduledTask` 直接「拒绝访问」）。
# 失败就必须**如实说失败**，不能照样打印"已登记" —— 那会让人以为装好了，
# 下次又发现执行器没起，白找一圈（2026-09-26 修）。
$registered = $false
try {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Force `
        -Description 'SciLoop 本机执行环境：让 SciLoop 能在本机执行命令（研究者可在设置里关掉）' `
        -ErrorAction Stop | Out-Null
    $registered = $true
}
catch {
    Write-Host ""
    Write-Host "❌ 登记开机自启失败：$($_.Exception.Message)"
    Write-Host ""
    Write-Host "常见原因：当前不是管理员，或系统策略不允许登记计划任务。"
    Write-Host "两条出路（任选其一）："
    Write-Host "  ① 用**管理员** PowerShell 重跑本脚本（就能登记成开机自启）；"
    Write-Host "  ② 不登记也行，需要时手动起一次："
    Write-Host "     python `"$runner`""
    Write-Host "     （不加参数即可：端口会自动跟随 .env 里的 SCILOOP_HOST_RUNNER_URL）"
    Write-Host ""
    exit 1
}

Write-Host "已登记开机自启：$TaskName"
$started = $false
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $started = $true
}
catch {
    Write-Host "⚠️ 计划任务登记好了，但这一次没能立刻启动：$($_.Exception.Message)"
    Write-Host "   重启（或下次登录）后会自动起；现在想用也可以先手动跑一次："
    Write-Host "   python `"$runner`""
}
if ($started) { Start-Sleep -Seconds 2 }

Write-Host ""
Write-Host "现在应该已经在跑了。验证一下："
# 用单引号：PowerShell 里没有 \ 转义引号这回事，写成双引号会让整份脚本**解析阶段就失败**
# （2026-09-26 实测：一个动作都没执行到，报错还指向 from 关键字，很难联想到是这行）。
Write-Host '  docker compose exec backend python -c "import asyncio; from services.agent import host_runner; print(asyncio.run(host_runner.health()))"'
Write-Host ""
Write-Host "卸载：powershell -ExecutionPolicy Bypass -File tools\host-runner\install-host-runner.ps1 -Uninstall"
