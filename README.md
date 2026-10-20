# 두레이 자동 출퇴근

Windows에서 Dooray 출퇴근 예약, 근무 달력, 개인 근태를 관리하는 데스크톱 프로그램입니다. 현재 버전은 **2.1.0**입니다.

## 설치와 업데이트

1. [최신 릴리스](https://github.com/kimasill/dooray-auto-checkin/releases/latest)에서 `DoorayAutoCheckin-v2.1.0-windows.zip`을 받습니다.
2. 압축을 풀고 `설치.bat`을 실행합니다.
3. 프로그램의 **연결 · 설정**에서 회사 두레이 주소, 로그인 ID와 비밀번호를 저장합니다.
4. **시간 · 예약**에서 출근·퇴근 시각을 설정하고, 대시보드에서 **자동 예약 켜기**를 누릅니다.

업데이트는 기존 계정·시간·달력 설정과 예약 ON/OFF 상태를 유지합니다. 이전 실행 파일·설정·예약은 `%LOCALAPPDATA%\DoorayCheckin\backups`에 백업합니다. 비밀번호는 Windows 자격 증명 관리자에 저장되며 배포 패키지에는 개인 계정 설정이 포함되지 않습니다. 설치 시 필요한 Chromium을 내려받으므로 인터넷 연결이 필요합니다.

## 주요 기능

- **근무 달력:** 월간 보기, 공휴일·대체휴일, 날짜별 출퇴근 ON/OFF 및 시간 설정
- **본인 근태 표시:** 이번 달 연차·휴가, 오전 반차, 오후 반차를 색상별로 표시
- **쉬운 시간 선택:** 출근·퇴근·반차·날짜별 시간을 시·분·초 드롭다운으로 선택
- **출퇴근 예약:** 고정 시각 또는 기준 시각 이전/이후 각각 0~720분의 랜덤 범위
- **반차별 퇴근:** 출근 시각은 유지하고 오전·오후 반차의 퇴근 시각을 별도 지정
- **설정 보존:** 이전 버전 설정 호환, 중복 처리 방지, 날짜별 수동 설정 우선 적용

![근무 달력 — 예시 일정](docs/images/calendar.png)

![시간 및 예약 설정 — 예시 설정](docs/images/schedule.png)

## 근태를 예약에 반영하기

저장된 계정으로 앱을 열면 이번 달 본인 근태를 가져옵니다. 가져온 연차·반차는 자동 예약 반영 여부와 관계없이 캘린더에 표시합니다. 날짜를 선택하면 오른쪽에서 근태와 예약 시각을 확인할 수 있습니다.

휴가일 자동 실행 제외와 반차별 퇴근 시각 적용을 사용하려면 달력 아래 **두레이 근태를 자동 예약에도 반영**을 체크한 후 **설정 저장**을 누르세요. 날짜별 수동 설정이 우선하며 전체 출근·퇴근 사용 OFF 상태에서는 실행되지 않습니다.

현재 월의 근태를 조회합니다. 다른 달은 날짜별로 직접 설정할 수 있습니다. 자동 예약이 실행되려면 PC가 켜져 있고 Windows에 로그인되어 있어야 합니다. 앱 창을 닫아도 켜 둔 Windows 예약은 유지됩니다.

## 개발 환경

Python 3.14 / Windows에서 빌드와 검증을 수행했습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r Duray\requirements.txt pyinstaller
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe Duray\dooray_gui.py
```

첫 실행 시 로컬 `Duray/config.json`이 기본값으로 생성됩니다. `Duray/config.example.json`은 계정 정보가 비어 있는 설정 예시입니다. 로컬 설정과 실행 기록은 Git에서 제외됩니다.

```powershell
# 개인정보가 없는 배포 패키지 빌드
.\Duray\make_package.ps1 -Python .\.venv\Scripts\python.exe -Out .\release

# 규칙 및 UI 테스트
.\.venv\Scripts\python.exe -m unittest discover -s Duray\tests -p test_attendance.py -v
.\.venv\Scripts\python.exe -m unittest discover -s Duray\tests -p test_gui.py -v
```

브라우저 테스트는 `DOORAY_TEST_BROWSER`에 설치된 Chromium의 `chrome.exe` 경로를 지정한 후 `test_browser.py`를 실행합니다. 외부 요청은 모의 HTML로 대체합니다. **2.1.0은 규칙 35개, 브라우저 6개, GUI 10개로 총 51개 테스트를 통과했습니다.**

자세한 설정과 예약 동작은 [사용·개발 문서](Duray/README.md), 버전별 변경은 [변경 기록](CHANGELOG.md)을 참고하세요.

Dooray 화면 구성을 참고한 개인 자동화 도구입니다.
