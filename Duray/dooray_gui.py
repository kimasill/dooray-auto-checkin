# -*- coding: utf-8 -*-
"""두레이 자동 출근 - GUI 진입점.

  dooray_gui.py          창을 띄운다
  dooray_gui.py --watch  달력 기반 출퇴근 예약 감시
  dooray_gui.py --run    창 없이 출근 체크만 수행 (이전 예약 호환)
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import types
from pathlib import Path

import dooray_checkin as core
import scheduler

CREATE_NO_WINDOW = 0x08000000
APP_TITLE = "두레이 자동 출퇴근"


# ----------------------------------------------------------------------------
# 스케줄러가 호출하는 무창(無窓) 모드
# ----------------------------------------------------------------------------
def run_headless() -> int:
    core.setup_logging()
    try:
        cfg = core.load_config()
    except (SystemExit, ValueError, OSError) as e:
        core.log.error("설정 오류: %s", e)
        return 1
    args = types.SimpleNamespace(dry_run=False, inspect=False, inspect_login=False,
                                 headed=False, force=False, action="checkout" if "--checkout" in sys.argv else "checkin")
    return core.do_run(cfg, args)


# ----------------------------------------------------------------------------
# Chromium 준비 상태
# ----------------------------------------------------------------------------
def browsers_dir() -> Path:
    # core 임포트 시점에 PLAYWRIGHT_BROWSERS_PATH 가 확정된다.
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or core.BROWSERS_DIR)


# 판정 기준을 코어와 하나로 유지한다 (예약 실행과 GUI 가 서로 다르게 보면 안 된다)
chromium_ready = core.chromium_ready


def install_chromium(emit) -> bool:
    """Playwright 내장 드라이버로 Chromium 을 내려받는다 (exe 에서도 동작)."""
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        node, cli = compute_driver_executable()
        cmd = [str(node), str(cli), "install", "chromium"]
        env = get_driver_env()
    except Exception:
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        env = os.environ.copy()

    emit("Chromium 다운로드를 시작합니다. 수 분 걸릴 수 있습니다...")
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              creationflags=CREATE_NO_WINDOW, timeout=900)
    except Exception as e:
        emit(f"설치 실행 실패: {e}")
        return False

    for line in (proc.stdout or "").splitlines()[-15:]:
        emit(line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-15:]:
            emit(line)
        emit(f"설치 실패 (종료 코드 {proc.returncode})")
        return False

    ok = chromium_ready()
    emit("Chromium 설치 완료." if ok else "설치가 끝났지만 Chromium 을 찾지 못했습니다.")
    return ok


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(path)])


# ----------------------------------------------------------------------------
# 로그를 GUI 로 흘려보내는 핸들러
# ----------------------------------------------------------------------------
class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


# ----------------------------------------------------------------------------
def build_app():
    from desktop_ui import build_app as build
    return build()


def install_browser_cli() -> int:
    """설치 스크립트가 호출한다. 창 없이 Chromium 만 내려받는다.

    --windowed 로 빌드해서 콘솔 출력이 없으므로 진행 상황은 로그 파일에 남긴다.
    설치 스크립트는 종료 코드로 성공 여부를 판단한다.
    """
    core.setup_logging()
    if chromium_ready():
        core.log.info("Chromium 이 이미 설치되어 있습니다.")
        return 0
    ok = install_chromium(lambda m: core.log.info("%s", m))
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        # Build verification uses explicit isolated paths and performs no attendance actions.
        if not os.environ.get("DOORAY_DATA_DIR") or not os.environ.get("DOORAY_CONFIG_PATH"):
            return 2
        root = build_app()
        root.withdraw()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        for name in root.app.pages:
            root.app.show(name)
            root.update_idletasks()
        root.after(1500, root.app.close)
        root.mainloop()
        core.log.info("UI self-test: %s", "FAIL" if errors else "OK")
        return 1 if errors else 0
    if "--enable-schedule" in sys.argv or "--migrate-schedule" in sys.argv:
        core.setup_logging()
        try:
            cfg = core.load_config()
            if "--time" in sys.argv:
                cfg["checkin_time"] = sys.argv[sys.argv.index("--time") + 1]
                core.rules.validate_config(cfg, require_account=True)
                core.save_config(cfg)
            scheduler.enable(cfg)
            return 0
        except (Exception, SystemExit) as exc:
            core.log.error("예약 등록 실패: %s", exc)
            return 1
    if "--watch" in sys.argv:
        from attendance_runtime import watch
        return watch()
    if "--run" in sys.argv:
        return run_headless()
    if "--install-browser" in sys.argv:
        return install_browser_cli()
    build_app().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
