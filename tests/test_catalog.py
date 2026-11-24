"""Panel Partner, RBAC por propiedad, navegación relacional, métricas, social y analítica."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from rascal.auth.rbac import PermissionDenied
from rascal.data.db import session_scope
from rascal.data.enums import CreditRole, PlaybackMode, SourceStatus
from rascal.data.models import AuditLog, Movie, MovieCredit
from rascal.domain.analytics import ANALYSES, run_analysis, year_rating_regression
from rascal.domain.schemas import FilmographyFilters, MagnetSourceForm, MovieForm, ReviewForm, TorrentSourceForm
from rascal.domain.services.catalog import CatalogError, CatalogService
from rascal.domain.services.queries import CatalogQueries
from rascal.domain.services.reviews import ReviewService
from rascal.domain.services.social import SocialService

from conftest import make_torrent

MAGNET = "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny"


def movie_form(**overrides) -> MovieForm:  # type: ignore[no-untyped-def]
    data = {
        "title": "Big Buck Bunny",
        "release_year": 2008,
        "runtime_min": 10,
        "synopsis": "Un conejo gigante contra tres roedores.",
        "imdb_id": "tt1254207",
        "genres": ["Animación", "Comedia"],
        "companies": ["Blender Institute"],
        "directors": [{"name": "Sacha Goedegebure"}],
        "cast": [{"name": "Bunny", "character_name": "Big Buck"}, {"name": "Frank", "character_name": "Ardilla"}],
        "crew": [{"name": "Jan Morgenstern", "role": "composer"}],
        "tech": {"aspect_ratio": "16:9", "color": "color", "original_language": "en"},
    }
    data.update(overrides)
    return MovieForm.model_validate(data)


@pytest.fixture
def movie_id(factory, settings, users) -> int:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        return CatalogService(s, users["partner"], settings).create_movie(movie_form())


def test_partner_creates_movie_with_relations(factory, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        detail = CatalogQueries(s, users["ana"]).movie_detail(movie_id=movie_id)
        owner = s.get(Movie, movie_id).owner_id
    assert detail is not None and detail.slug == "big-buck-bunny-2008"
    assert owner == users["partner"].user_id
    assert [g.name for g in detail.genres] == ["Animación", "Comedia"]
    assert [d.name for d in detail.directors] == ["Sacha Goedegebure"]
    assert [(c.person.name, c.character_name) for c in detail.cast] == [("Bunny", "Big Buck"), ("Frank", "Ardilla")]
    assert detail.crew[0].role is CreditRole.COMPOSER
    assert detail.tech is not None and detail.tech.aspect_ratio == "16:9"
    assert detail.companies[0].slug == "blender-institute"


def test_ownership_rbac(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PermissionDenied), session_scope(factory) as s:
        CatalogService(s, users["partner2"], settings).update_movie(movie_id, movie_form(title="Hackeada"))
    with pytest.raises(PermissionDenied), session_scope(factory) as s:
        CatalogService(s, users["ana"], settings).create_movie(movie_form(title="Otra", imdb_id=None))
    with session_scope(factory) as s:  # el admin edita cualquier película
        CatalogService(s, users["admin"], settings).update_movie(movie_id, movie_form(tagline="Editada por admin"))
    with session_scope(factory) as s:
        assert s.get(Movie, movie_id).tagline == "Editada por admin"
        assert s.get(Movie, movie_id).title == "Big Buck Bunny"


def test_update_replaces_relations_transactionally(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    form = movie_form(
        title="Big Buck Bunny (Remaster)",
        genres=["Comedia", "Familia"],
        cast=[{"name": "Frank", "character_name": "Ardilla líder"}, {"name": "Bunny", "character_name": "Big Buck"}],
        crew=[],
        tech={},
    )
    with session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).update_movie(movie_id, form)
    with session_scope(factory) as s:
        detail = CatalogQueries(s).movie_detail(movie_id=movie_id)
        assert detail.slug == "big-buck-bunny-2008"  # slug estable: no rompe enlaces
        assert [g.name for g in detail.genres] == ["Comedia", "Familia"]
        assert [c.person.name for c in detail.cast] == ["Frank", "Bunny"]
        assert detail.crew == [] and detail.tech is None
        assert s.scalar(select(func.count()).select_from(MovieCredit)) == 3
        actions = list(s.scalars(select(AuditLog.action).order_by(AuditLog.id)))
        assert actions[-2:] == ["movie.create", "movie.update"]

    # Un error a mitad (persona inexistente por id) revierte TODO, también el título.
    bad = movie_form(title="No debería guardarse", directors=[{"id": 9999, "name": "Fantasma"}])
    with pytest.raises(CatalogError), session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).update_movie(movie_id, bad)
    with session_scope(factory) as s:
        assert s.get(Movie, movie_id).title == "Big Buck Bunny (Remaster)"


def test_slugs_and_external_ids(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CatalogError, match="IMDb"), session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).create_movie(movie_form())
    with session_scope(factory) as s:
        other = CatalogService(s, users["partner"], settings).create_movie(movie_form(imdb_id=None))
        assert s.get(Movie, other).slug == "big-buck-bunny-2008-2"


def test_sources_magnet_and_torrent(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    rights = "CC BY 3.0 — Blender Foundation"
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        magnet = svc.add_magnet_source(movie_id, MagnetSourceForm(magnet_uri=MAGNET, rights_statement=rights))
        data = make_torrent({"Bunny/bunny.mp4": b"v" * 50_000, "Bunny/poster.jpg": b"j" * 1000}, name="BBB")
        torrent = svc.add_torrent_source(movie_id, TorrentSourceForm(label="1080p", rights_statement=rights), data)
    assert magnet.info_hash == "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c" and magnet.status is SourceStatus.PENDING
    assert torrent.file_path == "BBB/Bunny/bunny.mp4" and torrent.playback_mode is PlaybackMode.DIRECT

    with pytest.raises(CatalogError, match="ya está vinculada"), session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).add_magnet_source(
            movie_id, MagnetSourceForm(magnet_uri=MAGNET, rights_statement=rights))
    with pytest.raises(CatalogError, match="video"), session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).add_torrent_source(
            movie_id, TorrentSourceForm(rights_statement=rights), make_torrent({"a/readme.txt": b"x"}, name="Docs"))
    with pytest.raises(PermissionDenied), session_scope(factory) as s:
        CatalogService(s, users["partner2"], settings).add_magnet_source(
            movie_id, MagnetSourceForm(magnet_uri=MAGNET.replace("dd82", "aa82"), rights_statement=rights))

    with session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).update_source(magnet.id, enabled=False)
    with session_scope(factory) as s:
        assert [x.id for x in CatalogQueries(s, users["ana"]).movie_detail(movie_id=movie_id).sources] == [torrent.id]
        assert len(CatalogQueries(s, users["partner"]).movie_detail(movie_id=movie_id).sources) == 2


def test_unpublished_visibility(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        CatalogService(s, users["partner"], settings).set_published(movie_id, False)
    with session_scope(factory) as s:
        assert CatalogQueries(s, users["ana"]).movie_detail(movie_id=movie_id) is None
        assert CatalogQueries(s, users["partner"]).movie_detail(movie_id=movie_id) is not None


def test_reviews_metrics_votes(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        r1 = ReviewService(s, users["ana"]).add(movie_id, ReviewForm(rating=8, body="¡Genial!"))
        ReviewService(s, users["beto"]).add(movie_id, ReviewForm(rating=7))
        up, down = ReviewService(s, users["beto"]).vote(r1, 1)
    assert (up, down) == (1, 0)
    with pytest.raises(PermissionDenied), session_scope(factory) as s:
        ReviewService(s, users["ana"]).vote(r1, 1)
    with session_scope(factory) as s:
        rating = CatalogQueries(s).movie_detail(movie_id=movie_id).rating
        reviews = CatalogQueries(s).movie_reviews(movie_id, order="popular")
    assert (rating.count, rating.average, rating.median, rating.stdev) == (2, 7.5, 7.5, 0.5)
    assert rating.average_stars == 4.0  # Math.round(7.5) / 2
    assert [b.value for b in rating.present_bins] == [7, 8]
    assert reviews[0].id == r1 and reviews[0].votes_up == 1 and reviews[0].star_label == "★★★★"


def test_entity_profiles_and_navigation(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = CatalogService(s, users["partner"], settings)
        second = svc.create_movie(movie_form(title="Sintel", release_year=2010, imdb_id=None,
                                             directors=[{"name": "Colin Levy"}],
                                             cast=[{"name": "Halina Reijn", "character_name": "Sintel"}],
                                             crew=[{"name": "Sacha Goedegebure", "role": "producer"}]))
        ReviewService(s, users["ana"]).add(movie_id, ReviewForm(rating=10))
        ReviewService(s, users["beto"]).add(second, ReviewForm(rating=6))
        SocialService(s, users["ana"]).toggle_watchlist(second)

    with session_scope(factory) as s:
        q = CatalogQueries(s, users["ana"])
        person = q.entity_profile("person", "sacha-goedegebure")
        directed = q.entity_profile("person", "sacha-goedegebure", FilmographyFilters(role=CreditRole.DIRECTOR))
        genre = q.entity_profile("genre", "comedia", FilmographyFilters(sort="rating"))
        company = q.entity_profile("company", "blender-institute", FilmographyFilters(year_from=2009))
        assert q.entity_profile("person", "no-existe") is None

    assert person is not None and person.metrics.films == 2 and person.metrics.total_reviews == 2
    assert {r.role for r in person.roles} == {CreditRole.DIRECTOR, CreditRole.PRODUCER}
    by_slug = {c.slug: c for c in person.filmography}
    assert by_slug["sintel-2010"].roles == [CreditRole.PRODUCER]
    assert directed.metrics.films == 1 and directed.metrics.average == 10.0
    assert person.metrics.average == 8.0 and person.metrics.average_of_film_averages == 8.0
    assert person.metrics.watchlist_count == 1 and person.metrics.reviews_last_30d == 2
    assert person.metrics.popularity_score == 2 + 0.5 + 4  # 2 usuarios + 0.5·1 watchlist + 2·2 recientes
    assert [c.title for c in genre.filmography] == ["Big Buck Bunny", "Sintel"]
    assert [c.title for c in company.filmography] == ["Sintel"] and company.filmography_total == 1


def test_search_and_social(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        social = SocialService(s, users["ana"])
        assert social.follow("beto") and social.follow("partner")  # el bug de Mongo impedía el segundo
        assert not social.follow("beto")  # idempotente
        ReviewService(s, users["beto"]).add(movie_id, ReviewForm(rating=9))
    with session_scope(factory) as s:
        q = CatalogQueries(s, users["ana"])
        assert [h.slug for h in q.search("sacha", "directors")] == ["sacha-goedegebure"]
        assert [h.title for h in q.search("bun", "cast")] == ["Bunny"]
        assert [h.slug for h in q.search("anim", "genres")] == ["animacion"]
        assert [h.title for h in q.search("blender", "companies")] == ["Blender Institute"]
        assert [h.title for h in q.search("bet", "users")] == ["beto"]
        assert q.search("100%_", "movies") == []  # comodines escapados
        home = q.home()
        assert [c.id for c in home.popular_among_followed] == [movie_id]
        profile = q.user_profile("beto")
        assert profile.recent_reviews[0].movie_slug == "big-buck-bunny-2008" and profile.followers == 1


def test_analytics_and_regression(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = CatalogService(s, users["admin"], settings)
        for year, rating in [(1990, 4), (2000, 6), (2010, 8)]:
            mid = svc.create_movie(movie_form(title=f"Film {year}", release_year=year, imdb_id=None))
            ReviewService(s, users["ana"]).add(mid, ReviewForm(rating=rating, body="texto clave"))
    with session_scope(factory) as s:
        for analysis_id in ANALYSES:
            result = run_analysis(s, analysis_id)
            assert result.data.height > 0, analysis_id
        assert run_analysis(s, "ratings_distribution", q="clave").data["count"].sum() == 3
        regression = year_rating_regression(s, min_year=1980, max_year=2020)
    assert regression.metrics is not None
    assert regression.metrics.coef == pytest.approx(0.2) and regression.metrics.r2 == pytest.approx(1.0)
    assert regression.points["year"].to_list() == [1990, 2000, 2010]
