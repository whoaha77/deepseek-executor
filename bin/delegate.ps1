<#
.SYNOPSIS
  把一份精确规格派给 DeepSeek Flash 执行器（独立 codex 进程），落审计日志与成本账。

.DESCRIPTION
  成本设计（2026-10-15 实测）：
  - 用极简 CODEX_HOME（worker-home，无 skills/plugins）→ 固定上下文 52k → 9.1k tokens（约 5.7× 省）
  - 固定人设前缀 → 命中 DeepSeek 上下文缓存（实测 85%~98%）
  - --ephemeral 不落会话历史；--output-schema 强制结构化交付

.EXAMPLE
  .\delegate.ps1 -Spec "把 out\*.md 的一级标题改成 '# 序号 标题'，其它不动" -Workspace "D:\myproj"

.EXAMPLE
  .\delegate.ps1 -SpecFile .\specs\u01.md -Workspace "D:\myproj" -Effort high -Type refactor
#>
[CmdletBinding(DefaultParameterSetName = 'Spec')]
param(
  [Parameter(Mandatory = $true, Position = 0, ParameterSetName = 'Spec')]
  [string]$Spec,

  [Parameter(Mandatory = $true, ParameterSetName = 'SpecFile')]
  [string]$SpecFile,

  [Parameter(Mandatory = $true)]
  [string]$Workspace,

  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')][string]$UnitId = '',
  [string]$Type = 'general',
  [ValidateSet('low', 'high', 'max')][string]$Effort = 'low',
  [ValidateSet('workspace-write', 'read-only')][string]$Sandbox = 'workspace-write',
  [switch]$KeepSession,          # 需要留 Codex 会话（默认 --ephemeral 不落盘）
  [switch]$NoSchema,             # 不强制结构化交付（一般不用）
  [ValidateRange(0,600)][int]$TimeoutSec = 0,
  [ValidateRange(0,100)][int]$MaxToolCommands = 0,
  [ValidateRange(0,5)][int]$MaxPatchFailures = 0,
  [ValidateRange(-1,600)][int]$IdleSec = -1,
  [ValidateSet('quick','standard','extended')][string]$BudgetProfile = 'standard',
  [string]$BudgetReason = '',
  [string]$WatchPathsFile = '',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom; $OutputEncoding = $utf8NoBom } catch { }

$Root = Split-Path -Parent $PSScriptRoot
$LogsDir = Join-Path $Root 'logs'
$UnitsDir = Join-Path $LogsDir 'units'
New-Item -ItemType Directory -Force -Path $UnitsDir | Out-Null

# Resolve local estimates before launching any paid process. Overrides require a rationale.
$budgetArgs = @('-I', (Join-Path $PSScriptRoot 'budget.py'), '--profile', $BudgetProfile)
if ($TimeoutSec -gt 0) { $budgetArgs += @('--implementation-sec', "$TimeoutSec") }
if ($MaxToolCommands -gt 0) { $budgetArgs += @('--max-commands', "$MaxToolCommands") }
if ($MaxPatchFailures -gt 0) { $budgetArgs += @('--max-patch-errors', "$MaxPatchFailures") }
if ($IdleSec -ge 0) { $budgetArgs += @('--idle-sec', "$IdleSec") }
if ($BudgetReason) { $budgetArgs += @('--reason', $BudgetReason) }
$budgetJson = & python @budgetArgs
if ($LASTEXITCODE -ne 0) { throw 'Invalid task budget; supply a bounded estimate and BudgetReason for overrides.' }
$taskBudget = $budgetJson | ConvertFrom-Json
$TimeoutSec = [int]$taskBudget.implementation_sec
$MaxToolCommands = [int]$taskBudget.max_commands
$MaxPatchFailures = [int]$taskBudget.max_patch_errors
$IdleSec = [int]$taskBudget.idle_sec
$watchPaths = @()
if ($WatchPathsFile) {
  $watchPaths = @(Get-Content -LiteralPath $WatchPathsFile -Raw -Encoding UTF8 | ConvertFrom-Json)
  foreach ($watchPath in $watchPaths) { if ($watchPath -isnot [string]) { throw 'Watch paths must be explicit strings.' } }
}
if ($DryRun) {
  [ordered]@{ preflight='passed'; api_called=$false; task_budget=$taskBudget; watched_files=$watchPaths.Count } | ConvertTo-Json -Depth 5
  return
}

