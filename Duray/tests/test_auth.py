"""Offline login regressions. All URLs are intercepted; no real credentials."""
import copy
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dooray_checkin as core
import dooray_auth as auth
import attendance_runtime as runtime
import attendance_rules as rules
from playwright.sync_api import sync_playwright, BrowserType

BROWSER = os.environ.get("DOORAY_TEST_BROWSER", "")
OUTPUT = Path(__file__).resolve().parents[2] / "test-output" / "auth"
OUTPUT.mkdir(parents=True, exist_ok=True)
SUCCESS = "<button onclick='window.mutations++'>출근하기</button><button>퇴근하기</button>"
FORM = """<input id="loginId"><input id="password" type="password">
<button type="submit" onclick="window.submits++; document.body.innerHTML = window.result">로그인</button>"""


class AuthRulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg.update(tenant_url="https://fixture.dooray.com", login_id="fixture")

    def test_profile_isolates_account_company_and_password_changes(self):
        original = auth.profile_path(self.cfg)
        for key, value in (("login_id", "another"), ("tenant_url", "https://other.dooray.com"), ("login_revision", "new")):
            cfg = {**self.cfg, key: value}
            self.assertNotEqual(original, auth.profile_path(cfg))
        self.assertNotEqual(str(original).find("fixture"), 0)

    def test_network_error_has_actionable_message_without_url(self):
        error = auth.translate_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://private.example"))
        self.assertEqual(error.code, "ADDRESS")
        self.assertIn("VPN", str(error))
        self.assertNotIn("private.example", str(error))

    def test_missing_password_is_explicit(self):
        with patch("keyring.get_password", return_value=None):
            with self.assertRaisesRegex(auth.ConnectionProblem, "PASSWORD_MISSING"):
                core.get_password("fixture")

    def test_company_address_must_not_include_page_path(self):
        with self.assertRaisesRegex(ValueError, "페이지 경로"):
            rules.validate_config({**self.cfg, "tenant_url": "https://fixture.dooray.com/home"})

    def test_route_diagnostics_never_include_private_path_or_query(self):
        self.assertEqual(auth.route_kind('https://private.dooray.com/home?token=secret#private'), 'home')
        self.assertEqual(auth.route_kind('/work-schedule/user/register-month?member=secret'), 'calendar')
        self.assertEqual(auth.route_kind('/login?return=secret'), 'login')
        self.assertEqual(auth.route_kind('/private-person/private-token'), 'other')

    def test_login_failure_blocks_background_until_rechecked_or_password_changed(self):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, patch.object(core, "PROFILE_DIR", Path(temp)):
            assert Path(temp).resolve().is_relative_to(OUTPUT.resolve())
            error = auth.ConnectionProblem("CREDENTIALS", "로그인 오류", "다시 입력")
            auth.remember_failure(self.cfg, error)
            with self.assertRaisesRegex(auth.ConnectionProblem, "자동 재시도를 중지"):
                auth.check_blocked(self.cfg)
            auth.check_blocked({**self.cfg, "login_revision": "new"})
            auth.clear_failure(self.cfg)
            auth.check_blocked(self.cfg)

    def test_old_password_notice_block_is_rechecked_but_new_expiry_stays_blocked(self):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, patch.object(core, "PROFILE_DIR", Path(temp)):
            assert Path(temp).resolve().is_relative_to(OUTPUT.resolve())
            core.atomic_json(auth.profile_path(self.cfg) / "connection-block.json",
                             {"code": "PASSWORD_EXPIRED", "message": "old reminder"})
            auth.check_blocked(self.cfg)
            auth.remember_failure(self.cfg, auth.ConnectionProblem("PASSWORD_EXPIRED", "필수 변경", "웹에서 변경"))
            with self.assertRaisesRegex(auth.ConnectionProblem, "PASSWORD_EXPIRED"):
                auth.check_blocked(self.cfg)

    def test_legacy_unconfirmed_block_recovers_but_confirmed_errors_remain_blocked(self):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, patch.object(core, 'PROFILE_DIR', Path(temp)):
            path = auth.profile_path(self.cfg) / 'connection-block.json'
            core.atomic_json(path, {'version': 2, 'code': 'LOGIN_UNCONFIRMED', 'message': 'old timeout'})
            auth.check_blocked(self.cfg)
            self.assertFalse(path.exists())
            for code in ('CREDENTIALS', 'ACCOUNT_LOCKED', 'PASSWORD_EXPIRED', 'EXTRA_AUTH', 'PASSWORD_MISSING'):
                auth.remember_failure(self.cfg, auth.ConnectionProblem(code, 'confirmed', 'fix account'))
                with self.assertRaises(auth.ConnectionProblem) as error:
                    auth.check_blocked(self.cfg)
                self.assertEqual(error.exception.code, code)
                auth.clear_failure(self.cfg)

    def test_unconfirmed_failure_is_not_persisted_as_permanent_block(self):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, patch.object(core, 'PROFILE_DIR', Path(temp)):
            auth.remember_failure(self.cfg, auth.ConnectionProblem('LOGIN_UNCONFIRMED', 'timeout', 'retry'))
            self.assertFalse((auth.profile_path(self.cfg) / 'connection-block.json').exists())
            auth.check_blocked(self.cfg)


