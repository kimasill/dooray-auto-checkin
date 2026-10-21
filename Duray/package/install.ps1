<# Install/update while preserving personal configuration and credentials. #>
param([switch]$NonInteractive, [switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$srcExe = Join-Path $here 'DoorayAutoCheckin.exe'
$dataDir = Join-Path $env:LOCALAPPDATA 'DoorayCheckin'
$destDir = Join-Path $dataDir 'bin'
$destExe = Join-Path $destDir 'DoorayAutoCheckin.exe'
$configPath = Join-Path $dataDir 'config.json'
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '두레이 자동출근.lnk'
$taskName = 'DoorayCheckin'
function Step($n, $message) { Write-Host "[$n/5] $message" -ForegroundColor Cyan }
function Run-Hidden($path, $argument) {
    $process = Start-Process -FilePath $path -ArgumentList $argument -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0) { throw "$argument 실패 (종료 코드 $($process.ExitCode)). 로그: $dataDir\logs\checkin.log" }
}
function Installed-Processes {
    @(Get-Process DoorayAutoCheckin -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $destExe })
}
function Stop-InstalledProcesses {
    $processes = @(Installed-Processes)
    $processes | Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        try {
            if (-not $process.WaitForExit(10000)) { throw '기존 앱 프로세스가 종료되지 않았습니다.' }
        } catch [System.InvalidOperationException] { }
    }
}
function Copy-BinaryWithRetry([string]$source, [string]$destination) {
    # PyInstaller's bootloader and virus scanners can release image handles late.
    $deadline = (Get-Date).AddSeconds(20)
    while ($true) {
        try {
            Copy-Item -LiteralPath $source -Destination $destination -Force -ErrorAction Stop
            return
        } catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}
Write-Host "`n===== 두레이 자동 출퇴근 2.1.4 설치 / 업데이트 =====`n" -ForegroundColor Green
if (-not (Test-Path -LiteralPath $srcExe)) { throw "설치 파일과 EXE를 같은 폴더에 두세요: $here" }
if ([IO.Path]::GetFullPath($srcExe) -eq [IO.Path]::GetFullPath($destExe)) {
    throw '설치된 bin 폴더 대신 압축을 푼 배포 폴더에서 설치를 실행하세요.'
}
$sourceHash = (Get-FileHash -LiteralPath $srcExe -Algorithm SHA256).Hash
Step 1 '필요한 Chromium 버전 준비'
Unblock-File -LiteralPath $srcExe -ErrorAction SilentlyContinue
Run-Hidden $srcExe '--install-browser'

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$wasEnabled = [bool]($existingTask -and $existingTask.State -ne 'Disabled')
$wasRunning = [bool]($existingTask -and $existingTask.State -eq 'Running')
$taskXml = if ($existingTask) { Export-ScheduledTask -TaskName $taskName } else { $null }
$hadExe = Test-Path -LiteralPath $destExe
$backup = Join-Path $dataDir ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $backup | Out-Null
if ($taskXml) { [IO.File]::WriteAllText((Join-Path $backup 'DoorayCheckin.xml'), $taskXml, [Text.Encoding]::Unicode) }
if ($hadExe) { Copy-Item -LiteralPath $destExe -Destination (Join-Path $backup 'DoorayAutoCheckin.exe') }
$settingsHash = $null
$settingsCaptured = $false
$binaryChanged = $false
$taskTouched = $false
try {
    Step 2 '기존 프로그램과 예약 일시 중지'
    if ($existingTask) {
        $taskTouched = $true
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Stop-ScheduledTask -TaskName $taskName
    }
    $running = @(Installed-Processes)
    foreach ($process in $running) {
        if ($process.MainWindowHandle -ne 0) { $process.CloseMainWindow() | Out-Null }
    }
    if ($running.Count) { Start-Sleep -Seconds 2 }
    Stop-InstalledProcesses
    Step 3 '기존 설정 백업 및 새 실행 파일 설치'
    foreach ($name in @('config.json', 'state.json', 'calendar.json')) {
        $personalFile = Join-Path $dataDir $name
        if (Test-Path -LiteralPath $personalFile) { Copy-Item -LiteralPath $personalFile -Destination (Join-Path $backup $name) }
    }
    if (Test-Path -LiteralPath $configPath) { $settingsHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash }
    $settingsCaptured = $true
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    $binaryChanged = $true
    Copy-BinaryWithRetry $srcExe $destExe
    if ((Get-FileHash -LiteralPath $destExe -Algorithm SHA256).Hash -ne $sourceHash) { throw '복사한 실행 파일의 무결성 검사 실패' }
    Step 4 '기존 예약 상태 복원'
    if ($wasEnabled) {
        Run-Hidden $destExe '--migrate-schedule'
        $updated = Get-ScheduledTask -TaskName $taskName
        if ($updated.State -eq 'Disabled' -or $updated.Actions.Arguments -notmatch '--watch') { throw '예약 전환 확인 실패' }
        Write-Host '      기존 시간 설정으로 자동 예약을 유지합니다.'
    } else { Write-Host '      기존 예약 OFF 상태를 유지합니다.' }
    if ($settingsHash -and (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $settingsHash) {
        throw '설정 파일 보존 확인 실패'
    }
} catch {
    $failureMessage = $_.Exception.Message
    Write-Warning "업데이트 실패. 기존 버전과 예약을 복원합니다: $failureMessage"
    try {
        if ($binaryChanged) {
            if ($existingTask) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
            Stop-InstalledProcesses
            if ($hadExe) { Copy-BinaryWithRetry (Join-Path $backup 'DoorayAutoCheckin.exe') $destExe }
            elseif (Test-Path -LiteralPath $destExe) { Remove-Item -LiteralPath $destExe -Force }
        }
        if ($settingsCaptured -and $settingsHash -and (Test-Path -LiteralPath (Join-Path $backup 'config.json'))) {
            Copy-Item -LiteralPath (Join-Path $backup 'config.json') -Destination $configPath -Force
        }
        if ($taskTouched -and $taskXml) {
            Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
            if (-not $wasEnabled) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
            elseif ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
        }
        if ($binaryChanged -and $hadExe -and -not $NoLaunch) {
            Start-Process -FilePath $destExe -WindowStyle Hidden
        }
    } catch { Write-Warning "자동 복원 실패. 백업 위치: $backup / $($_.Exception.Message)" }
    throw "업데이트를 완료하지 못했습니다. $failureMessage / 백업: $backup"
}
Step 5 '바탕화면 바로가기 갱신'
try {
    $shellLink = New-Object -ComObject WScript.Shell
    $shortcut = $shellLink.CreateShortcut($lnk)
    $shortcut.TargetPath = $destExe
    $shortcut.WorkingDirectory = $destDir
    $shortcut.Description = '두레이 자동 출퇴근 2.1'
    $shortcut.Save()
} catch { Write-Warning "바로가기 생성 실패: $($_.Exception.Message). 실행 파일: $destExe" }
$report = [ordered]@{
    installedAt = (Get-Date).ToString('o')
    version = (Get-Item -LiteralPath $destExe).VersionInfo.FileVersion
    installedExe = $destExe
    exeSha256 = $sourceHash
    configPreserved = $true
    previousConfigSha256 = $settingsHash
    automaticScheduleEnabled = $wasEnabled
    backupDirectory = $backup
}
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup 'update-result.json') -Encoding UTF8
Write-Host "`n[완료] 기존 계정·시간·달력 설정을 유지했습니다." -ForegroundColor Green
Write-Host "  버전: $($report.version)"
Write-Host "  백업: $backup"
Write-Host '  비밀번호와 로그인 프로필은 기존 저장 위치에서 그대로 사용합니다.'
if (-not $NoLaunch) {
    if ($NonInteractive) { Start-Process -FilePath $destExe -WindowStyle Hidden }
    else {
        $answer = Read-Host '지금 프로그램을 실행할까요? (Y/N)'
        if ($answer -match '^[Yy]') { Start-Process -FilePath $destExe }
    }
}