# ---- 解析 codex.exe（bin 目录带哈希会随更新变化，动态取最新）----
$Codex = (Get-Command codex.exe -ErrorAction SilentlyContinue).Source
if (-not $Codex) {
  $Codex = (Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'codex.exe') -ErrorAction SilentlyContinue } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $Codex) { throw "找不到 codex.exe（$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe）" }

# ---- 极简 CODEX_HOME（省 token 的关键）----
$WorkerHome = Join-Path $Root 'worker-home'
if (-not (Test-Path (Join-Path $WorkerHome 'config.toml'))) { throw "缺少 worker-home\config.toml（执行器专用配置）" }
$env:CODEX_HOME = $WorkerHome

# ---- unit id 与文件 ----
if (-not $UnitId) {
  $UnitId = 'u-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 4)
}
$ArtifactId = $UnitId + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$SpecPath   = Join-Path $UnitsDir "$ArtifactId.spec.txt"
$OutJsonl   = Join-Path $UnitsDir "$ArtifactId.jsonl"
$ErrLog     = Join-Path $UnitsDir "$ArtifactId.err.log"
$LastMsg    = Join-Path $UnitsDir "$ArtifactId.last.json"

# ---- 固定人设前缀（固定 = 命中 DeepSeek 上下文缓存）----
$Prefix = @'
# 角色（固定，勿改）
你是执行器，只做下面精确指定的机械工作：不重新设计、不扩大范围、不做取舍决策。
- 规格不足或有歧义：立即停下，按 schema 返回 status=blocked 并说明缺什么；绝不猜测。
- 只允许改动工作区内文件；禁止 git commit / push；禁止删除数据；禁止触碰只读区（如 D:\protected-data、D:\research-raw）。
- **写文件必须保持原编码与行尾风格**：不要用 PowerShell 的 Set-Content -Encoding utf8（PS 5.1 会自动加 BOM，实测被抓过）；
  优先用 apply_patch，或 [System.IO.File]::WriteAllText($p,$t,(New-Object System.Text.UTF8Encoding($false)))；不要弄丢原文件结尾换行。
- 改完自检一次：字节数变化是否合理、有没有多出 BOM、行尾是否与原文一致。
- 尽量一次成型：先看清输入再动手，减少往返；files_changed 必须如实列出真实改动的文件。
- 范围明确的新建独立文件：直接依据规格实现；不读取 README、无关脚本、历史 work 目录，不递归盘点工作区。规格已给编码/行尾时直接遵循，不重复调查。
- 预检通常合并为1次、最多2次工具调用后开始实现；若缺少关键输入，返回blocked说明，不继续无关探索。这是行为预算指导，不是工具层硬限制。确需读取既有实现时，只读规格点名的文件和必要依赖。
- 最终回答**只输出 JSON 对象**（不要代码围栏、不要任何说明文字），严格符合 output-schema。
- 补丁通道出现格式/UTF-8参数错误后不要反复猜测shell转义；缺可靠写入方式则blocked。外部runner监控命令总数和补丁失败次数，到预算即终止，部分文件由主脑验收。
- 只完成当前单一目标的小工作包，不接管整个模块；需要扩大文件白名单、改变设计或发现新任务时，交回主脑，不自行追加阶段。输出简短diff说明和验证结果后停止。

# 任务规格
'@

if ($PSCmdlet.ParameterSetName -eq 'SpecFile') {
  if (-not (Test-Path $SpecFile)) { throw "规格文件不存在：$SpecFile" }
  $Spec = Get-Content -LiteralPath $SpecFile -Raw -Encoding UTF8
}
[System.IO.File]::WriteAllText($SpecPath, ($Prefix + "`r`n" + $Spec), $utf8NoBom)

