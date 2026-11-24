"""Servicio de autenticación.

Corrige los problemas de seguridad del flujo anterior:

* El código 2FA se guardaba en ``session['2fa_code']``; la sesión de Flask es una cookie
  **firmada pero no cifrada**, así que el usuario podía leer su propio código y saltarse el
  segundo factor. Ahora sólo se persiste un HMAC del código en ``login_challenges``.
* ``random.randint`` → ``secrets.randbelow`` (generador criptográfico).
* Mensajes de error genéricos (antes "Usuario no encontrado" permitía enumerar cuentas)
  y verificación de tiempo constante con hash ficticio.
* Bloqueo temporal tras N intentos fallidos y límite de intentos por código OTP.

Los resultados se devuelven como valores (no excepciones) para que la unidad de trabajo
confirme los contadores de fallos en lugar de revertirlos.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from rascal.auth.mailer import Mailer
from rascal.auth.passwords import dummy_verify, hash_password, verify_password
from rascal.auth.rbac import Permission, PermissionDenied, Principal, require
from rascal.config import Settings
from rascal.data.enums import Role
from rascal.data.models import LoginChallenge, User, utcnow
from rascal.data.repositories.media import AuditRepository
from rascal.data.repositories.users import UserRepository
from rascal.domain.schemas import RegisterForm

GENERIC_LOGIN_ERROR = "Usuario o contraseña incorrectos"


@dataclass(frozen=True, slots=True)
class LoginSuccess:
    principal: Principal


@dataclass(frozen=True, slots=True)
class ChallengeRequired:
    challenge_id: int
    masked_email: str


@dataclass(frozen=True, slots=True)
class LoginFailed:
    message: str


LoginResult = LoginSuccess | ChallengeRequired | LoginFailed


class RegistrationError(Exception):
    pass


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    visible = local[:1] if len(local) <= 3 else local[:2]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


def principal_of(user: User) -> Principal:
    return Principal(user_id=user.id, username=user.username, role=user.role)


class AuthService:
    def __init__(self, session: Session, settings: Settings, mailer: Mailer) -> None:
        self.session = session
        self.settings = settings
        self.mailer = mailer
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    # ── Registro ─────────────────────────────────────────────────────────────
    def register(self, form: RegisterForm, *, role: Role = Role.USER) -> Principal:
        if self.users.by_username(form.username) is not None:
            raise RegistrationError("Ese nombre de usuario ya está en uso")
        if form.email and self.users.by_email(form.email) is not None:
            raise RegistrationError("Ese correo ya está registrado")
        user = self.users.add(
            User(
                username=form.username,
                email=str(form.email) if form.email else None,
                password_hash=hash_password(form.password.get_secret_value()),
                role=role,
                mfa_email_enabled=bool(form.email) and self.settings.smtp_configured,
            )
        )
        self.audit.record(actor_id=user.id, action="user.register", entity_type="user", entity_id=user.id)
        return principal_of(user)

    # ── Login (primer factor) ────────────────────────────────────────────────
    def authenticate(self, username: str, password: str) -> LoginResult:
        user = self.users.by_username(username.strip())
        if user is None or not user.is_active:
            dummy_verify()
            return LoginFailed(GENERIC_LOGIN_ERROR)

        now = utcnow()
        if user.locked_until and user.locked_until > now:
            dummy_verify()
            minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            return LoginFailed(f"Cuenta bloqueada temporalmente. Intenta en {minutes} min.")

        result = verify_password(user.password_hash, password)
        if not result.valid:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= self.settings.login_max_failures:
                user.locked_until = now + timedelta(minutes=self.settings.login_lockout_minutes)
                user.failed_login_count = 0
            return LoginFailed(GENERIC_LOGIN_ERROR)

        user.failed_login_count = 0
        user.locked_until = None
        if result.needs_rehash:  # migración transparente Werkzeug → Argon2id
            user.password_hash = hash_password(password)

        if user.mfa_email_enabled and user.email:
            return self._start_challenge(user)
        return self._complete_login(user)

    # ── Segundo factor ───────────────────────────────────────────────────────
    def _code_hash(self, code: str, salt: str) -> str:
        key = self.settings.secret_key.get_secret_value().encode("utf-8")
        digest = hmac.new(key, f"otp:{salt}:{code}".encode(), hashlib.sha256).hexdigest()
        return f"{salt}${digest}"

    def _start_challenge(self, user: User) -> LoginResult:
        assert user.email is not None
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        now = utcnow()
        self.users.purge_challenges(user.id, older_than=now)
        challenge = self.users.add_challenge(
            LoginChallenge(
                user_id=user.id,
                code_hash=self._code_hash(code, salt),
                expires_at=now + timedelta(seconds=self.settings.otp_ttl_s),
            )
        )
        minutes = max(1, self.settings.otp_ttl_s // 60)
        try:
            self.mailer.send(
                user.email,
                "Código de verificación - Rascal Does Not Dream of Cinema",
                f"Tu código de verificación es: {code}\n\nExpira en {minutes} minutos.",
            )
        except OSError:
            return LoginFailed("No se pudo enviar el código de verificación. Intenta más tarde.")
        return ChallengeRequired(challenge_id=challenge.id, masked_email=mask_email(user.email))

    def verify_challenge(self, challenge_id: int, code: str) -> LoginResult:
        challenge = self.users.get_challenge(challenge_id)
        now = utcnow()
        if challenge is None or challenge.consumed_at is not None:
            return LoginFailed("No hay un código pendiente. Inicia sesión de nuevo.")
        if challenge.expires_at < now:
            return LoginFailed("El código ha expirado. Inicia sesión de nuevo.")
        if challenge.attempts >= self.settings.otp_max_attempts:
            return LoginFailed("Demasiados intentos. Inicia sesión de nuevo.")

        challenge.attempts += 1
        salt, _, _ = challenge.code_hash.partition("$")
        if not hmac.compare_digest(self._code_hash(code.strip(), salt), challenge.code_hash):
            return LoginFailed("Código incorrecto.")

        challenge.consumed_at = now
        user = self.users.get(challenge.user_id)
        if user is None or not user.is_active:
            return LoginFailed(GENERIC_LOGIN_ERROR)
        return self._complete_login(user)

    def _complete_login(self, user: User) -> LoginSuccess:
        user.last_login_at = utcnow()
        return LoginSuccess(principal_of(user))

    # ── Sesión ───────────────────────────────────────────────────────────────
    def refresh_principal(self, user_id: int) -> Principal | None:
        """Revalida la sesión en cada rerun: refleja cambios de rol o desactivación."""
        user = self.users.get(user_id)
        if user is None or not user.is_active:
            return None
        return principal_of(user)

    def change_password(self, principal: Principal, current: str, new: str) -> None:
        user = self.users.get(principal.user_id)
        if user is None or not verify_password(user.password_hash, current).valid:
            raise PermissionDenied("La contraseña actual no es correcta")
        RegisterForm(username=user.username, password=new)  # reutiliza la política de fortaleza
        user.password_hash = hash_password(new)
        self.audit.record(actor_id=user.id, action="user.password_change", entity_type="user", entity_id=user.id)

    # ── Administración de roles ──────────────────────────────────────────────
    def set_role(self, actor: Principal, user_id: int, role: Role) -> None:
        require(actor, Permission.USER_MANAGE)
        user = self.users.get(user_id)
        if user is None:
            raise ValueError("Usuario no encontrado")
        if user.id == actor.user_id and role is not Role.ADMIN:
            raise PermissionDenied("Un admin no puede quitarse su propio rol")
        previous = user.role
        user.role = role
        self.audit.record(
            actor_id=actor.user_id,
            action="user.set_role",
            entity_type="user",
            entity_id=user.id,
            payload={"from": previous.value, "to": role.value},
        )

    def set_active(self, actor: Principal, user_id: int, active: bool) -> None:
        require(actor, Permission.USER_MANAGE)
        user = self.users.get(user_id)
        if user is None:
            raise ValueError("Usuario no encontrado")
        if user.id == actor.user_id:
            raise PermissionDenied("No puedes desactivar tu propia cuenta")
        user.is_active = active
        self.audit.record(
            actor_id=actor.user_id, action="user.set_active", entity_type="user", entity_id=user.id,
            payload={"active": active},
        )
