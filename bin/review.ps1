<#
.SYNOPSIS
  复核者（flash + effort=high + 只读沙箱）：审 diff / 审产物，只给质疑清单，不改任何文件。

.DESCRIPTION
  触发时机（不必每次都跑，避免浪费）：改动跨 ≥3 文件 / 涉及数据口径 / 涉及要交付的成果。
  只读沙箱在独立进程里真能生效（原生子代理做不到 —— 官方文档：custom agent files can't create a separate read-only sandbox）。

.EXAMPLE
  .\review.ps1 -Workspace D:\proj -What "把 5 个 md 的一级标题统一成 '# 序号 标题'" -Files "out\a.md","out\b.md"
  .\review.ps1 -Workspace D:\proj -What "重命名批次" -GitDiff
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Workspace,
  [Parameter(Mandatory = $true)][string]$What,      # 原本要做什么（验收标准）
  [string[]]$Files,
  [ValidateSet('auto','text','agent')][string]$Mode = 'auto',
  [ValidateSet('off','low','high')][string]$TextThinking = 'off',
  [ValidateSet('low','high','max')][string]$Effort = 'low',                                  # 要复核的具体文件
  [switch]$GitDiff,                                  # 复核工作区 git diff
  [switch]$DryRun,
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')][string]$UnitId = '',
  [ValidateRange(1,3600)][int]$TimeoutSec = 60
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }

$Root = Split-Path -Parent $PSScriptRoot
if ($Files) { $Files = @($Files | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if ($Mode -eq 'auto') { $Mode = if ($Files -or $GitDiff) { 'text' } else { 'agent' } }
if ($Mode -eq 'text') {
  $reviewArgs = @('-I', (Join-Path $PSScriptRoot 'review_text.py'), '--workspace', $Workspace, '--what', $What,
    '--timeout', "$TimeoutSec", '--effort', $TextThinking)
  if ($Files) { $reviewArgs += @('--files', ($Files -join '|')) }
  if ($GitDiff) { $reviewArgs += '--git-diff' }
  if ($DryRun) { $reviewArgs += '--dry-run' }
  & python @reviewArgs
  exit $LASTEXITCODE
}
if ($DryRun) { throw 'DryRun supports text review only; select -Mode text with explicit files or diff.' }
$UnitsDir = Join-Path $Root 'logs\units'
New-Item -ItemType Directory -Force -Path $UnitsDir | Out-Null

$Codex = (Get-Command codex.exe -ErrorAction SilentlyContinue).Source
if (-not $Codex) {
  $Codex = (Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'codex.exe') -ErrorAction SilentlyContinue } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $Codex) { throw '找不到 codex.exe' }

$WorkerHome = Join-Path $Root 'worker-home'
if (-not (Test-Path (Join-Path $WorkerHome 'config.toml'))) { throw '缺少 worker-home\config.toml' }
$env:CODEX_HOME = $WorkerHome

if (-not $UnitId) { $UnitId = 'r-' + (Get-Date -Format 'yyyyMMdd-HHmmss') }
$ArtifactId = $UnitId + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$SpecPath = Join-Path $UnitsDir "$ArtifactId.review.spec.txt"
$OutJsonl = Join-Path $UnitsDir "$ArtifactId.review.jsonl"
$ErrLog   = Join-Path $UnitsDir "$ArtifactId.review.err.log"
$LastMsg  = Join-Path $UnitsDir "$ArtifactId.review.md"

$fileList = ''
if ($Files) { $fileList = ($Files | ForEach-Object { "  - $_" }) -join "`r`n" }
$diffHint = if ($GitDiff) { "先自己运行 `git diff` 查看改动（只读允许）。" } else { '' }

$spec = @"
# 任务：独立复核（你不修改任何文件，只读）
原任务与验收标准：
$What

$diffHint
要复核的对象：
$(if ($fileList) { $fileList } else { '（见工作区当前改动）' })

# 范围纪律（务必遵守，这是成本纪律）
只依据上面列出的文件、以及对它们的直接读取（必要时 `git diff`）作判断。
**不要**遍历 logs\、不要翻历史运行记录、不要全仓搜索 —— 除非列出的证据明显不足以判断。
拿不准就写"存疑"，不要为了确认一件事把范围扩大十倍。

# 输出要求（只输出这三类，不要复述原文凑字数）
1) 明确的问题：位置 + 为什么是问题（能附最小复现/判据就附）
2) 存疑但不确定的点
3) 通过项清单
如果确实没有发现问题，就直接写"未发现问题"。
"@
[System.IO.File]::WriteAllText($SpecPath, $spec, $utf8NoBom)

