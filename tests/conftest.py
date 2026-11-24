"""Fixtures compartidas: BD SQLite temporal, configuración aislada y usuarios por rol."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from rascal.auth.mailer import ConsoleMailer
from rascal.auth.rbac import Principal
from rascal.auth.service import AuthService
from rascal.config import Settings
from rascal.data.db import build_engine, create_schema, session_scope
from rascal.data.enums import Role
from rascal.domain.schemas import RegisterForm
from rascal.torrents.bencode import bencode

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        data_dir=tmp_path / "var",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        stream_engine="local",
        local_media_dir=tmp_path / "media",
        secret_key="test-secret-key-0123456789abcdefghijklmnop",  # type: ignore[arg-type]
        service_key="svc-test-key",  # type: ignore[arg-type]
        login_max_failures=3,
    )


@pytest.fixture
def factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    engine = build_engine(settings.database_url)
    create_schema(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def mailer() -> ConsoleMailer:
    return ConsoleMailer()


PASSWORD = "contrasena-segura-1"


@pytest.fixture
def users(factory: sessionmaker[Session], settings: Settings, mailer: ConsoleMailer) -> dict[str, Principal]:
    roles = {"admin": Role.ADMIN, "partner": Role.PARTNER, "partner2": Role.PARTNER, "ana": Role.USER, "beto": Role.USER}
    result: dict[str, Principal] = {}
    with session_scope(factory) as session:
        auth = AuthService(session, settings, mailer)
        for name, role in roles.items():
            form = RegisterForm(username=name, email=f"{name}@example.com", password=PASSWORD)  # type: ignore[arg-type]
            result[name] = auth.register(form, role=role)
    return result


def make_torrent(files: dict[str, bytes], *, name: str = "Pack", piece_length: int = 16384) -> bytes:
    """Construye un .torrent v1 (multiarchivo, o de un archivo si ``files`` tiene una sola
    entrada sin '/') con hashes de pieza reales."""
    if len(files) == 1 and "/" not in next(iter(files)):
        file_name, payload = next(iter(files.items()))
        info: dict = {"name": file_name, "piece length": piece_length, "length": len(payload)}
        blob = payload
    else:
        info = {"name": name, "piece length": piece_length, "files": []}
        blob = b""
        for path, payload in files.items():
            info["files"].append({"length": len(payload), "path": path.split("/")})
            blob += payload
    info["pieces"] = b"".join(
        hashlib.sha1(blob[i : i + piece_length]).digest() for i in range(0, len(blob), piece_length)  # noqa: S324
    )
    return bencode({"announce": "udp://tracker.example:1337/announce", "info": info})


def make_video(path: Path, *, seconds: int = 8, container_args: list[str] | None = None, audio: str = "aac",
               faststart: bool = False) -> Path:
    """Genera un video de prueba con ffmpeg (patrón + tono)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "1500k", "-c:a", audio,
    ]
    if path.suffix == ".mp4":
        cmd += ["-movflags", "+faststart" if faststart else "-faststart"]
    cmd += [*(container_args or []), str(path)]
    subprocess.run(cmd, check=True)
    return path
