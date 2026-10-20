<# Register the calendar-aware background scheduler. #>
param([string]$Time = "", [string]$Python = "python")
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cliArgs = @((Join-Path $dir "dooray_gui.py"), "--enable-schedule")
if ($Time) { $cliArgs += @("--time", $Time) }
& $Python @cliArgs
if ($LASTEXITCODE -ne 0) { throw "Scheduler registration failed. Check the application log." }
Write-Host "[OK] Calendar-aware check-in/out scheduler enabled."
