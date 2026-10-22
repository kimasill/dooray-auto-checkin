"""Pure calendar and scheduling rules shared by the UI and background worker."""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import hashlib
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

KST = dt.timezone(dt.timedelta(hours=9))
ATTENDANCE_PATH = "/work-schedule/user/register-month"
ACTIONS = ("checkin", "checkout")
LABELS = {"checkin": "출근", "checkout": "퇴근"}
DAY_LABELS = {"auto": "자동 판정", "work": "근무일", "holiday": "휴무 / 대체휴일",
              "vacation": "종일 휴가", "am_half": "오전 반차", "pm_half": "오후 반차"}
NEW_DEFAULTS = {
    "checkin_enabled": True, "checkout_enabled": False,
    "checkout_time": "18:00", "checkout_time_mode": "fixed",
    "random_before_minutes": 10, "random_after_minutes": 10,
    "random_before_seconds": 0, "random_after_seconds": 0,
    "checkout_random_before_minutes": 0, "checkout_random_after_minutes": 0,
    "checkout_random_before_seconds": 0, "checkout_random_after_seconds": 0,
    "workdays": [0, 1, 2, 3, 4], "date_overrides": {},
    "half_day_policy": "checkout_time", "am_half_checkout_time": "18:00",
    "pm_half_checkout_time": "14:00", "late_grace_minutes": 15,
    "dooray_sync_enabled": False,
    "dooray_calendar_path": ATTENDANCE_PATH,
    "dooray_calendar_selector": "", "sync_interval_minutes": 60,
}


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def normalize_config(raw: dict) -> dict:
    cfg = {**copy.deepcopy(NEW_DEFAULTS), **copy.deepcopy(raw)}
    # The old default is Dooray's portal, which may have no attendance controls.
    # Correct it on read for both the GUI and watcher, without changing custom
    # paths or rewriting an existing config file during a read-only operation.
    if cfg.get("attendance_path", ATTENDANCE_PATH) in ("/home", "/home/"):
        cfg["attendance_path"] = ATTENDANCE_PATH
    if cfg["half_day_policy"] in ("skip_action", "skip_day", "custom_time"):
        cfg["half_day_policy"] = "checkout_time"
    if "am_half_checkout_time" not in raw:
        cfg["am_half_checkout_time"] = cfg.get("checkout_time", "18:00")
    # Preserve the old symmetric random range, including zero.
    for key in ("random_before_minutes", "random_after_minutes"):
        if key not in raw:
            cfg[key] = raw.get("random_window_minutes", 10)
    if "workdays" not in raw and not raw.get("skip_weekends", True):
        cfg["workdays"] = list(range(7))
    return cfg


def seconds(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        raise ValueError("시각은 HH:MM 또는 HH:MM:SS 형식으로 입력하세요.")
    parts = [int(p) for p in value.split(":")]
    h, m, s = (*parts, 0) if len(parts) == 2 else parts
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"유효하지 않은 시각: {value}")
    return h * 3600 + m * 60 + s


def clock_text(value: int) -> str:
    return f"{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}"


def valid_date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value}")
    return dt.date.fromisoformat(value)


