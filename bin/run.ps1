<#
.SYNOPSIS
  分流器：把一件活按最省的档位送出去（T0 本地脚本 → T1 直连 API → T2 Flash 执行器 → T3 主脑）。

.DESCRIPTION
  档位仅描述执行方式，费用与速度以任务实测为准：
    T0 toolbox.py   本地确定性脚本（也可用明确的本地命令）
    T1 ask.py       明确文本输入到文本输出，也可生成待本地验收的源码
    T2 delegate.ps1 需要工具读写的有界工作包，按任务分配预算
    T3 主脑         —— 判断、取舍、验收（本脚本不负责 T3）

.EXAMPLE
  .\run.ps1 "把这个目录的文件清单导成 markdown" -Workspace D:\proj
  .\run.ps1 "润色这段文字" -Tier text
  .\run.ps1 "把 out 目录下所有 md 的一级标题改成 '# 序号 标题'" -Tier agent
  .\run.ps1 "..." -DryRun        # 只看路由决策与将执行的命令
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Task,
  [string]$Workspace = (Get-Location).Path,
  [ValidateSet('auto', 'toolbox', 'text', 'agent', 'main')][string]$Tier = 'auto',
  [string]$ToolboxCmd,           # 明确的 toolbox 命令名（如 manifest / rename / encoding）
  [string]$ToolboxArgs,
  [string[]]$ToolboxArgumentList,
  [ValidateSet('low', 'high', 'max')][string]$Effort = 'low',
  [string]$Type = 'general',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }

$Bin = $PSScriptRoot
$Py = 'python'

# ---------- 路由判断 ----------
$decision = $Tier
$why = ''

if ($ToolboxCmd) { $decision = 'toolbox'; $why = '显式指定了 toolbox 命令' }

if ($decision -eq 'auto') {
  $t = $Task
  if ($t -match '判断|取舍|架构|决策|策略|诊断|最佳|方案比较|design|architecture|decide') {
    $decision = 'main'; $why = 'Requires main-agent judgment'
  } elseif ($t -match '改名|重命名|清单|manifest|去重|dedupe|编码|encoding|抽.*(docx|pptx|Word|PPT)|csv.*概览|目录树') {
    $decision = 'toolbox'; $why = 'Deterministic transformation; supply exact toolbox args'
  } elseif ($t -match '运行.*测试|执行.*测试|运行.*lint|运行.*构建') {
    $decision = 'main'; $why = 'Select explicit local argv/test scope; no paid worker for merely running checks'
  } elseif ($t -match '改文件|批量改|重写文件|替换文件里的|执行命令|安装|修复.*脚本') {
    $decision = 'agent'; $why = 'Requires file/command tools'
  } elseif ($t -match '改写|翻译|润色|抽取|分类|摘要|写正则|rewrite|translate|summari') {
    $decision = 'text'; $why = 'Bounded text transformation'
  } else {
    $decision = 'main'; $why = 'Insufficient routing evidence; no paid call'
  }
}
if (-not $why) { $why = '用户显式指定档位' }

# ---------- 组装命令 ----------
switch ($decision) {
  'main' { $line = 'Main agent handles this task; no external call.' }
  'toolbox' {
    if ($ToolboxCmd) {
      $line = "$Py -I `"$Bin\toolbox.py`" $ToolboxCmd $ToolboxArgs"
    } else {
      $line = '(尚未指定子命令 —— 请用 -ToolboxCmd <命令> -ToolboxArgs "<参数>" 再跑一次)'
    }
  }
  'text' {
    $line = "$Py -I `"$Bin\ask.py`" `"$Task`""
  }
  'agent' {
    $line = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$Bin\delegate.ps1`" -Spec `"$Task`" -Workspace `"$Workspace`" -Effort $Effort -Type $Type"
  }
}

Write-Host "[router] 档位=$decision  原因=$why"
Write-Host "[router] 命令：" -NoNewline; Write-Host $line -ForegroundColor Cyan
if ($decision -eq 'toolbox') { Write-Host "[router] 提示：toolbox 默认 dry-run，改动类命令需追加 --apply" -ForegroundColor Yellow }
if ($DryRun) { return }

# ---------- 执行 ----------
switch ($decision) {
  'main' { exit 0 }
  'toolbox' {
    if (-not $ToolboxCmd) {
      Write-Host "[router] 这看起来是 T0 层的活（零 token、毫秒级）。可用子命令：" -ForegroundColor Yellow
      & $Py -I "$Bin\toolbox.py" list
      Write-Host "[router] 选定后这样调用：.\run.ps1 `"$Task`" -ToolboxCmd manifest -ToolboxArgs `"<目录> --out 清单.md --apply`"" -ForegroundColor Yellow
      exit 0
    }
    $argv = @('-I', "$Bin\toolbox.py", $ToolboxCmd)
    if ($ToolboxArgumentList) { $argv += $ToolboxArgumentList } elseif ($ToolboxArgs) { $argv += $ToolboxArgs.Split(' ') }
    & $Py @argv
  }
  'text' {
    & $Py -I "$Bin\ask.py" $Task
  }
  'agent' {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$Bin\delegate.ps1" -Spec $Task -Workspace $Workspace -Effort $Effort -Type $Type
  }
}
exit $LASTEXITCODE
