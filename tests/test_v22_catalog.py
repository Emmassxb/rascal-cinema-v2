"""v2.2 · Series, fuentes directas/HLS, "Dónde ver", biografías y agregados/paginación."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import rascal.web as web
from rascal.auth.rbac import PermissionDenied
from rascal.auth.tokens import playback_token
from rascal.data.db import session_scope
from rascal.data.enums import MediaType, SourceKind
from rascal.data.models import Episode, MediaSource, Movie, Person, Review, Season, WatchlistEntry
from rascal.domain.schemas import (
    DirectSourceForm,
    EpisodeForm,
    MagnetSourceForm,
    PersonForm,
    SeasonForm,
    WatchLinkForm,
)
from rascal.domain.services.catalog import CatalogError, CatalogService
from rascal.domain.services.people import PersonService, can_edit_person
from rascal.domain.services.queries import CatalogQueries
from rascal.domain.services.series import SeriesService
from rascal.domain.services.watch import WatchLinkService
from rascal.integrations.tmdb import TmdbEpisode, TmdbSeason, build_person, build_series, build_watch_offers
from rascal.streaming.api import DbListener, create_app
from rascal.streaming.engine import LocalFileEngine
from rascal.web import mount_web

from conftest import PASSWORD
from test_catalog import MAGNET, movie_form

MP4 = "https://cdn.example.com/peliculas/bbb-1080p.mp4"
HLS = "https://cdn.example.com/hls/master.m3u8"


def direct(url: str = MP4, **kw):  # type: ignore[no-untyped-def]
    return DirectSourceForm(stream_url=url, rights_statement="CC BY 3.0", **kw)


@pytest.fixture
def series_id(factory, settings, users) -> int:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        return CatalogService(s, users["partner"], settings).create_movie(
            movie_form(title="Serie Demo", media_type=MediaType.SERIES, imdb_id=None, tmdb_id=1399)
        )


@pytest.fixture
def season_and_episode(factory, users, series_id):  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = SeriesService(s, users["partner"])
        season = svc.add_season(series_id, SeasonForm(season_number=1, title="Uno"))
        ep1 = svc.add_episode(season, EpisodeForm(episode_number=1, title="Piloto", runtime_min=45))
        ep2 = svc.add_episode(season, EpisodeForm(episode_number=2, title="Segundo"))
    return season, ep1, ep2


# ── Series ───────────────────────────────────────────────────────────────────
def test_movie_and_series_can_share_tmdb_number(factory, settings, users, series_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        movie_id = svc.create_movie(movie_form(tmdb_id=1399))  # película 1399 ≠ serie 1399 en TMDB
        assert movie_id != series_id
        with pytest.raises(CatalogError):
            svc.create_movie(movie_form(title="Otra", imdb_id=None, tmdb_id=1399, media_type=MediaType.SERIES))


def test_seasons_and_episodes_crud(factory, users, series_id, season_and_episode) -> None:  # type: ignore[no-untyped-def]
    season, ep1, _ = season_and_episode
    with session_scope(factory) as s:
        svc = SeriesService(s, users["partner"])
        with pytest.raises(CatalogError):
            svc.add_season(series_id, SeasonForm(season_number=1))
        with pytest.raises(CatalogError):
            svc.add_episode(season, EpisodeForm(episode_number=2))
        svc.update_episode(ep1, EpisodeForm(episode_number=1, title="Piloto (editado)", synopsis="x" * 3000))
    with session_scope(factory) as s:
        assert s.get(Episode, ep1).title == "Piloto (editado)"
        with pytest.raises(PermissionDenied):
            SeriesService(s, users["partner2"]).add_season(series_id, SeasonForm(season_number=2))
        detail = CatalogQueries(s, None).movie_detail(movie_id=series_id)
        assert detail.is_series and detail.seasons[0].episode_count == 2


def test_series_sources_live_on_episodes(factory, settings, users, series_id, season_and_episode) -> None:  # type: ignore[no-untyped-def]
    _, ep1, ep2 = season_and_episode
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        with pytest.raises(CatalogError):
            svc.add_direct_source(series_id, direct())  # una serie no tiene fuentes «sueltas»
        a = svc.add_direct_source(series_id, direct(audio_language="Español Latino"), episode_id=ep1)
        b = svc.add_magnet_source(series_id, MagnetSourceForm(magnet_uri=MAGNET, rights_statement="CC BY"), episode_id=ep2)
        assert (a.episode_id, b.episode_id) == (ep1, ep2)
        assert [x.id for x in svc.list_sources(series_id, episode_id=ep1)] == [a.id]
    with session_scope(factory) as s:
        SeriesService(s, users["partner"]).delete_episode(ep2)
    with session_scope(factory) as s:
        assert s.scalar(select(MediaSource.id).where(MediaSource.episode_id == ep2)) is None


def test_tmdb_season_import_is_additive(factory, users, series_id, season_and_episode) -> None:  # type: ignore[no-untyped-def]
    _, ep1, _ = season_and_episode
    tmdb = [
        TmdbSeason(1, "Temporada 1", "Resumen", None, 2011, 10, (
            TmdbEpisode(1, "Se acerca el invierno", "Sinopsis TMDB", 62, "https://image.tmdb.org/t/p/w300/a.jpg", "2011-04-17", 63056),
            TmdbEpisode(3, "Lord Nieve", "Otra", 58, None, "2011-05-01", 63058),
        )),
        TmdbSeason(2, None, None, None, 2012, 11, (TmdbEpisode(1, "El Norte no olvida", None, None, None, None, 1),)),
    ]
    with session_scope(factory) as s:
        stats = SeriesService(s, users["partner"]).import_tmdb_seasons(series_id, tmdb)
    assert stats["seasons_created"] == 1 and stats["episodes_created"] == 2
    with session_scope(factory) as s:
        ep = s.get(Episode, ep1)
        assert ep.title == "Piloto"  # lo editado por el partner no se pisa
        assert ep.synopsis == "Sinopsis TMDB" and ep.runtime_min == 45 and ep.still_path.endswith("a.jpg")
        assert s.get(Season, ep.season_id).title == "Uno"


def test_build_series_maps_tv_payload() -> None:
    series = build_series({
        "id": 1399, "name": "Juego de tronos", "original_name": "Game of Thrones", "overview": "Siete reinos.",
        "first_air_date": "2011-04-17", "episode_run_time": [60], "genres": [{"name": "Drama"}],
        "created_by": [{"name": "David Benioff"}], "credits": {"cast": [{"name": "Emilia Clarke", "character": "Daenerys", "order": 0}]},
        "videos": {"results": [{"site": "YouTube", "key": "KPLWWIOCOOQ", "type": "Trailer", "official": True}]},
    })
    fields = series.to_form_fields()
    assert fields["media_type"] == "series" and fields["release_year"] == "2011" and fields["directors"] == "David Benioff"
    assert fields["trailer_url"].endswith("KPLWWIOCOOQ") and fields["runtime_min"] == "60"


def test_public_series_page_tabs_episodes_and_direct_play(factory, settings, users, series_id, season_and_episode, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, ep1, _ = season_and_episode
    with session_scope(factory) as s:
        src = CatalogService(s, users["partner"], settings).add_direct_source(series_id, direct(), episode_id=ep1)
        slug = s.get(Movie, series_id).slug
    app = FastAPI()
    mount_web(app, settings, session_factory=factory)
    client = TestClient(app)
    html = client.get(f"/movie/{slug}").text
    assert 'class="season-tabs"' in html and "Piloto" in html and "Segundo" in html
    assert "Elige un capítulo" in html
    started: list[int] = []
    monkeypatch.setattr(web.StreamerClient, "start", lambda self, sid: started.append(sid) or {})
    client.post("/login", data={"username": "ana", "password": PASSWORD})
    page = client.get(f"/movie/{slug}?season=1&episode={ep1}&source={src.id}&play=1").text
    assert 'id="rascal-player"' in page and f"/player/{src.id}" in page
    assert started == []  # enlace directo: nunca arranca libtorrent
    assert "Privacidad P2P" not in page


# ── Fuentes directas / HLS ───────────────────────────────────────────────────
def test_direct_and_hls_sources(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        mid = svc.create_movie(movie_form())
        mp4 = svc.add_direct_source(mid, direct(quality="1080p"))
        hls = svc.add_direct_source(mid, direct(HLS))
        assert (mp4.kind, mp4.source_type, mp4.is_p2p, mp4.container) == (SourceKind.DIRECT_URL, "direct_url", False, "mp4")
        assert hls.kind is SourceKind.HLS and hls.info_hash is None
        for bad in ("http://cdn.example.com/a.mp4", "https://127.0.0.1/a.mp4", "javascript:alert(1)", "https://cdn.example.com/a.avi"):
            with pytest.raises(CatalogError):
                svc.add_direct_source(mid, direct(bad))
        with pytest.raises(CatalogError):
            svc.add_direct_source(mid, direct())  # duplicado


def test_streamer_serves_direct_source_without_engine(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        mid = svc.create_movie(movie_form())
        es = svc.add_direct_source(mid, direct(audio_language="Español Latino", quality="1080p"))
        en = svc.add_direct_source(mid, direct(MP4 + "?lang=en", audio_language="Inglés", quality="720p"))
    engine = LocalFileEngine(settings, DbListener(factory))
    app = create_app(settings, engine=engine, session_factory=factory)
    token = playback_token(settings.secret_key.get_secret_value(), user_id=1, source_id=es.id, ttl_s=60)
    with TestClient(app) as client:
        page = client.get(f"/player/{es.id}", params={"token": token, "t": 42})
        assert page.status_code == 200
        assert '"mode": "external"' in page.text and MP4 in page.text and '"startAt": 42.0' in page.text
        assert '"audio": "Ingl\\u00e9s"' in page.text and f"/player/{en.id}?token=" in page.text
        assert not engine.has(es.id)
        assert client.get(f"/stream/{es.id}", params={"token": token}).status_code == 409
        start = client.post(f"/api/sources/{es.id}/start", headers={"X-Service-Key": "svc-test-key"})
        assert start.json()["state"] == "direct" and not engine.has(es.id)
        purge = client.post(f"/api/sources/{es.id}/purge", headers={"X-Service-Key": "svc-test-key"})
        assert purge.status_code == 409


# ── Dónde ver ────────────────────────────────────────────────────────────────
def test_watch_links_and_rendering(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        mid = CatalogService(s, users["partner"], settings).create_movie(movie_form())
        svc = WatchLinkService(s, users["partner"])
        svc.add(mid, WatchLinkForm(platform_name="Apple TV", url="https://tv.apple.com/mx/movie/bbb", offer_type="rent"))
        link = svc.add(mid, WatchLinkForm(platform_name="Mubi", url="https://mubi.com/es/mx/films/bbb", offer_type="subscription"))
        with pytest.raises(CatalogError):
            svc.add(mid, WatchLinkForm(platform_name="Mubi", url="https://mubi.com/otra", offer_type="subscription"))
        with pytest.raises(PermissionDenied):
            WatchLinkService(s, users["partner2"]).delete(link.id)
        slug = s.get(Movie, mid).slug
    with pytest.raises(ValueError):
        WatchLinkForm(platform_name="X", url="javascript:alert(1)")
    app = FastAPI()
    mount_web(app, settings, session_factory=factory)
    html = TestClient(app).get(f"/movie/{slug}").text
    assert 'href="https://mubi.com/es/mx/films/bbb" target="_blank" rel="noopener noreferrer"' in html
    assert html.index("Mubi") < html.index("Apple TV")  # suscripción antes que renta
    assert html.index('id="donde-ver"') < html.index('class="panel trailer"') if 'class="panel trailer"' in html else True


def test_build_watch_offers_mx() -> None:
    payload = {"results": {"MX": {"link": "https://www.themoviedb.org/movie/1/watch?locale=MX",
                                  "flatrate": [{"provider_name": "Netflix", "logo_path": "/n.jpg", "display_priority": 1}],
                                  "rent": [{"provider_name": "Apple TV", "display_priority": 2}]}}}
    offers = build_watch_offers(payload, "mx")
    assert [(o.platform_slug, o.offer_type) for o in offers] == [("netflix", "subscription"), ("apple-tv", "rent")]
    assert offers[0].logo_url.endswith("/n.jpg") and build_watch_offers(payload, "US") == []


# ── Personas ─────────────────────────────────────────────────────────────────
def test_person_bio_edit_permissions_and_public_page(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).create_movie(movie_form())
        person = s.scalars(select(Person).where(Person.name == "Bunny")).one()
        assert can_edit_person(s, users["partner"], person.id) and can_edit_person(s, users["admin"], person.id)
        assert not can_edit_person(s, users["partner2"], person.id) and not can_edit_person(s, users["ana"], person.id)
        long_bio = ("Párrafo largo. " * 4000).strip()  # ≈ 60 000 caracteres
        PersonService(s, users["partner"]).update(person.id, PersonForm(
            name="Bunny", bio=long_bio, place_of_birth="Ámsterdam", birth_date="2008-04-10", tmdb_person_id=99))
        with pytest.raises(PermissionDenied):
            PersonService(s, users["partner2"]).update(person.id, PersonForm(name="Hack"))
        slug, pid = person.slug, person.id
    with session_scope(factory) as s:
        stored = s.get(Person, pid)
        assert len(stored.bio) == len(long_bio) and stored.tmdb_person_id == 99
    app = FastAPI()
    mount_web(app, settings, session_factory=factory)
    client = TestClient(app)
    html = client.get(f"/person/{slug}").text
    assert "Ámsterdam" in html and "10/04/2008" in html and "Editar bio" not in html
    client.post("/login", data={"username": "partner", "password": PASSWORD})
    assert f"/partner/person/{pid}" in client.get(f"/person/{slug}").text
    saved = client.post(f"/partner/person/{pid}", data={"name": "Bunny", "bio": "Nueva bio"}, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"].startswith(f"/person/{slug}")


def test_build_person_falls_back_to_english_bio() -> None:
    person = build_person({"id": 5, "name": "Ana", "biography": "", "birthday": "1980-02-03", "profile_path": "/p.jpg"},
                          {"id": 5, "biography": "English bio"})
    assert person.bio == "English bio" and person.filled_from_fallback and person.photo_url.endswith("/p.jpg")


# ── Agregados y paginación ───────────────────────────────────────────────────
def test_stats_follow_reviews_and_watchlist(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        mid = CatalogService(s, users["partner"], settings).create_movie(movie_form())
        s.add_all([Review(user_id=users["ana"].user_id, movie_id=mid, rating=8),
                   Review(user_id=users["beto"].user_id, movie_id=mid, rating=4),
                   WatchlistEntry(user_id=users["ana"].user_id, movie_id=mid)])
    with session_scope(factory) as s:
        movie = s.get(Movie, mid)
        assert (movie.rating_avg, movie.rating_count, movie.review_count, movie.watchlist_count, movie.popularity) == (6.0, 2, 2, 1, 3)
        s.delete(s.scalars(select(Review).where(Review.rating == 4)).one())
    with session_scope(factory) as s:
        assert s.get(Movie, mid).rating_avg == 8.0


def test_browse_and_partner_panel_are_paginated(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        for i in range(70):
            s.add(Movie(slug=f"m-{i}", title=f"Película {i:03d}", owner_id=users["partner"].user_id,
                        media_type=MediaType.SERIES if i % 10 == 0 else MediaType.MOVIE))
    app = FastAPI()
    mount_web(app, settings, session_factory=factory)
    client = TestClient(app)
    page1 = client.get("/browse?sort=title").text
    assert page1.count('class="card"') == web.PAGE_SIZE and "Página 1 de 2" in page1
    assert client.get("/browse?sort=title&page=2").text.count('class="card"') == 10
    assert client.get("/browse?type=series").text.count('class="card"') == 7
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    panel = client.get("/partner").text
    assert panel.count("/partner/movie/") - panel.count("/partner/movie/new") <= web.PAGE_SIZE * 2
    assert "Página 1 de 2" in panel
    assert "Película 001" in client.get("/partner?pq=001").text


# ── Panel Partner: las pantallas nuevas se renderizan ────────────────────────
def test_partner_screens_render(factory, settings, users, series_id, season_and_episode) -> None:  # type: ignore[no-untyped-def]
    _, ep1, _ = season_and_episode
    with session_scope(factory) as s:
        mid = CatalogService(s, users["partner"], settings).create_movie(movie_form(title="Peli", imdb_id=None))
        CatalogService(s, users["partner"], settings).add_magnet_source(
            mid, MagnetSourceForm(magnet_uri=MAGNET, label="1080p", rights_statement="CC BY"))
        person_id = s.scalars(select(Person.id).where(Person.name == "Bunny")).first()
        slug = s.get(Movie, mid).slug
    app = FastAPI()
    mount_web(app, settings, session_factory=factory)
    client = TestClient(app)
    client.post("/login", data={"username": "partner", "password": PASSWORD})
    series_editor = client.get(f"/partner/movie/{series_id}").text
    assert 'id="temporadas"' in series_editor and f"/partner/episode/{ep1}" in series_editor
    assert 'id="donde-ver"' in series_editor and "Editar bio" in series_editor
    movie_editor = client.get(f"/partner/movie/{mid}").text
    assert "/add-direct" in movie_editor and 'id="fuentes"' in movie_editor
    episode_editor = client.get(f"/partner/episode/{ep1}").text
    assert f"/partner/episode/{ep1}/add-direct" in episode_editor and "Subtítulos del capítulo" in episode_editor
    assert 'value="series" selected' in client.get("/partner/movie/new?type=series").text
    assert "Biografía de Bunny" in client.get(f"/partner/person/{person_id}").text
    # Elegir una fuente P2P muestra el aviso de privacidad (sin arrancar nada).
    assert "Privacidad P2P" in client.get(f"/movie/{slug}").text
    # Subir un capítulo nuevo, una temporada y un enlace por formulario.
    r = client.post(f"/partner/series/{series_id}/seasons", data={"season_number": "2", "title": "Dos"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    r = client.post(f"/partner/episode/{ep1}/add-direct",
                    data={"stream_url": HLS, "label": "HLS", "rights_statement": "CC BY"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    r = client.post(f"/partner/movie/{mid}/watch-links",
                    data={"platform_name": "Netflix", "url": "http://localhost/x"}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # URL interna rechazada
    other = TestClient(app)
    other.post("/login", data={"username": "partner2", "password": PASSWORD})
    r = other.post(f"/partner/episode/{ep1}/add-direct",
                   data={"stream_url": MP4, "rights_statement": "CC BY"}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # otro partner no puede tocar esta serie
