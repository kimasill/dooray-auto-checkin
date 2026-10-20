"""Browser execution, monthly attendance synchronization and due-time checks."""
from __future__ import annotations

import datetime as dt
import time
import types

import attendance_rules as rules
import dooray_calendar
import dooray_checkin as core


def sync_page(page, cfg: dict, reader=None) -> dict:
    today = rules.now_kst().date()
    url = cfg["tenant_url"].rstrip("/") + cfg["dooray_calendar_path"]
    if page.url.rstrip("/") != url.rstrip("/"):
        page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
    # A SPA may finish the document before its calendar has rendered.
    deadline = time.monotonic() + cfg["timeout_ms"] / 1000
    while True:
        try:
            key = today.strftime("%Y-%m")
            if reader and key in reader.errors:
                raise RuntimeError(reader.errors[key])
            days = reader.months.get(key) if reader else None
            if days is None:
                days = dooray_calendar.read_month(page, cfg, today)
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            page.wait_for_timeout(500)
    cache = {"identity": rules.cache_identity(cfg), "synced_at": rules.now_kst().isoformat(),
             "month": today.strftime("%Y-%m"), "days": days, "error": ""}
    core.atomic_json(core.CALENDAR_PATH, cache)
    core.log.info("두레이 근태 동기화: %s, %d일, 휴가/휴무 %d일", cache["month"], len(days),
                  sum(d["kind"] != "work" for d in days.values()))
    return cache


def sync_calendar(cfg: dict, *, headed: bool = False) -> dict:
    with core.execution_lock():
        try:
            return run_once(cfg, dry_run=True, headed=headed, inspect=False, sync_only=True)
        except Exception as exc:
            cache = core.load_calendar(cfg)
            cache["identity"] = rules.cache_identity(cfg)
            cache["error"] = str(exc)
            core.atomic_json(core.CALENDAR_PATH, cache)
            raise


def needs_remote(cfg: dict, day: dt.date, action: str) -> bool:
    item = cfg["date_overrides"].get(day.isoformat(), {})
    return cfg["dooray_sync_enabled"] and item.get("kind", "auto") == "auto" and item.get(action, "auto") == "auto"


def due(cfg: dict, now: dt.datetime, action: str, cache: dict) -> tuple[bool, str]:
    plan = rules.plan_action(cfg, now.date(), action, cache)
    if not plan.enabled:
        return False, plan.reason
    second = now.hour * 3600 + now.minute * 60 + now.second
    if second < plan.target:
        return False, "예약 시각 전"
    if second > plan.target + cfg["late_grace_minutes"] * 60 + 5:
        return False, "지연 허용 시간 초과"
    return True, ""


