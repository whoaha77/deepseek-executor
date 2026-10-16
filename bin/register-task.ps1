<#
.SYNOPSIS
  注册每周巡检任务（Windows 任务计划程序）：巡检 DeepSeek 版本 + 出成本周报 + 自检。

.DESCRIPTION
  任务名：DeepSeek-Executor-Watch
  默认：每周一 09:05，依次执行
    1) watch_deepseek.py          巡检 flash 版本与官方更新日志
    2) cost_report.py --proposals 出成本报表与优化提案草案
    3) doctor.ps1                 执行器链路自检
  巡检只提醒、不改配置（用户明确要求）。

.EXAMPLE
  .\register-task.ps1                 # 注册/更新（每周一 09:05）
  .\register-task.ps1 -Time 20:30 -DaysOfWeek Monday,Thursday
  .\register-task.ps1 -Remove         # 卸载
#>
[CmdletBinding()]
param(
  [string]$Time = '09:05',
  [string[]]$DaysOfWeek = @('Monday'),
  [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }

$TaskName = 'DeepSeek-Executor-Watch'
$Root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "[task] 已卸载 $TaskName"
  exit 0
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw '找不到 python（巡检脚本依赖它）' }

$actions = @(
  (New-ScheduledTaskAction -Execute $py -Argument "-I `"$Root\bin\watch_deepseek.py`" --quiet" -WorkingDirectory $Root),
  (New-ScheduledTaskAction -Execute $py -Argument "-I `"$Root\bin\cost_report.py`" --days 7 --proposals" -WorkingDirectory $Root),
  (New-ScheduledTaskAction -Execute 'powershell' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Root\bin\doctor.ps1`"" -WorkingDirectory $Root)
)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $actions -Trigger $trigger -Settings $settings `
  -Principal $principal -Description 'DeepSeek 执行器：每周巡检版本/成本/链路（只提醒，不改配置）' -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host "[task] 已注册：$TaskName  状态=$($t.State)  触发=$($DaysOfWeek -join ',') $Time"
Write-Host '[task] 立刻试跑一次：schtasks /run /tn "DeepSeek-Executor-Watch"'
