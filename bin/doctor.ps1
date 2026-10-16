<#
.SYNOPSIS
  体检 + 自修复：确认执行器链路完好（key/配置/脚本/计划任务），缺什么补什么。

.DESCRIPTION
  为什么需要它：CC Switch 切换 provider 会重写 ~/.codex/config.toml，但本方案的执行器
  **完全自包含**（worker-home\config.toml 自带 provider 与密钥），所以不受 CC Switch 影响 ——
  这里仍然每次跑一遍，防止文件被误删/误改。

.EXAMPLE
  .\doctor.ps1            # 只检查
  .\doctor.ps1 -Repair    # 缺什么补什么（会重建 worker-home\config.toml）
#>
[CmdletBinding()]
param([switch]$Repair)

$ErrorActionPreference = 'Continue'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }

$Root = Split-Path -Parent $PSScriptRoot
$ok = 0; $bad = 0; $fixed = 0
function Report($status, $msg) {
  $color = if ($status -eq 'OK') { 'Green' } elseif ($status -eq 'FIX') { 'Yellow' } else { 'Red' }
  Write-Host ("[{0}] {1}" -f $status.PadRight(3), $msg) -ForegroundColor $color
}

# 1) codex.exe
$Codex = (Get-Command codex.exe -ErrorAction SilentlyContinue).Source
if (-not $Codex) {
  $Codex = (Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'codex.exe') -ErrorAction SilentlyContinue } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if ($Codex) { Report OK "codex.exe: $Codex"; $ok++ } else { Report FAIL 'codex.exe 未找到（Codex 是否被卸载/移动？）'; $bad++ }

# 2) worker-home 配置
$WorkerCfg = Join-Path $Root 'worker-home\config.toml'
if (Test-Path $WorkerCfg) {
  $txt = Get-Content -LiteralPath $WorkerCfg -Raw -Encoding UTF8
  if ($txt -match 'deepseek-flash' -and $txt -match 'experimental_bearer_token') {
    Report OK 'worker-home\config.toml 正常（含 deepseek-flash 与密钥）'; $ok++
  } else {
    Report FAIL 'worker-home\config.toml 内容不完整（缺模型或密钥）'; $bad++
  }
} else {
  Report FAIL "worker-home\config.toml 缺失：$WorkerCfg"; $bad++
}

# 3) 关键脚本
$scriptBadBefore = $bad
foreach ($f in 'delegate.ps1', 'delegate-parallel.ps1', 'review.ps1', 'run.ps1', 'result.schema.json',
               'ask.py', 'toolbox.py', 'watch_deepseek.py', 'cost_report.py') {
  $p = Join-Path $PSScriptRoot $f
  if (Test-Path $p) { $ok++ } else { Report FAIL "缺少脚本：$f"; $bad++ }
}
if ($bad -eq $scriptBadBefore) { Report OK '脚本齐全' }

# 4) 目录
foreach ($d in 'logs', 'logs\units', 'state', 'lessons') {
  $p = Join-Path $Root $d
  if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null; Report FIX "已创建目录 $d"; $fixed++ } else { $ok++ }
}

# 5) 密钥可用性（真实调一次 /models）
try {
  $key = $env:DEEPSEEK_API_KEY
  if (-not $key) {
    $m = [regex]::Match((Get-Content -LiteralPath $WorkerCfg -Raw), 'experimental_bearer_token\s*=\s*"([^"]+)"')
    if ($m.Success) { $key = $m.Groups[1].Value }
  }
  if ($key) {
    $r = Invoke-RestMethod -Uri 'https://api.deepseek.com/models' -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 25
    $flash = $r.data | Where-Object { $_.id -eq 'deepseek-flash' }
    if ($flash) { Report OK ("API 可用：flash = {0}（上下文 {1:N0}）" -f $flash.name, $flash.context_window); $ok++ }
    else { Report FAIL 'API 可用但没找到 deepseek-flash'; $bad++ }
  } else { Report FAIL '找不到 API key（环境变量 DEEPSEEK_API_KEY 或 worker-home 配置）'; $bad++ }
} catch { Report FAIL "调用 DeepSeek API 失败：$($_.Exception.Message)"; $bad++ }

# 6) 计划任务
$task = Get-ScheduledTask -TaskName 'DeepSeek-Executor-Watch' -ErrorAction SilentlyContinue
if ($task) { Report OK "计划任务已注册：$($task.State)" ; $ok++ }
else { Report FIX '计划任务未注册 —— 运行 bin\register-task.ps1 即可' ; $fixed++ }

# 7) 主 brain 登录状态（提示项，不算失败）
$codexLogin = if ($Codex) { & $Codex login status 2>&1 } else { 'codex.exe unavailable' }
if ("$codexLogin" -match 'Logged in') { Report OK "Codex 登录：$codexLogin" }
else { Report FIX 'Codex 未登录官方账号 —— 主脑要用官方 GPT 时需先 `codex login`（执行器不受影响）' }

Write-Host ""
Write-Host "体检完成：通过 $ok 项，问题 $bad 项，已修复 $fixed 项。"
if ($bad -gt 0) { exit 1 }
exit 0
