import time

import pytest

from rascal.auth.passwords import hash_password, verify_password
from rascal.auth.rbac import Permission, PermissionDenied, Principal, can_edit_movie, require
from rascal.auth.service import AuthService, ChallengeRequired, LoginFailed, LoginSuccess
from rascal.auth.tokens import TokenError, sign_token, verify_token
from rascal.data.db import session_scope
from rascal.data.enums import Role
from rascal.data.models import User

from conftest import PASSWORD


def test_argon2_roundtrip() -> None:
    hashed = hash_password("hola-mundo-123")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "hola-mundo-123").valid
    assert not verify_password(hashed, "otra").valid
    assert not verify_password(None, "x").valid


@pytest.mark.parametrize("method", ["pbkdf2:sha256", "pbkdf2:sha256:1000", "scrypt"])
def test_legacy_werkzeug_hashes(method: str) -> None:
    security = pytest.importorskip("werkzeug.security")
    legacy = security.generate_password_hash("clave-flask-1", method=method)
    result = verify_password(legacy, "clave-flask-1")
    assert result.valid and result.needs_rehash
    assert not verify_password(legacy, "incorrecta").valid


def test_legacy_hash_is_migrated_on_login(factory, settings, mailer, users) -> None:  # type: ignore[no-untyped-def]
    security = pytest.importorskip("werkzeug.security")
    with session_scope(factory) as s:
        user = s.get(User, users["ana"].user_id)
        user.password_hash = security.generate_password_hash("clave-flask-1")
    with session_scope(factory) as s:
        assert isinstance(AuthService(s, settings, mailer).authenticate("ana", "clave-flask-1"), LoginSuccess)
    with session_scope(factory) as s:
        assert s.get(User, users["ana"].user_id).password_hash.startswith("$argon2id$")


def test_tokens() -> None:
    token = sign_token("s3cret", "play", {"sid": 4, "sub": 1}, ttl_s=60)
    assert verify_token("s3cret", "play", token)["sid"] == 4
    with pytest.raises(TokenError):
        verify_token("s3cret", "internal", token)  # scope distinto → clave distinta
    with pytest.raises(TokenError):
        verify_token("otro", "play", token)
    body, sig = token.split(".")
    with pytest.raises(TokenError):
        verify_token("s3cret", "play", body[:-2] + "AA." + sig)
    expired = sign_token("s3cret", "play", {"sid": 1}, ttl_s=10, now=time.time() - 100)
    with pytest.raises(TokenError, match="expirado"):
        verify_token("s3cret", "play", expired)


def test_rbac_matrix() -> None:
    user = Principal(1, "u", Role.USER)
    partner = Principal(2, "p", Role.PARTNER)
    admin = Principal(3, "a", Role.ADMIN)
    assert user.can(Permission.STREAM_WATCH) and not user.can(Permission.PARTNER_PANEL)
    assert partner.can(Permission.MOVIE_CREATE) and not partner.can(Permission.MOVIE_EDIT_ANY)
    assert all(admin.can(p) for p in Permission)
    assert can_edit_movie(partner, owner_id=2) and not can_edit_movie(partner, owner_id=99)
    assert can_edit_movie(admin, owner_id=99) and not can_edit_movie(user, owner_id=1)
    with pytest.raises(PermissionDenied):
        require(None, Permission.REVIEW_WRITE)
    with pytest.raises(PermissionDenied):
        require(user, Permission.ANALYTICS_VIEW)


def test_login_generic_errors_and_lockout(factory, settings, mailer, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        unknown = AuthService(s, settings, mailer).authenticate("nadie", "x")
    for _ in range(settings.login_max_failures):
        with session_scope(factory) as s:  # cada intento en su propia transacción (como en la UI)
            wrong = AuthService(s, settings, mailer).authenticate("ana", "incorrecta")
    assert isinstance(unknown, LoginFailed) and isinstance(wrong, LoginFailed)
    assert unknown.message == wrong.message  # no revela si el usuario existe
    with session_scope(factory) as s:
        locked = AuthService(s, settings, mailer).authenticate("ana", PASSWORD)
    assert isinstance(locked, LoginFailed) and "bloqueada" in locked.message


def test_email_otp_flow(factory, settings, mailer, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        s.get(User, users["beto"].user_id).mfa_email_enabled = True
    with session_scope(factory) as s:
        result = AuthService(s, settings, mailer).authenticate("BETO", PASSWORD)  # usuario case-insensitive
    assert isinstance(result, ChallengeRequired) and result.masked_email.endswith("@example.com")
    code = mailer.outbox[-1][2].split(": ")[1][:6]
    assert code not in str(result)  # el código nunca viaja al cliente
    with session_scope(factory) as s:
        assert isinstance(AuthService(s, settings, mailer).verify_challenge(result.challenge_id, "000000" if code != "000000" else "111111"), LoginFailed)
    with session_scope(factory) as s:
        ok = AuthService(s, settings, mailer).verify_challenge(result.challenge_id, code)
    assert isinstance(ok, LoginSuccess) and ok.principal.username == "beto"
    with session_scope(factory) as s:
        assert isinstance(AuthService(s, settings, mailer).verify_challenge(result.challenge_id, code), LoginFailed)


def test_role_management(factory, settings, mailer, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        AuthService(s, settings, mailer).set_role(users["admin"], users["ana"].user_id, Role.PARTNER)
    with session_scope(factory) as s:
        assert AuthService(s, settings, mailer).refresh_principal(users["ana"].user_id).role is Role.PARTNER
    with pytest.raises(PermissionDenied), session_scope(factory) as s:
        AuthService(s, settings, mailer).set_role(users["partner"], users["beto"].user_id, Role.ADMIN)
