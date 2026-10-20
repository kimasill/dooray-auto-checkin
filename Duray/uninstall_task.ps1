<# 등록된 두레이 출근 체크 작업을 제거합니다. #>
param([string]$TaskName = "DoorayCheckin")

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "등록된 작업이 없습니다: $TaskName" -ForegroundColor Yellow
    return
}

Stop-ScheduledTask -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "[OK] 작업을 제거했습니다: $TaskName" -ForegroundColor Green
Write-Host "저장된 비밀번호까지 지우려면 '자격 증명 관리자 > Windows 자격 증명' 에서" -ForegroundColor Cyan
Write-Host "dooray-checkin 항목을 삭제하세요."
