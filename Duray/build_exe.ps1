<#
  두레이 자동 출근 GUI 를 단일 exe 로 빌드합니다.

    .\build_exe.ps1            단일 파일 exe (dist\DoorayAutoCheckin.exe)
    .\build_exe.ps1 -OneDir    폴더 형태 (실행이 훨씬 빠름)

  빌드 중간 산출물은 OneDrive 동기화를 피해 임시 폴더에 만듭니다.
#>
param(
    [switch]$OneDir,
    [switch]$Clean,          # 배포용: 번들 설정에서 개인 정보(login_id)를 비운다
    [string]$Name = "DoorayAutoCheckin",
    [string]$DistPath = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$work = Join-Path $dir ".build"

# exe 는 OneDrive 밖에 두어야 한다.
# OneDrive '파일 온디맨드' 가 큰 exe 를 클라우드 전용(ReparsePoint)으로 바꿔버리면
# 실행할 때마다 다시 내려받느라 멈춰서, 예약 실행이 통째로 실패한다.
if ([string]::IsNullOrWhiteSpace($DistPath)) {
    $dist = Join-Path $env:LOCALAPPDATA "DoorayCheckin\bin"
} else {
    $dist = $DistPath
}

New-Item -ItemType Directory -Force -Path $work | Out-Null
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$mode = "--onefile"
if ($OneDir) { $mode = "--onedir" }

# 번들에 넣을 설정 템플릿. -Clean 이면 개인 정보를 비운 사본을 임시로 만든다.
$configForBundle = Join-Path $dir "config.json"
if ($Clean) {
    # Always start from defaults: no personal dates, selectors or account in a distributable.
    $cfg = (& $Python -c "import sys,json; sys.path.insert(0,sys.argv[1]); import dooray_checkin as c; print(json.dumps(c.DEFAULT_CONFIG))" $dir) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "기본 설정 생성 실패" }
    # ConvertFrom-Json 이 만든 객체에는 원본에 없던 키가 아예 없다.
    # 있으면 덮어쓰고 없으면 추가하도록 -Force 로 처리한다.
    function Set-Field($obj, $name, $value) {
        $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
    }
    Set-Field $cfg "login_id"     ""
    Set-Field $cfg "checkin_time" "09:00"
    Set-Field $cfg "time_mode"    "fixed"
    Set-Field $cfg "skip_dates"   @()

    # BOM 없는 UTF-8 로 써야 한다. Set-Content -Encoding UTF8 은 BOM 을 붙이는데,
    # 그러면 파이썬이 이 파일을 json 으로 읽을 때 첫 글자에서 실패한다.
    $configForBundle = Join-Path $work "config.json"
    $json = $cfg | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($configForBundle, $json,
                                   (New-Object System.Text.UTF8Encoding $false))
    Write-Host "[배포용] 번들 설정에서 login_id 를 비웠습니다 (BOM 없는 UTF-8)." -ForegroundColor Yellow
}

$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    $mode,
    "--windowed",
    "--name", $Name,
    "--version-file", (Join-Path $dir "version_info.txt"),
    "--add-data", "$configForBundle;.",
    "--collect-all", "playwright",
    "--collect-all", "keyring",
    "--collect-all", "holidays",
    "--copy-metadata", "keyring",
    "--hidden-import", "keyring.backends.Windows",
    "--hidden-import", "keyring.backends.null",
    "--exclude-module", "pytest",
    "--workpath", (Join-Path $work "build"),
    "--specpath", $work,
    "--distpath", $dist,
    (Join-Path $dir "dooray_gui.py")
)

Write-Host "빌드를 시작합니다. 수 분 걸립니다..." -ForegroundColor Cyan
& $Python @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "빌드 실패 (exit $LASTEXITCODE)" }

if ($OneDir) {
    $exe = Join-Path $dist "$Name\$Name.exe"
} else {
    $exe = Join-Path $dist "$Name.exe"
}

if (-not (Test-Path $exe)) { throw "exe 를 찾을 수 없습니다: $exe" }

$item = Get-Item $exe
$sizeMb = [math]::Round($item.Length / 1MB, 1)

if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    Write-Warning "이 exe 가 클라우드 전용 파일(ReparsePoint)입니다. OneDrive 밖으로 옮기세요."
}

Write-Host ""
Write-Host "[OK] 빌드 완료" -ForegroundColor Green
Write-Host "  경로 : $exe"
Write-Host "  크기 : $sizeMb MB"
Write-Host ""
Write-Host "실행하면 창이 뜹니다. 작업 스케줄러는 이 exe 를 '--watch' 인자로 호출합니다." -ForegroundColor Cyan
Write-Host ""
Write-Host "바탕화면에 바로가기 만들기:" -ForegroundColor Cyan
Write-Host "  `$s=(New-Object -ComObject WScript.Shell).CreateShortcut(`"`$env:USERPROFILE\Desktop\두레이 자동출근.lnk`"); `$s.TargetPath=`"$exe`"; `$s.Save()"
