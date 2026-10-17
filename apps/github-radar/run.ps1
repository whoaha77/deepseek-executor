[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$RadarArgs)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$python = (Get-Command python -ErrorAction Stop).Source
if (-not $RadarArgs) { $RadarArgs = @('scan') }
& $python -I (Join-Path $PSScriptRoot 'scripts\radar.py') @RadarArgs
exit $LASTEXITCODE
