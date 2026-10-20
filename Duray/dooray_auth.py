"""Bounded login state detection and actionable, credential-free diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlparse

import dooray_checkin as core


class ConnectionProblem(RuntimeError):
    def __init__(self, code: str, title: str, help_text: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(f"{title} [{code}]\n{help_text}")


def profile_path(cfg):
    # An ID/tenant/password change must never reuse another account's cookies.
    identity = f"{cfg['tenant_url']}|{cfg['login_id']}|{cfg.get('login_revision', '')}"
    return core.PROFILE_DIR / "accounts" / hashlib.sha256(identity.encode()).hexdigest()[:24]


def check_blocked(cfg):
    path = profile_path(cfg) / "connection-block.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    raise ConnectionProblem(value["code"], value["message"],
                            "자동 재시도를 중지했습니다. 연결 · 설정에서 수정 후 저장하고 연결 확인 또는 브라우저에서 직접 로그인을 실행하세요.")


def remember_failure(cfg, exc):
    if isinstance(exc, ConnectionProblem) and exc.code in {
            "CREDENTIALS", "ACCOUNT_LOCKED", "PASSWORD_EXPIRED", "EXTRA_AUTH", "PASSWORD_MISSING", "LOGIN_UNCONFIRMED"}:
        core.atomic_json(profile_path(cfg) / "connection-block.json", {"code": exc.code, "message": str(exc)})


def clear_failure(cfg):
    (profile_path(cfg) / "connection-block.json").unlink(missing_ok=True)


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise ConnectionProblem("CANCELLED", "연결 확인을 취소했습니다.", "필요할 때 다시 연결 확인을 누르세요.")


def translate_error(exc):
    if isinstance(exc, ConnectionProblem):
        return exc
    text = str(exc)
    if "ERR_NAME_NOT_RESOLVED" in text:
        return ConnectionProblem("ADDRESS", "회사 두레이 주소에 연결할 수 없습니다.",
                                 "회사 주소의 철자를 확인하세요. 회사망에서만 접속되는 주소라면 VPN을 연결하세요.", retryable=True)
    if "ERR_CERT" in text or "SSL" in text:
        return ConnectionProblem("CERTIFICATE", "사이트의 보안 인증서를 확인할 수 없습니다.",
                                 "PC 날짜·시간과 회사 보안 프로그램을 확인하고 사내 관리자에게 문의하세요.")
    if any(word in text for word in ("ERR_CONNECTION", "ERR_INTERNET", "ERR_PROXY", "ERR_TUNNEL", "Timeout", "timed out")):
        return ConnectionProblem("NETWORK", "두레이 응답을 기다리다 연결이 끊겼습니다.",
                                 "인터넷·회사 VPN·프록시 연결을 확인한 뒤 다시 시도하세요.", retryable=True)
    if "Executable doesn't exist" in text:
        return ConnectionProblem("BROWSER", "연결에 필요한 Chromium이 설치되지 않았습니다.",
                                 "연결 · 설정 아래의 Chromium 설치를 누른 뒤 다시 확인하세요.")
    if "Target" in text and "closed" in text:
        return ConnectionProblem("CANCELLED", "로그인 브라우저가 닫혔습니다.",
                                 "브라우저에서 직접 로그인을 다시 누르고, 연결 확인이 끝날 때까지 창을 유지하세요.")
    return exc


def visible_problem(page):
    text = core.page_text(page)
    if re.search(r"계정.{0,20}(잠[겼금]|차단|정지)|로그인.{0,20}횟수.{0,20}초과", text):
        return ConnectionProblem("ACCOUNT_LOCKED", "계정이 잠겼거나 로그인이 제한되었습니다.",
                                 "반복 로그인을 중단했습니다. 두레이 웹에서 계정 잠금을 해제하거나 관리자에게 문의하세요.")
    if re.search(r"비밀번호.{0,20}(만료|변경해야|변경해 주)|비밀번호 변경이 필요", text):
        return ConnectionProblem("PASSWORD_EXPIRED", "비밀번호 변경이 필요합니다.",
                                 "두레이 웹에서 비밀번호를 변경한 뒤, 앱에 새 비밀번호를 입력하고 저장하고 연결 확인을 누르세요.")
    if re.search(r"(아이디|비밀번호|계정|로그인 정보).{0,35}(일치하지|틀[렸림]|올바르지|잘못)|아이디 또는 비밀번호를 확인|invalid (password|credentials)|incorrect (password|username)", text, re.I):
        return ConnectionProblem("CREDENTIALS", "로그인 ID 또는 비밀번호가 올바르지 않습니다.",
                                 "회사 두레이 웹에서 사용하는 ID를 확인하고 비밀번호를 다시 입력하세요. 저장하고 연결 확인을 눌러야 새 값이 적용됩니다.")
    otp, _ = core.find_first(page, ["input[autocomplete='one-time-code']", "input[name='otp']", "input[name='authCode']"])
    if otp is not None or re.search(r"인증번호.{0,25}입력|OTP.{0,25}입력|2단계 인증.{0,25}(필요|진행)|새로운 기기.{0,30}인증|보안문자.{0,20}입력", text, re.I):
        return ConnectionProblem("EXTRA_AUTH", "추가 인증이 필요합니다.",
                                 "브라우저에서 직접 로그인을 누르고 회사 SSO·OTP·보안 인증을 직접 완료하세요. 완료된 세션은 이 PC에 저장됩니다.")
    return None


def trusted_password_origin(url, cfg):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        parsed.netloc == urlparse(cfg["tenant_url"]).netloc or host == "dooray.com" or host.endswith(".dooray.com"))


def authenticated(page, cfg, evidence=None):
    if urlparse(page.url).netloc != urlparse(cfg["tenant_url"]).netloc:
        return False
    # Absence of a password field is NOT proof of an authenticated session.
    pw, _ = core.find_first(page, core.candidates(cfg, "login_password", core.LOGIN_PW_CANDIDATES))
    if pw is not None:
        return False
    if evidence and evidence():
        return True
    marker, _ = core.find_first(page, core.candidates(cfg, "checkin_button", core.CHECKIN_CANDIDATES)
                               + core.candidates(cfg, "checkout_button", core.CHECKOUT_CANDIDATES)
                               + ["button:has-text('로그아웃')", "a:has-text('로그아웃')"])
    return marker is not None


def ensure_logged_in(page, cfg, *, evidence=None, manual=False, progress=None, cancel=None):
    emit = progress or (lambda message: core.log.info("%s", message))
    deadline = time.monotonic() + (180 if manual else cfg["timeout_ms"] / 1000)
    submitted = False
    id_submitted = False
    seen_form = False
    last_progress = ""
    while time.monotonic() < deadline:
        check_cancel(cancel)
        if page.is_closed():
            raise ConnectionProblem("CANCELLED", "로그인 브라우저가 닫혔습니다.", "브라우저에서 직접 로그인을 다시 실행하세요.")
        if authenticated(page, cfg, evidence):
            emit("로그인 확인 완료 · 출퇴근 기록은 변경하지 않습니다.")
            return
        problem = visible_problem(page)
        if problem and not manual:
            raise problem
        pw, _ = core.find_first(page, core.candidates(cfg, "login_password", core.LOGIN_PW_CANDIDATES))
        login, _ = core.find_first(page, core.candidates(cfg, "login_id", core.LOGIN_ID_CANDIDATES))
        seen_form = seen_form or pw is not None or login is not None
        if manual:
            status = "열린 브라우저에서 로그인·추가 인증을 완료하세요. 최대 3분 대기 · 앱에서 취소할 수 있습니다."
        elif not trusted_password_origin(page.url, cfg):
            raise ConnectionProblem("SSO", "회사 통합 로그인(SSO)이 필요합니다.",
                                    "브라우저에서 직접 로그인을 눌러 회사 인증을 완료하세요. 외부 인증 사이트에는 앱이 비밀번호를 자동 입력하지 않습니다.")
        elif pw is not None and not submitted:
            if not trusted_password_origin(pw.evaluate("(el) => el.ownerDocument.location.href"), cfg):
                raise ConnectionProblem("SSO", "외부 인증 화면에서 직접 로그인이 필요합니다.",
                                        "브라우저에서 직접 로그인을 눌러 회사 인증을 완료하세요.")
            form = pw.locator("xpath=ancestor::form").last
            scope = form if form.count() else page
            login, _ = core.find_first(scope, core.candidates(cfg, "login_id", core.LOGIN_ID_CANDIDATES))
            if login is None and not id_submitted:
                raise ConnectionProblem("LOGIN_FORM", "로그인 ID 입력란을 찾지 못했습니다.",
                                        "브라우저에서 직접 로그인으로 회사 로그인 화면을 확인하세요.")
            if login is not None:
                login.fill(cfg["login_id"])
            pw.fill(core.get_password(cfg["login_id"]))
            submit, _ = core.find_first(scope, core.candidates(cfg, "login_submit", core.LOGIN_SUBMIT_CANDIDATES))
            if submit is not None:
                submit.click()
            else:
                pw.press("Enter")
            submitted = True
            status = "ID·비밀번호 전송 완료 · 로그인 결과를 확인하고 있습니다."
        elif login is not None and pw is None and not id_submitted and not submitted:
            next_button, _ = core.find_first(page, ["button:has-text('다음')", "button:has-text('계속')", "button:has-text('Next')"])
            if next_button is not None:
                login.fill(cfg["login_id"])
                next_button.click()
                id_submitted = True
            status = "로그인 화면을 확인하고 있습니다."
        else:
            status = "로그인 결과를 확인하고 있습니다." if submitted else "두레이 로그인 화면을 기다리고 있습니다."
        if status != last_progress:
            emit(status)
            last_progress = status
        page.wait_for_timeout(250)
    if manual:
        raise ConnectionProblem("LOGIN_TIMEOUT", "직접 로그인 대기 시간이 끝났습니다.",
                                "3분 안에 로그인을 완료하지 못했습니다. 브라우저에서 직접 로그인을 다시 눌러 진행하세요.")
    if seen_form:
        raise ConnectionProblem("LOGIN_UNCONFIRMED", "로그인 완료를 확인하지 못했습니다.",
                                "ID·비밀번호 또는 회사 인증 방식을 확인하세요. 브라우저에서 직접 로그인으로 상세 안내를 확인할 수 있습니다.")
    raise ConnectionProblem("PAGE_UNCONFIRMED", "로그인 또는 근태 화면을 확인하지 못했습니다.",
                            "회사 두레이 주소와 출퇴근 페이지 경로를 확인하세요. SSO를 사용하면 브라우저에서 직접 로그인을 누르세요.")
