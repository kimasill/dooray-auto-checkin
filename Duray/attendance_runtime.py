"""Browser execution, monthly attendance synchronization and due-time checks."""
from __future__ import annotations

import datetime as dt
import time
import types

import attendance_rules as rules
import dooray_calendar
import dooray_checkin as core
import dooray_auth as auth


def check_connection(cfg: dict, *, manual=False, progress=None, cancel=None):
    """Read-only connection verification; never call either attendance action."""
    rules.validate_config(cfg, require_account=True)
    if not core.chromium_ready():
        raise auth.ConnectionProblem("BROWSER", "Chromium이 설치되지 않았습니다.",
                                     "연결 · 설정 아래의 Chromium 설치를 누른 뒤 다시 확인하세요.")
    try:
        with core.execution_lock(timeout=120, label="로그인 연결 확인", on_wait=progress, cancel=cancel):
            return run_once(cfg, dry_run=True, headed=manual, inspect=False,
                            connection_only=True, manual_login=manual, progress=progress, cancel=cancel)
    except Exception as exc:
        raise auth.translate_error(exc) from exc


def sync_page(page, cfg: dict, reader=None, cancel=None) -> dict:
    today = rules.now_kst().date()
    url = cfg["tenant_url"].rstrip("/") + cfg["dooray_calendar_path"]
    if page.url.rstrip("/") != url.rstrip("/"):
        page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
    # A SPA may finish the document before its calendar has rendered.
    deadline = time.monotonic() + cfg["timeout_ms"] / 1000
    while True:
        auth.check_cancel(cancel)
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
    with core.execution_lock(timeout=120, label="근태 달력 조회"):
        try:
            return run_once(cfg, dry_run=True, headed=headed, inspect=False, sync_only=True)
        except Exception as exc:
            exc = auth.translate_error(exc)
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
             scheduled: bool = False, sync_only: bool = False, connection_only: bool = False,
             manual_login: bool = False, progress=None, cancel=None, requested_at=None):
    from playwright.sync_api import sync_playwright
    if action not in rules.ACTIONS:
        raise ValueError("알 수 없는 출퇴근 동작")
    title = rules.LABELS[action]
    selectors = core.CHECKIN_CANDIDATES if action == "checkin" else core.CHECKOUT_CANDIDATES
    url = cfg["tenant_url"].rstrip("/") + (cfg["dooray_calendar_path"] if sync_only else cfg["attendance_path"])
    profile = auth.profile_path(cfg)
    if not connection_only and not inspect_login:
        auth.check_blocked(cfg)
    profile.mkdir(parents=True, exist_ok=True)
    def emit(message):
        # UI callbacks must not replace the persistent support log.
        core.log.info("%s", message)
        if progress:
            progress(message)
    core.log.info("두레이 접속 시작: 방식=%s / 출퇴근 경로=%s / 근태 경로=%s",
                  "직접 로그인" if manual_login else "자동 로그인", auth.route_kind(cfg["attendance_path"]),
                  auth.route_kind(cfg["dooray_calendar_path"]))
    started_at = requested_at or rules.now_kst()
    started_day = started_at.date()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chromium",
            headless=cfg["headless"] and not (headed or inspect or inspect_login),
            locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.set_default_timeout(cfg["timeout_ms"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        reader = dooray_calendar.AttendanceReader(cfg)
        page.on("response", reader.observe)
        login_verified = False
        try:
            emit("회사 두레이 주소에 접속하고 있습니다.")
            response = page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
            core.log.info("두레이 첫 응답: HTTP %s", response.status if response else "없음")
            if response and response.status >= 400:
                raise auth.ConnectionProblem("HTTP", f"두레이 페이지 접속 오류 (HTTP {response.status}).",
                                             "회사 주소·페이지 경로와 접속 권한을 확인하세요. 서버 장애이면 잠시 후 다시 시도하세요.",
                                             retryable=response.status >= 500)
            if inspect_login:
                core.dump_inputs(page)
                core.screenshot(page, "inspect-login")
                return "로그인 화면 점검 완료"
            def evidence():
                if reader.months:
                    return True
                if sync_only:
                    try:
                        return bool(dooray_calendar.read_month(page, cfg, started_day))
                    except RuntimeError:
                        pass
                return False
            core.ensure_logged_in(page, cfg, evidence=evidence, manual=manual_login,
                                  progress=emit, cancel=cancel, verification_url=url)
            login_verified = True
            auth.clear_failure(cfg)
            auth.check_cancel(cancel)
            if page.url.rstrip("/") != url.rstrip("/"):
                emit("로그인 후 설정된 출퇴근·근태 경로로 이동합니다.")
                page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
            page.wait_for_timeout(1500)
            if sync_only:
                return sync_page(page, cfg, reader)
            if connection_only:
                emit("로그인 완료 · 출퇴근 화면을 확인하고 있습니다.")
                button, _ = core.wait_for_any(page, core.candidates(cfg, "checkin_button", core.CHECKIN_CANDIDATES)
                                             + core.candidates(cfg, "checkout_button", core.CHECKOUT_CANDIDATES),
                                             cfg["timeout_ms"], cancel=cancel)
                if button is None:
                    raise auth.ConnectionProblem("ATTENDANCE_PAGE", "로그인했지만 출퇴근 화면을 찾지 못했습니다.",
                                                 "출퇴근 페이지 경로와 회사의 근태 사용 권한을 확인하세요.")
                emit("로그인·출퇴근 화면 확인 완료 · 이번 달 근태를 가져오고 있습니다.")
                try:
                    cache = sync_page(page, cfg, reader, cancel=cancel)
                except Exception as exc:
                    if isinstance(exc, auth.ConnectionProblem) and exc.code == "CANCELLED":
                        raise
                    error = str(auth.translate_error(exc))
                    cache = core.load_calendar(cfg)
                    cache.update(identity=rules.cache_identity(cfg), error=error)
                    core.atomic_json(core.CALENDAR_PATH, cache)
                    return {"ok": False, "message": "로그인·출퇴근 화면 확인 완료 / 근태 조회 실패\n"
                            "본인 월별 근태 경로와 조회 권한을 확인하세요.\n" + error}
                return {"ok": True, "message": "연결 완료 · 로그인 / 출퇴근 화면 / 이번 달 근태 확인\n"
                        f"{cache['month']} 근태 {len(cache['days'])}일을 가져왔습니다. 실제 출퇴근은 처리하지 않았습니다."}
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
                core.log.info("%s 기존 두레이 기록 확인: %s · 새 클릭 없음", title, core.TIME_RE.search(before).group())
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
                    core.log.info("%s 클릭 후 두레이 기록 확인: %s", title,
                                  core.TIME_RE.search(core.slot_text(target, action)).group())
                    core.screenshot(page, "success-" + action)
                    return f"{title} 체크 성공"
            raise RuntimeError(f"{title} 클릭 후 기록된 시각을 확인하지 못했습니다. 로그와 스크린샷을 확인하세요.")
        except Exception as exc:
            problem = auth.translate_error(exc)
            auth.log_page_state(page, cfg, "connection_failed", manual=manual_login)
            if not login_verified and not isinstance(problem, auth.ConnectionProblem):
                problem = auth.ConnectionProblem("LOGIN_SCREEN", "로그인 화면을 처리하지 못했습니다.",
                                                 "페이지가 바뀌었거나 입력란을 사용할 수 없습니다. 브라우저에서 직접 로그인을 실행하세요.")
            auth.remember_failure(cfg, problem)
            # Login pages may contain typed credentials; don't capture them.
            if login_verified and not isinstance(problem, auth.ConnectionProblem):
                core.screenshot(page, "failed-" + action)
            raise problem from exc
        finally:
            ctx.close()


def do_run(cfg: dict, args) -> int:
    try:
        return _do_run(cfg, args)
    except Exception as exc:
        args.error = str(exc)
        core.log.error("실행 실패: %s", exc)
        return 1


def _attempt(cfg, args, action, probing, requested_at):
    scheduled = getattr(args, "scheduled", False)
    title = rules.LABELS[action]
    with core.execution_lock(timeout=getattr(args, "lock_timeout", 240 if scheduled else 120),
                             label=title + (" 기록 없는 점검" if probing else " 처리"),
                             on_wait=getattr(args, "progress", None)):
        if rules.now_kst().date() != requested_at.date():
            raise RuntimeError("대기 중 날짜가 변경되어 실행을 중단했습니다.")
        if scheduled:
            cfg = core.load_config()
        state = core.load_state()
        # Recheck after acquiring the lock: another waiter may have completed it.
        if not probing and state.get("last_" + action + "_date") == requested_at.date().isoformat():
            return f"이미 {title} 체크됨"
        if not args.force and not probing:
            cache = core.load_calendar(cfg)
            plan = rules.plan_action(cfg, requested_at.date(), action, cache)
            refresh_required = (needs_remote(cfg, requested_at.date(), action) and
                                not rules.cache_fresh(cache, cfg, rules.now_kst()) and
                                rules.plan_action(cfg, requested_at.date(), action, {}).enabled)
            if not plan.enabled and not refresh_required:
                return "건너뜀: " + plan.reason
        if not core.chromium_ready():
            raise RuntimeError("Chromium이 없습니다. 설정 > Chromium 설치를 실행하세요.")
        result = run_once(cfg, dry_run=args.dry_run, headed=args.headed, inspect=args.inspect,
                          inspect_login=args.inspect_login, action=action, force=args.force,
                          scheduled=scheduled, requested_at=requested_at)
        if not probing and result in (f"{title} 체크 성공", f"이미 {title} 체크됨"):
            state = core.load_state()
            state["last_" + action + "_date"] = requested_at.date().isoformat()
            state["last_" + action + "_result"] = result
            state.pop("last_" + action + "_error", None)
            state.pop("last_" + action + "_error_at", None)
            state["last_result"] = result
            state["last_run_at"] = rules.now_kst().isoformat(timespec="seconds")
            state.setdefault("history", []).append({"date": requested_at.date().isoformat(), "action": action,
                                                    "at": state["last_run_at"], "result": result})
            state["history"] = state["history"][-180:]
            core.save_state(state)
        return result


def _do_run(cfg: dict, args) -> int:
    rules.validate_config(cfg, require_account=True)
    action = getattr(args, "action", "checkin")
    title = rules.LABELS[action]
    requested_at = getattr(args, "requested_at", None) or rules.now_kst()
    today = requested_at.date()
    probing = args.dry_run or args.inspect or args.inspect_login
    attempts = 1 if probing else cfg["retry_count"]
    last_error = ""
    for attempt in range(attempts):
        try:
            if rules.now_kst().date() != today:
                raise RuntimeError("날짜가 변경되어 재시도를 중단했습니다.")
            result = _attempt(cfg, args, action, probing, requested_at)
            args.result = result
            core.log.info("%s", result)
            return 0
        except core.OperationBusy as exc:
            args.error = str(exc)
            core.log.info("%s 대기: %s", title, exc)
            return 2
        except Exception as exc:
            last_error = str(exc)
            core.log.error("%s 실패 (%d/%d): %s", title, attempt + 1, attempts, exc)
            if isinstance(exc, auth.ConnectionProblem) and not exc.retryable:
                break
            if attempt + 1 < attempts:
                time.sleep(cfg["retry_delay_seconds"])
    if not probing:
        notify = False
        try:
            with core.execution_lock(timeout=5, label="실행 결과 저장"):
                state = core.load_state()
                # Another operation may have recovered while this one was retrying.
                if state.get("last_" + action + "_date") != today.isoformat():
                    notify = state.get("last_" + action + "_error") != last_error or not state.get(
                        "last_" + action + "_error_at", "").startswith(today.isoformat())
                    state["last_" + action + "_error"] = last_error
                    state["last_" + action + "_error_at"] = rules.now_kst().isoformat(timespec="seconds")
                    state["last_error_at"] = state["last_" + action + "_error_at"]
                    core.save_state(state)
        except core.OperationBusy:
            core.log.warning("다른 작업이 실행 중이어서 실패 상태 저장을 보류합니다. 원인: %s", last_error)
        # Never hold the browser lock while showing a dialog or sleeping.
        if cfg["notify_on_failure"] and notify:
            core.notify_failure(f"두레이 {title} 실패\n{last_error}")
    args.error = last_error
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
        # A reservation accepted within grace keeps its timestamp across slow sync.
        # Newly due reservations use the current clock; no expired job is backdated.
        current = provided_now or rules.now_kst()
        accepted = now if due(cfg, now, action, cache)[0] else current
        ok, _ = due(cfg, accepted, action, cache)
        if not ok:
            continue
        state = core.load_state()
        if state.get("last_" + action + "_date") == current.date().isoformat():
            continue
        if needs_remote(cfg, current.date(), action) and not rules.cache_fresh(cache, cfg, current):
            continue
        args = types.SimpleNamespace(action=action, dry_run=False, inspect=False, inspect_login=False,
                                     force=False, headed=False, scheduled=True, requested_at=accepted)
        core.log.info("%s 예약 접수: 예정 %s / 접수 %s", rules.LABELS[action],
                      rules.clock_text(rules.plan_action(cfg, accepted.date(), action, cache).target),
                      accepted.isoformat(timespec="seconds"))
        result = do_run(cfg, args)
        if result == 2:
            continue
        if result != 0:
            raise RuntimeError(f"{rules.LABELS[action]} 예약 실행 실패. 잠시 후 다시 확인합니다.")
        completed.append(action)
    if sync_error:
        raise sync_error
    return completed


def watch() -> int:
    core.setup_logging()
    try:
        with core.execution_lock("watcher"):
            core.log.info("출퇴근 예약 감시 시작 (한국 시간)")
            previous = None
            while True:
                try:
                    cfg = core.load_config()
                    now = rules.now_kst()
                    cache = core.load_calendar(cfg)
                    plans = []
                    state = core.load_state()
                    for action in rules.ACTIONS:
                        plan = rules.plan_action(cfg, now.date(), action, cache)
                        status = "처리 확인됨" if state.get("last_" + action + "_date") == now.date().isoformat() else due(cfg, now, action, cache)[1] or "실행 시각 도달"
                        plans.append((rules.LABELS[action], plan.enabled, plan.description, rules.clock_text(plan.target), status))
                    signature = (now.date(), tuple(plans))
                    if signature != previous:
                        for title, enabled, description, target, status in plans:
                            core.log.info("오늘 %s 계획: %s / 선택 시각 %s / %s", title,
                                          description if enabled else "OFF · " + description, target, status)
                        previous = signature
                    tick(cfg)
                    time.sleep(2)
                except (Exception, SystemExit) as exc:
                    core.log.error("예약 확인 실패: %s", exc)
                    time.sleep(60)
    except core.OperationBusy:
        core.log.debug("예약 감시가 이미 실행 중입니다.")
        return 0
