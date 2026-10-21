import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'test-output'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            for name in updater.PACKAGE_FILES:
                z.writestr(name, b'fixture')
        self.package = stream.getvalue()
        self.release = self.release_for(self.package)

    def release_for(self, package):
        prefix = 'https://github.com/kimasill/dooray-auto-checkin/releases/download/v2.1.3/'
        return updater.Release('2.1.3', prefix+'DoorayAutoCheckin-v2.1.3-windows.zip',
                               prefix+'SHA256SUMS-v2.1.3.txt', len(package), 'sha256:'+hashlib.sha256(package).hexdigest())

    def manifest(self, release=None):
        r = release or self.release
        return f'{r.digest[7:]}  {r.filename}\n'.encode()

    def test_verifies_package_and_only_extracts_allowed_files(self):
        progress = []
        with patch.object(updater, '_read', return_value=self.manifest()), \
                patch.object(updater, '_open', return_value=io.BytesIO(self.package)):
            folder = updater.prepare_update(self.release, self.root, progress.append)
        self.assertTrue((folder/'install.ps1').is_file())
        self.assertEqual(set(p.name for p in folder.iterdir()), updater.PACKAGE_FILES | {self.release.filename})
        self.assertIn('100%', progress[-2])

    def test_tampered_or_truncated_download_never_leaves_installer(self):
        for body in (self.package[:-1], self.package[:-1]+b'x', self.package+b'x'):
            with self.subTest(size=len(body)), patch.object(updater, '_read', return_value=self.manifest()), \
                    patch.object(updater, '_open', return_value=io.BytesIO(body)):
                with self.assertRaises(RuntimeError):
                    updater.prepare_update(self.release, self.root)
        self.assertEqual(list(self.root.rglob('install.ps1')), [])

    def test_manifest_disagreement_blocks_download(self):
        with patch.object(updater, '_read', return_value=b'0'*64+b'  '+self.release.filename.encode()), \
                patch.object(updater, '_open') as download:
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                updater.prepare_update(self.release, self.root)
            download.assert_not_called()

    def test_paths_duplicates_and_extra_files_rejected_even_with_matching_hash(self):
        for names in (['../install.ps1'], ['install.ps1']*4, list(updater.PACKAGE_FILES)+['config.json']):
            with self.subTest(names=names):
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w') as z:
                    for name in names:
                        z.writestr(name, b'fixture')
                body=stream.getvalue()
                r=self.release_for(body)
                with patch.object(updater,'_read',return_value=self.manifest(r)), \
                        patch.object(updater,'_open',return_value=io.BytesIO(body)):
                    with self.assertRaisesRegex(RuntimeError,'파일 구성'):
                        updater.prepare_update(r,self.root)
        self.assertFalse((self.root/'install.ps1').exists())

    def test_foreign_repository_is_rejected(self):
        r=updater.Release('2.1.3','https://example.com/file.zip',self.release.checksum_url,1,self.release.digest)
        with patch.object(updater,'_read') as read:
            with self.assertRaisesRegex(RuntimeError,'공식 저장소'):
                updater.prepare_update(r,self.root)
            read.assert_not_called()

    def test_version_comparison_and_latest_release_assets(self):
        r=self.release
        data={'tag_name':'v2.1.3','draft':False,'prerelease':False,'assets':[
            {'name':r.filename,'state':'uploaded','browser_download_url':r.package_url,'size':r.size,'digest':r.digest},
            {'name':'SHA256SUMS-v2.1.3.txt','state':'uploaded','browser_download_url':r.checksum_url}]}
        with patch.object(updater,'_read',return_value=json.dumps(data).encode()):
            self.assertEqual(updater.check_latest('2.1.2'),r)
            self.assertIsNone(updater.check_latest('2.1.3'))
            self.assertIsNone(updater.check_latest('2.1.10'))
        self.assertGreater(updater.version_tuple('2.1.10'),updater.version_tuple('2.1.9'))

    def test_network_failure_never_launches_installer(self):
        with patch.object(updater, '_read', side_effect=TimeoutError('offline')), \
                patch.object(updater, 'launch_installer') as install:
            with self.assertRaises(TimeoutError):
                updater.check_latest()
            install.assert_not_called()

    def test_installer_uses_existing_preserving_install_flow_and_log(self):
        (self.root/'install.ps1').write_text('fixture')
        with patch.object(updater.subprocess,'Popen') as run:
            updater.launch_installer(self.root)
            args=run.call_args.args[0]
            self.assertIn('-NonInteractive',args)
            self.assertEqual(args[args.index('-File')+1],str(self.root/'install.ps1'))
            self.assertEqual(run.call_args.kwargs['creationflags'],0x08000000)
        self.assertTrue((self.root/'install.log').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows image-file locking')
    def test_installer_retries_until_windows_releases_binary(self):
        import ctypes
        import subprocess
        import threading
        src, dst = self.root/'source.exe', self.root/'destination.exe'
        src.write_bytes(b'new verified binary')
        dst.write_bytes(b'previous binary')
        kernel = ctypes.windll.kernel32
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,
                                      ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(dst),0x80000000,1,None,3,0x80,None)
        self.assertNotIn(handle,(None,ctypes.c_void_p(-1).value))
        script = self.root/'retry-test.ps1'
        script.write_text('''param($Install,$Source,$Destination)
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Install,[ref]$null,[ref]$null)
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Copy-BinaryWithRetry'},$true)
Invoke-Expression $fn.Extent.Text
Copy-BinaryWithRetry $Source $Destination
''',encoding='utf-8-sig')
        timer=threading.Timer(2,lambda:kernel.CloseHandle(handle))
        timer.start()
        try:
            proc=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                                 str(Path(__file__).resolve().parents[1]/'package/install.ps1'),str(src),str(dst)],
                                capture_output=True,timeout=30,creationflags=0x08000000)
            self.assertEqual(proc.returncode,0,proc.stderr.decode(errors='replace'))
            self.assertEqual(dst.read_bytes(),src.read_bytes())
        finally:
            timer.join()


if __name__ == '__main__':
    unittest.main()