$argList = @('exec', '--skip-git-repo-check', '-C', $Workspace, '-s', 'read-only', '--ephemeral', '--json',
             '-o', $LastMsg, '-c', "model_reasoning_effort=`"$Effort`"", '-')

$startedAt = Get-Date
$requestPath = $SpecPath + '.request.json'
$request = @{ exe = $Codex; args = @($argList); stdin = $SpecPath; stdout = $OutJsonl; stderr = $ErrLog; timeout = $TimeoutSec }
[System.IO.File]::WriteAllText($requestPath, ($request | ConvertTo-Json -Depth 5), $utf8NoBom)
& python -I (Join-Path $PSScriptRoot 'process_runner.py') --request $requestPath
$p = [pscustomobject]@{ ExitCode = $LASTEXITCODE }

$dur = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

if ($p.ExitCode -eq 0 -and (-not (Test-Path -LiteralPath $LastMsg) -or (Get-Item -LiteralPath $LastMsg).Length -eq 0)) { $p.ExitCode = 3 }
$usage = $null
foreach ($line in [System.IO.File]::ReadAllLines($OutJsonl)) {
  if ($line -notmatch '"turn\.completed"') { continue }
  try { $ev = $line | ConvertFrom-Json; if ($ev.usage) { $usage = $ev.usage } } catch { }
}

# 记账（复核用 effort=high，价格按 token 量计，与 worker 同价）
$now = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'China Standard Time')
$isPeak = ($now.DayOfWeek -ge 'Monday' -and $now.DayOfWeek -le 'Friday') -and
          ((($now.Hour -ge 9) -and ($now.Hour -lt 12)) -or (($now.Hour -ge 14) -and ($now.Hour -lt 18)))
$rateHit = 0.02; $rateMiss = 1.0; $rateOut = 4.0
if ($isPeak) { $rateHit = 0.04; $rateMiss = 2.0; $rateOut = 8.0 }
$cost = $null
if ($usage) {
  $miss = [Math]::Max(0, [int]$usage.input_tokens - [int]$usage.cached_input_tokens)
  $cost = [math]::Round(([int]$usage.cached_input_tokens * $rateHit + $miss * $rateMiss + [int]$usage.output_tokens * $rateOut) / 1e6, 4)
  $rec = [ordered]@{
    ts = (Get-Date).ToString('s'); unit_id = $UnitId; type = 'review'; model = 'deepseek-flash'; effort = $Effort
    sandbox = 'read-only'; workspace = $Workspace; is_peak = $isPeak; duration_sec = $dur; exit_code = $p.ExitCode
    status = $(if ($p.ExitCode -eq 0) { 'done' } else { 'failed' }); files_changed = @(); summary = "复核：$What"; unverified = @(); blocked_reason = ''
    input_tokens = [int]$usage.input_tokens; cached_input_tokens = [int]$usage.cached_input_tokens
    output_tokens = [int]$usage.output_tokens; reasoning_output_tokens = [int]$usage.reasoning_output_tokens
    cost_estimated_cny = $cost; artifacts = @{ spec = $SpecPath; report = $LastMsg }
  }
  [System.IO.File]::AppendAllText((Join-Path $Root 'logs\delegations.jsonl'),
    (($rec | ConvertTo-Json -Depth 5 -Compress) + "`r`n"), $utf8NoBom)
}

Write-Host ("[flash-reviewer] {0}  exit={1}  {2}s  cost={3}" -f $UnitId, $p.ExitCode, $dur, $(if ($null -eq $cost) { 'n/a' } else { ('¥{0:N4}' -f $cost) }))
if (Test-Path $LastMsg) { Get-Content -LiteralPath $LastMsg -Encoding UTF8 | Write-Host }
exit $p.ExitCode
