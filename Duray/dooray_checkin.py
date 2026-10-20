# -*- coding: utf-8 -*-
"""두레이(Dooray!) 출근 체크 자동화.

사용법
------
  python dooray_checkin.py --setup      최초 설정 (URL / ID / 비밀번호 저장)
  python dooray_checkin.py --inspect    로그인 후 화면의 버튼 목록을 덤프 (셀렉터 확인용)
  python dooray_checkin.py --dry-run    클릭 직전까지만 수행 (안전 테스트)
  python dooray_checkin.py              실제 출근 체크
  python dooray_checkin.py --force      주말/공휴일/중복 가드를 무시하고 실행
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import copy
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
import attendance_rules as rules
import dooray_calendar

# ----------------------------------------------------------------------------
# 경로
# ----------------------------------------------------------------------------
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent          # exe 가 놓인 폴더
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))     # exe 안에 포함된 리소스
else:
    APP_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = APP_DIR

DATA_DIR = Path(os.environ.get("DOORAY_DATA_DIR") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DoorayCheckin"))
LOG_DIR = DATA_DIR / "logs"
SHOT_DIR = DATA_DIR / "screenshots"
PROFILE_DIR = DATA_DIR / "profile"
STATE_PATH = DATA_DIR / "state.json"

# exe 는 Program Files 같은 쓰기 불가 위치에 있을 수 있으므로 설정을 데이터 폴더에 둔다.
CONFIG_PATH = Path(os.environ["DOORAY_CONFIG_PATH"]) if os.environ.get("DOORAY_CONFIG_PATH") else ((DATA_DIR / "config.json") if FROZEN else (APP_DIR / "config.json"))
CALENDAR_PATH = DATA_DIR / "calendar.json"

# Playwright 의 node 드라이버는 자기 패키지가 node_modules 밖에 있으면 브라우저를
# 패키지 옆 .local-browsers 에서 찾는다. exe 로 묶으면 그 경로가 매번 바뀌는 임시
# 폴더가 되어버리므로, 실제 설치 위치를 항상 명시해준다.
BROWSERS_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
if os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") in ("", "0", "1"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)

KEYRING_SERVICE = "dooray-checkin"


def chromium_ready() -> bool:
    """Chromium 바이너리가 설치돼 있는가. (exe 에는 포함되지 않는다)"""
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or BROWSERS_DIR)
    try:
        from playwright._impl._driver import compute_driver_executable
        _, cli = compute_driver_executable()
        registry = json.loads((Path(cli).parent / "browsers.json").read_text(encoding="utf-8"))
        revision = next(b["revision"] for b in registry["browsers"] if b["name"] == "chromium")
        folder = base / ("chromium-" + revision)
        return any((folder / part / "chrome.exe").is_file() for part in ("chrome-win64", "chrome-win"))
    except (ImportError, OSError, ValueError, StopIteration):
        return False

log = logging.getLogger("dooray")

# --inspect 결과 출력 경로. GUI 는 이걸 로그 창으로 바꿔 끼운다.
DUMP_SINK = print
# --inspect 후 Enter 를 기다릴지. GUI 에서는 False.
INSPECT_PAUSE = True


# ----------------------------------------------------------------------------
# 셀렉터 후보 (config.json 의 selectors 값이 있으면 그게 우선)
# ----------------------------------------------------------------------------
LOGIN_ID_CANDIDATES = [
    "#loginId",
    "input[name='loginId']",
    "input[name='userId']",
    "input[name='username']",
    "input[name='email']",
    "input[name='id']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[type='text']",
]

LOGIN_PW_CANDIDATES = [
    "#password",
    "input[name='password']",
    "input[type='password']",
]

LOGIN_SUBMIT_CANDIDATES = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('로그인')",
    "a:has-text('로그인')",
]

# 출근 버튼 후보. 순서가 곧 우선순위다.
CHECKIN_CANDIDATES = [
    "button:has-text('출근하기')",
    "a:has-text('출근하기')",
    "[role='button']:has-text('출근하기')",
    "button:has-text('업무 시작')",
    "button:has-text('출근 등록')",
    "button:has-text('출근')",
    "a:has-text('출근')",
    "[role='button']:has-text('출근')",
    "button:has-text('Check in')",
    "button:has-text('Check In')",
]

CHECKOUT_CANDIDATES = [
    "button:has-text('퇴근하기')", "a:has-text('퇴근하기')",
    "[role='button']:has-text('퇴근하기')", "button:has-text('업무 종료')",
    "button:has-text('퇴근 등록')", "button:has-text('퇴근')",
    "a:has-text('퇴근')", "[role='button']:has-text('퇴근')",
    "button:has-text('Check out')", "button:has-text('Check Out')",
]

# 출근 여부는 '출근 칸에 시각이 찍혔는지' 로 판단한다.
#
# 예전에는 '퇴근하기' 버튼이 보이면 출근한 것으로 봤는데, 실제 두레이 근태 화면은
# 출근 전에도 퇴근하기 버튼이 항상 표시된다. 그래서 출근 버튼을 한 번도 누르지
# 못하고 '이미 출근 체크됨' 으로 끝나버렸다. 기본값을 비워 두고, 화면 구조에
# 맞는 셀렉터를 아는 경우에만 config 의 selectors.already_checked_in 으로 덮어쓴다.
ALREADY_CANDIDATES: list[str] = []

# 09:48 / 09:48:24 같은 시각 표기
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")

# 버튼에서 위로 올라가며 각 조상의 텍스트를 모은다 (출근 칸을 찾기 위해)
ANCESTOR_TEXT_JS = """
(el) => {
  const out = [];
  let cur = el;
  for (let i = 0; i < 5; i++) {
    cur = cur.parentElement;
    if (!cur) break;
    out.push((cur.innerText || '').replace(/\\s+/g, ' ').trim());
  }
  return out;
}
"""

# 로그인 실패 / 추가 인증 감지용
BLOCKER_TEXTS = ["인증번호", "2단계", "OTP", "새로운 기기", "비밀번호가 일치", "아이디 또는 비밀번호"]

# 클릭 대상 텍스트에 이게 들어 있으면 절대 누르지 않는다 (오클릭 방지)
FORBIDDEN_IN_LABEL = ["퇴근"]


# ----------------------------------------------------------------------------
# 로깅
# ----------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(LOG_DIR / "checkin.log", maxBytes=1_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # --windowed 로 빌드한 exe 에서는 sys.stdout 이 None 이다.
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def notify_failure(message: str) -> None:
    """실패 시 자동 소멸하는 메시지 박스를 띄운다 (30초 후 자동 닫힘)."""
    try:
        MB_ICONWARNING = 0x30
        MB_SETFOREGROUND = 0x10000
        ctypes.windll.user32.MessageBoxTimeoutW(
            None, message, "두레이 출퇴근 처리 실패",
            MB_ICONWARNING | MB_SETFOREGROUND, 0, 30_000,
        )
    except Exception:  # 알림 실패가 본 작업을 망치면 안 된다
        log.debug("알림 표시 실패", exc_info=True)


# ----------------------------------------------------------------------------
# 설정 / 자격 증명 / 상태
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "tenant_url": "https://YOURCOMPANY.dooray.com",
    "attendance_path": "/home",
    "login_id": "",
    "checkin_time": "09:00",
    # "fixed"  = checkin_time 그대로
    # "random" = checkin_time 기준 앞뒤 random_window_minutes 안에서 랜덤
    "time_mode": "fixed",
    "random_window_minutes": 10,
    "skip_weekends": True,
    "skip_korean_holidays": True,
    "skip_dates": [],
    "headless": True,
    "timeout_ms": 30000,
    "retry_count": 3,
    "retry_delay_seconds": 60,
    "notify_on_failure": True,
    "selectors": {
        "login_id": None,
        "login_password": None,
        "login_submit": None,
        "checkin_button": None,
        "already_checked_in": None,
    },
}


DEFAULT_CONFIG = rules.normalize_config(DEFAULT_CONFIG)
DEFAULT_CONFIG["selectors"].update({"checkout_button": None, "already_checked_out": None})


def ensure_config() -> None:
    """설정 파일이 없으면 만들어 둔다 (exe 첫 실행 시 번들된 템플릿을 복사)."""
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    template = BUNDLE_DIR / "config.json"
    if template.exists() and template.resolve() != CONFIG_PATH.resolve():
        # utf-8-sig: BOM 이 붙어 있으면 떼고 읽는다. PowerShell 로 만든 템플릿에는
        # BOM 이 붙는데, 그대로 두면 json.loads 가 첫 글자에서 실패한다.
        CONFIG_PATH.write_text(template.read_text(encoding="utf-8-sig"), encoding="utf-8")
    else:
        save_config(dict(DEFAULT_CONFIG))


def load_config(validate: bool = True) -> dict:
    """설정을 읽는다. validate=False 면 미완성 설정이어도 그대로 돌려준다 (GUI용)."""
    ensure_config()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    merged = {**copy.deepcopy(DEFAULT_CONFIG), **rules.normalize_config(cfg)}
    merged["selectors"] = {**DEFAULT_CONFIG["selectors"], **(cfg.get("selectors") or {})}
    if validate and "YOURCOMPANY" in merged["tenant_url"]:
        raise SystemExit("설정의 두레이 주소가 아직 예시값입니다. 실제 주소로 바꿔주세요.")
    if validate:
        rules.validate_config(merged, require_account=True)
    return merged


def save_config(cfg: dict) -> None:
    atomic_json(CONFIG_PATH, cfg)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def execution_lock(name: str = "execution"):
    """An OS lock serializes GUI, scheduled runs and profile access."""
    import msvcrt
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / (name + ".lock")).open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("다른 출퇴근 작업이 실행 중입니다. 잠시 후 다시 시도하세요.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def get_password(login_id: str) -> str:
    import keyring
    pw = keyring.get_password(KEYRING_SERVICE, login_id)
    if not pw:
        # SystemExit 이 아니라 RuntimeError 여야 재시도/알림 경로를 정상적으로 탄다.
        raise RuntimeError(f"저장된 비밀번호가 없습니다 ({login_id}). "
                           f"설정 탭에서 비밀번호를 입력하고 저장하세요.")
    return pw


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    atomic_json(STATE_PATH, state)


def load_calendar(cfg: dict | None = None) -> dict:
    try:
        cache = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
        if cfg and cache.get("identity") != rules.cache_identity(cfg):
            return {}
        return cache
    except (OSError, ValueError):
        return {}


# ----------------------------------------------------------------------------
# 오늘 실행해야 하는가
# ----------------------------------------------------------------------------
def should_run_today(cfg: dict, today: dt.date, action: str = "checkin") -> tuple[bool, str]:
    plan = rules.plan_action(cfg, today, action, load_calendar(cfg))
    if not plan.enabled:
        return False, plan.reason
    if load_state().get("last_" + action + "_date") == today.isoformat():
        return False, "이미 처리한 날짜입니다."
    return True, ""


# ----------------------------------------------------------------------------
# Playwright 헬퍼
# ----------------------------------------------------------------------------
def candidates(cfg: dict, key: str, defaults: list[str]) -> list[str]:
    """config 에 셀렉터가 지정돼 있으면 그것만, 없으면 기본 후보 전체를 쓴다."""
    override = (cfg.get("selectors") or {}).get(key)
    if override:
        return [override] if isinstance(override, str) else list(override)
    return defaults


def find_first(page, selectors: list[str]):
    """모든 프레임을 뒤져 화면에 보이는 첫 요소를 찾는다. (locator, selector) 반환."""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc, sel
            except Exception:
                continue
    return None, None


def wait_for_any(page, selectors: list[str], timeout_ms: int):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        loc, sel = find_first(page, selectors)
        if loc is not None:
            return loc, sel
        if time.monotonic() >= deadline:
            return None, None
        page.wait_for_timeout(500)


def page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.inner_text("body"))
        except Exception:
            continue
    return "\n".join(chunks)


def slot_text(btn, action: str = "checkin") -> str:
    """출근 버튼이 들어 있는 '칸' 의 텍스트를 돌려준다.

    두레이 근태 카드는 [출근 / 값 / 출근하기] 가 한 칸에 묶여 있다. 버튼에서
    부모로 올라가며 '출근' 라벨과 값('-' 또는 시각) 이 함께 있는 가장 가까운
    조상을 고른다. 클래스 이름에 의존하지 않으므로 UI 개편에 비교적 강하다.
    """
    try:
        texts = btn.evaluate(ANCESTOR_TEXT_JS)
    except Exception:
        log.debug("조상 텍스트 조회 실패", exc_info=True)
        return ""
    label = rules.LABELS[action]
    opposite = "퇴근" if action == "checkin" else "출근"
    for t in texts:
        if label in t and opposite not in t and ("-" in t or TIME_RE.search(t)):
            return t
    return ""


def has_checkin_time(slot: str) -> bool:
    """출근 칸에 시각이 찍혀 있는가. (아직이면 '-' 만 있다)"""
    return bool(TIME_RE.search(slot))


def screenshot(page, tag: str) -> Path | None:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        log.debug("스크린샷 실패", exc_info=True)
        return None


# ----------------------------------------------------------------------------
# 로그인
# ----------------------------------------------------------------------------
def ensure_logged_in(page, cfg: dict) -> None:
    """이미 세션이 살아 있으면 아무것도 하지 않고, 아니면 ID/PW 로 로그인한다."""
    timeout = cfg["timeout_ms"]

    pw_loc, _ = find_first(page, candidates(cfg, "login_password", LOGIN_PW_CANDIDATES))
    if pw_loc is None:
        log.info("기존 세션이 유효합니다. 로그인 생략.")
        return

    log.info("로그인 화면 감지. ID/비밀번호로 로그인합니다.")
    id_loc, id_sel = find_first(page, candidates(cfg, "login_id", LOGIN_ID_CANDIDATES))
    if id_loc is None:
        screenshot(page, "no-id-field")
        raise RuntimeError("로그인 ID 입력란을 찾지 못했습니다. --inspect 로 셀렉터를 확인하세요.")

    password = get_password(cfg["login_id"])
    id_loc.fill(cfg["login_id"])
    pw_loc.fill(password)
    log.debug("ID 입력란 셀렉터: %s", id_sel)

    submit, _ = find_first(page, candidates(cfg, "login_submit", LOGIN_SUBMIT_CANDIDATES))
    if submit is not None:
        submit.click()
    else:
        pw_loc.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        page.wait_for_timeout(3000)

    text = page_text(page)
    for marker in BLOCKER_TEXTS:
        if marker in text:
            shot = screenshot(page, "login-blocked")
            raise RuntimeError(
                f"로그인이 완료되지 않았습니다. 화면에서 '{marker}' 문구가 감지됐습니다. "
                f"(추가 인증이 필요하거나 비밀번호가 틀렸을 수 있습니다) 스크린샷: {shot}"
            )

    still_pw, _ = find_first(page, candidates(cfg, "login_password", LOGIN_PW_CANDIDATES))
    if still_pw is not None:
        shot = screenshot(page, "login-failed")
        raise RuntimeError(f"로그인 후에도 비밀번호 입력란이 남아 있습니다. 스크린샷: {shot}")

    log.info("로그인 성공.")


# ----------------------------------------------------------------------------
# --inspect : 셀렉터 조사
# ----------------------------------------------------------------------------
def dump_clickables(page) -> None:
    js = """
    () => Array.from(document.querySelectorAll("button, a, [role='button'], input[type='submit']"))
      .map(el => {
        const r = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.value || "").trim().slice(0, 60),
          id: el.id || "",
          cls: (typeof el.className === "string" ? el.className : "").slice(0, 80),
          testid: el.getAttribute("data-testid") || el.getAttribute("data-test") || "",
          aria: el.getAttribute("aria-label") || "",
          visible: r.width > 0 && r.height > 0
        };
      })
      .filter(e => e.visible && (e.text || e.aria));
    """
    _dump(page, js, "화면에서 발견된 클릭 가능 요소")


def dump_inputs(page) -> None:
    """로그인 폼의 입력란을 덤프한다. 비밀번호를 입력하지 않아도 동작한다."""
    js = """
    () => Array.from(document.querySelectorAll("input, button, [role='button']"))
      .map(el => {
        const r = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase() + (el.type ? ":" + el.type : ""),
          text: (el.innerText || el.placeholder || el.value || "").trim().slice(0, 60),
          id: el.id || "",
          cls: (typeof el.className === "string" ? el.className : "").slice(0, 80),
          testid: el.getAttribute("data-testid") || el.getAttribute("name") || "",
          aria: el.getAttribute("aria-label") || "",
          visible: r.width > 0 && r.height > 0
        };
      })
      .filter(e => e.visible);
    """
    _dump(page, js, "로그인 화면의 입력란 / 버튼")


def _dump(page, js: str, heading: str) -> None:
    out = DUMP_SINK
    out("=" * 70)
    out(heading)
    out("=" * 70)
    for i, frame in enumerate(page.frames):
        try:
            items = frame.evaluate(js)
        except Exception:
            continue
        if not items:
            continue
        out(f"--- frame[{i}] {frame.url[:100]} ---")
        for it in items:
            parts = [f"<{it['tag']}>", repr(it["text"])]
            if it["id"]:
                parts.append(f"id=#{it['id']}")
            if it["testid"]:
                parts.append(f"attr={it['testid']}")
            if it["aria"]:
                parts.append(f"aria-label={it['aria']!r}")
            if it["cls"]:
                parts.append(f"class={it['cls']!r}")
            out("  " + "  ".join(parts))
    out("=" * 70)


# ----------------------------------------------------------------------------
# 메인 동작
# ----------------------------------------------------------------------------
def run_once(cfg: dict, **kwargs):
    from attendance_runtime import run_once as run
    return run(cfg, **kwargs)


def do_run(cfg: dict, args) -> int:
    from attendance_runtime import do_run as run
    return run(cfg, args)


# ----------------------------------------------------------------------------
# --setup
# ----------------------------------------------------------------------------
def do_setup() -> int:
    import getpass
    import keyring

    print("=" * 60)
    print(" 두레이 출근 체크 - 최초 설정")
    print("=" * 60)

    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            cfg = {**cfg, **existing}
            cfg["selectors"] = {**DEFAULT_CONFIG["selectors"], **(existing.get("selectors") or {})}
        except Exception:
            print("기존 config.json 을 읽지 못해 기본값으로 시작합니다.")

    def ask(prompt: str, current: str) -> str:
        shown = f" [{current}]" if current and "YOURCOMPANY" not in current else ""
        val = input(f"{prompt}{shown}: ").strip()
        return val or current

    cfg["tenant_url"] = ask("두레이 주소 (예: https://mycompany.dooray.com)",
                            cfg["tenant_url"]).rstrip("/")
    cfg["attendance_path"] = ask("근태 페이지 경로", cfg["attendance_path"]) or "/home"
    cfg["login_id"] = ask("로그인 ID (이메일)", cfg["login_id"])
    cfg["checkin_time"] = ask("출근 체크 시각 (HH:MM)", cfg["checkin_time"])

    if not cfg["login_id"]:
        print("로그인 ID는 필수입니다.")
        return 1

    pw = getpass.getpass("비밀번호 (입력 내용은 화면에 표시되지 않습니다): ")
    if pw:
        keyring.set_password(KEYRING_SERVICE, cfg["login_id"], pw)
        print("[OK] 비밀번호를 Windows 자격 증명 관리자에 저장했습니다.")
    elif keyring.get_password(KEYRING_SERVICE, cfg["login_id"]):
        print("[OK] 기존에 저장된 비밀번호를 그대로 사용합니다.")
    else:
        print("비밀번호가 없으면 자동 로그인을 할 수 없습니다.")
        return 1

    save_config(cfg)
    print(f"[OK] 설정을 저장했습니다: {CONFIG_PATH}")
    print("\n다음 단계:")
    print("  1) python dooray_checkin.py --inspect   (버튼 셀렉터 확인)")
    print("  2) python dooray_checkin.py --dry-run   (클릭 없이 동작 확인)")
    print("  3) .\\install_task.ps1                   (작업 스케줄러 등록)")
    return 0


def do_set_password() -> int:
    """config.json 의 login_id 에 대한 비밀번호만 저장한다."""
    import getpass
    import keyring

    cfg = load_config()
    login_id = cfg.get("login_id", "")
    if not login_id:
        print("config.json 에 login_id 가 비어 있습니다.")
        return 1

    print(f"계정: {login_id}")
    pw = getpass.getpass("두레이 비밀번호 (화면에 표시되지 않습니다): ")
    if not pw:
        print("입력이 비어 있어 취소했습니다.")
        return 1

    keyring.set_password(KEYRING_SERVICE, login_id, pw)
    print("[OK] Windows 자격 증명 관리자에 저장했습니다.")
    print("     (제어판 > 자격 증명 관리자 > Windows 자격 증명 > dooray-checkin)")
    return 0


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="두레이 출근 체크 자동화")
    ap.add_argument("--action", choices=rules.ACTIONS, default="checkin", help="출근 또는 퇴근")
    ap.add_argument("--setup", action="store_true", help="최초 설정 (URL/ID/비밀번호)")
    ap.add_argument("--set-password", action="store_true",
                    help="비밀번호만 다시 저장 (config 는 건드리지 않음)")
    ap.add_argument("--inspect-login", action="store_true",
                    help="로그인 '전' 화면의 입력란 덤프 (비밀번호 불필요)")
    ap.add_argument("--inspect", action="store_true", help="로그인 후 버튼 목록 덤프 (셀렉터 확인)")
    ap.add_argument("--dry-run", action="store_true", help="클릭 직전까지만 수행")
    ap.add_argument("--headed", action="store_true", help="브라우저 창을 보이게 실행")
    ap.add_argument("--force", action="store_true", help="날짜별 OFF/휴가/휴일 무시 (중복 방지는 유지)")
    ap.add_argument("-v", "--verbose", action="store_true", help="상세 로그")
    args = ap.parse_args()

    setup_logging(args.verbose)

    if args.setup:
        return do_setup()

    if args.set_password:
        return do_set_password()

    cfg = load_config()
    return do_run(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
