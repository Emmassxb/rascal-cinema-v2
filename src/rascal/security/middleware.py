"""Middleware de endurecimiento HTTP (ASGI puro, sin dependencias externas).

``install_security(app, settings)`` añade, en este orden de ejecución:

1. **Rate limiting** por IP en ``POST /login``, ``POST /login/verify``, ``POST /register`` y
   ``/api/*``. Ventana deslizante en memoria (un proceso: el servicio corre con 1 worker por
   diseño). Las llamadas internas con ``X-Service-Key`` válida no cuentan.
2. **Protección CSRF** en métodos que cambian estado (POST/PUT/PATCH/DELETE) fuera de
   ``/api`` y ``/internal``: si llega ``Origin`` (o, en su defecto, ``Referer``) debe coincidir
   con el host de la petición o con ``RASCAL_PUBLIC_URL``. En modo estricto (por defecto en
   ``prod``) además se exige que al menos una de las dos cabeceras exista. Junto con las
   cookies ``SameSite=Lax`` cubre la recomendación de OWASP para apps sin tokens por formulario.
3. **Cabeceras de seguridad** en todas las respuestas y una **CSP con nonce por petición**:
   las plantillas reciben el nonce (``csp_nonce()``) para sus ``<script>``; los manejadores
   inline (``onclick``, ``onerror``…) quedan bloqueados.

La IP del cliente es ``scope["client"]``: detrás de Nginx, uvicorn la reescribe desde
``X-Forwarded-For`` sólo si el proxy está en ``--forwarded-allow-ips`` (``RASCAL_TRUSTED_PROXIES``).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from rascal.config import Settings, parse_rate

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_nonce: ContextVar[str] = ContextVar("csp_nonce", default="")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_EXEMPT_PREFIXES = ("/api/", "/internal/")


def csp_nonce() -> str:
    """Nonce CSP de la petición en curso ('' fuera de una petición)."""
    return _nonce.get()


def content_security_policy(nonce: str, *, allow_http_media: bool = False) -> str:
    # Video.js y hls se sirven desde jsDelivr. Los videos directos/HLS y los pósters vienen de
    # cualquier origen HTTPS; YouTube sólo como iframe del tráiler.
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data: https: http:",
            "media-src 'self' blob: https:" + (" http:" if allow_http_media else ""),
            "connect-src 'self' https:" + (" http:" if allow_http_media else ""),
            "font-src 'self' data: https://cdn.jsdelivr.net",
            "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'self'",
        )
    )


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return None


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _plain(send: Send, status: int, text: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = text.encode("utf-8")
    headers = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """Ventana deslizante por (cubeta, IP). Limpia claves inactivas para no crecer sin límite."""

    def __init__(self, rules: dict[str, tuple[int, float]], *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rules = rules
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def hit(self, bucket: str, key: str) -> float | None:
        """Registra un intento. Devuelve ``None`` si se permite, o los segundos a esperar."""
        limit, window = self.rules[bucket]
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault((bucket, key), deque())
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1.0, window - (now - hits[0]))
            hits.append(now)
            if now - self._last_sweep > 300:
                self._sweep(now)
        return None

    def _sweep(self, now: float) -> None:
        longest = max(window for _, window in self.rules.values())
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] > longest]
        for k in stale:
            del self._hits[k]
        self._last_sweep = now


def _bucket_for(method: str, path: str) -> str | None:
    if method == "POST" and path in ("/login", "/login/verify"):
        return "login"
    if method == "POST" and path == "/register":
        return "register"
    if path.startswith("/api/"):
        return "api"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Middleware
# ─────────────────────────────────────────────────────────────────────────────
class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.service_key = settings.service_key.get_secret_value()
        self.limiter = RateLimiter(
            {
                "login": parse_rate(settings.rate_limit_login),
                "register": parse_rate(settings.rate_limit_register),
                "api": parse_rate(settings.rate_limit_api),
            }
        )
        public = urlsplit(settings.public_url) if settings.public_url else None
        self.public_origin = f"{public.scheme}://{public.netloc}".lower() if public and public.netloc else None

    # ── CSRF ─────────────────────────────────────────────────────────────────
    def _origin_allowed(self, scope: Scope, origin: str) -> bool:
        parts = urlsplit(origin)
        if not parts.scheme or not parts.netloc:
            return False
        candidate = f"{parts.scheme}://{parts.netloc}".lower()
        if self.public_origin and hmac.compare_digest(candidate, self.public_origin):
            return True
        host = (_header(scope, b"host") or "").lower()
        return bool(host) and parts.netloc.lower() == host

    def _csrf_ok(self, scope: Scope) -> bool:
        origin = _header(scope, b"origin")
        if origin and origin != "null":
            return self._origin_allowed(scope, origin)
        referer = _header(scope, b"referer")
        if referer:
            return self._origin_allowed(scope, referer)
        return not self.settings.csrf_is_strict

    def _has_service_key(self, scope: Scope) -> bool:
        presented = _header(scope, b"x-service-key")
        return bool(presented) and hmac.compare_digest(presented, self.service_key)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if self.settings.rate_limit_enabled:
            bucket = _bucket_for(method, path)
            if bucket and not self._has_service_key(scope):
                retry = self.limiter.hit(bucket, _client_ip(scope))
                if retry is not None:
                    await _plain(send, 429, "Demasiadas peticiones. Espera un momento e inténtalo de nuevo.",
                                 [(b"retry-after", str(int(retry)).encode())])
                    return

        if method in UNSAFE_METHODS and not path.startswith(CSRF_EXEMPT_PREFIXES) and not self._csrf_ok(scope):
            await _plain(send, 403, "Petición rechazada: el origen no coincide con este sitio (protección CSRF).")
            return

        nonce = secrets.token_urlsafe(16)
        token = _nonce.set(nonce)
        secure = self.settings.secure_cookies

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}

                def add(name: bytes, value: str) -> None:
                    if name not in present:
                        headers.append((name, value.encode("latin-1")))

                add(b"x-content-type-options", "nosniff")
                add(b"x-frame-options", "SAMEORIGIN")
                add(b"referrer-policy", "strict-origin-when-cross-origin")
                add(b"permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()")
                add(b"cross-origin-opener-policy", "same-origin")
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if content_type.startswith(b"text/html"):
                    add(b"content-security-policy",
                        content_security_policy(nonce, allow_http_media=self.settings.allow_insecure_stream_urls))
                if secure:
                    add(b"strict-transport-security", "max-age=31536000; includeSubDomains")
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            _nonce.reset(token)


def install_security(app: Any, settings: Settings) -> None:
    """Registra el middleware una sola vez por aplicación."""
    if getattr(app.state, "_rascal_security", False):
        return
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.state._rascal_security = True
