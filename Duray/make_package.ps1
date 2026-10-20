<#
  배포용 패키지를 만든다.

    .\make_package.ps1              기본 위치(바탕화면\두레이자동출근_배포)에 생성
    .\make_package.ps1 -Out "E:\"   USB 등 원하는 위치에 생성

  개인 정보(login_id)를 비운 exe 를 새로 빌드하고, 설치 스크립트와 안내문을
  한 폴더에 모은다. 받는 사람은 '설치.bat' 만 더블클릭하면 된다.
#>
param(
    [string]$Out = "",
    [switch]$SkipBuild,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pkgSrc = Join-Path $dir "package"

if ([string]::IsNullOrWhiteSpace($Out)) {
    $Out = Join-Path ([Environment]::GetFolderPath("Desktop")) "두레이자동출근_배포"
}

# 빌드 결과는 배포 폴더와 섞이지 않도록 별도 임시 위치에 받는다
$buildOut = Join-Path $dir ".build\dist"

if (-not $SkipBuild) {
    Write-Host "배포용 exe 를 빌드합니다 (login_id 제거)..." -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $dir "build_exe.ps1") `
        -Clean -DistPath $buildOut -Python $Python
    if ($LASTEXITCODE -ne 0) { throw "빌드 실패" }
}

$exe = Join-Path $buildOut "DoorayAutoCheckin.exe"
if (-not (Test-Path $exe)) { throw "빌드된 exe 를 찾을 수 없습니다: $exe" }

# --- 패키지 폴더 구성 -------------------------------------------------------
# Update only the four distribution files; never recursively delete a caller's folder.
New-Item -ItemType Directory -Force -Path $Out | Out-Null

Copy-Item $exe (Join-Path $Out "DoorayAutoCheckin.exe") -Force
Copy-Item (Join-Path $pkgSrc "install.ps1")   $Out -Force
Copy-Item (Join-Path $pkgSrc "설치.bat")      $Out -Force
Copy-Item (Join-Path $pkgSrc "읽어보기.txt")  $Out -Force

# 배포 파일에는 차단 표시가 붙어 있으면 안 된다
Get-ChildItem $Out -File | Unblock-File -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[OK] 배포 패키지 생성 완료" -ForegroundColor Green
Write-Host "  위치: $Out"
Write-Host ""
Get-ChildItem $Out | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,2)}} |
    Format-Table -AutoSize
$total = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "  합계: $([math]::Round($total/1MB,1)) MB"
Write-Host ""
Write-Host "이 폴더를 통째로 USB 에 복사해서 전달하세요." -ForegroundColor Cyan
Write-Host "받는 사람은 '설치.bat' 만 더블클릭하면 됩니다."
