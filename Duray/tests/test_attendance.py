import copy
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attendance_rules as rules
import attendance_runtime as runtime
import dooray_calendar as calendar
import dooray_checkin as core
import scheduler


def config(**kw):
    cfg = copy.deepcopy(core.DEFAULT_CONFIG)
    cfg.update(tenant_url="https://test.dooray.com", login_id="test", notify_on_failure=False)
    cfg.update(kw)
    return cfg


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.day = dt.date(2026, 10, 20)
        self.cfg = config(checkout_enabled=True)

    def plan(self, action="checkin", day=None, cache=None):
        return rules.plan_action(self.cfg, day or self.day, action, cache)

    def test_legacy_random_zero_and_custom(self):
        for minutes in (0, 37):
            cfg = rules.normalize_config({"random_window_minutes": minutes, "skip_weekends": False})
            self.assertEqual(cfg["random_before_minutes"], minutes)
            self.assertEqual(cfg["random_after_minutes"], minutes)
            self.assertEqual(cfg["workdays"], list(range(7)))

    def test_fixed_time_to_seconds(self):
        self.cfg["checkin_time"] = "08:34:12"
        self.assertEqual(self.plan().target, 8 * 3600 + 34 * 60 + 12)

    def test_random_asymmetric_and_restart_stable(self):
        self.cfg.update(time_mode="random", random_before_minutes=47, random_after_minutes=3)
        a, b = self.plan(), self.plan()
        self.assertEqual((a.start, a.end), (8 * 3600 + 13 * 60, 9 * 3600 + 3 * 60))
        self.assertEqual(a.target, b.target)
        self.assertLessEqual(a.start, a.target)
        self.assertLessEqual(a.target, a.end)

    def test_midnight_clipping(self):
        self.cfg.update(checkin_time="00:02", time_mode="random", random_before_minutes=60)
        self.assertEqual(self.plan().start, 0)
        self.cfg.update(checkout_time="23:59", checkout_time_mode="random", checkout_random_after_minutes=60)
        self.assertEqual(self.plan("checkout").end, 86399)

    def test_saturday_manual_on(self):
        day = dt.date(2026, 10, 24)
        self.assertFalse(self.plan(day=day).enabled)
        self.cfg["date_overrides"][day.isoformat()] = {"checkin": "on", "checkout": "off"}
        self.assertTrue(self.plan(day=day).enabled)
        self.assertFalse(self.plan("checkout", day).enabled)

    def test_korean_substitute_holiday(self):
        day = dt.date(2026, 3, 2)
        self.assertFalse(self.plan(day=day).enabled)
        self.assertTrue(rules.holiday_name(day))

    def test_global_off_beats_date_on(self):
        self.cfg["checkout_enabled"] = False
        self.cfg["date_overrides"][self.day.isoformat()] = {"checkout": "on"}
        self.assertFalse(self.plan("checkout").enabled)

    def test_remote_vacation_and_manual_override(self):
        self.cfg["dooray_sync_enabled"] = True
        cache = {"days": {self.day.isoformat(): {"kind": "vacation", "label": "연차"}}}
        self.assertFalse(self.plan(cache=cache).enabled)
        self.cfg["date_overrides"][self.day.isoformat()] = {"checkin": "on"}
        self.assertTrue(self.plan(cache=cache).enabled)
        self.assertFalse(self.plan("checkout", cache=cache).enabled)

    def test_half_day_policies(self):
        for kind in ("am_half", "pm_half"):
            with self.subTest(kind=kind):
                self.cfg["date_overrides"][self.day.isoformat()] = {"kind": kind}
                for policy in ("checkout_time", "keep_time", "skip_checkout"):
                    self.cfg["half_day_policy"] = policy
                    self.assertTrue(self.plan("checkin").enabled)
                    self.assertEqual(self.plan("checkin").target, rules.seconds(self.cfg["checkin_time"]))
                self.assertFalse(self.plan("checkout").enabled)
                self.cfg["half_day_policy"] = "checkout_time"
                self.assertTrue(self.plan("checkout").enabled)
                self.assertEqual(self.plan("checkout").target, rules.seconds(self.cfg[kind + "_checkout_time"]))
                self.cfg["half_day_policy"] = "keep_time"
                self.assertEqual(self.plan("checkout").target, rules.seconds(self.cfg["checkout_time"]))

    def test_half_day_keeps_random_checkin_and_date_time_wins(self):
        self.cfg.update(time_mode="random", random_before_minutes=20, random_after_minutes=5)
        usual = self.plan().target
        self.cfg["date_overrides"][self.day.isoformat()] = {"kind": "pm_half", "checkout_time": "15:35:00"}
        self.assertEqual(self.plan().target, usual)
        self.assertEqual(self.plan("checkout").target, rules.seconds("15:35:00"))

    def test_date_time_is_fixed(self):
        self.cfg["time_mode"] = "random"
        self.cfg["date_overrides"][self.day.isoformat()] = {"checkin_time": "10:01:23"}
        self.assertEqual(self.plan().start, self.plan().end)
        self.assertEqual(self.plan().target, rules.seconds("10:01:23"))

    def test_skip_dates_and_manual_workday(self):
        self.cfg["skip_dates"] = [self.day.isoformat()]
        self.assertFalse(self.plan().enabled)
        self.cfg["date_overrides"][self.day.isoformat()] = {"kind": "work"}
        self.assertTrue(self.plan().enabled)

    def test_validation_bad_values(self):
        for change in ({"checkin_time": "25:00"}, {"checkin_time": "9:00"}, {"random_before_minutes": -1},
                       {"random_after_minutes": "5"}, {"late_grace_minutes": 181}, {"tenant_url": "http://test"},
                       {"attendance_path": "//other.test"}, {"skip_dates": ["2026-02-30"]},
                       {"date_overrides": {"2026-10-20": {"kind": "bad"}}}, {"checkout_time": "08:00"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                rules.validate_config({**self.cfg, **change})

    def test_checkout_only_has_no_false_overlap(self):
        self.cfg.update(checkin_enabled=False, checkout_time="08:00")
        rules.validate_config(self.cfg)

    def test_cache_expiry_identity_and_error(self):
        now = dt.datetime(2026, 10, 20, 8, 0, tzinfo=rules.KST)
        cache = {"identity": rules.cache_identity(self.cfg), "synced_at": now.isoformat(), "days": {"2026-10-20": {}}}
        self.assertTrue(rules.cache_fresh(cache, self.cfg, now))
        self.assertFalse(rules.cache_fresh(cache, self.cfg, now + dt.timedelta(hours=2)))
        self.assertFalse(rules.cache_fresh({**cache, "error": "offline"}, self.cfg, now))
        self.assertFalse(rules.cache_fresh(cache, {**self.cfg, "login_id": "different"}, now))


class CalendarTest(unittest.TestCase):
    def payload(self, *records):
        return {"header": {"isSuccessful": True}, "result": list(records), "totalCount": len(records)}

    def leave(self, name, start, end):
        return {"attendNm": name, "attStaDateTime": start, "attEndDateTime": end, "attendTypeCd": "DEDUCT_LEAVE"}

    def test_real_response_schema_and_empty_month(self):
        payload = self.payload(self.leave("오후반차", "2026-10-02T15:00:00+09:00", "2026-10-02T19:00:00+09:00"))
        result = calendar.parse_off_duties(payload, 2026, 10)
        self.assertEqual(len(result), 31)
        self.assertEqual(result["2026-10-02"]["kind"], "pm_half")
        self.assertEqual(calendar.parse_off_duties(self.payload(), 2026, 10)["2026-10-02"]["kind"], "work")

    def test_multi_day_leave_exclusive_end_and_combined_halves(self):
        payload = self.payload(self.leave("연차", "2026-10-03T00:00:00+09:00", "2026-10-06T00:00:00+09:00"),
                               self.leave("오전반차", "2026-10-20T09:00:00+09:00", "2026-10-20T13:00:00+09:00"),
                               self.leave("오후반차", "2026-10-20T14:00:00+09:00", "2026-10-20T18:00:00+09:00"))
        result = calendar.parse_off_duties(payload, 2026, 10)
        self.assertTrue(all(result[f"2026-10-{i:02d}"]["kind"] == "vacation" for i in (3,4,5)))
        self.assertEqual(result["2026-10-06"]["kind"], "work")
        self.assertEqual(result["2026-10-03"]["label"], "연차")
        self.assertEqual(result["2026-10-20"]["kind"], "vacation")

    def test_broken_response_not_treated_as_no_leave(self):
        for payload in ({"header": {"isSuccessful": False}, "result": []},
                        {**self.payload(), "totalCount": 1}, self.payload({"attendNm": "연차"})):
            with self.assertRaises(RuntimeError):
                calendar.parse_off_duties(payload, 2026, 10)

    def test_reader_scopes_origin_method_full_month_and_self(self):
        reader = calendar.AttendanceReader(config())
        def response(query, host="test.dooray.com", method="GET"):
            return types.SimpleNamespace(url=f"https://{host}/wapi/work-schedule/v1/off-duties?{query}",
                                         request=types.SimpleNamespace(method=method), status=200,
                                         json=lambda: self.payload())
        for item in (response("staDate=2026-10-18&endDate=2026-10-24"),
                     response("staDate=2026-10-01&endDate=2026-10-31&memberId=someone"),
                     response("staDate=2026-10-01&endDate=2026-10-31", host="unrelated.test"),
                     response("staDate=2026-10-01&endDate=2026-10-31", method="POST")):
            reader.observe(item)
        self.assertEqual(reader.months, {})
        reader.observe(response("staDate=2026-10-01&endDate=2026-10-31"))
        self.assertEqual(len(reader.months["2026-10"]), 31)

    def test_leave_classification(self):
        for text, expected in (("20\n연차", "vacation"), ("오전반차", "am_half"), ("오후 반차", "pm_half"),
                               ("오전 반차\n오후 반차", "vacation"), ("반반차", "unknown_leave"),
                               ("반차", "unknown_leave"), ("대체 휴일", "holiday"),
                               ("출근 09:00 퇴근 18:00", "work"), ("연차 취소", "work"),
                               ("휴가 신청", "work")):
            with self.subTest(text=text):
                self.assertEqual(calendar.classify_leave(text)[0], expected)

    def test_complete_month_and_cancellation(self):
        records = [{"date": d.isoformat(), "text": ""} for d in rules.month_dates(2026, 10)]
        records[19]["text"] = "오전 반차"
        self.assertEqual(calendar.parse_month(records, 2026, 10)["2026-10-20"]["kind"], "am_half")
        records[19]["text"] = ""
        self.assertEqual(calendar.parse_month(records, 2026, 10)["2026-10-20"]["kind"], "work")

    def test_partial_empty_wrong_month_fail(self):
        for records in ([], [{"date": "2026-10-20", "text": "연차"}],
                        [{"date": d.isoformat(), "text": ""} for d in rules.month_dates(2026, 11)]):
            with self.assertRaises(RuntimeError):
                calendar.parse_month(records, 2026, 10)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        output = Path(__file__).resolve().parents[2] / "test-output"
        output.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=output)
        assert Path(self.tmp.name).resolve().is_relative_to(output.resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("DATA_DIR", self.root), ("STATE_PATH", self.root / "state.json"),
                            ("CONFIG_PATH", self.root / "config.json"), ("CALENDAR_PATH", self.root / "calendar.json")):
            patcher = patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = dt.datetime(2026, 10, 20, 9, 0, 1, tzinfo=rules.KST)
        patcher = patch.object(rules, "now_kst", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = config(retry_count=1, retry_delay_seconds=0)
        self.args = types.SimpleNamespace(action="checkin", dry_run=False, inspect=False, inspect_login=False, force=False, headed=False)

    def test_due_window_and_missed_run(self):
        self.assertTrue(runtime.due(self.cfg, self.now, "checkin", {})[0])
        self.assertFalse(runtime.due(self.cfg, self.now - dt.timedelta(minutes=1), "checkin", {})[0])
        self.assertFalse(runtime.due(self.cfg, self.now + dt.timedelta(minutes=16), "checkin", {})[0])

    def test_state_separate_for_checkout(self):
        self.cfg["checkout_enabled"] = True
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", return_value="출근 체크 성공"):
            self.assertEqual(runtime.do_run(self.cfg, self.args), 0)
        self.assertEqual(core.load_state()["last_checkin_date"], "2026-10-20")
        self.args.action = "checkout"
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", return_value="퇴근 체크 성공"):
            self.assertEqual(runtime.do_run(self.cfg, self.args), 0)
        self.assertEqual(core.load_state()["last_checkout_date"], "2026-10-20")
        self.assertEqual(len(core.load_state()["history"]), 2)

    def test_duplicate_guard_even_manual(self):
        core.save_state({"last_checkin_date": "2026-10-20"})
        self.args.force = True
        with patch.object(runtime, "run_once") as run:
            self.assertEqual(runtime.do_run(self.cfg, self.args), 0)
            run.assert_not_called()

    def test_dry_run_does_not_write_state(self):
        self.args.dry_run = True
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", return_value="출근 체크 성공"):
            runtime.do_run(self.cfg, self.args)
        self.assertEqual(core.load_state(), {})

    def test_stale_leave_does_not_skip_refresh_after_cancellation(self):
        self.cfg["dooray_sync_enabled"] = True
        core.atomic_json(core.CALENDAR_PATH, {"identity": rules.cache_identity(self.cfg),
            "synced_at": (self.now-dt.timedelta(hours=3)).isoformat(),
            "days": {"2026-10-20": {"kind": "vacation", "label": "연차"}}})
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", return_value="출근 체크 성공") as run:
            self.assertEqual(runtime.do_run(self.cfg, self.args), 0)
            run.assert_called_once()

    def test_unconfirmed_click_is_failure(self):
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", side_effect=RuntimeError("unconfirmed")):
            self.assertEqual(runtime.do_run(self.cfg, self.args), 1)
        self.assertNotIn("last_checkin_date", core.load_state())

    def test_failed_sync_blocks_automatic_action(self):
        self.cfg["dooray_sync_enabled"] = True
        with patch.object(runtime, "sync_calendar", side_effect=RuntimeError("offline")), patch.object(runtime, "do_run") as run:
            with self.assertRaises(RuntimeError):
                runtime.tick(self.cfg, now=self.now)
            run.assert_not_called()

    def test_explicit_day_does_not_depend_on_remote(self):
        self.cfg["dooray_sync_enabled"] = True
        self.cfg["date_overrides"]["2026-10-20"] = {"kind": "work"}
        with patch.object(runtime, "sync_calendar") as sync, patch.object(runtime, "do_run", return_value=0) as run:
            self.assertEqual(runtime.tick(self.cfg, now=self.now), ["checkin"])
            run.assert_called_once()
            sync.assert_not_called()

    def test_os_lock_excludes_second_process(self):
        with core.execution_lock():
            with self.assertRaises(RuntimeError):
                with core.execution_lock():
                    pass

    def test_lock_waits_for_owner_and_reports_what_is_running(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def holder():
            with core.execution_lock(label="근태 달력 조회"):
                entered.set()
                release.wait(3)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(2))
        messages = []
        try:
            def waiting(message):
                messages.append(message)
                release.set()
            with core.execution_lock(timeout=2, label="퇴근 점검", on_wait=waiting):
                pass
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(messages), 1)
        self.assertIn("근태 달력 조회", messages[0])
        self.assertIn("자동으로 시작", messages[0])

    def test_notification_is_outside_browser_lock_and_not_repeated(self):
        self.cfg["notify_on_failure"] = True
        def notification(message):
            with core.execution_lock():
                pass
        with patch.object(core, "chromium_ready", return_value=True), \
                patch.object(runtime, "run_once", side_effect=RuntimeError("fixture failure")), \
                patch.object(core, "notify_failure", side_effect=notification) as notify:
            self.assertEqual(runtime.do_run(self.cfg, self.args), 1)
            self.assertEqual(runtime.do_run(self.cfg, self.args), 1)
            notify.assert_called_once()

    def test_retry_sleep_is_outside_browser_lock(self):
        self.cfg.update(retry_count=2, retry_delay_seconds=1)
        def sleep(delay):
            with core.execution_lock():
                pass
        with patch.object(core, "chromium_ready", return_value=True), \
                patch.object(runtime, "run_once", side_effect=[RuntimeError("retry"), "출근 체크 성공"]), \
                patch.object(runtime.time, "sleep", side_effect=sleep) as wait:
            self.assertEqual(runtime.do_run(self.cfg, self.args), 0)
            wait.assert_called_once_with(1)

    def test_waiting_second_request_rechecks_duplicate_before_click(self):
        import threading
        import time
        entered = threading.Event()
        def run(*args, **kwargs):
            entered.set()
            time.sleep(.15)
            return "출근 체크 성공"
        with patch.object(core, "chromium_ready", return_value=True), patch.object(runtime, "run_once", side_effect=run) as browser:
            results = []
            thread = threading.Thread(target=lambda: results.append(runtime.do_run(self.cfg, copy.copy(self.args))))
            thread.start()
            self.assertTrue(entered.wait(2))
            results.append(runtime.do_run(self.cfg, copy.copy(self.args)))
            thread.join(3)
            self.assertEqual(results, [0, 0])
            browser.assert_called_once()

    def test_sync_delay_does_not_discard_reservation_accepted_within_grace(self):
        self.cfg.update(dooray_sync_enabled=True, late_grace_minutes=0)
        fresh = {"identity": rules.cache_identity(self.cfg), "synced_at": (self.now + dt.timedelta(seconds=20)).isoformat(),
                 "days": {self.now.date().isoformat(): {"kind": "work", "label": "근무일"}}}
        clock = [self.now]
        def sync(cfg):
            clock[0] += dt.timedelta(seconds=20)
            return fresh
        with patch.object(rules, "now_kst", side_effect=lambda: clock[0]), \
                patch.object(runtime, "sync_calendar", side_effect=sync), \
                patch.object(runtime, "do_run", return_value=0) as run:
            self.assertEqual(runtime.tick(self.cfg), ["checkin"])
            self.assertEqual(run.call_args.args[1].requested_at, self.now)

    def test_atomic_config_preserves_unicode(self):
        self.cfg["date_overrides"]["2026-10-20"] = {"note": "대체휴무"}
        core.save_config(self.cfg)
        loaded = core.load_config()
        self.assertEqual(loaded["date_overrides"]["2026-10-20"]["note"], "대체휴무")


class SchedulerTest(unittest.TestCase):
    def test_registration_uses_watcher_and_logon_and_quoted_paths(self):
        with patch.object(scheduler, "_run_powershell", return_value=(0, "OK", "")) as run:
            scheduler.enable(config())
        source = run.call_args.args[0]
        self.assertIn("--watch", source)
        self.assertIn("-AtLogOn", source)
        self.assertIn("-Daily", source)
        self.assertNotIn("-Weekly", source)
        self.assertIn("[TimeSpan]::Zero", source)
        self.assertEqual(scheduler._ps_quote("a'b"), "'a''b'")

    def test_disable_stops_resident_worker(self):
        with patch.object(scheduler, "_run_powershell", return_value=(0, "OK", "")) as run:
            scheduler.disable()
        self.assertIn("Stop-ScheduledTask", run.call_args.args[0])

    def test_error_is_not_reported_as_enabled(self):
        with patch.object(scheduler, "_run_powershell", return_value=(1, "", "denied")):
            self.assertEqual(scheduler.status()["error"], "denied")
            with self.assertRaises(RuntimeError):
                scheduler.enable()


if __name__ == "__main__":
    unittest.main()