@unittest.skipUnless(BROWSER and Path(BROWSER).is_file(), "Set DOORAY_TEST_BROWSER")
class LoginBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True, timeout=15000)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg.update(tenant_url="https://fixture.dooray.com", login_id="fixture", timeout_ms=2500)
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(1500)
        self.addCleanup(self.ctx.close)
        self.pw_mock = patch.object(core, "get_password", return_value="fixture-secret").start()
        self.addCleanup(patch.stopall)

    def load(self, body, script="", url="https://fixture.dooray.com" + rules.ATTENDANCE_PATH):
        html = f"<meta charset='utf-8'><body>{body}<script>window.mutations=0; window.submits=0; window.result={SUCCESS!r}; {script}</script></body>"
        self.ctx.route("**/*", lambda r: r.fulfill(body=html, content_type="text/html"))
        self.page.goto(url)

    def test_delayed_form_is_not_mistaken_for_existing_session(self):
        self.load("불러오는 중", f"setTimeout(()=>document.body.insertAdjacentHTML('afterbegin', {FORM!r}), 500)")
        auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)
        self.assertEqual(self.page.evaluate("window.mutations"), 0)
        self.pw_mock.assert_called_once()

    def test_completed_login_is_rechecked_when_diagnostics_cross_deadline(self):
        self.load(FORM)
        clock = [0.0]
        def slow_diagnostic(*args, **kwargs):
            clock[0] = 100.0
        with patch.object(auth, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(auth, 'log_page_state', side_effect=slow_diagnostic):
            auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate('window.submits'), 1)
        self.assertEqual(self.page.evaluate('window.mutations'), 0)

    def test_unconfirmed_timeout_is_retryable_without_reporting_success(self):
        self.load(FORM, "window.result='Loading'")
        clock = [0.0]
        with patch.object(auth, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(auth, 'log_page_state', side_effect=lambda *a, **k: clock.__setitem__(0, 100.0)):
            with self.assertRaises(auth.ConnectionProblem) as error:
                auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(error.exception.code, 'LOGIN_UNCONFIRMED')
        self.assertTrue(error.exception.retryable)
        self.assertEqual(self.page.evaluate('window.submits'), 1)

    def test_confirmed_password_error_at_deadline_still_stops_retry(self):
        self.load(FORM, "window.result='아이디 또는 비밀번호가 올바르지 않습니다.'")
        clock = [0.0]
        with patch.object(auth, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(auth, 'log_page_state', side_effect=lambda *a, **k: clock.__setitem__(0, 100.0)):
            with self.assertRaises(auth.ConnectionProblem) as error:
                auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(error.exception.code, 'CREDENTIALS')
        self.assertFalse(error.exception.retryable)

    def test_visible_duplicate_input_after_hidden_field(self):
        self.load('<input type="password" style="display:none">' + FORM)
        auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)

    def test_login_landing_on_mail_without_visible_logout_opens_attendance(self):
        visits=[]
        def route(request):
            path=request.request.url.split('fixture.dooray.com')[-1]
            visits.append(path)
            if path == '/login':
                body='<input id="loginId"><input type="password"><button type="submit" onclick="location.href=\'/mail\'">로그인</button>'
            elif path == rules.ATTENDANCE_PATH:
                body=SUCCESS
            else:
                body='<main><h1>메일</h1><input type="text" placeholder="검색"></main>'
            request.fulfill(body='<meta charset="utf-8">'+body,content_type='text/html')
        self.ctx.route('**/*',route)
        self.page.goto('https://fixture.dooray.com/login')
        auth.ensure_logged_in(self.page,self.cfg)
        self.assertEqual(self.page.url,'https://fixture.dooray.com'+rules.ATTENDANCE_PATH)
        self.assertEqual(visits.count(rules.ATTENDANCE_PATH),1)
        self.pw_mock.assert_called_once()

    def test_manual_logged_in_project_page_recovers_without_typing_or_clicking(self):
        visits=[]
        self.ctx.route('**/*',lambda r: (visits.append(r.request.url),r.fulfill(content_type='text/html; charset=utf-8',
            body=SUCCESS if r.request.url.endswith(rules.ATTENDANCE_PATH) else '<h1>Project</h1><input type="text">')))
        self.page.goto('https://fixture.dooray.com/project')
        cancel=threading.Event()
        timer=threading.Timer(8,cancel.set); timer.start()
        try:
            auth.ensure_logged_in(self.page,self.cfg,manual=True,cancel=cancel)
        finally:
            timer.cancel()
        self.assertEqual(self.page.url,'https://fixture.dooray.com'+rules.ATTENDANCE_PATH)
        self.assertEqual(len(visits),2)
        self.pw_mock.assert_not_called()

    def test_destination_probe_is_not_login_proof_and_does_not_loop(self):
        self.cfg['timeout_ms']=1100
        requests=[]
        self.ctx.route('**/*',lambda r:(requests.append(r.request.url),r.fulfill(body='Loading',content_type='text/html')))
        self.page.goto('https://fixture.dooray.com/project')
        with self.assertRaisesRegex(auth.ConnectionProblem,'ATTENDANCE_PAGE'):
            auth.ensure_logged_in(self.page,self.cfg)
        self.assertEqual(len(requests),2)
        self.pw_mock.assert_not_called()

    def test_manual_otp_screen_is_not_interrupted_by_destination_probe(self):
        self.load('<p>인증번호를 입력하세요</p><input autocomplete="one-time-code">',
                  f'setTimeout(()=>document.body.innerHTML={SUCCESS!r}, 1300)',
                  url='https://fixture.dooray.com/security')
        auth.ensure_logged_in(self.page,self.cfg,manual=True)
        self.assertEqual(self.page.url,'https://fixture.dooray.com/security')
        self.pw_mock.assert_not_called()

    def test_id_first_then_password(self):
        self.load("<input id='loginId'><button onclick=\"document.body.innerHTML=window.second\">다음</button>",
                  f"window.second={FORM.replace('<input id=\"loginId\">', '')!r}")
        auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)

    def test_unsubmitted_form_does_not_blame_credentials(self):
        self.load('<input id="loginId"><button>도움말</button>')
        with self.assertRaisesRegex(auth.ConnectionProblem, '아직 전송되지 않았습니다') as error:
            auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(error.exception.code, 'LOGIN_FORM')
        self.pw_mock.assert_not_called()

    def test_page_state_is_only_structure_without_account_or_page_contents(self):
        self.load(FORM + '<div>PRIVATE-PAGE-BODY</div>', url='https://fixture.dooray.com/login?token=PRIVATE-TOKEN')
        self.page.locator('#loginId').fill('PRIVATE-LOGIN')
        self.page.locator('#password').fill('PRIVATE-PASSWORD')
        with self.assertLogs(core.log, level='INFO') as logs:
            auth.log_page_state(self.page, self.cfg, 'connection_failed', submitted=True)
        text='\n'.join(logs.output)
        self.assertIn('"route": "login"',text)
        self.assertIn('"pw": true',text)
        self.assertIn('"submitted": true',text)
        for secret in ('PRIVATE-', 'fixture.dooray.com', '?token=', 'loginId', '#password'):
            self.assertNotIn(secret,text)

    def test_wrong_password_is_specific_and_submitted_only_once(self):
        self.load(FORM, "window.result='아이디 또는 비밀번호가 올바르지 않습니다.'")
        with self.assertRaisesRegex(auth.ConnectionProblem, "CREDENTIALS"):
            auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)

    def test_otp_requires_user_and_does_not_submit_credentials(self):
        self.load('<p>인증번호를 입력하세요</p><input autocomplete="one-time-code">')
        with self.assertRaisesRegex(auth.ConnectionProblem, "EXTRA_AUTH"):
            auth.ensure_logged_in(self.page, self.cfg)
        self.pw_mock.assert_not_called()

    def test_otp_help_link_is_not_a_blocker(self):
        self.load('<a>OTP 도움말 / 비밀번호를 확인하세요</a>' + FORM)
        auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)

    def test_blank_page_never_reports_login_success(self):
        self.load("불러오는 중")
        with self.assertRaisesRegex(auth.ConnectionProblem, "PAGE_UNCONFIRMED"):
            auth.ensure_logged_in(self.page, self.cfg)

    def test_external_sso_does_not_receive_saved_password(self):
        self.load(FORM, url="https://sso.example.test/login")
        with self.assertRaisesRegex(auth.ConnectionProblem, "SSO"):
            auth.ensure_logged_in(self.page, self.cfg)
        self.pw_mock.assert_not_called()

    def test_manual_auth_waits_for_completion_without_auto_typing(self):
        self.load('<p>인증번호를 입력하세요</p><input autocomplete="one-time-code">',
                  f"setTimeout(()=>document.body.innerHTML={SUCCESS!r}, 400)")
        auth.ensure_logged_in(self.page, self.cfg, manual=True)
        self.pw_mock.assert_not_called()
        self.assertEqual(self.page.evaluate("window.mutations"), 0)

    def test_manual_login_can_be_cancelled(self):
        self.load(FORM)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(auth.ConnectionProblem, "CANCELLED"):
            auth.ensure_logged_in(self.page, self.cfg, manual=True, cancel=cancel)
        self.pw_mock.assert_not_called()

    def test_optional_90_day_reminder_can_continue_without_changing_password(self):
        self.load("<p>비밀번호를 변경해 주세요</p><p>마지막으로 비밀번호를 변경하신지 90일이 지났습니다.</p>"
                  "<input type='password' autocomplete='new-password'>"
                  "<button onclick=\"window.submits++; setTimeout(()=>document.body.innerHTML=window.result, 600)\">다음에 변경하기</button>")
        auth.ensure_logged_in(self.page, self.cfg)
        self.assertEqual(self.page.evaluate("window.submits"), 1)
        self.assertEqual(self.page.evaluate("window.mutations"), 0)
        self.pw_mock.assert_not_called()

    def test_mandatory_password_change_still_stops(self):
        self.load("<p>비밀번호가 만료되었습니다. 비밀번호를 변경해 주세요</p>"
                  "<input type='password' autocomplete='new-password'><button>변경하기</button>")
        with self.assertRaisesRegex(auth.ConnectionProblem, "PASSWORD_EXPIRED"):
            auth.ensure_logged_in(self.page, self.cfg)
        self.pw_mock.assert_not_called()


