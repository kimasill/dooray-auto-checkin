# -*- coding: utf-8 -*-
"""Windows 작업 스케줄러 제어 (자동 출근 on/off).

GUI 의 토글 스위치가 여기를 호출한다. exe 로 빌드된 경우 exe 자신을,
개발 중이면 pythonw.exe + 스크립트를 작업으로 등록한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TASK_NAME = "DoorayCheckin"

CREATE_NO_WINDOW = 0x08000000


def watcher_running() -> bool:
    """Probe the OS lock, not stale metadata or Task Scheduler's registration."""
    import msvcrt
    import dooray_checkin as core
    path = core.DATA_DIR / 'watcher.lock'
    if not path.exists():
        return False
    try:
        with path.open('r+b') as handle:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (13, 36):
                    return True
                raise
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except FileNotFoundError:
        return False


# ----------------------------------------------------------------------------
def _ps_quote(value: str) -> str:
    """PowerShell 작은따옴표 문자열로 안전하게 감싼다."""
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str, timeout: int = 60) -> tuple[int, str, str]:
    """임시 .ps1 파일로 실행한다. 인자 따옴표 문제를 피하기 위함.

    한글 경로/메시지가 섞여도 깨지지 않도록 양쪽 모두 UTF-8 로 고정한다.
    (기본값으로 두면 Python 이 cp949 로 디코딩하다 UnicodeDecodeError 가 난다.)
    """
    header = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    tmp = Path(tempfile.gettempdir()) / f"dooray_ps_{os.getpid()}.ps1"
    tmp.write_text(header + script, encoding="utf-8-sig")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------------
def task_command() -> tuple[str, str]:
    """작업 스케줄러에 등록할 (실행파일, 인자) 를 돌려준다."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--watch"

    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"
    exe = str(pythonw if pythonw.exists() else Path(sys.executable))
    script = Path(__file__).resolve().parent / "dooray_gui.py"
    return exe, f'"{script}" --watch'


def is_enabled() -> bool:
    rc, out, _ = _run_powershell(
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue\n"
        f"if ($null -eq $t) {{ 'NONE' }} else {{ $t.State }}\n"
    )
    if rc != 0:
        return False
    return out.strip() not in ("NONE", "Disabled", "")


def status() -> dict:
    """등록 상태 / 다음 실행 시각 / 마지막 결과를 조회한다."""
    script = (
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue\n"
        "if ($null -eq $t) { ConvertTo-Json @{ exists = $false }; exit 0 }\n"
        f"$i = Get-ScheduledTaskInfo -TaskName {_ps_quote(TASK_NAME)}\n"
        "ConvertTo-Json @{\n"
        "  exists = $true\n"
        "  state = [string]$t.State\n"
        "  next = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }\n"
        "  last = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }\n"
        "  lastResult = $i.LastTaskResult\n"
        "  watcher = [bool]($t.Actions.Arguments -match '--watch')\n"
        "}\n"
    )
    rc, out, err = _run_powershell(script)
    if rc != 0 or not out:
        return {"exists": False, "error": err or f"rc={rc}"}
    try:
        result = json.loads(out)
        result['worker_running'] = watcher_running()
        return result
    except json.JSONDecodeError:
        return {"exists": False, "error": out[:200]}


def enable(cfg: dict | None = None) -> None:
    """Start one resident scheduler; daily retries recover after process failure.

    It reloads settings every two seconds and opens Chromium only when needed.
    Calendar exceptions and half-days therefore need no Windows task per date.
    """
    exe, arg = task_command()
    workdir = str(Path(exe).parent if getattr(sys, "frozen", False)
                  else Path(__file__).resolve().parent)
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(arg)} -WorkingDirectory {_ps_quote(workdir)}
$daily = New-ScheduledTaskTrigger -Daily -At '00:00'
$repeat = New-ScheduledTaskTrigger -Once -At '00:00' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)
$daily.Repetition = $repeat.Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn -User {_ps_quote(user)}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId {_ps_quote(user)} -LogonType Interactive -RunLevel Limited
$old = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue
if ($old) {{ Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} }}
Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action -Trigger @($daily,$logon) -Settings $settings -Principal $principal -Description '두레이 달력 기반 자동 출퇴근 2.1' -Force | Out-Null
Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}
'OK'
"""
    rc, out, err = _run_powershell(script)
    if rc != 0 or "OK" not in out:
        raise RuntimeError(f"예약 등록 실패: {err or out or f'rc={rc}'}")
    deadline = time.monotonic() + 25
    while not watcher_running():
        if time.monotonic() >= deadline:
            raise RuntimeError("Windows 예약은 등록됐지만 감시 프로그램이 시작되지 않았습니다. 실행 기록을 확인하고 예약 다시 시작을 누르세요.")
        time.sleep(.25)
    import dooray_checkin as core
    core.log.info("자동 예약 시작 확인: Windows 등록 완료 / 감시 프로그램 실행 중")


def disable() -> None:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue\n"
        "if ($null -ne $t) {\n"
        f"  Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}\n"
        f"  Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false\n"
        "}\n'OK'\n"
    )
    rc, out, err = _run_powershell(script)
    if rc != 0 or "OK" not in out:
        raise RuntimeError(f"예약 해제 실패: {err or out or f'rc={rc}'}")
