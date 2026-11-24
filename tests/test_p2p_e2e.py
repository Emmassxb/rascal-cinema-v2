"""Prueba de extremo a extremo con BitTorrent real en loopback.

Un *seeder* libtorrent (subida limitada para simular una red real) comparte:
  1. ``Demo/demo.mp4`` (H.264/AAC, átomo ``moov`` AL FINAL) + ``Demo/readme.txt``
  2. ``demo_ac3.mkv`` (H.264 + AC3, audio que ningún navegador decodifica)

El microservicio corre en uvicorn con ``LibtorrentEngine`` y se verifica que:
  * la cola del MP4 (moov) se sirve con 206 ANTES de completar la descarga,
  * el archivo completo servido por Range es idéntico byte a byte,
  * sólo se descarga el archivo de video (readme.txt con prioridad 0),
  * ffprobe (vía el endpoint interno) clasifica MP4 → direct y MKV/AC3 → remux_audio,
  * /remux entrega fMP4 válido (H.264 + AAC) mientras el MKV se descarga,
  * se escribe resume data al apagar.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from conftest import HAS_FFMPEG, make_video

lt = pytest.importorskip("libtorrent")
uvicorn = pytest.importorskip("uvicorn")
pytestmark = [pytest.mark.p2p, pytest.mark.skipif(not HAS_FFMPEG, reason="requiere ffmpeg/ffprobe")]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_lt_torrent(path: Path, piece_size: int = 16384) -> bytes:
    creator = lt.create_torrent(lt.list_files(str(path)), piece_size)
    lt.set_piece_hashes(creator, str(path.parent))
    return lt.bencode(creator.generate())


def wait_until(predicate, timeout: float, message: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def test_sequential_p2p_streaming_end_to_end(tmp_path: Path) -> None:
    from rascal.auth.mailer import ConsoleMailer
    from rascal.auth.service import AuthService
    from rascal.auth.tokens import playback_token
    from rascal.config import Settings
    from rascal.data.db import build_engine, create_schema, session_scope
    from rascal.data.enums import PlaybackMode, Role, SourceStatus
    from rascal.data.models import MediaSource
    from rascal.domain.schemas import MovieForm, RegisterForm, TorrentSourceForm
    from rascal.domain.services.catalog import CatalogService
    from rascal.streaming.api import create_app
    from sqlalchemy.orm import sessionmaker

    # ── Contenido y torrents ────────────────────────────────────────────────
    content = tmp_path / "seed"
    mp4 = make_video(content / "Demo" / "demo.mp4", seconds=20, faststart=False)
    (content / "Demo" / "readme.txt").write_bytes(b"no deberia descargarse " * 2000)
    mkv = make_video(content / "demo_ac3.mkv", seconds=12, audio="ac3")
    mp4_bytes = mp4.read_bytes()
    assert mp4_bytes.rfind(b"moov") > len(mp4_bytes) * 0.9, "el moov debe estar al final"
    torrent_mp4 = make_lt_torrent(content / "Demo")
    torrent_mkv = make_lt_torrent(mkv)

    # ── Seeder ──────────────────────────────────────────────────────────────
    seed_port = free_port()
    seeder = lt.session({
        "listen_interfaces": f"127.0.0.1:{seed_port}", "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False, "upload_rate_limit": 700_000,
    })
    # libtorrent exime a los peers locales (loopback/LAN) de los límites globales: se limita
    # la clase de peers local para que la descarga sea lenta como en Internet.
    local_class = seeder.get_peer_class(seeder.local_peer_class_id)
    local_class["upload_limit"] = 700_000
    seeder.set_peer_class(seeder.local_peer_class_id, local_class)
    seeds = []
    for data in (torrent_mp4, torrent_mkv):
        params = lt.load_torrent_buffer(data)
        params.save_path = str(content)
        seeds.append(seeder.add_torrent(params))
    wait_until(lambda: all(h.status().is_seeding for h in seeds), 30, "el seeder no terminó de verificar")

    # ── Servicio de streaming con libtorrent real ───────────────────────────
    api_port = free_port()
    settings = Settings(
        env="test", data_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path / 'e2e.db'}",
        stream_engine="libtorrent", lt_listen_port=free_port(), lt_enable_dht=False, lt_enable_port_mapping=False,
        streamer_internal_url=f"http://127.0.0.1:{api_port}", stream_head_bytes=256 * 1024,
        stream_tail_bytes=256 * 1024, stream_piece_timeout_s=60, secret_key="e2e-secret-0123456789abcdefghijklmnopq",
        service_key="e2e-service-key",
    )
    db = build_engine(settings.database_url)
    create_schema(db)
    factory = sessionmaker(bind=db, expire_on_commit=False)
    with session_scope(factory) as s:
        partner = AuthService(s, settings, ConsoleMailer()).register(
            RegisterForm(username="studio", password="clave-segura-99"), role=Role.PARTNER)  # type: ignore[arg-type]
        svc = CatalogService(s, partner, settings)
        movie = svc.create_movie(MovieForm(title="Demo", release_year=2024))
        mp4_source = svc.add_torrent_source(movie, TorrentSourceForm(rights_statement="Material propio"), torrent_mp4)
        mkv_source = svc.add_torrent_source(movie, TorrentSourceForm(label="MKV", rights_statement="Material propio"), torrent_mkv)
    assert mp4_source.file_path == "Demo/demo.mp4" and mkv_source.container == "mkv"

    app = create_app(settings, session_factory=factory)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: server.started, 15, "uvicorn no arrancó")

    secret = settings.secret_key.get_secret_value()
    service = {"X-Service-Key": "e2e-service-key"}
    report: dict[str, object] = {}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=90) as http:
            # 1) MP4 con moov al final: se pide la COLA primero, como hace el navegador.
            assert http.post(f"/api/sources/{mp4_source.id}/start", headers=service).status_code == 200
            engine = app.state.engine
            engine.connect_peer(mp4_source.id, "127.0.0.1", seed_port)
            token = playback_token(secret, user_id=1, source_id=mp4_source.id, ttl_s=600)
            t0 = time.monotonic()
            tail = http.get(f"/stream/{mp4_source.id}", params={"token": token}, headers={"Range": "bytes=-65536"})
            report["tail_seconds"] = round(time.monotonic() - t0, 2)
            assert tail.status_code == 206 and tail.content == mp4_bytes[-65536:]
            status = http.get(f"/api/sources/{mp4_source.id}/status", headers=service).json()
            report["progress_when_tail_served"] = status["progress"]
            assert status["progress"] < 0.9, "la cola debía servirse antes de completar la descarga"
            assert status["has_metadata"] and status["file_path"] == "Demo/demo.mp4"

            # 2) Lectura completa por Range mientras termina de descargarse.
            full = http.get(f"/stream/{mp4_source.id}", params={"token": token})
            assert full.status_code == 200
            assert hashlib.sha256(full.content).digest() == hashlib.sha256(mp4_bytes).digest()

            # 3) Probe: MP4 H.264/AAC → reproducción directa.
            probe = http.post(f"/api/sources/{mp4_source.id}/probe", headers=service).json()
            assert (probe["video_codec"], probe["audio_codec"], probe["playback_mode"]) == ("h264", "aac", "direct")
            page = http.get(f"/player/{mp4_source.id}", params={"token": token})
            assert page.status_code == 200 and '"mode": "direct"' in page.text

            # 4) MKV con AC3 → remux_audio y fMP4 al vuelo.
            http.post(f"/api/sources/{mkv_source.id}/start", headers=service)
            engine.connect_peer(mkv_source.id, "127.0.0.1", seed_port)
            probe_mkv = http.post(f"/api/sources/{mkv_source.id}/probe", headers=service).json()
            assert probe_mkv["audio_codec"] == "ac3" and probe_mkv["playback_mode"] == "remux_audio"
            token_mkv = playback_token(secret, user_id=1, source_id=mkv_source.id, ttl_s=600)
            remuxed = http.get(f"/remux/{mkv_source.id}", params={"token": token_mkv})
            assert remuxed.status_code == 200 and remuxed.headers["x-playback-mode"] == "remux_audio"
            assert remuxed.content[4:8] == b"ftyp" and b"moof" in remuxed.content
            out = tmp_path / "remuxed.mp4"
            out.write_bytes(remuxed.content)
            info = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
                check=True, capture_output=True).stdout)
            codecs = sorted(s["codec_name"] for s in info["streams"])
            assert codecs == ["aac", "h264"]
            report["remux_bytes"] = len(remuxed.content)

            # 5) Sólo se descargó el video: readme.txt tenía prioridad 0.
            wait_until(lambda: http.get(f"/api/sources/{mp4_source.id}/status", headers=service).json()["progress"] >= 1.0,
                       60, "el MP4 no terminó de descargar")
            readme = settings.downloads_dir / mp4_source.info_hash / "Demo" / "readme.txt"
            assert not readme.exists() or readme.read_bytes() != (content / "Demo" / "readme.txt").read_bytes()
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        del seeder

    with session_scope(factory) as s:
        stored = s.get(MediaSource, mp4_source.id)
        assert stored.status is SourceStatus.COMPLETE and stored.video_codec == "h264"
        assert stored.playback_mode is PlaybackMode.DIRECT
        assert s.get(MediaSource, mkv_source.id).playback_mode is PlaybackMode.REMUX_AUDIO
    resume_files = sorted(p.name for p in settings.resume_dir.glob("*.fastresume"))
    assert f"{mp4_source.info_hash}.fastresume" in resume_files
    print("\nE2E:", report)