def validate_config(cfg: dict, *, require_account: bool = False) -> None:
    parsed = urlparse(cfg.get("tenant_url", ""))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("두레이 주소는 https://로 시작하는 회사 주소를 입력하세요.")
    if parsed.path not in ("", "/"):
        raise ValueError("회사 두레이 주소에는 https://회사.dooray.com까지만 입력하세요. 근태 경로는 아래 출퇴근 페이지 경로에 입력하세요.")
    if require_account and (not cfg.get("login_id", "").strip() or "YOURCOMPANY" in cfg["tenant_url"]):
        raise ValueError("두레이 주소와 로그인 ID를 먼저 저장하세요.")
    for key in ("attendance_path", "dooray_calendar_path"):
        if not cfg.get(key, "").startswith("/") or cfg[key].startswith("//"):
            raise ValueError(f"{key}: 같은 두레이 사이트 내 /경로를 입력하세요.")
    for key in ("checkin_time", "checkout_time", "am_half_checkout_time", "pm_half_checkout_time"):
        seconds(cfg[key])
    for key in ("time_mode", "checkout_time_mode"):
        if cfg.get(key) not in ("fixed", "random"):
            raise ValueError("시간 모드는 fixed 또는 random이어야 합니다.")
    for key, low, high in (("random_before_minutes", 0, 720), ("random_after_minutes", 0, 720),
                          ("checkout_random_before_minutes", 0, 720), ("checkout_random_after_minutes", 0, 720),
                          ("random_before_seconds", 0, 59), ("random_after_seconds", 0, 59),
                          ("checkout_random_before_seconds", 0, 59), ("checkout_random_after_seconds", 0, 59),
                          ("late_grace_minutes", 0, 180), ("sync_interval_minutes", 5, 1440),
                          ("retry_count", 1, 10), ("retry_delay_seconds", 0, 600), ("timeout_ms", 1000, 120000)):
        if type(cfg.get(key)) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key}: {low}~{high} 범위의 정수를 입력하세요.")
    if cfg["half_day_policy"] not in ("checkout_time", "keep_time", "skip_checkout"):
        raise ValueError("반차 처리 방식을 선택하세요.")
    if not isinstance(cfg["workdays"], list) or any(type(d) is not int or not 0 <= d <= 6 for d in cfg["workdays"]):
        raise ValueError("근무 요일 값이 올바르지 않습니다.")
    for value in cfg.get("skip_dates", []):
        valid_date(value)
    if not isinstance(cfg["date_overrides"], dict):
        raise ValueError("날짜별 설정 형식이 잘못되었습니다.")
    for date, item in cfg["date_overrides"].items():
        valid_date(date)
        if not isinstance(item, dict) or item.get("kind", "auto") not in DAY_LABELS:
            raise ValueError(f"{date}: 날짜 유형이 잘못되었습니다.")
        for action in ACTIONS:
            if item.get(action, "auto") not in ("auto", "on", "off"):
                raise ValueError(f"{date}: {LABELS[action]} 설정이 잘못되었습니다.")
            if item.get(action + "_time"):
                seconds(item[action + "_time"])
    # A same-day program must not silently interpret an overnight shift.
    dates = [None] + [valid_date(d) for d in cfg["date_overrides"]]
    for date in dates:
        day = date or dt.date(2026, 1, 5)
        item = cfg["date_overrides"].get(day.isoformat(), {}) if date else {}
        kinds = ("work", "am_half", "pm_half") if item.get("kind", "auto") == "auto" else (item["kind"],)
        for kind in kinds:
            if not all(cfg[a + "_enabled"] and item.get(a, "auto") != "off" for a in ACTIONS):
                continue
            if kind in ("holiday", "vacation") and not all(item.get(a) == "on" for a in ACTIONS):
                continue
            if kind in ("am_half", "pm_half") and cfg["half_day_policy"] == "skip_checkout" and item.get("checkout") != "on":
                continue
            a = time_bounds(cfg, day, "checkin", kind)
            b = time_bounds(cfg, day, "checkout", kind)
            if a[1] >= b[0]:
                raise ValueError(f"{str(day) if date else '기본 시간'}: 출근 시간 범위가 퇴근 시간과 겹칩니다. 당일 출근 후 퇴근하도록 설정하세요.")


@lru_cache(maxsize=8)
def korean_holidays(year: int) -> dict:
    import holidays
    return dict(holidays.SouthKorea(years=year, language="ko"))


def holiday_name(day: dt.date) -> str:
    return korean_holidays(day.year).get(day, "")


