<#
.SYNOPSIS
  并行执行器池：把互不重叠的工作单元同时派给 N 个 Flash 执行器，自动重试并汇总。

.DESCRIPTION
  任务清单 JSON 形如：
  [
    { "unit_id": "u01", "spec": "把 a.md 的标题格式改成 ...", "workspace": "D:\\proj", "effort": "low", "type": "format" },
    { "unit_id": "u02", "spec": "把 b.md 的标题格式改成 ...", "workspace": "D:\\proj" }
  ]
  铁律：各单元的改动范围必须互不重叠（一个文件同一时刻只归一个 worker）。

.EXAMPLE
  .\delegate-parallel.ps1 -TasksFile .\state\tasks-plan.json -MaxParallel 3
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TasksFile,
  [ValidateRange(1,3)][int]$MaxParallel = 3,
  [ValidateRange(0,2)][int]$MaxRetry = 2,
  [int[]]$RetryBackoffSec = @(5, 20)
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }

$Root = Split-Path -Parent $PSScriptRoot
$Delegate = Join-Path $PSScriptRoot 'delegate.ps1'
$StateDir = Join-Path $Root 'state'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$tasks = Get-Content -LiteralPath $TasksFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($tasks -isnot [array]) { $tasks = @($tasks) }
Write-Host "[pool] 共 $($tasks.Count) 个单元，并发上限 $MaxParallel"

$state = @{}
foreach ($t in $tasks) {
  $uid = if ($t.unit_id) { $t.unit_id } else { 'u-' + [guid]::NewGuid().ToString('N').Substring(0, 6) }
  if ($state.ContainsKey($uid)) { throw "Duplicate unit_id: $uid" }
  $state[$uid] = [ordered]@{
    unit_id = $uid; spec = $t.spec; workspace = $t.workspace; effort = $(if ($t.effort) { $t.effort } else { 'low' })
    type = $(if ($t.type) { $t.type } else { 'general' }); retry_safe = ($t.retry_safe -eq $true); status = 'pending'; attempts = 0; exit_code = $null; last_error = ''
  }
}

function Invoke-Unit($uid) {
  $u = $state[$uid]
  $u.attempts++
  $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Delegate,
            '-Spec', $u.spec, '-Workspace', $u.workspace, '-UnitId', $uid,
            '-Effort', $u.effort, '-Type', $u.type)
  $out = & powershell @args 2>&1
  $code = $LASTEXITCODE
  $u.exit_code = $code
  $u.last_error = if ($code -ne 0) { ($out | Select-Object -Last 3) -join ' | ' } else { '' }
  $u.status = if ($code -eq 0) { 'done' } elseif ($code -eq 2 -or $code -eq 124) { 'blocked' } else { 'failed' }
  return ($out -join "`n")
}

# ---- 第一轮：按并发上限跑 ----
# 注意：用真正的 Queue —— PowerShell 的 $arr[1..0] 在单元素时不会清空数组（会死循环重复派发）
$queue = New-Object System.Collections.Queue
foreach ($k in $state.Keys) { $queue.Enqueue($k) }
$running = @{}
while ($queue.Count -gt 0 -or $running.Count -gt 0) {
  while ($running.Count -lt $MaxParallel -and $queue.Count -gt 0) {
    $uid = $queue.Dequeue()
    Write-Host "[pool] 启动 $uid"
    $job = Start-Job -ScriptBlock {
      param($uid, $spec, $ws, $effort, $type, $delegate)
      & powershell -NoProfile -ExecutionPolicy Bypass -File $delegate -Spec $spec -Workspace $ws -UnitId $uid -Effort $effort -Type $type 2>&1
      $LASTEXITCODE
    } -ArgumentList $uid, $state[$uid].spec, $state[$uid].workspace, $state[$uid].effort, $state[$uid].type, $Delegate
    $running[$uid] = $job
  }
  if ($running.Count -eq 0) { break }
  Start-Sleep -Seconds 2
  foreach ($uid in @($running.Keys)) {
    $job = $running[$uid]
    if ($job.State -in 'Completed', 'Failed', 'Stopped') {
      $output = Receive-Job $job
      Remove-Job $job -Force
      $code = $output[-1]
      $state[$uid].attempts++
      $state[$uid].exit_code = $code
      $state[$uid].status = if ($code -eq 0) { 'done' } elseif ($code -eq 2 -or $code -eq 124) { 'blocked' } else { 'failed' }
      if ($code -ne 0) { $state[$uid].last_error = (($output[0..([Math]::Max(0, $output.Count - 2))] | Select-Object -Last 3) -join ' | ') }
      Write-Host "[pool] 完成 $uid  exit=$code  status=$($state[$uid].status)"
      $running.Remove($uid)
    }
  }
}

# ---- 重试：失败的按指数退避重跑 ----
for ($r = 1; $r -le $MaxRetry; $r++) {
  $failed = @($state.Keys | Where-Object { $state[$_].status -eq 'failed' -and $state[$_].retry_safe -and $state[$_].exit_code -in @(75,429,500,502,503,504) })
  if ($failed.Count -eq 0) { break }
  $wait = if ($r -le $RetryBackoffSec.Count) { $RetryBackoffSec[$r - 1] } else { 30 }
  Write-Host "[pool] 第 $r 轮重试 $($failed.Count) 个单元（等待 ${wait}s）"
  Start-Sleep -Seconds $wait
  foreach ($uid in $failed) {
    Write-Host "[pool] 重试 $uid"
    Invoke-Unit $uid | Out-Null
  }
}

# ---- 汇总 ----
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$statePath = Join-Path $StateDir "tasks-$stamp.json"
($state.Values | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Host "`n===== 汇总 ====="
$ok = 0; $bad = 0
foreach ($uid in $state.Keys) {
  $u = $state[$uid]
  $mark = if ($u.status -eq 'done') { '✅' } else { '❌' }
  Write-Host ("{0} {1}  attempts={2}  exit={3}  {4}" -f $mark, $uid, $u.attempts, $u.exit_code, $u.last_error)
  if ($u.status -eq 'done') { $ok++ } else { $bad++ }
}
Write-Host "成功 $ok / 失败 $bad   状态文件：$statePath"

# 有失败且重试到顶 → 提示升级主脑接管，并留痕
if ($bad -gt 0) {
  $esc = Join-Path $Root 'lessons\lessons.md'
  $line = "- [$(Get-Date -Format 'yyyy-MM-dd HH:mm')] 单元失败（已重试 $MaxRetry 次）：" +
          (($state.Keys | Where-Object { $state[$_].status -ne 'done' } | ForEach-Object { $state[$_].unit_id }) -join ', ')
  Add-Content -LiteralPath $esc -Value $line -Encoding UTF8
  Write-Host "[pool] 失败单元已记录到 lessons.md —— 建议主脑接管这几项" -ForegroundColor Yellow
  exit 1
}
exit 0
