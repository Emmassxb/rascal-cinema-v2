"""API de streaming con el motor local: Range/206, tokens, estado y reproductor."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rascal.auth.tokens import playback_token
from rascal.data.db import session_scope
from rascal.data.enums import SourceStatus
from rascal.data.models import MediaSource
from rascal.domain.schemas import MovieForm, TorrentSourceForm
from rascal.domain.services.catalog import CatalogService
from rascal.streaming.api import DbListener, create_app
from rascal.streaming.client import StreamerClient
from rascal.streaming.engine import LocalFileEngine

from conftest import make_torrent

PAYLOAD = bytes(range(256)) * 400  # 102 400 bytes


@pytest.fixture
def source_id(factory, settings, users) -> int:  # type: ignore[no-untyped-def]
    data = make_torrent({"Film/film.mp4": PAYLOAD, "Film/notes.txt": b"hola"}, name="Pack")
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        movie = svc.create_movie(MovieForm(title="Prueba", release_year=2020))
        source = svc.add_torrent_source(movie, TorrentSourceForm(rights_statement="Dominio público"), data)
    target = settings.local_media_dir / source.info_hash / "Pack" / "Film" / "film.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(PAYLOAD)
    return source.id


@pytest.fixture
def client(settings, factory, source_id):  # type: ignore[no-untyped-def]
    app = create_app(settings, engine=LocalFileEngine(settings, DbListener(factory)), session_factory=factory)
    with TestClient(app) as test_client:
        yield test_client


def token_for(settings, source_id: int) -> str:  # type: ignore[no-untyped-def]
    return playback_token(settings.secret_key.get_secret_value(), user_id=1, source_id=source_id, ttl_s=60)


def test_range_requests(client, settings, source_id) -> None:  # type: ignore[no-untyped-def]
    token = token_for(settings, source_id)
    head = client.head(f"/stream/{source_id}", params={"token": token})
    assert head.status_code == 200 and head.headers["content-length"] == str(len(PAYLOAD))
    assert head.headers["accept-ranges"] == "bytes" and head.headers["content-type"] == "video/mp4"

    part = client.get(f"/stream/{source_id}", params={"token": token}, headers={"Range": "bytes=1000-1999"})
    assert part.status_code == 206 and part.content == PAYLOAD[1000:2000]
    assert part.headers["content-range"] == f"bytes 1000-1999/{len(PAYLOAD)}"

    tail = client.get(f"/stream/{source_id}", params={"token": token}, headers={"Range": "bytes=-300"})
    assert tail.status_code == 206 and tail.content == PAYLOAD[-300:]

    full = client.get(f"/stream/{source_id}", params={"token": token})
    assert full.status_code == 200 and full.content == PAYLOAD

    bad = client.get(f"/stream/{source_id}", params={"token": token}, headers={"Range": f"bytes={len(PAYLOAD)}-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{len(PAYLOAD)}"


def test_token_enforcement(client, settings, source_id) -> None:  # type: ignore[no-untyped-def]
    assert client.get(f"/stream/{source_id}").status_code == 401
    other = token_for(settings, source_id + 100)
    assert client.get(f"/stream/{source_id}", params={"token": other}).status_code == 403
    assert client.post(f"/api/sources/{source_id}/start").status_code == 401
    assert client.post(f"/api/sources/{source_id}/start", headers={"X-Service-Key": "nope"}).status_code == 401


def test_control_status_and_persistence(client, settings, factory, source_id) -> None:  # type: ignore[no-untyped-def]
    started = client.post(f"/api/sources/{source_id}/start", headers={"X-Service-Key": "svc-test-key"})
    assert started.status_code == 200 and started.json()["head_ready"] is True
    status = client.get(f"/api/sources/{source_id}/status", params={"token": token_for(settings, source_id)}).json()
    assert status["progress"] == 1.0 and status["playback_mode"] == "direct" and status["db_status"] == "complete"
    with session_scope(factory) as s:
        source = s.get(MediaSource, source_id)
        assert source.status is SourceStatus.COMPLETE and source.file_size == len(PAYLOAD)
    health = client.get("/health").json()
    assert health == {"status": "ok", "engine": "local", "active_torrents": 1}


def test_player_page(client, settings, source_id) -> None:  # type: ignore[no-untyped-def]
    token = token_for(settings, source_id)
    page = client.get(f"/player/{source_id}", params={"token": token})
    assert page.status_code == 200 and "video.js" in page.text and '"mode": "direct"' in page.text
    assert f"/stream/{source_id}?token=" in page.text
    assert page.headers["content-security-policy"] == "frame-ancestors 'self' http://localhost:8501"
    assert "$config_json" not in page.text


def test_disabled_source_is_refused(client, settings, factory, source_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        s.get(MediaSource, source_id).status = SourceStatus.DISABLED
    assert client.get(f"/stream/{source_id}", params={"token": token_for(settings, source_id)}).status_code == 409


def test_streamer_client(settings, client, source_id) -> None:  # type: ignore[no-untyped-def]
    api = StreamerClient(settings, transport=client._transport)  # noqa: SLF001 — transporte en memoria
    assert api.health()["engine"] == "local"
    assert api.start(source_id)["file_size"] == len(PAYLOAD)
    url = api.player_url(user_id=7, source_id=source_id)
    assert url.startswith(f"http://localhost:8001/player/{source_id}?token=")