def run_once(cfg: dict, *, dry_run: bool, headed: bool, inspect: bool,
             inspect_login: bool = False, action: str = "checkin", force: bool = False,
             scheduled: bool = False, sync_only: bool = False):
    from playwright.sync_api import sync_playwright
    if action not in rules.ACTIONS:
        raise ValueError("알 수 없는 출퇴근 동작")
    title = rules.LABELS[action]
    selectors = core.CHECKIN_CANDIDATES if action == "checkin" else core.CHECKOUT_CANDIDATES
    url = cfg["tenant_url"].rstrip("/") + (cfg["dooray_calendar_path"] if sync_only else cfg["attendance_path"])
    core.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    started_at = rules.now_kst()
    started_day = started_at.date()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(core.PROFILE_DIR),
            channel="chromium",
            headless=cfg["headless"] and not (headed or inspect or inspect_login),
            locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.set_default_timeout(cfg["timeout_ms"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        reader = dooray_calendar.AttendanceReader(cfg)
        page.on("response", reader.observe)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
            page.wait_for_timeout(1500)
            if inspect_login:
                core.dump_inputs(page)
                core.screenshot(page, "inspect-login")
                return "로그인 화면 점검 완료"
            core.ensure_logged_in(page, cfg)
            if page.url.rstrip("/") != url.rstrip("/"):
                page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
            page.wait_for_timeout(1500)
            if sync_only:
                return sync_page(page, cfg, reader)
            if inspect:
                core.dump_clickables(page)
                core.screenshot(page, "inspect-" + action)
                return "버튼 점검 완료"
            cache = core.load_calendar(cfg)
            if not force and needs_remote(cfg, started_day, action):
                # Read live attendance before any automatic mutation, even if the preview is cached.
                cache = sync_page(page, cfg, reader)
                if page.url.rstrip("/") != url.rstrip("/"):
                    page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
                    page.wait_for_timeout(1500)
            if not force and not dry_run:
                plan = rules.plan_action(cfg, started_day, action, cache)
                if not plan.enabled:
                    return "건너뜀: " + plan.reason
            btn, selector = core.wait_for_any(page, core.candidates(cfg, action + "_button", selectors), cfg["timeout_ms"])
            if btn is None:
                raise RuntimeError(f"{title} 버튼을 찾지 못했습니다. 설정의 버튼 선택자와 근태 경로를 확인하세요.")
            label = (btn.inner_text() or "").strip()
            opposite = "퇴근" if action == "checkin" else "출근"
            expected = ("출근", "업무 시작", "check in") if action == "checkin" else ("퇴근", "업무 종료", "check out")
            if opposite in label or not any(word in label.lower() for word in expected):
                raise RuntimeError(f"{title} 버튼 문구를 확인할 수 없어 중단했습니다: {label!r}")
            before = core.slot_text(btn, action)
            if core.has_checkin_time(before):
                return f"이미 {title} 체크됨"
            if not btn.is_enabled():
                raise RuntimeError(f"{title} 버튼이 비활성 상태이며 기록된 시각이 없습니다.")
            if action == "checkout":
                in_btn, _ = core.find_first(page, core.candidates(cfg, "checkin_button", core.CHECKIN_CANDIDATES))
                if in_btn is None or not core.has_checkin_time(core.slot_text(in_btn, "checkin")):
                    raise RuntimeError("오늘의 출근 시각을 확인할 수 없어 퇴근을 중단했습니다.")
            if dry_run:
                core.screenshot(page, "dry-run-" + action)
                return f"{title} 테스트 성공 (실제 클릭 없음)"
            now = rules.now_kst()
            if now.date() != started_day:
                raise RuntimeError("실행 중 날짜가 변경되어 중단했습니다.")
            # Re-read settings right before mutation so a calendar edit or OFF takes effect.
            if scheduled:
                latest = core.load_config()
                # Grace bounds the start of a reservation, not browser/login latency.
                ok, reason = due(latest, started_at, action, cache)
                if not ok:
                    return "건너뜀: " + reason
                if rules.cache_identity(latest) != rules.cache_identity(cfg):
                    raise RuntimeError("실행 중 계정/근태 설정이 바뀌었습니다. 다음 확인에서 다시 실행합니다.")
                if needs_remote(latest, now.date(), action) and not rules.cache_fresh(cache, latest, now):
                    raise RuntimeError("최신 근태 확인이 필요해 실행을 보류했습니다.")
            core.log.info("%s 실행: %s (%s)", title, label, selector)
            btn.click()
            deadline = time.monotonic() + min(cfg["timeout_ms"] / 1000, 15)
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
                target, _ = core.find_first(page, core.candidates(cfg, action + "_button", selectors))
                if target is not None and core.has_checkin_time(core.slot_text(target, action)):
                    core.screenshot(page, "success-" + action)
                    return f"{title} 체크 성공"
            raise RuntimeError(f"{title} 클릭 후 기록된 시각을 확인하지 못했습니다. 로그와 스크린샷을 확인하세요.")
        except Exception:
            core.screenshot(page, "failed-" + action)
            raise
        finally:
            ctx.close()


def do_run(cfg: dict, args) -> int:
    try:
        with core.execution_lock():
            return _do_run(cfg, args)
    except Exception as exc:
        core.log.error("실행 실패: %s", exc)
        return 1


def _do_run(cfg: dict, args) -> int:
    rules.validate_config(cfg, require_account=True)
    action = getattr(args, "action", "checkin")
    title = rules.LABELS[action]
    today = rules.now_kst().date()
    probing = args.dry_run or args.inspect or args.inspect_login
    state = core.load_state()
    # Manual actions still respect duplicate protection.
    if not probing and state.get("last_" + action + "_date") == today.isoformat():
        core.log.info("오늘 %s 기록이 이미 있습니다.", title)
        return 0
    if not args.force and not probing:
        cache = core.load_calendar(cfg)
        plan = rules.plan_action(cfg, today, action, cache)
        refresh_required = (needs_remote(cfg, today, action) and
                            not rules.cache_fresh(cache, cfg, rules.now_kst()) and
                            rules.plan_action(cfg, today, action, {}).enabled)
        if not plan.enabled and not refresh_required:
            core.log.info("건너뜀: %s", plan.reason)
            return 0
    if not core.chromium_ready():
        raise RuntimeError("Chromium이 없습니다. 설정 > Chromium 설치를 실행하세요.")
    attempts = 1 if probing else cfg["retry_count"]
    last_error = ""
    for attempt in range(attempts):
        try:
            if rules.now_kst().date() != today:
                raise RuntimeError("날짜가 변경되어 재시도를 중단했습니다.")
            result = run_once(cfg, dry_run=args.dry_run, headed=args.headed, inspect=args.inspect,
                              inspect_login=args.inspect_login, action=action, force=args.force,
                              scheduled=getattr(args, "scheduled", False))
            core.log.info("%s", result)
            if not probing and result in (f"{title} 체크 성공", f"이미 {title} 체크됨"):
                state = core.load_state()
                state["last_" + action + "_date"] = today.isoformat()
                state["last_" + action + "_result"] = result
                state["last_result"] = result
                state["last_run_at"] = rules.now_kst().isoformat(timespec="seconds")
                state.setdefault("history", []).append({"date": today.isoformat(), "action": action,
                                                        "at": state["last_run_at"], "result": result})
                state["history"] = state["history"][-180:]
                core.save_state(state)
            return 0
        except Exception as exc:
            last_error = str(exc)
            core.log.error("%s 실패 (%d/%d): %s", title, attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(cfg["retry_delay_seconds"])
    if not probing:
        state = core.load_state()
        state["last_" + action + "_error"] = last_error
        state["last_error_at"] = rules.now_kst().isoformat(timespec="seconds")
        core.save_state(state)
        if cfg["notify_on_failure"]:
            core.notify_failure(f"두레이 {title} 실패\n{last_error}")
    return 1


def tick(cfg: dict, *, now: dt.datetime | None = None) -> list[str]:
    provided_now = now
    now = now or rules.now_kst()
    completed = []
    cache = core.load_calendar(cfg)
    sync_error = None
    if any(cfg[a + "_enabled"] and needs_remote(cfg, now.date(), a) for a in rules.ACTIONS) and not rules.cache_fresh(cache, cfg, now):
        try:
            cache = sync_calendar(cfg)
        except Exception as exc:
            sync_error = exc
            cache = core.load_calendar(cfg)
    for action in rules.ACTIONS:
        # Browser work may take minutes: never reuse the pre-sync wall clock in production.
        current = provided_now or rules.now_kst()
        ok, _ = due(cfg, current, action, cache)
        if not ok:
            continue
        state = core.load_state()
        if state.get("last_" + action + "_date") == current.date().isoformat():
            continue
        if needs_remote(cfg, current.date(), action) and not rules.cache_fresh(cache, cfg, current):
            continue
        args = types.SimpleNamespace(action=action, dry_run=False, inspect=False, inspect_login=False,
                                     force=False, headed=False, scheduled=True)
        if do_run(cfg, args) != 0:
            raise RuntimeError(f"{rules.LABELS[action]} 예약 실행 실패. 잠시 후 다시 확인합니다.")
        completed.append(action)
    if sync_error:
        raise sync_error
    return completed


def watch() -> int:
    core.setup_logging()
    core.log.info("출퇴근 예약 감시 시작 (한국 시간)")
    try:
        with core.execution_lock("watcher"):
            while True:
                try:
                    cfg = core.load_config()
                    tick(cfg)
                    time.sleep(2)
                except (Exception, SystemExit) as exc:
                    core.log.error("예약 확인 실패: %s", exc)
                    time.sleep(60)
    except RuntimeError:
        core.log.info("예약 감시가 이미 실행 중입니다.")
        return 0
