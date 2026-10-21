"""User-triggered updates from this project's GitHub releases only."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request
from urllib.parse import urlparse
import uuid
import zipfile

from app_version import VERSION

REPOSITORY = "kimasill/dooray-auto-checkin"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_DOWNLOAD = 200 * 1024 * 1024
PACKAGE_FILES = {"DoorayAutoCheckin.exe", "install.ps1", "설치.bat", "읽어보기.txt"}


def version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("지원하지 않는 버전 표기입니다.")
    return tuple(map(int, value.lstrip("v").split(".")))


@dataclass(frozen=True)
class Release:
    version: str
    package_url: str
    checksum_url: str
    size: int
    digest: str

    @property
    def filename(self):
        return f"DoorayAutoCheckin-v{self.version}-windows.zip"


def _open(url):
    request = urllib.request.Request(url, headers={"User-Agent": "DoorayAutoCheckin/" + VERSION,
                                                   "Accept": "application/vnd.github+json" if url == API else "application/octet-stream"})
    response = urllib.request.urlopen(request, timeout=30)
    parsed = urlparse(response.geturl())
    if parsed.scheme != "https" or parsed.hostname not in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        response.close()
        raise RuntimeError("신뢰할 수 없는 업데이트 다운로드 주소입니다.")
    return response


def _read(url, limit):
    with _open(url) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise RuntimeError("업데이트 응답의 크기가 허용 범위를 넘었습니다.")
    return value


def validate_release(release):
    version_tuple(release.version)
    prefix = f"https://github.com/{REPOSITORY}/releases/download/v{release.version}/"
    if release.package_url != prefix + release.filename or release.checksum_url != prefix + f"SHA256SUMS-v{release.version}.txt":
        raise RuntimeError("공식 저장소의 업데이트 파일 주소가 아닙니다.")
    if not 0 < release.size <= MAX_DOWNLOAD or not re.fullmatch(r"sha256:[0-9a-f]{64}", release.digest):
        raise RuntimeError("배포 파일의 크기 또는 GitHub 무결성 정보를 확인할 수 없습니다.")


def check_latest(current=VERSION):
    data = json.loads(_read(API, 1024 * 1024))
    if data.get("draft") or data.get("prerelease"):
        raise RuntimeError("정식 배포 버전을 확인할 수 없습니다.")
    version = str(data.get("tag_name", "")).lstrip("v")
    if version_tuple(version) <= version_tuple(current):
        return None
    assets = {item["name"]: item for item in data.get("assets", [])}
    try:
        package = assets[f"DoorayAutoCheckin-v{version}-windows.zip"]
        checksum = assets[f"SHA256SUMS-v{version}.txt"]
        if package.get("state") != "uploaded" or checksum.get("state") != "uploaded":
            raise KeyError("incomplete upload")
        release = Release(version, package["browser_download_url"], checksum["browser_download_url"],
                          package["size"], package.get("digest", ""))
        validate_release(release)
        return release
    except (KeyError, TypeError) as exc:
        raise RuntimeError("최신 버전의 Windows 설치 파일이 아직 준비되지 않았습니다. 잠시 후 다시 확인하세요.") from exc


def prepare_update(release, destination: Path, progress=lambda message: None):
    validate_release(release)
    manifest = _read(release.checksum_url, 64 * 1024).decode("utf-8-sig")
    hashes = {}
    for line in manifest.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip())
        if match:
            if match[2] in hashes:
                raise RuntimeError("중복된 업데이트 무결성 정보입니다.")
            hashes[match[2]] = match[1].lower()
    expected = hashes.get(release.filename)
    if not expected or release.digest != "sha256:" + expected:
        raise RuntimeError("배포 목록과 GitHub의 SHA-256이 일치하지 않아 설치를 중단했습니다.")
    folder = Path(destination) / (f"v{release.version}-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    archive = folder / release.filename
    digest = hashlib.sha256()
    received, last_percent = 0, -1
    try:
        with _open(release.package_url) as response, archive.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > release.size or received > MAX_DOWNLOAD:
                    raise RuntimeError("다운로드 크기가 배포 정보와 다릅니다.")
                target.write(chunk)
                digest.update(chunk)
                percent = int(received * 100 / release.size)
                if percent != last_percent:
                    progress(f"v{release.version} 다운로드 {percent}% · 계정과 예약 설정은 유지됩니다.")
                    last_percent = percent
        if received != release.size or digest.hexdigest() != expected:
            raise RuntimeError("다운로드 파일의 SHA-256 검증에 실패했습니다. 다시 다운로드하세요.")
        progress("파일 검증 완료 · 설치 파일을 준비하고 있습니다.")
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(PACKAGE_FILES) or set(names) != PACKAGE_FILES:
                raise RuntimeError("업데이트 ZIP의 파일 구성이 올바르지 않습니다.")
            if sum(item.file_size for item in bundle.infolist()) > MAX_DOWNLOAD:
                raise RuntimeError("압축 해제 크기가 허용 범위를 넘었습니다.")
            # No paths from the archive are used; only our fixed allowlist is written.
            for name in sorted(PACKAGE_FILES):
                (folder / name).write_bytes(bundle.read(name))
        return folder
    except Exception:
        archive.unlink(missing_ok=True)
        # An incomplete installer must never be runnable after failed validation.
        for name in PACKAGE_FILES:
            (folder / name).unlink(missing_ok=True)
        raise


def launch_installer(folder):
    installer = Path(folder) / "install.ps1"
    if not installer.is_file():
        raise RuntimeError("검증된 설치 파일을 찾을 수 없습니다.")
    with (Path(folder) / "install.log").open('ab') as output:
        return subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(installer), "-NonInteractive"],
                                cwd=str(folder), creationflags=0x08000000,
                                stdout=output, stderr=subprocess.STDOUT)
