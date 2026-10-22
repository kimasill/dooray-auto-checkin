"""Old /home settings must reach the page that actually contains attendance."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dooray_checkin as core
import attendance_rules as rules
import attendance_runtime as runtime
from playwright.sync_api import BrowserType

PATH = '/work-schedule/user/register-month'
BROWSER = os.environ.get('DOORAY_TEST_BROWSER', '')
OUTPUT = Path(__file__).resolve().parents[2] / 'test-output/legacy-path'


class LegacySettingsTest(unittest.TestCase):
    def test_loading_old_file_corrects_only_home_without_rewriting_file(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for path in ('/home', '/home/'):
            with self.subTest(path=path), tempfile.TemporaryDirectory(dir=OUTPUT) as temp:
                config_path = Path(temp) / 'config.json'
                old = copy.deepcopy(core.DEFAULT_CONFIG)
                old.update(attendance_path=path, login_id='fixture', checkin_time='09:55',
                           checkout_time='19:00', time_mode='random', random_before_minutes=5,
                           random_after_minutes=5, checkout_enabled=True,
                           date_overrides={'2026-10-23': {'kind': 'vacation'}})
                config_path.write_text(json.dumps(old), encoding='utf-8')
                before = config_path.read_bytes()
                with patch.object(core, 'CONFIG_PATH', config_path):
                    loaded = core.load_config(validate=False)
                self.assertEqual(loaded['attendance_path'], PATH)
                self.assertEqual(config_path.read_bytes(), before)
                for key, value in old.items():
                    if key != 'attendance_path':
                        self.assertEqual(loaded[key], value, key)
                for action in rules.ACTIONS:
                    day = dt.date(2026, 10, 22)
                    self.assertEqual(rules.plan_action(old, day, action).target,
                                     rules.plan_action(loaded, day, action).target)

    def test_explicit_custom_paths_and_calendar_settings_are_preserved(self):
        for path in (PATH, '/custom-attendance', '/home?custom=1', '/homepage'):
            cfg = rules.normalize_config({'attendance_path': path, 'dooray_calendar_path': '/custom-calendar'})
            self.assertEqual(cfg['attendance_path'], path)
            self.assertEqual(cfg['dooray_calendar_path'], '/custom-calendar')

    def test_new_install_template_and_defaults_use_attendance_page(self):
        template = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(core.DEFAULT_CONFIG['attendance_path'], PATH)
        self.assertEqual(template['attendance_path'], PATH)


@unittest.skipUnless(BROWSER and Path(BROWSER).is_file(), 'Set DOORAY_TEST_BROWSER')
class LegacyBrowserTest(unittest.TestCase):
    def run_legacy(self, *, manual=False, action=None):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        cfg.update(tenant_url='https://fixture.dooray.com', login_id='fixture', attendance_path='/home',
                   timeout_ms=1500, dooray_sync_enabled=True, checkout_enabled=True,
                   checkin_time='09:00', checkout_time='18:00')
        now = dt.datetime(2026, 10, 22, 9 if action != 'checkout' else 18, 0, 1, tzinfo=rules.KST)
        cells = ''.join(f'<div data-date="{d}">{d.day}</div>' for d in rules.month_dates(2026, 10))
        buttons = ('<div>출근 <span>' + ('09:00:00' if action == 'checkout' else '-') + '</span>'
                   '<button onclick="mutation(\'checkin\');this.previousElementSibling.textContent=\'09:00:01\'">출근하기</button></div>'
                   '<div>퇴근 <span>-</span><button onclick="mutation(\'checkout\');this.previousElementSibling.textContent=\'18:00:01\'">퇴근하기</button></div>')
        visits, mutations = [], []
        launch = BrowserType.launch_persistent_context

        def fixture(browser_type, **kw):
            kw.pop('channel', None)
            kw['headless'] = True
            ctx = launch(browser_type, executable_path=BROWSER, timeout=15000, **kw)
            ctx.expose_function('mutation', lambda action: mutations.append(action))

            def respond(route):
                visits.append(route.request.url)
                body = cells + buttons if route.request.url.endswith(PATH) else '<h1>홈</h1>'
                route.fulfill(content_type='text/html; charset=utf-8', body=body)
            ctx.route('**/*', respond)
            return ctx

        cancel = threading.Event()
        timer = threading.Timer(8, cancel.set)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, \
                patch.object(core, 'PROFILE_DIR', Path(temp) / 'profile'), \
                patch.object(core, 'CALENDAR_PATH', Path(temp) / 'calendar.json'), \
                patch.object(core, 'SHOT_DIR', Path(temp) / 'screenshots'), \
                patch.object(core, 'load_config', return_value=cfg), \
                patch.object(rules, 'now_kst', return_value=now), \
                patch.object(BrowserType, 'launch_persistent_context', fixture):
            timer.start()
            try:
                result = runtime.run_once(cfg, dry_run=action is None, headed=False, inspect=False,
                                          connection_only=action is None, manual_login=manual,
                                          action=action or 'checkin', scheduled=action is not None,
                                          requested_at=now, cancel=cancel)
            finally:
                timer.cancel()
        self.assertEqual(cfg['attendance_path'], '/home')
        self.assertTrue(visits)
        self.assertTrue(all(url.endswith(PATH) for url in visits), visits)
        self.assertEqual(mutations, [action] if action else [])
        return result

    def test_saved_home_connection_reaches_buttons_and_calendar(self):
        self.assertTrue(self.run_legacy()['ok'])

    def test_manual_connection_with_saved_home_reaches_attendance(self):
        self.assertTrue(self.run_legacy(manual=True)['ok'])

    def test_scheduled_checkin_with_saved_home_uses_attendance_page(self):
        self.assertEqual(self.run_legacy(action='checkin'), '출근 체크 성공')

    def test_scheduled_checkout_with_saved_home_uses_attendance_page(self):
        self.assertEqual(self.run_legacy(action='checkout'), '퇴근 체크 성공')


if __name__ == '__main__':
    unittest.main()
