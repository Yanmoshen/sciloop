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
#     powershell -ExecutionPolicy Bypass -File scripts\install-host-runner.ps1
# 卸载：
#     powershell -ExecutionPolicy Bypass -File scripts\install-host-runner.ps1 -Uninstall
#
# ⚠️ 本文件**不写死任何绝对路径**（仓库路径可能含中文，写死容易踩编码坑）：
# 一切从 $PSScriptRoot 推出来。

param(
    [switch]$Uninstall,
    [string]$TaskName = 'SciLoopHostRunner'
)

$ErrorActionPreference = 'Stop'

# 仓库根 = scripts 的上一层
$repoRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repoRoot 'tools\host-runner\host_runner.py'

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

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Error "没找到 python。请先装 Python 3，并确认它在 PATH 里（python --version 能用）。"
    exit 1
}

Write-Host "仓库：$repoRoot"
Write-Host "执行器：$runner"
Write-Host "解释器：$python"

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$runner`"" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'SciLoop 本机执行环境：让 SciLoop 能在本机执行命令（研究者可在设置里关掉）' | Out-Null

Write-Host "已登记开机自启：$TaskName"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "现在应该已经在跑了。验证一下："
Write-Host "  docker compose exec backend python -c \"import asyncio; from services.agent import host_runner; print(asyncio.run(host_runner.health()))\""
Write-Host ""
Write-Host "卸载：powershell -ExecutionPolicy Bypass -File scripts\install-host-runner.ps1 -Uninstall"