def day_kind(cfg: dict, day: dt.date, remote: dict | None = None) -> tuple[str, str]:
    override = cfg.get("date_overrides", {}).get(day.isoformat(), {})
    kind = override.get("kind", "auto")
    if kind != "auto":
        return kind, "수동 · " + DAY_LABELS[kind]
    if day.isoformat() in cfg.get("skip_dates", []):
        return "holiday", "기존 제외 날짜"
    if cfg.get("dooray_sync_enabled") and remote:
        record = remote.get("days", {}).get(day.isoformat())
        if record and record.get("kind") not in (None, "work"):
            return record["kind"], "두레이 · " + record.get("label", DAY_LABELS.get(record["kind"], "근태"))
    if day.weekday() not in cfg["workdays"]:
        return "holiday", "근무 요일 아님"
    if cfg.get("skip_korean_holidays", True):
        name = holiday_name(day)
        if name:
            return "holiday", name
    return "work", "기본 근무일"


def time_bounds(cfg: dict, day: dt.date, action: str, kind: str = "work") -> tuple[int, int]:
    override = cfg.get("date_overrides", {}).get(day.isoformat(), {})
    special = override.get(action + "_time")
    if not special and cfg["half_day_policy"] == "checkout_time" and action == "checkout":
        if kind in ("am_half", "pm_half"):
            special = cfg[kind + "_checkout_time"]
    base = seconds(special or cfg[action + "_time"])
    mode = cfg["time_mode" if action == "checkin" else "checkout_time_mode"]
    prefix = "" if action == "checkin" else "checkout_"
    if special or mode == "fixed":
        return base, base
    before = cfg[prefix + "random_before_minutes"] * 60 + cfg.get(prefix + "random_before_seconds", 0)
    after = cfg[prefix + "random_after_minutes"] * 60 + cfg.get(prefix + "random_after_seconds", 0)
    return max(0, base - before), min(86399, base + after)


@dataclass(frozen=True)
class Plan:
    enabled: bool
    reason: str
    start: int
    end: int
    target: int

    @property
    def description(self) -> str:
        if not self.enabled:
            return "OFF · " + self.reason
        if self.start == self.end:
            return clock_text(self.target)
        return f"{clock_text(self.start)} ~ {clock_text(self.end)}"


def plan_action(cfg: dict, day: dt.date, action: str, remote: dict | None = None) -> Plan:
    kind, reason = day_kind(cfg, day, remote)
    override = cfg.get("date_overrides", {}).get(day.isoformat(), {})
    setting = override.get(action, "auto")
    enabled = True
    if not cfg[action + "_enabled"]:
        enabled, reason = False, "전체 " + LABELS[action] + " 예약 꺼짐"
    elif setting == "off":
        enabled, reason = False, "날짜별 끄기"
    elif setting == "on":
        reason = "날짜별 켜기"
    elif kind in ("holiday", "vacation", "unknown_leave"):
        enabled = False
    elif kind in ("am_half", "pm_half"):
        policy = cfg["half_day_policy"]
        if policy == "skip_checkout" and action == "checkout":
            enabled = False
    start, end = time_bounds(cfg, day, action, kind)
    # Uniform over every second, inclusive. Stable per user/date/action: restarting
    # the worker never redraws today's time. Zero seconds preserves legacy plans.
    seed = hashlib.sha256(f"{cfg.get('tenant_url')}|{cfg.get('login_id')}|{day}|{action}|{start}|{end}".encode()).digest()
    target = random.Random(seed).randint(start, end)
    return Plan(enabled, reason, start, end, target)


def month_dates(year: int, month: int) -> list[dt.date]:
    return [dt.date(year, month, n) for n in range(1, calendar.monthrange(year, month)[1] + 1)]


def cache_identity(cfg: dict) -> str:
    return hashlib.sha256(f"{cfg['tenant_url']}|{cfg['login_id']}|{cfg['dooray_calendar_path']}|{cfg['dooray_calendar_selector']}".encode()).hexdigest()


def cache_fresh(cache: dict, cfg: dict, now: dt.datetime) -> bool:
    try:
        stamp = dt.datetime.fromisoformat(cache["synced_at"])
        age = (now - stamp).total_seconds()
        return (cache.get("identity") == cache_identity(cfg) and
                now.date().isoformat() in cache.get("days", {}) and
                0 <= age <= cfg["sync_interval_minutes"] * 60 and not cache.get("error"))
    except (KeyError, TypeError, ValueError):
        return False
