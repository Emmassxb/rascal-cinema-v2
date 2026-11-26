"""Configuración centralizada (12-factor) compartida por la UI y el microservicio de streaming.

Todas las variables se leen del entorno con prefijo ``RASCAL_`` (o de un archivo ``.env``).
Ningún secreto vive en el código fuente: el código anterior tenía la contraseña SMTP,
la URI de MongoDB Atlas y la API key de TMDB escritas en claro.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-insecure-secret-change-me-0000000000"
_INSECURE_SERVICE_KEY = "dev-service-key-change-me"


class Settings(BaseSettings):
    """Parámetros de ejecución. Los valores por defecto sirven para desarrollo local."""

    model_config = SettingsConfigDict(
        env_prefix="RASCAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── General ──────────────────────────────────────────────────────────────
    env: Literal["dev", "test", "prod"] = "dev"
    data_dir: Path = Path("./var")
    # sqlite:///var/rascal.db (relativa al directorio de trabajo) · sqlite:////srv/rascal/var/rascal.db (absoluta)
    # postgresql+psycopg://rascal:CLAVE@127.0.0.1:5432/rascal
    database_url: str = "sqlite:///./var/rascal.db"
    sql_echo: bool = False

    # ── Servidor HTTP ────────────────────────────────────────────────────────
    host: str = "127.0.0.1"  # detrás de Nginx: SIEMPRE loopback. 0.0.0.0 sólo en LAN de confianza.
    port: int = 8001
    secure_cookies: bool = False  # True detrás de HTTPS (cookie con atributo Secure)
    public_url: str | None = None  # https://cine.ejemplo.com · origen extra aceptado en la comprobación CSRF
    trusted_proxies: str = "127.0.0.1"  # IPs cuyo X-Forwarded-For se respeta (uvicorn --forwarded-allow-ips)

    # ── Endurecimiento HTTP ──────────────────────────────────────────────────
    csrf_strict: bool | None = None  # None → estricto sólo en prod (exige Origin o Referer en POST)
    rate_limit_enabled: bool = True
    rate_limit_login: str = "10/60"  # peticiones/segundos por IP
    rate_limit_register: str = "5/600"
    rate_limit_api: str = "240/60"
    allow_private_urls: bool = False  # permite URLs a IPs privadas/loopback (sólo LAN de confianza)
    allow_insecure_stream_urls: bool = False  # permite http:// (sin TLS) en enlaces directos de video

    # ── Seguridad ────────────────────────────────────────────────────────────
    secret_key: SecretStr = SecretStr(_INSECURE_SECRET)
    service_key: SecretStr = SecretStr(_INSECURE_SERVICE_KEY)
    playback_token_ttl_s: int = 6 * 3600
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    otp_ttl_s: int = 300
    otp_max_attempts: int = 5

    # ── Correo (2FA por email) ───────────────────────────────────────────────
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str = "no-reply@rascal.local"
    smtp_starttls: bool = True

    # ── Integraciones ────────────────────────────────────────────────────────
    tmdb_api_key: SecretStr | None = None
    tmdb_region: str = "MX"  # región de "Dónde ver" (watch/providers)

    # ── Microservicio de streaming ───────────────────────────────────────────
    streamer_internal_url: str = "http://127.0.0.1:8001"
    streamer_public_url: str = "http://localhost:8001"
    ui_public_url: str = "http://localhost:8501"  # origen autorizado a embeber el reproductor (iframe)
    stream_engine: Literal["libtorrent", "local"] = "libtorrent"
    local_media_dir: Path | None = None

    lt_listen_port: int = 6881
    # Interfaz(es) de libtorrent, p. ej. "10.8.0.2:6881" (VPN/WireGuard) para no exponer la IP pública
    # del servidor en los enjambres. Vacío = todas las interfaces en lt_listen_port.
    lt_listen_interfaces: str | None = None
    lt_enable_dht: bool = True  # DHT + LSD (descubrimiento de peers sin tracker)
    lt_enable_port_mapping: bool = True  # UPnP / NAT-PMP
    lt_download_rate_limit: int = 0  # bytes/s; 0 = sin límite
    lt_upload_rate_limit: int = 0

    stream_head_bytes: int = 10 * 1024 * 1024  # cabecera: ftyp/moov (faststart) o EBML/Segment
    stream_tail_bytes: int = 6 * 1024 * 1024  # cola: moov al final, Cues de MKV, índice AVI
    stream_readahead_bytes: int = 32 * 1024 * 1024
    stream_chunk_bytes: int = 256 * 1024
    stream_piece_timeout_s: float = 120.0
    stream_metadata_timeout_s: float = 60.0
    resume_save_interval_s: float = 60.0

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    allow_full_transcode: bool = False
    max_torrent_file_bytes: int = 10 * 1024 * 1024

    # ── Rutas derivadas ──────────────────────────────────────────────────────
    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def resume_dir(self) -> Path:
        return self.data_dir / "resume"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def subtitles_dir(self) -> Path:
        return self.data_dir / "subtitles"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def csrf_is_strict(self) -> bool:
        return self.env == "prod" if self.csrf_strict is None else self.csrf_strict

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        """Crea los directorios de trabajo si no existen."""
        for path in (self.data_dir, self.downloads_dir, self.resume_dir, self.uploads_dir):
            path.mkdir(parents=True, exist_ok=True)

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.env == "prod":
            secret = self.secret_key.get_secret_value()
            if secret == _INSECURE_SECRET or len(secret) < 32:
                raise ValueError("RASCAL_SECRET_KEY debe definirse (>= 32 caracteres) en producción")
            if self.service_key.get_secret_value() == _INSECURE_SERVICE_KEY:
                raise ValueError("RASCAL_SERVICE_KEY debe definirse en producción")
        for name in ("rate_limit_login", "rate_limit_register", "rate_limit_api"):
            parse_rate(getattr(self, name))  # falla al arrancar si el formato es inválido
        return self


def parse_rate(value: str) -> tuple[int, float]:
    """``"10/60"`` → (10 peticiones, 60 s). Formato de ``RASCAL_RATE_LIMIT_*``."""
    try:
        count, window = value.split("/", 1)
        parsed = int(count), float(window)
    except ValueError as exc:
        raise ValueError(f"Límite inválido {value!r}: usa 'peticiones/segundos', p. ej. 10/60") from exc
    if parsed[0] < 1 or parsed[1] <= 0:
        raise ValueError(f"Límite inválido {value!r}")
    return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton de configuración (se lee una vez por proceso)."""
    return Settings()
