"""v2.2 · Seguridad: validación de URLs (SSRF), CSP con nonce, CSRF por Origin/Referer, rate limit, 2FA."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rascal.web as web
from rascal.auth.mailer import ConsoleMailer
from rascal.data.db import session_scope
from rascal.data.models import User
from rascal.security.middleware import RateLimiter
from rascal.security.urls import UnsafeUrl, validate_external_url
from rascal.web import mount_web

from conftest import PASSWORD


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd",
        "http://127.0.0.1/", "http://localhost:8001/admin", "http://10.1.2.3/x", "http://192.168.0.1/",
        "http://172.16.5.4/", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
        "http://2130706433/", "http://0x7f.0.0.1/", "http://017700000001/", "http://0.0.0.0/",
        "https://user:pass@example.com/", "http://printer.local/", "https://intranet/", "http://100.64.0.1/",
        "https://exa mple.com/",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        validate_external_url(url)


@pytest.mark.parametrize("url", ["https://image.tmdb.org/t/p/w500/x.jpg", "https://cdn.example.com/a.m3u8", "http://8.8.8.8/x"])
def test_public_urls_are_accepted(url: str) -> None:
    assert validate_external_url(url) == url


def test_https_only_when_requested() -> None:
    with pytest.raises(UnsafeUrl):
        validate_external_url("http://cdn.example.com/a.mp4", schemes=("https",))


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter({"login": (2, 60)}, clock=lambda: now[0])
    assert limiter.hit("login", "1.1.1.1") is None
    assert limiter.hit("login", "1.1.1.1") is None
    assert limiter.hit("login", "1.1.1.1") is not None  # tercer intento dentro de la ventana
    assert limiter.hit("login", "2.2.2.2") is None  # otra IP no se ve afectada
    now[0] = 61
    assert limiter.hit("login", "1.1.1.1") is None


def make_client(settings, factory, **overrides):  # type: ignore[no-untyped-def]
    cfg = settings.model_copy(update=overrides)
    app = FastAPI()
    mount_web(app, cfg, session_factory=factory)
    return TestClient(app)


def test_security_headers_and_csp_nonce(settings, factory) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory)
    response = client.get("/login")
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)  # type: ignore[union-attr]
    assert f'<script nonce="{nonce}">' in response.text
    assert "object-src 'none'" in csp and "frame-ancestors 'self'" in csp and "unsafe-inline" not in csp.split("style-src")[0]
    assert client.get("/login").headers["content-security-policy"] != csp  # nonce distinto por petición
    assert "strict-transport-security" not in headers


def test_hsts_and_secure_cookie_behind_https(settings, factory, users) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory, secure_cookies=True)
    response = client.post("/login", data={"username": "ana", "password": PASSWORD}, follow_redirects=False)
    assert "strict-transport-security" in response.headers
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_cross_origin_post_is_rejected(settings, factory, users) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory)
    evil = client.post("/login", data={"username": "ana", "password": PASSWORD},
                       headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert evil.status_code == 403
    evil_ref = client.post("/login", data={"username": "ana", "password": PASSWORD},
                           headers={"Referer": "https://evil.example/form"}, follow_redirects=False)
    assert evil_ref.status_code == 403
    ok = client.post("/login", data={"username": "ana", "password": PASSWORD},
                     headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


def test_public_url_origin_is_accepted(settings, factory, users) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory, public_url="https://cine.example.com")
    ok = client.post("/login", data={"username": "ana", "password": PASSWORD},
                     headers={"Origin": "https://cine.example.com"}, follow_redirects=False)
    assert ok.status_code == 303


def test_strict_csrf_requires_origin_or_referer(settings, factory, users) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory, csrf_strict=True)
    bare = client.post("/login", data={"username": "ana", "password": PASSWORD}, follow_redirects=False)
    assert bare.status_code == 403
    ok = client.post("/login", data={"username": "ana", "password": PASSWORD},
                     headers={"Referer": "http://testserver/login"}, follow_redirects=False)
    assert ok.status_code == 303


def test_login_rate_limit(settings, factory, users) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory, rate_limit_login="3/60", login_max_failures=99)
    codes = [client.post("/login", data={"username": "ana", "password": "mala"}, follow_redirects=False).status_code
             for _ in range(4)]
    assert codes == [303, 303, 303, 429]


def test_api_rate_limit_ignores_service_key(settings, factory) -> None:  # type: ignore[no-untyped-def]
    client = make_client(settings, factory, rate_limit_api="2/60")
    with_key = [client.get("/api/x", headers={"X-Service-Key": "svc-test-key"}).status_code for _ in range(4)]
    assert 429 not in with_key
    without = [client.get("/api/x").status_code for _ in range(3)]
    assert without[-1] == 429


def test_two_factor_is_enforced(settings, factory, users, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Antes, ChallengeRequired emitía la cookie de sesión igualmente (2FA anulado)."""
    outbox = ConsoleMailer()
    monkeypatch.setattr(web, "build_mailer", lambda cfg: outbox)
    with session_scope(factory) as s:
        s.query(User).filter(User.username == "ana").update({"mfa_email_enabled": True})
    client = make_client(settings, factory)
    first = client.post("/login", data={"username": "ana", "password": PASSWORD}, follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"] == "/login/verify"
    assert web.COOKIE_NAME not in first.cookies  # sin sesión todavía
    assert "a***@example.com" in client.get("/login/verify").text or "@example.com" in client.get("/login/verify").text

    wrong = client.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert web.COOKIE_NAME not in wrong.cookies

    code = re.search(r"\b(\d{6})\b", outbox.outbox[-1][2]).group(1)  # type: ignore[union-attr]
    good = client.post("/login/verify", data={"code": code}, follow_redirects=False)
    assert good.status_code == 303 and web.COOKIE_NAME in good.cookies
    assert "ana" in client.get("/").text
