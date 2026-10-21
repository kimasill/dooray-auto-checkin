"""Registration and status probes must never share PowerShell scripts."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scheduler


class PowerShellConcurrencyTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'test-output'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='ps-concurrency-', dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        temp_patch = patch.object(tempfile, 'tempdir', str(self.folder))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def test_overlapping_registration_and_status_use_their_own_script(self):
        barrier = threading.Barrier(2)
        paths = []
        def run(args, **kwargs):
            path = Path(args[args.index('-File') + 1])
            paths.append(path)
            barrier.wait(timeout=5)
            body = path.read_text(encoding='utf-8-sig')
            # Both processes finish reading before either caller deletes its file.
            barrier.wait(timeout=5)
            return subprocess.CompletedProcess(args, 0, body.splitlines()[-1], '')
        with patch.object(scheduler.subprocess, 'run', side_effect=run), ThreadPoolExecutor(2) as pool:
            first = pool.submit(scheduler._run_powershell, "'REGISTER_OK'")
            second = pool.submit(scheduler._run_powershell, "'STATUS_JSON'")
            self.assertEqual(first.result()[1], "'REGISTER_OK'")
            self.assertEqual(second.result()[1], "'STATUS_JSON'")
        self.assertEqual(len(set(paths)), 2)
        self.assertFalse(list(self.folder.glob('*.ps1')))

    def test_timeout_and_spawn_failure_remove_their_temporary_file(self):
        for error in (subprocess.TimeoutExpired('powershell', 1), OSError('spawn failed')):
            with self.subTest(error=type(error).__name__), patch.object(scheduler.subprocess, 'run', side_effect=error):
                with self.assertRaises(type(error)):
                    scheduler._run_powershell("'fixture'")
                self.assertFalse(list(self.folder.glob('*.ps1')))

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration')
    def test_real_concurrent_processes_keep_unique_unicode_output(self):
        # These scripts only print text; they never change Windows tasks.
        labels = [f'출근 예약 확인 {index}' for index in range(8)]
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda label: scheduler._run_powershell(
                f"Start-Sleep -Milliseconds 100\nWrite-Output '{label}'"), labels))
        self.assertEqual([item[0] for item in results], [0] * len(labels))
        self.assertEqual([item[1] for item in results], labels)
        self.assertFalse(list(self.folder.glob('*.ps1')))


if __name__ == '__main__':
    unittest.main()
