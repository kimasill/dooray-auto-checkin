<# Read-only support bundle. Does not log in, change attendance, or modify tasks/settings. #>
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'DoorayCheckin'),
    [string]$OutputDir = ([Environment]::GetFolderPath('Desktop')),
    [switch]$SkipSystemInfo
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$configPath = Join-Path $DataDir 'config.json'
$cfg = $null
$warnings = New-Object 'System.Collections.Generic.List[string]'
$redactions = New-Object 'System.Collections.Generic.List[string]'
foreach ($value in @($DataDir, $env:USERPROFILE, $env:COMPUTERNAME)) {
    if ($value) { $redactions.Add($value) }
}
if (Test-Path -LiteralPath $configPath) {
    try {
        $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($prop in $cfg.PSObject.Properties) {
            if ($prop.Name -match '(?i)password|passwd|secret|token|login_id|tenant_url' -and $prop.Value -is [string] -and $prop.Value) {
                $redactions.Add($prop.Value)
            }
        }
    } catch { $warnings.Add('config.json을 읽지 못했습니다. 파일 형식을 확인하세요.') }
} else { $warnings.Add('설치된 앱의 config.json이 없습니다. 앱을 사용한 Windows 계정에서 실행했는지 확인하세요.') }

function Protect-Text([string]$text) {
    $text = [regex]::Replace($text, '(?i)https?://[^\s"<>]+', '[URL]')
    foreach ($value in ($redactions | Sort-Object Length -Descending)) {
        $text = [regex]::Replace($text, [regex]::Escape($value), '[REDACTED]', 'IgnoreCase')
        # Logs can contain JSON-escaped Windows paths.
        $text = [regex]::Replace($text, [regex]::Escape($value.Replace('\', '\\')), '[REDACTED]', 'IgnoreCase')
    }
    $text = [regex]::Replace($text, '(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', '[EMAIL]')
    $text = [regex]::Replace($text, '(?im)^.*(?:authorization\s*:|cookie\s*:|password\s*[:=]|passwd\s*[:=]|access_token\s*[:=]).*$', '[SENSITIVE LINE REMOVED]')
    return $text
}
function Pick($obj, [string[]]$keys) {
    $result = [ordered]@{}
    foreach ($key in $keys) {
        if ($null -ne $obj -and $null -ne $obj.PSObject.Properties[$key]) { $result[$key] = $obj.$key }
    }
    return $result
}
if (-not $OutputDir) { $OutputDir = $PSScriptRoot }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$bundleName = 'Dooray-diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6)
$folder = Join-Path ([IO.Path]::GetFullPath($OutputDir)) $bundleName
New-Item -ItemType Directory -Path $folder | Out-Null
function Save-Json([string]$name, $value) {
    ConvertTo-Json -InputObject (Protect-Value $value) -Depth 12 | Set-Content -LiteralPath (Join-Path $folder $name) -Encoding UTF8
}
function Protect-Value($value) {
    if ($null -eq $value) { return $null }
    if ($value -is [string]) { return Protect-Text $value }
    # Pipeline-wrapped booleans/numbers can also satisfy -is [PSCustomObject].
    if ($value -is [System.ValueType]) { return $value.PSObject.BaseObject }
    if ($value -is [System.Collections.IDictionary]) {
        $safe = [ordered]@{}
        foreach ($key in $value.Keys) { $safe[$key] = Protect-Value $value[$key] }
        return $safe
    }
    if ($value -is [System.Array]) {
        $safe = @($value | ForEach-Object { Protect-Value $_ })
        return ,$safe
    }
    if ($value -is [PSCustomObject]) {
        $safe = [ordered]@{}
        foreach ($prop in $value.PSObject.Properties) { $safe[$prop.Name] = Protect-Value $prop.Value }
        return $safe
    }
    return $value
}

Write-Host '두레이 진단 정보를 읽고 있습니다. 출퇴근 처리나 설정 변경은 하지 않습니다.' -ForegroundColor Cyan
$keys = @('checkin_enabled','checkin_time','time_mode','random_window_minutes','random_before_minutes','random_after_minutes',
    'checkout_enabled','checkout_time','checkout_time_mode','checkout_random_before_minutes','checkout_random_after_minutes',
    'late_grace_minutes','workdays','skip_weekends','skip_korean_holidays','skip_dates','half_day_policy',
    'am_half_checkout_time','pm_half_checkout_time','dooray_sync_enabled','sync_interval_minutes',
    'timeout_ms','retry_count','retry_delay_seconds','headless','notify_on_failure')
$safeConfig = Pick $cfg $keys
$overrides = [ordered]@{}
if ($cfg -and $cfg.date_overrides) {
    foreach ($prop in $cfg.date_overrides.PSObject.Properties) {
        if ($prop.Name -match '^\d{4}-\d{2}-\d{2}$') {
            $overrides[$prop.Name] = Pick $prop.Value @('kind','checkin','checkout','checkin_time','checkout_time')
        }
    }
}
$safeConfig['date_overrides'] = $overrides
$safeConfig['accountConfigured'] = [bool]($cfg -and $cfg.login_id -and $cfg.tenant_url)
Save-Json 'schedule-settings.json' $safeConfig

foreach ($name in @('state','calendar','watcher.owner','execution.owner')) {
    $path = Join-Path $DataDir ($name + '.json')
    if (-not (Test-Path -LiteralPath $path)) { continue }
    try {
        $data = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($name -eq 'calendar') {
            $data = Pick $data @('month','synced_at','error')
        } elseif ($name -eq 'state') {
            $data = Pick $data @('last_checkin_date','last_checkin_result','last_checkin_error','last_checkin_error_at',
                'last_checkout_date','last_checkout_result','last_checkout_error','last_checkout_error_at',
                'last_result','last_run_at','last_error_at','history')
        } else { $data = Pick $data @('pid','label','started_at') }
        Save-Json ($name + '.json') $data
    } catch { $warnings.Add($name + '.json 조회 실패') }
}
$logs = Join-Path $DataDir 'logs'
foreach ($name in @('checkin.log','checkin.log.1','checkin.log.2')) {
    $path = Join-Path $logs $name
    if (Test-Path -LiteralPath $path) {
        try {
            Protect-Text ((Get-Content -LiteralPath $path -Tail 5000 -Encoding UTF8) -join "`r`n") |
                Set-Content -LiteralPath (Join-Path $folder $name) -Encoding UTF8
        } catch { $warnings.Add($name + ' 조회 실패') }
    }
}

$environment = [ordered]@{ collectedAt = (Get-Date).ToString('o'); collectorVersion = 2; dataFound = (Test-Path -LiteralPath $configPath) }
if (-not $SkipSystemInfo) {
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        $environment['windowsVersion'] = $os.Version
        $environment['lastBootTime'] = $os.LastBootUpTime.ToString('o')
        $environment['timeZone'] = (Get-TimeZone).Id
        $browsers = Join-Path $env:LOCALAPPDATA 'ms-playwright'
        $environment['chromiumFolders'] = @(Get-ChildItem -LiteralPath $browsers -Directory -ErrorAction SilentlyContinue |
            Where-Object Name -Like 'chromium*' | Select-Object -ExpandProperty Name)
        $exe = Join-Path $DataDir 'bin\DoorayAutoCheckin.exe'
        if (Test-Path -LiteralPath $exe) {
            $environment['installedVersion'] = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
            $environment['installedSha256'] = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
        }
        $processes = @(Get-CimInstance Win32_Process -Filter "Name='DoorayAutoCheckin.exe'" | ForEach-Object {
            @{ pid=$_.ProcessId; parentPid=$_.ParentProcessId; watcher=[bool]($_.CommandLine -match '--watch'); startedAt=$_.CreationDate.ToString('o') }
        })
        Save-Json 'processes.json' $processes
    } catch { $warnings.Add('실행 환경 일부 조회 실패: ' + $_.Exception.Message) }
    try {
        $task = Get-ScheduledTask -TaskName 'DoorayCheckin' -ErrorAction SilentlyContinue
        $taskData = [ordered]@{ exists = [bool]$task }
        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName 'DoorayCheckin'
            $taskData['state'] = [string]$task.State
            $taskData['lastRun'] = $info.LastRunTime.ToString('o')
            $taskData['lastResult'] = $info.LastTaskResult
            $taskData['nextRun'] = $info.NextRunTime.ToString('o')
            $taskData['missedRuns'] = $info.NumberOfMissedRuns
            $taskData['actions'] = @($task.Actions | ForEach-Object { @{ executable = Split-Path -Leaf $_.Execute; watcher = [bool]($_.Arguments -match '--watch') } })
            $taskData['settings'] = Pick $task.Settings @('Enabled','StartWhenAvailable','WakeToRun','ExecutionTimeLimit','MultipleInstances','RestartCount','RestartInterval','DisallowStartIfOnBatteries','StopIfGoingOnBatteries')
            $taskData['triggers'] = @($task.Triggers | ForEach-Object {
                @{ type=$_.CimClass.CimClassName; start=$_.StartBoundary; enabled=$_.Enabled;
                   interval=$_.Repetition.Interval; duration=$_.Repetition.Duration }
            })
        }
        Save-Json 'windows-task.json' $taskData
    } catch { $warnings.Add('Windows 예약 조회 실패: ' + $_.Exception.Message) }
    $since = (Get-Date).AddDays(-2)
    try {
        $events = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since; Id=@(1,12,13,42,107)} -MaxEvents 500 -ErrorAction Stop |
            Where-Object ProviderName -In @('Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter','Microsoft-Windows-Kernel-General') |
            ForEach-Object { @{ at=$_.TimeCreated.ToString('o'); provider=$_.ProviderName; eventId=$_.Id } })
        Save-Json 'power-events.json' $events
    } catch { $warnings.Add('최근 절전·부팅 이벤트를 읽지 못했거나 해당 이벤트가 없습니다.') }
    try {
        $events = @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'; StartTime=$since} -MaxEvents 3000 -ErrorAction Stop |
            ForEach-Object {
                $event = $_
                $xml = [xml]$event.ToXml()
                $taskName = @($xml.Event.EventData.Data | Where-Object Name -EQ 'TaskName' | ForEach-Object { $_.'#text' })
                if ($taskName -contains '\DoorayCheckin') {
                    @{ at=$event.TimeCreated.ToString('o'); eventId=$event.Id; result=@($xml.Event.EventData.Data | Where-Object Name -EQ 'ResultCode' | ForEach-Object { $_.'#text' }) }
                }
            })
        Save-Json 'task-events.json' $events
    } catch { $warnings.Add('Windows 작업 기록이 꺼져 있거나 읽을 수 없습니다. 나머지 로그로 분석합니다.') }
}
Save-Json 'environment.json' $environment
Save-Json 'collection-warnings.json' @($warnings.ToArray())
@'
예약 실패가 발생한 PC에서 수집한 진단 자료입니다.
포함: 예약 시간/ON-OFF/날짜 예외, 최근 실행 로그, 결과 상태, Windows 예약 상태 및 최근 절전/부팅 이벤트.
제외: 비밀번호 저장소, 로그인 프로필/쿠키, 스크린샷, 원본 config.json, 계정 ID/회사 URL, 달력의 전체 휴가 내역과 메모.
앱 설정·예약을 변경하지 않고 출퇴근 버튼도 누르지 않습니다. 자동 전송하지 않습니다.
이 ZIP과 함께 실패 날짜, 기대한 출근/퇴근 시각, 당시 PC가 켜져 있었는지/절전 상태였는지 알려주세요.
관리자 권한이 없어 일부 Windows 기록을 읽지 못해도 앱 로그는 수집됩니다.
'@ | Set-Content -LiteralPath (Join-Path $folder '읽어보기.txt') -Encoding UTF8
$zip = $folder + '.zip'
Compress-Archive -Path (Join-Path $folder '*') -DestinationPath $zip
Write-Host "`n완료: $zip" -ForegroundColor Green
Write-Host '이 ZIP을 분석 담당자에게 보내주세요. 내용 확인용 폴더도 같은 위치에 남겨 두었습니다.'
Write-Output $zip
