"""Migración Mongo → SQL ejecutada contra documentos con la forma real del dataset."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from bson import ObjectId
from sqlalchemy import func, select

from rascal.auth.service import AuthService, ChallengeRequired
from rascal.data.db import session_scope
from rascal.data.enums import CreditRole, Role
from rascal.data.models import Follow, Movie, MovieCredit, Review, ReviewVote, User, WatchlistEntry
from rascal.domain.services.queries import CatalogQueries

mongomock = pytest.importorskip("mongomock")
security = pytest.importorskip("werkzeug.security")


def load_migrator():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_from_mongo.py"
    spec = importlib.util.spec_from_file_location("migrate_from_mongo", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.Migrator


def test_full_migration(factory, settings, mailer) -> None:  # type: ignore[no-untyped-def]
    db = mongomock.MongoClient()["Peliculas_database"]
    admin_id, ana_id = ObjectId(), ObjectId()
    db.usuarios.insert_many([
        {"_id": admin_id, "username": "rascal", "email": "r@x.com", "rol": "admin",
         "password": security.generate_password_hash("admin-pass-1"), "registered_at": datetime(2025, 9, 1)},
        {"_id": ana_id, "username": "Ana", "email": "R@X.com", "rol": "user",
         "password": security.generate_password_hash("ana-pass-12"), "fav_movies": ["Toy Story", "No existe"]},
    ])
    db.peliculas.insert_many([
        {"movieId": 1, "title": "Toy Story (1995)", "genres": "Adventure|Animation|Children"},
        {"movieId": 2, "title": "Heat", "year": "1995", "genres": ["Action", "Crime"], "director": "Michael Mann",
         "cast": ["Al Pacino", "Robert De Niro", "Al Pacino"], "poster_path": "posters/heat.jpg",
         "trailer_url": "https://www.youtube.com/watch?v=abc"},
        {"movieId": 3, "title": "", "genres": "Drama"},
    ])
    db.links.insert_many([{"movieId": 1, "imdbId": 114709, "tmdbId": 862}])
    review_id = ObjectId()
    db["reseñas"].insert_many([
        {"movieId": 1, "userId": 7, "rating": 4.5, "timestamp": 964982703},  # MovieLens 0.5–5
        {"movieId": 1, "userId": 8, "rating": 3.0, "timestamp": 964982800},
        {"_id": review_id, "movieId": 2, "userId": ana_id, "rating": 9.0, "text": "Obra maestra",
         "created_at": datetime(2025, 11, 1), "first_time": False, "votes": {str(admin_id): "up", str(ana_id): "up"}},
        {"movieId": 999, "userId": ana_id, "rating": 5},  # huérfana
    ])
    db.watchlist.insert_many([{"userId": ana_id, "movieId": 2}, {"userId": ana_id, "movieId": 2}])
    db.followers.insert_many([{"follower": ana_id, "followed": admin_id}, {"follower": ana_id, "followed": ana_id}])

    Migrator = load_migrator()
    with session_scope(factory) as s:
        stats = Migrator(db, s, scale_mode="auto", batch=2).run()
    assert stats["users"] == 2 and stats["movies"] == 2 and stats["movies_skipped_no_title"] == 1
    assert stats["reviews"] == 3 and stats["reviews_orphan"] == 1 and stats["placeholder_users"] == 2
    assert stats["votes"] == 1 and stats["watchlist"] == 1 and stats["follows"] == 1 and stats["favorites"] == 1

    with session_scope(factory) as s:
        toy = s.scalars(select(Movie).where(Movie.legacy_movie_id == 1)).one()
        assert (toy.title, toy.release_year, toy.slug, toy.imdb_id, toy.tmdb_id) == (
            "Toy Story", 1995, "toy-story-1995", "tt0114709", 862)
        assert sorted(r for r in s.scalars(select(Review.rating).where(Review.movie_id == toy.id))) == [6, 9]
        heat = s.scalars(select(Movie).where(Movie.legacy_movie_id == 2)).one()
        roles = s.execute(select(MovieCredit.role, func.count()).where(MovieCredit.movie_id == heat.id)
                          .group_by(MovieCredit.role)).all()
        assert dict(roles) == {CreditRole.DIRECTOR: 1, CreditRole.CAST: 2}  # Al Pacino deduplicado
        review = s.scalars(select(Review).where(Review.legacy_id == str(review_id))).one()
        assert review.rating == 9 and review.is_rewatch and review.body == "Obra maestra"
        assert s.scalar(select(func.count()).select_from(ReviewVote)) == 1  # el autor no se auto-vota
        ana = s.scalars(select(User).where(User.username == "Ana")).one()
        assert ana.email is None  # email duplicado case-insensitive
        assert s.scalars(select(User).where(User.username == "rascal")).one().role is Role.ADMIN
        assert s.scalar(select(func.count()).select_from(WatchlistEntry)) == 1
        assert s.scalar(select(func.count()).select_from(Follow)) == 1
        assert CatalogQueries(s).entity_profile("person", "michael-mann").metrics.total_reviews == 1
        assert CatalogQueries(s).user_profile("Ana").favorites[0].title == "Toy Story"

    with session_scope(factory) as s:  # la contraseña Werkzeug funciona y se migra a Argon2id
        result = AuthService(s, settings, mailer).authenticate("rascal", "admin-pass-1")
    assert isinstance(result, ChallengeRequired)  # se conserva el 2FA por correo de la app anterior
    with session_scope(factory) as s:
        assert s.scalars(select(User).where(User.username == "rascal")).one().password_hash.startswith("$argon2id$")
