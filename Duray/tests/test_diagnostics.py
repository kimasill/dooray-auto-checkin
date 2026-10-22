"""Exercise the shareable collector and verify its privacy boundary on Windows."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'Duray' / 'package' / 'collect-diagnostics.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows support collector')
class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        output = ROOT / 'test-output'
        output.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        (self.data / 'logs').mkdir(parents=True)
        self.output = self.root / 'result'

    def collect(self):
        proc = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(SCRIPT), '-DataDir', str(self.data),
                               '-OutputDir', str(self.output), '-SkipSystemInfo'],
                              capture_output=True, timeout=30, creationflags=0x08000000)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', errors='replace'))
        with zipfile.ZipFile(next(self.output.glob('*.zip'))) as bundle:
            self.assertIsNone(bundle.testzip())
            return {name: bundle.read(name).decode('utf-8-sig') for name in bundle.namelist()}

    def test_collects_evidence_without_credentials_sessions_or_notes(self):
        cfg = {'login_id': 'private.login', 'tenant_url': 'https://private-company.dooray.com',
               'password': 'fake-password-DO-NOT-SHARE', 'checkin_time': '09:55', 'checkin_enabled': True, 'workdays': [0,1,2,3,4],
               'random_before_seconds': 30, 'checkout_random_after_seconds': 15,
               'attendance_path': '/home?token=PRIVATE-QUERY',
               'dooray_calendar_path': '/private-person/PRIVATE-CALENDAR',
               'selectors': {'login_id': '#PRIVATE-SELECTOR'},
               'date_overrides': {'2026-10-21': {'kind': 'work', 'checkin_time': '09:30', 'note': 'PRIVATE-NOTE'}}}
        config_path = self.data / 'config.json'
        config_path.write_text(json.dumps(cfg), encoding='utf-8')
        before = config_path.read_bytes()
        (self.data / 'logs' / 'checkin.log').write_text(
            '2026-10-21 09:55:01 INFO 예약 실행\nprivate.login fake-password-DO-NOT-SHARE\n'
            'https://private-company.dooray.com/home?secret=anything\nAuthorization: Bearer private-secret\n'
            'Cookie: session=private-cookie\nother.person@example.com', encoding='utf-8')
        (self.data / 'profile').mkdir()
        (self.data / 'profile' / 'cookie.json').write_text('PRIVATE-COOKIE')
        (self.data / 'screenshots').mkdir()
        (self.data / 'screenshots' / 'screen.png').write_text('PRIVATE-IMAGE')
        (self.data / 'calendar.json').write_text(json.dumps({'month':'2026-10', 'days':{'private':'PRIVATE-LEAVE'},
                                                          'identity':'PRIVATE-IDENTITY'}), encoding='utf-8')
        (self.data / 'state.json').write_text(json.dumps({'last_checkin_error': 'password=private-secret\n확인 필요'}), encoding='utf-8')
        files = self.collect()
        for name, content in files.items():
            if name.endswith('.json'):
                json.loads(content)
        everything = '\n'.join(files.values())
        for secret in ('private.login', 'fake-password-DO-NOT-SHARE', 'private-company', 'private-secret',
                       'private-cookie', 'other.person@', 'PRIVATE-', 'secret=anything'):
            self.assertNotIn(secret, everything)
        settings = json.loads(files['schedule-settings.json'])
        self.assertEqual(settings['checkin_time'], '09:55')
        self.assertEqual(settings['random_before_seconds'], 30)
        self.assertEqual(settings['checkout_random_after_seconds'], 15)
        connection = json.loads(files['connection-settings.json'])
        self.assertEqual(connection['attendanceRoute'], 'home')
        self.assertEqual(connection['calendarRoute'], 'other')
        self.assertIs(connection['attendanceHasQuery'], True)
        self.assertIs(connection['customSelectors']['login_id'], True)
        self.assertIs(connection['customSelectors']['login_password'], False)
        self.assertEqual(json.loads(files['environment.json'])['collectorVersion'], 3)
        self.assertEqual(settings['workdays'], [0,1,2,3,4])
        self.assertIs(settings['checkin_enabled'], True)
        self.assertIs(json.loads(files['environment.json'])['dataFound'], True)
        self.assertEqual(settings['date_overrides']['2026-10-21']['checkin_time'], '09:30')
        self.assertNotIn('login_id', settings)
        self.assertIn('2026-10-21 09:55:01 INFO 예약 실행', files['checkin.log'])
        self.assertEqual(config_path.read_bytes(), before)
        self.assertNotIn('config.json', files)

    def test_missing_install_still_produces_diagnostic_warning(self):
        files = self.collect()
        self.assertFalse(json.loads(files['environment.json'])['dataFound'])
        self.assertIn('config.json', files['collection-warnings.json'])

    def test_broken_config_preserves_logs_without_copying_original(self):
        (self.data / 'config.json').write_text('{broken:PRIVATE-CONFIG}', encoding='utf-8')
        (self.data / 'logs' / 'checkin.log').write_text('2026-10-21 09:55 ERROR 설정 오류', encoding='utf-8')
        files = self.collect()
        self.assertIn('설정 오류', files['checkin.log'])
        self.assertNotIn('PRIVATE-CONFIG', '\n'.join(files.values()))


if __name__ == '__main__':
    unittest.main()
