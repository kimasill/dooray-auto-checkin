"""Desktop smoke tests with isolated files and mocked Windows scheduling."""
import copy
import json
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dooray_checkin as core
import desktop_ui
import scheduler
import attendance_rules as rules
from ui_widgets import day_display


class DesktopTest(unittest.TestCase):
    def setUp(self):
        output = Path(__file__).resolve().parents[2] / "test-output"
        output.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.base = Path(self.temp.name)
        assert self.base.resolve().is_relative_to(output.resolve())
        self.patches = []
        for name, value in (("CONFIG_PATH", self.base / "config.json"), ("DATA_DIR", self.base),
                            ("CALENDAR_PATH", self.base / "calendar.json"), ("LOG_DIR", self.base / "logs")):
            self.patches.append(patch.object(core, name, value))
        self.patches.append(patch.object(scheduler, "status", return_value={"exists": False}))
        self.patches.append(patch.object(scheduler, "enable"))
        self.patches.append(patch.object(desktop_ui.App, "auto_sync"))
        for p in self.patches:
            p.start()
        cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        cfg.update(login_id="fixture", tenant_url="https://fixture.dooray.com")
        core.save_config(cfg)
        self.root = desktop_ui.build_app()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.app = self.root.app
        self.root.update()

    def tearDown(self):
        self.wait()
        self.app.close()
        for handler in core.log.handlers:
            handler.close()
        core.log.handlers.clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def wait(self):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            self.root.update()
            if not self.app.busy and not self.app.status_pending and self.app.events.empty():
                break
            time.sleep(.02)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.errors, [])

    def test_all_pages_and_resizing(self):
        for size in ("1180x800", "1440x900"):
            self.root.geometry(size)
            for name in self.app.pages:
                self.app.show(name)
                self.root.update()
        self.assertEqual(self.errors, [])

    def test_time_picker_saves_selected_values_and_preserves_seconds(self):
        self.app.vars["checkin_time"].set("10:17:42")
        picker = self.app.time_pickers["checkin_time"]
        self.assertEqual([p.get() for p in picker.parts], ["10", "17", "42"])
        picker.parts[0].set("09")
        picker.parts[1].set("35")
        picker.commit()
        self.app.save()
        self.wait()
        self.assertEqual(core.load_config()["checkin_time"], "09:35:42")

    def test_date_time_can_inherit_or_use_dropdown(self):
        picker = self.app.time_pickers["day_checkout_time"]
        self.assertTrue(picker.inherit.get())
        picker.inherit.set(False)
        picker.toggle_inherit()
        picker.parts[0].set("15")
        picker.parts[1].set("20")
        picker.commit()
        self.assertEqual(self.app.day_vars["checkout_time"].get(), "15:20")
        picker.inherit.set(True)
        picker.toggle_inherit()
        self.assertEqual(self.app.day_vars["checkout_time"].get(), "")

    def test_remote_leave_visible_when_automation_integration_off(self):
        day = self.app.selected
        cache = {"days": {day.isoformat(): {"kind": "pm_half", "label": "오후 반차"}}}
        self.app.cfg["dooray_sync_enabled"] = False
        self.app.cache = cache
        self.root.deiconify()
        self.root.update()
        self.app.render_calendar()
        view = day_display(self.app.cfg, day, cache)
        self.assertEqual(view["events"][0]["label"], "오후 반차")
        self.assertIn("예약 반영 꺼짐", self.app.remote_detail.cget("text"))
        self.assertIn("오후 반차", [self.app.calendar_grid.itemcget(i, "text") for i in self.app.calendar_grid.find_all() if self.app.calendar_grid.type(i) == "text"])
        self.app.cfg["dooray_sync_enabled"] = True
        self.app.cfg["checkout_enabled"] = True
        self.app.cfg["pm_half_checkout_time"] = "14:30"
        view = day_display(self.app.cfg, day, cache)
        self.assertEqual(view["plans"]["checkout"].target, rules.seconds("14:30"))

    def test_sync_preserves_pending_edits_and_does_not_rewrite_config(self):
        original = core.CONFIG_PATH.read_bytes()
        self.app.day_vars["note"].set("아직 저장하지 않은 메모")
        self.app.vars["checkin_time"].set("08:30")
        cache = {"days": {self.app.selected.isoformat(): {"kind": "vacation", "label": "연차"}}}
        with patch.object(desktop_ui.runtime, "sync_calendar", return_value=cache):
            self.app.sync()
            self.wait()
        self.assertEqual(self.app.day_vars["note"].get(), "아직 저장하지 않은 메모")
        self.assertEqual(self.app.vars["checkin_time"].get(), "08:30")
        self.assertEqual(core.CONFIG_PATH.read_bytes(), original)
        self.assertIn("연차", self.app.remote_detail.cget("text"))

    def test_calendar_click_and_keyboard_change_selected_date(self):
        self.app.calendar_grid.navigate(1)
        self.assertEqual(self.app.selected, self.app.today + desktop_ui.dt.timedelta(days=1))
        self.app.calendar_grid.navigate(-7)
        self.assertEqual(self.app.selected, self.app.today - desktop_ui.dt.timedelta(days=6))

    def test_manual_override_remains_visible_beside_remote_leave(self):
        day = self.app.selected
        cfg = copy.deepcopy(self.app.cfg)
        cfg["date_overrides"][day.isoformat()] = {"kind": "work"}
        view = day_display(cfg, day, {"days": {day.isoformat(): {"kind": "vacation", "label": "연차"}}})
        self.assertEqual([e["source"] for e in view["events"]], ["Dooray", "직접 설정"])
        self.assertEqual(view["kind"], "work")

    def test_save_half_day_checkout_and_random_bounds(self):
        self.app.vars["checkout_enabled"].set(True)
        self.app.vars["am_half_checkout_time"].set("17:30")
        self.app.vars["pm_half_checkout_time"].set("15:00")
        self.app.vars["random_before_minutes"].set("45")
        self.app.vars["random_after_minutes"].set("2")
        self.app.save()
        self.wait()
        saved = core.load_config()
        self.assertEqual(saved["am_half_checkout_time"], "17:30")
        self.assertEqual(saved["pm_half_checkout_time"], "15:00")
        self.assertEqual(saved["random_before_minutes"], 45)
        self.assertEqual(saved["half_day_policy"], "checkout_time")
        scheduler.enable.assert_not_called()

    def test_calendar_save_and_reset(self):
        self.app.day_vars["kind"].set("오후 반차")
        self.app.day_vars["checkout_time"].set("15:20")
        self.app.day_vars["note"].set("반차 일정")
        self.app.save_day()
        self.wait()
        key = self.app.selected.isoformat()
        self.assertEqual(core.load_config()["date_overrides"][key]["checkout_time"], "15:20")
        self.app.reset_day()
        self.wait()
        self.assertNotIn(key, core.load_config()["date_overrides"])

    def test_bad_date_time_shows_error_without_saving(self):
        original = core.CONFIG_PATH.read_text(encoding="utf-8")
        self.app.day_vars["checkout_time"].set("25:80")
        with patch.object(desktop_ui.messagebox, "showerror") as error:
            self.app.save_day()
            error.assert_called_once()
        self.assertEqual(core.CONFIG_PATH.read_text(encoding="utf-8"), original)

    def test_connect_saves_current_credentials_before_check(self):
        self.app.vars["login_id"].set("new-fixture")
        self.app.vars["password"].set("new-fixture-password")
        def verify(cfg, **kwargs):
            self.assertEqual(core.load_config()["login_id"], "new-fixture")
            self.assertEqual(cfg["login_id"], "new-fixture")
            self.assertTrue(cfg["login_revision"])
            return {"ok": True, "message": "연결 완료"}
        with patch("keyring.set_password") as saved, \
                patch.object(desktop_ui.runtime, "check_connection", side_effect=verify):
            self.app.connect()
            self.wait()
            saved.assert_called_once_with(core.KEYRING_SERVICE, "new-fixture", "new-fixture-password")
        self.assertEqual(self.app.vars["password"].get(), "")
        self.assertEqual(self.app.connection_status.cget("text"), "연결 완료")

    def test_sync_does_not_silently_use_old_account(self):
        self.app.vars["password"].set("not-saved")
        with patch.object(desktop_ui.runtime, "sync_calendar") as sync:
            self.app.sync()
            sync.assert_not_called()
        self.assertEqual(self.app.current, "settings")
        self.assertIn("저장", self.app.connection_status.cget("text"))

    def test_connection_failure_remains_visible_on_settings(self):
        with patch.object(desktop_ui.runtime, "check_connection", side_effect=RuntimeError("비밀번호 오류 [CREDENTIALS]")), \
                patch.object(desktop_ui.messagebox, "showerror"):
            self.app.connect()
            self.wait()
        self.assertIn("CREDENTIALS", self.app.connection_status.cget("text"))
        self.assertEqual(str(self.app.cancel_connection.cget("state")), "disabled")

    def test_partial_connection_is_not_shown_as_full_success(self):
        with patch.object(desktop_ui.runtime, "check_connection", return_value={"ok": False, "message": "로그인 완료 / 근태 조회 실패"}):
            self.app.connect()
            self.wait()
        self.assertIn("근태 조회 실패", self.app.connection_status.cget("text"))
        self.assertEqual(self.app.connection_status.cget("fg"), "#B25545")


if __name__ == "__main__":
    unittest.main()
