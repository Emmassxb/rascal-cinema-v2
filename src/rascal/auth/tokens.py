"""Tokens firmados (HMAC-SHA256) de corta duración, sin dependencias externas.

El navegador pide el video directamente al microservicio de streaming (Streamlit no puede
servir Range requests), así que la UI emite un token de reproducción firmado con el secreto
compartido. El microservicio lo verifica sin consultar la sesión de Streamlit.

Formato: ``base64url(json_payload).base64url(hmac)``. Cada *scope* usa una clave derivada
distinta, así un token de reproducción nunca sirve como token interno y viceversa.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Literal

Scope = Literal["play", "internal"]


class TokenError(Exception):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _derive_key(secret: str, scope: Scope) -> bytes:
    return hmac.new(secret.encode("utf-8"), f"rascal-token:{scope}".encode(), hashlib.sha256).digest()


def sign_token(secret: str, scope: Scope, claims: dict[str, Any], ttl_s: int, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = {**claims, "scp": scope, "iat": issued, "exp": issued + ttl_s}
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(_derive_key(secret, scope), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_token(secret: str, scope: Scope, token: str, *, now: float | None = None) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(_derive_key(secret, scope), body.encode("ascii"), hashlib.sha256).digest())
    except (ValueError, UnicodeEncodeError) as exc:
        raise TokenError("Token mal formado") from exc
    if not hmac.compare_digest(signature, expected):
        raise TokenError("Firma inválida")
    try:
        payload: dict[str, Any] = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("Payload inválido") from exc
    if payload.get("scp") != scope:
        raise TokenError("Scope incorrecto")
    current = now if now is not None else time.time()
    if int(payload.get("exp", 0)) < current:
        raise TokenError("Token expirado")
    return payload


def playback_token(secret: str, *, user_id: int, source_id: int, ttl_s: int) -> str:
    return sign_token(secret, "play", {"sub": user_id, "sid": source_id}, ttl_s)


def internal_token(secret: str, *, source_id: int, ttl_s: int = 3600) -> str:
    return sign_token(secret, "internal", {"sid": source_id}, ttl_s)