# ---- 组装参数 ----
$argList = @('exec', '--skip-git-repo-check', '-C', $Workspace, '-s', $Sandbox, '--json', '-o', $LastMsg)
if (-not $KeepSession) { $argList += '--ephemeral' }
if (-not $NoSchema) { $argList += @('--output-schema', (Join-Path $PSScriptRoot 'result.schema.json')) }
$argList += @('-c', "model_reasoning_effort=`"$Effort`"")
$argList += '-'   # 提示词从 stdin 读（即 spec 文件）

# ---- 运行 ----
$startedAt = Get-Date
$requestPath = $SpecPath + '.request.json'
$MonitorPath = $OutJsonl + '.monitor.json'
$request = @{ exe = $Codex; args = @($argList); stdin = $SpecPath; stdout = $OutJsonl; stderr = $ErrLog; timeout = $TimeoutSec; max_commands = $MaxToolCommands; max_patch_errors = $MaxPatchFailures; idle_sec = $IdleSec; watch_paths = @($watchPaths); monitor = $MonitorPath }
[System.IO.File]::WriteAllText($requestPath, ($request | ConvertTo-Json -Depth 5), $utf8NoBom)
& python -I (Join-Path $PSScriptRoot 'process_runner.py') --request $requestPath
$p = [pscustomobject]@{ ExitCode = $LASTEXITCODE }

$exitCode = $p.ExitCode
$durationSec = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

# ---- 解析用量与交付 ----
$usage = $null
foreach ($line in [System.IO.File]::ReadAllLines($OutJsonl)) {
  if ($line -notmatch '"turn\.completed"') { continue }
  try { $ev = $line | ConvertFrom-Json; if ($ev.usage) { $usage = $ev.usage } } catch { }
}

$status = 'unknown'; $summary = ''; $filesChanged = @(); $unverified = @(); $blockedReason = ''
if (Test-Path $LastMsg) {
  $raw = (Get-Content -LiteralPath $LastMsg -Raw -Encoding UTF8).Trim()
  $raw = $raw -replace '^```(json)?\s*', '' -replace '\s*```\s*$', ''   # 去掉代码围栏
  $json = $raw
  if ($json -notmatch '^\s*\{') {
    # 模型有时会在 JSON 前带说明文字：取最后一个花括号块
    $m = [regex]::Matches($json, '(?s)\{.*\}')
    if ($m.Count -gt 0) { $json = $m[$m.Count - 1].Value }
  }
  try {
    $r = $json | ConvertFrom-Json
    $status = $r.status; $summary = $r.summary; $blockedReason = $r.blocked_reason
    if ($r.files_changed) { $filesChanged = @($r.files_changed) }
    if ($r.unverified)    { $unverified    = @($r.unverified) }
  } catch { $status = 'unparsed'; $summary = $raw.Substring(0, [Math]::Min(200, $raw.Length)) }
}

# A completed process alone is not a valid structured delivery.
if ($exitCode -eq 0) {
  if ($status -eq 'blocked' -or $status -eq 'partial') { $exitCode = 2 }
  elseif ($NoSchema -and (Test-Path -LiteralPath $LastMsg) -and (Get-Item -LiteralPath $LastMsg).Length -gt 0) { $status = 'done'; $unverified += 'NoSchema: response is unstructured; main-agent verification required' }
  elseif ($status -ne 'done' -or $null -eq $r.summary -or $null -eq $r.files_changed -or $null -eq $r.unverified -or $null -eq $r.blocked_reason) { $exitCode = 3; $status = 'invalid_result' }
}
if ($p.ExitCode -ne 0) { $status = if ($p.ExitCode -eq 124) { 'timeout' } elseif ($p.ExitCode -eq 125) { 'budget_exceeded' } else { 'failed' } }

# ---- 成本（¥，flash 峰谷价：谷 0.02/1/4，峰翻倍，单位 元/百万 token）----
$now = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'China Standard Time')
$isPeak = ($now.DayOfWeek -ge 'Monday' -and $now.DayOfWeek -le 'Friday') -and
          ((($now.Hour -ge 9) -and ($now.Hour -lt 12)) -or (($now.Hour -ge 14) -and ($now.Hour -lt 18)))
$rateHit  = 0.02; $rateMiss = 1.0; $rateOut = 4.0
if ($isPeak) { $rateHit = 0.04; $rateMiss = 2.0; $rateOut = 8.0 }

$inTok = $null; $cachedTok = $null; $outTok = $null; $reasonTok = $null; $costCny = $null
$usageStatus = 'unknown'
if ($usage) {
  $usageStatus = 'partial'
  if ($null -ne $usage.input_tokens) { $inTok = [long]$usage.input_tokens }
  if ($null -ne $usage.cached_input_tokens) { $cachedTok = [long]$usage.cached_input_tokens }
  if ($null -ne $usage.output_tokens) { $outTok = [long]$usage.output_tokens }
  if ($null -ne $usage.reasoning_output_tokens) { $reasonTok = [long]$usage.reasoning_output_tokens }
  if ($null -ne $inTok -and $null -ne $cachedTok -and $null -ne $outTok -and
      $inTok -ge 0 -and $cachedTok -ge 0 -and $cachedTok -le $inTok -and $outTok -ge 0) {
    $usageStatus = 'reported'
    $missTok = $inTok - $cachedTok
    $costCny = [math]::Round(($cachedTok * $rateHit + $missTok * $rateMiss + $outTok * $rateOut) / 1e6, 4)
  }
}

# ---- 落账 ----
$record = [ordered]@{
  trace_id = $env:DEEPSEEK_TRACE_ID; node_id = $env:DEEPSEEK_NODE_ID
  ts = (Get-Date).ToString('s'); unit_id = $UnitId; type = $Type; model = 'deepseek-flash'; effort = $Effort
  sandbox = $Sandbox; workspace = $Workspace; is_peak = $isPeak; duration_sec = $durationSec; exit_code = $exitCode
  status = $status; files_changed = $filesChanged; summary = $summary; unverified = $unverified
  blocked_reason = $blockedReason
  input_tokens = $inTok; cached_input_tokens = $cachedTok; output_tokens = $outTok; reasoning_output_tokens = $reasonTok
  usage_status = $usageStatus
  task_budget = $taskBudget
  cost_estimated_cny = $costCny
  artifacts = @{ spec = $SpecPath; jsonl = $OutJsonl; err = $ErrLog; last = $LastMsg; monitor = $MonitorPath }
}
[System.IO.File]::AppendAllText((Join-Path $LogsDir 'delegations.jsonl'),
  (($record | ConvertTo-Json -Depth 5 -Compress) + "`r`n"), $utf8NoBom)

# ---- 控制台回报 ----
$costText = if ($null -eq $costCny) { 'n/a' } else { ('¥{0:N4}' -f $costCny) }
$inText = if ($null -eq $inTok) { 'n/a' } else { $inTok }
$outText = if ($null -eq $outTok) { 'n/a' } else { $outTok }
$cacheText = if ($null -eq $cachedTok) { 'n/a' } else { $cachedTok }
Write-Host ("[flash-worker] {0}  exit={1}  {2}s  tokens in/out={3}/{4} (cache {5})  cost={6}  status={7}" -f `
  $UnitId, $exitCode, $durationSec, $inText, $outText, $cacheText, $costText, $status)
if ($summary) { Write-Host "  summary: $summary" }
foreach ($f in $filesChanged) { Write-Host "  changed: $f" }
foreach ($u in $unverified)   { Write-Host "  unverified: $u" }
if ($blockedReason) { Write-Host "  blocked: $blockedReason" }

exit $exitCode