@unittest.skipUnless(BROWSER and Path(BROWSER).is_file(), "Set DOORAY_TEST_BROWSER")
class ConnectionBrowserTest(unittest.TestCase):
    def test_connection_verifies_calendar_without_attendance_mutation(self):
        cfg = {**copy.deepcopy(core.DEFAULT_CONFIG), "tenant_url": "https://fixture.dooray.com",
               "login_id": "fixture", "timeout_ms": 2000}
        days = rules.month_dates(rules.now_kst().year, rules.now_kst().month)
        calendar = "".join(f'<div data-date="{d}">{d.day}</div>' for d in days)
        mutations = []
        launch = BrowserType.launch_persistent_context
        def fixture(browser_type, **kw):
            kw.pop("channel", None)
            ctx = launch(browser_type, executable_path=BROWSER, timeout=15000, **kw)
            ctx.expose_function("mutation", lambda: mutations.append(True))
            ctx.route("**/*", lambda r: r.fulfill(content_type="text/html; charset=utf-8", body=calendar +
                      "<button onclick='mutation()'>출근하기</button><button onclick='mutation()'>퇴근하기</button>"))
            return ctx
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temp, patch.object(core, "PROFILE_DIR", Path(temp) / "profile"), \
                patch.object(core, "CALENDAR_PATH", Path(temp) / "calendar.json"), \
                patch.object(BrowserType, "launch_persistent_context", fixture):
            assert Path(temp).resolve().is_relative_to(OUTPUT.resolve())
            progress=[]
            with self.assertLogs(core.log, level='INFO') as logs:
                result = runtime.run_once(cfg, dry_run=True, headed=False, inspect=False, connection_only=True,
                                          progress=progress.append)
        self.assertTrue(result["ok"])
        self.assertEqual(mutations, [])
        self.assertTrue(progress)
        recorded='\n'.join(logs.output)
        for message in progress:
            self.assertIn(message, recorded)
        self.assertIn('두레이 첫 응답: HTTP 200',recorded)
        self.assertIn('출퇴근 경로=calendar',recorded)


if __name__ == "__main__":
    unittest.main()
