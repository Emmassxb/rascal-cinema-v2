"""Rutas de la interfaz web (TestClient): orden de la ficha, arranque bajo demanda, permisos y TMDB."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rascal.web as web
from rascal.data.db import session_scope
from rascal.domain.schemas import MagnetSourceForm
from rascal.domain.services.catalog import CatalogService
from rascal.web import mount_web

from test_catalog import MAGNET, movie_form
from conftest import PASSWORD


@pytest.fixture
def app(factory, settings):  # type: ignore[no-untyped-def]
    application = FastAPI()
    mount_web(application, settings, session_factory=factory)
    return application


@pytest.fixture
def client(app):  # type: ignore[no-untyped-def]
    return TestClient(app)


@pytest.fixture
def film(factory, settings, users):  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        mid = CatalogService(s, users["partner"], settings).create_movie(movie_form(trailer_url="https://youtu.be/dQw4w9WgXcQ"))
        source = CatalogService(s, users["partner"], settings).add_magnet_source(
            mid, MagnetSourceForm(magnet_uri=MAGNET, label="1080p", audio_language="Español Latino", quality="1080p",
                                  rights_statement="Licencia de prueba"))
        return {"slug": "big-buck-bunny-2008", "source_id": source.id}


def login(client: TestClient, username: str) -> None:
    response = client.post("/login", data={"username": username, "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def test_movie_page_orders_sections_info_trailer_p2p_reviews(client, film, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(web, "_source_live", lambda cfg, sid: None)
    html = client.get(f"/movie/{film['slug']}").text
    positions = [html.index(m) for m in ('class="hero', 'class="panel trailer"', 'id="p2p"', 'id="resenas"')]
    assert positions == sorted(positions)


def test_opening_the_movie_never_starts_a_download(client, film, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started: list[int] = []
    monkeypatch.setattr(web, "_source_live", lambda cfg, sid: None)
    monkeypatch.setattr(web.StreamerClient, "start", lambda self, sid: started.append(sid) or {})
    client.get(f"/movie/{film['slug']}")
    client.get(f"/movie/{film['slug']}?source={film['source_id']}")  # seleccionar versión tampoco arranca
    assert started == []


def test_play_request_starts_only_when_asked(client, film, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started: list[int] = []
    monkeypatch.setattr(web, "_source_live", lambda cfg, sid: None)
    monkeypatch.setattr(web.StreamerClient, "start", lambda self, sid: started.append(sid) or {})
    monkeypatch.setattr(web.StreamerClient, "player_url",
                        lambda self, user_id, source_id: f"http://127.0.0.1:8001/player/{source_id}?token=t")
    login(client, "ana")
    html = client.get(f"/movie/{film['slug']}?source={film['source_id']}&play=1").text
    assert started == [film["source_id"]]
    assert 'id="rascal-player"' in html and "/player/" in html


def test_anonymous_play_request_asks_for_login_and_starts_nothing(client, film, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started: list[int] = []
    monkeypatch.setattr(web, "_source_live", lambda cfg, sid: None)
    monkeypatch.setattr(web.StreamerClient, "start", lambda self, sid: started.append(sid) or {})
    html = client.get(f"/movie/{film['slug']}?play=1").text
    assert started == []
    assert "Inicia sesión para reproducir" in html


def test_settings_requires_login(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_tmdb_lookup_is_partner_only(client) -> None:  # type: ignore[no-untyped-def]
    login(client, "ana")
    response = client.get("/partner/tmdb-lookup", params={"ref": "129"})
    assert response.status_code == 403 and response.json()["ok"] is False


def test_purge_is_partner_only(client, film) -> None:  # type: ignore[no-untyped-def]
    login(client, "ana")
    response = client.post(f"/movie/{film['slug']}/sources/{film['source_id']}/purge", follow_redirects=False)
    assert response.status_code == 303
    assert "err=" in response.headers["location"] and "partners" in response.headers["location"]


def test_avatars_and_subtitles_reject_path_traversal(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/avatars/..%2F..%2Fsecret.txt").status_code == 404
    assert client.get("/subtitles/1/..%2Fsecret.vtt").status_code == 404


def test_partner_request_review_is_admin_only(client, users) -> None:  # type: ignore[no-untyped-def]
    login(client, "partner")
    response = client.post("/partner/requests/1/approve", follow_redirects=False)
    assert response.status_code == 303 and "Solo" in response.headers["location"]
