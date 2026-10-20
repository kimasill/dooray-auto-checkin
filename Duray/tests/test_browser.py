"""Offline browser fixtures: every network request is fulfilled locally."""
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attendance_rules as rules
import attendance_runtime as runtime
import dooray_calendar as calendar
import dooray_checkin as core
from playwright.sync_api import BrowserType, sync_playwright

OUTPUT = Path(__file__).resolve().parents[2] / "test-output" / "browser"
BROWSER = os.environ.get("DOORAY_TEST_BROWSER", "")


@unittest.skipUnless(BROWSER and Path(BROWSER).is_file(), "Set DOORAY_TEST_BROWSER to an installed Chromium executable")
class BrowserTest(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg.update(tenant_url="https://fixture.dooray.com", login_id="fixture", timeout_ms=2000,
                        retry_count=1, retry_delay_seconds=0, notify_on_failure=False, checkout_enabled=True)
        for name, path in (("PROFILE_DIR", OUTPUT / "profile"), ("SHOT_DIR", OUTPUT / "screenshots"),
                           ("CALENDAR_PATH", OUTPUT / "calendar.json")):
            patcher = patch.object(core, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def html(self, in_time="-", out_time="-", out_label="퇴근하기", update=True):
        click = "this.parentElement.querySelector('span').textContent='18:00:00'" if update else "this.disabled=true"
        return f"""<!doctype html><meta charset='utf-8'><section>
          <div id='in'>출근 <span>{in_time}</span><button onclick="this.parentElement.querySelector('span').textContent='09:00:00'">출근하기</button></div>
          <div id='out'>퇴근 <span>{out_time}</span><button onclick="{click}">{out_label}</button></div>
          </section>"""

    def run_fixture(self, html, **kw):
        original = BrowserType.launch_persistent_context
        def launch(browser_type, **kwargs):
            kwargs.pop("channel", None)
            kwargs["executable_path"] = BROWSER
            kwargs["timeout"] = 15000
            context = original(browser_type, **kwargs)
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            return context
        with patch.object(BrowserType, "launch_persistent_context", launch):
            return runtime.run_once(self.cfg, dry_run=False, headed=False, inspect=False, force=True, **kw)

    def test_checkin_writes_correct_slot(self):
        self.assertEqual(self.run_fixture(self.html()), "출근 체크 성공")

    def test_checkout_does_not_confuse_checkin_timestamp(self):
        self.assertEqual(self.run_fixture(self.html(in_time="09:00:00"), action="checkout"), "퇴근 체크 성공")

    def test_checkout_requires_live_checkin(self):
        with self.assertRaisesRegex(RuntimeError, "출근 시각"):
            self.run_fixture(self.html(), action="checkout")

    def test_wrong_button_rejected(self):
        self.cfg["selectors"]["checkout_button"] = "#in button"
        with self.assertRaisesRegex(RuntimeError, "버튼 문구"):
            self.run_fixture(self.html(in_time="09:00:00"), action="checkout")

    def test_disabled_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "기록된 시각"):
            self.run_fixture(self.html(in_time="09:00:00", update=False), action="checkout")

    def test_calendar_dom_full_month_hidden_content_and_iframe(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=BROWSER, headless=True, timeout=15000)
            try:
                page = browser.new_page()
                days = rules.month_dates(2026, 10)
                cells = ''.join(f'<td data-date="{day}">{day.day}<div>{"오전 반차" if day.day == 20 else ""}</div></td>' for day in days)
                page.set_content('<h2>2026년 10월</h2><table><tr>' + cells + '</tr></table><div style="display:none" data-date="2026-10-21">연차</div>')
                result = calendar.read_month(page, self.cfg, days[0])
                self.assertEqual(result["2026-10-20"]["kind"], "am_half")
                self.assertEqual(result["2026-10-21"]["kind"], "work")
                page.set_content('<iframe></iframe>')
                frame = page.frames[1]
                frame.set_content('<h2>2026년 10월</h2><table><tr>' + cells.replace(' data-date=', ' data-unused=') + '</tr></table>')
                self.assertEqual(calendar.read_month(page, self.cfg, days[0])["2026-10-20"]["kind"], "am_half")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
