#!/usr/bin/env python3
"""Migra la base MongoDB ``Peliculas_database`` al esquema relacional normalizado.

Uso::

    pip install -r requirements-migrate.txt
    alembic upgrade head            # o: python scripts/init_db.py
    python scripts/migrate_from_mongo.py --mongo-uri mongodb://localhost:27017 --dry-run
    python scripts/migrate_from_mongo.py --mongo-uri mongodb://localhost:27017

La migración es transaccional: si algo falla no queda nada a medias. Es idempotente a nivel
de base vacía (ejecútala sobre una BD nueva). Las contraseñas Werkzeug se copian tal cual y
se re-hashean a Argon2id en el primer login.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import insert, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from rascal.data.db import create_schema, get_engine, session_scope  # noqa: E402
from rascal.data.enums import CreditRole, Role  # noqa: E402
from rascal.data.legacy import (  # noqa: E402
    imdb_from_links,
    legacy_user_key,
    parse_date,
    parse_datetime,
    parse_genres,
    poster_from_doc,
    rating_scale,
    safe_username,
    split_list,
    split_title_year,
    trailer_from_doc,
)
from rascal.data.models import (  # noqa: E402
    FavoriteMovie,
    Follow,
    Genre,
    Movie,
    MovieCredit,
    Person,
    Review,
    ReviewVote,
    User,
    WatchlistEntry,
    utcnow,
)
from rascal.domain.ratings import normalize_legacy_rating  # noqa: E402
from rascal.domain.slugs import movie_slug_base, next_free_slug, slugify  # noqa: E402


class DryRun(Exception):
    pass


class Migrator:
    def __init__(self, mongo: Any, session: Session, *, scale_mode: str, batch: int) -> None:
        self.mongo = mongo
        self.session = session
        self.scale_mode = scale_mode
        self.batch = batch
        self.stats: Counter[str] = Counter()
        self.users: dict[str, int] = {}  # clave legacy → id
        self.movies: dict[int, int] = {}  # movieId legacy → id
        self.movies_by_oid: dict[str, int] = {}
        self.movies_by_title: dict[str, int] = {}
        self.genres: dict[str, Genre] = {}
        self.people: dict[str, Person] = {}
        self.slugs: dict[str, set[str]] = {"movie": set(), "person": set(), "genre": set()}
        self.usernames: set[str] = set()
        self.emails: set[str] = set()

    # ── Usuarios ─────────────────────────────────────────────────────────────
    def migrate_users(self) -> None:
        for doc in self.mongo["usuarios"].find():
            username = safe_username(str(doc.get("username") or "usuario"), self.usernames)
            email = (doc.get("email") or "").strip() or None
            if email and email.casefold() in self.emails:
                email = None
            if email:
                self.emails.add(email.casefold())
            role = Role.ADMIN if doc.get("rol") == "admin" else Role.USER
            user = User(
                username=username, email=email, password_hash=doc.get("password") or doc.get("password_hash"),
                role=role, avatar_path=doc.get("profile_pic"), is_active=True,
                mfa_email_enabled=bool(email),  # el flujo anterior exigía código por correo siempre
                created_at=parse_datetime(doc.get("registered_at")) or utcnow(), legacy_id=str(doc["_id"]),
            )
            self.session.add(user)
            self.session.flush()
            self.users[str(doc["_id"]).lower()] = user.id
            self.users[f"name:{str(doc.get('username', '')).casefold()}"] = user.id
            self.stats["users"] += 1

    def placeholder_user(self, key: str) -> int:
        """Autor MovieLens sin cuenta: usuario inactivo ``usuario_<n>`` (como mostraba la app)."""
        if key in self.users:
            return self.users[key]
        username = safe_username(f"usuario_{key.split(':', 1)[1]}", self.usernames)
        user = User(username=username, role=Role.USER, is_active=False, legacy_id=key)
        self.session.add(user)
        self.session.flush()
        self.users[key] = user.id
        self.stats["placeholder_users"] += 1
        return user.id

    # ── Catálogo ─────────────────────────────────────────────────────────────
    def genre(self, name: str) -> Genre:
        key = name.casefold()
        if key not in self.genres:
            genre = Genre(name=name, slug=next_free_slug(slugify(name), self.slugs["genre"]))
            self.slugs["genre"].add(genre.slug)
            self.session.add(genre)
            self.genres[key] = genre
        return self.genres[key]

    def person(self, name: str) -> Person:
        key = name.casefold()
        if key not in self.people:
            person = Person(name=name[:200], slug=next_free_slug(slugify(name), self.slugs["person"]))
            self.slugs["person"].add(person.slug)
            self.session.add(person)
            self.people[key] = person
        return self.people[key]

    def migrate_movies(self) -> None:
        pending = 0
        for doc in self.mongo["peliculas"].find():
            title, year = split_title_year(str(doc.get("title") or ""), doc.get("year"))
            if not title:
                self.stats["movies_skipped_no_title"] += 1
                continue
            slug = next_free_slug(movie_slug_base(title, year), self.slugs["movie"])
            self.slugs["movie"].add(slug)
            trailer_url, trailer_path = trailer_from_doc(doc)
            legacy_id = doc.get("movieId")
            movie = Movie(
                slug=slug, title=title[:300], release_year=year, synopsis=doc.get("description") or doc.get("synopsis"),
                poster_path=poster_from_doc(doc), trailer_url=trailer_url, trailer_path=trailer_path,
                legacy_movie_id=int(legacy_id) if isinstance(legacy_id, (int, float)) else None,
                is_published=True,
            )
            movie.genres = [self.genre(g) for g in parse_genres(doc.get("genres"))]
            self.session.add(movie)
            self.session.flush()
            seen: set[tuple[int, CreditRole]] = set()
            for order, name in enumerate(split_list(doc.get("director"))):
                person = self.person(name)
                self.session.flush()
                if (person.id, CreditRole.DIRECTOR) not in seen:
                    seen.add((person.id, CreditRole.DIRECTOR))
                    self.session.add(MovieCredit(movie_id=movie.id, person_id=person.id, role=CreditRole.DIRECTOR, billing_order=order))
            for order, name in enumerate(split_list(doc.get("cast"))):
                person = self.person(name)
                self.session.flush()
                if (person.id, CreditRole.CAST) not in seen:
                    seen.add((person.id, CreditRole.CAST))
                    self.session.add(MovieCredit(movie_id=movie.id, person_id=person.id, role=CreditRole.CAST, billing_order=order))
            if movie.legacy_movie_id is not None:
                self.movies[movie.legacy_movie_id] = movie.id
            self.movies_by_oid[str(doc["_id"]).lower()] = movie.id
            self.movies_by_title.setdefault(title.casefold(), movie.id)
            self.stats["movies"] += 1
            pending += 1
            if pending >= 500:
                self.session.flush()
                pending = 0
        self.session.flush()

    def migrate_links(self) -> None:
        if "links" not in self.mongo.list_collection_names():
            return
        used_imdb: set[str] = set()
        used_tmdb: set[int] = set()
        for doc in self.mongo["links"].find():
            movie_id = self.movies.get(int(doc.get("movieId", -1)))
            if movie_id is None:
                continue
            movie = self.session.get(Movie, movie_id)
            imdb = imdb_from_links(doc.get("imdbId"))
            if imdb and imdb not in used_imdb:
                movie.imdb_id = imdb
                used_imdb.add(imdb)
            tmdb = doc.get("tmdbId")
            if isinstance(tmdb, (int, float)) and int(tmdb) > 0 and int(tmdb) not in used_tmdb:
                movie.tmdb_id = int(tmdb)
                used_tmdb.add(int(tmdb))
            self.stats["links"] += 1
        self.session.flush()

    def resolve_movie(self, raw: Any) -> int | None:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return self.movies.get(int(raw))
        if raw is not None:
            text = str(raw)
            return self.movies.get(int(text)) if text.isdigit() else self.movies_by_oid.get(text.lower())
        return None

    def resolve_user(self, raw: Any) -> int | None:
        key = legacy_user_key(raw)
        if key is None:
            return None
        if key in self.users:
            return self.users[key]
        if key.startswith("ml:"):
            return self.placeholder_user(key)
        return None

    # ── Reseñas y votos ──────────────────────────────────────────────────────
    def migrate_reviews(self) -> None:
        rows: list[dict[str, Any]] = []
        votes: list[tuple[str, Any, Any]] = []
        for doc in self.mongo["reseñas"].find().batch_size(self.batch):
            movie_id = self.resolve_movie(doc.get("movieId"))
            user_id = self.resolve_user(doc.get("userId"))
            if movie_id is None or user_id is None:
                self.stats["reviews_orphan"] += 1
                continue
            scale = rating_scale(doc.get("userId"), self.scale_mode)  # type: ignore[arg-type]
            first_time = doc.get("first_time")
            rows.append({
                "user_id": user_id, "movie_id": movie_id,
                "rating": normalize_legacy_rating(doc.get("rating"), scale_factor=scale),
                "body": (doc.get("text") or doc.get("comment") or None),
                "watched_on": parse_date(doc.get("watched_at")),
                "is_rewatch": (first_time is False),
                "contains_spoilers": False,
                "created_at": parse_datetime(doc.get("created_at") or doc.get("timestamp") or doc.get("date")) or utcnow(),
                "updated_at": utcnow(),
                "legacy_id": str(doc["_id"]),
            })
            for voter, value in (doc.get("votes") or {}).items():
                votes.append((str(doc["_id"]), voter, value))
            if len(rows) >= self.batch:
                self.session.execute(insert(Review), rows)
                self.stats["reviews"] += len(rows)
                rows.clear()
        if rows:
            self.session.execute(insert(Review), rows)
            self.stats["reviews"] += len(rows)
        self.migrate_votes(votes)

    def migrate_votes(self, votes: list[tuple[str, Any, Any]]) -> None:
        if not votes:
            return
        legacy_ids = sorted({v[0] for v in votes})
        review_ids: dict[str, int] = {}
        for i in range(0, len(legacy_ids), 900):
            chunk = legacy_ids[i : i + 900]
            rows = self.session.execute(select(Review.legacy_id, Review.id).where(Review.legacy_id.in_(chunk)))
            review_ids.update({legacy: rid for legacy, rid in rows})  # Result expone .keys(): no pasar a dict()
        authors = {
            rid: uid
            for rid, uid in self.session.execute(select(Review.id, Review.user_id).where(Review.id.in_(review_ids.values())))
        }
        seen: set[tuple[int, int]] = set()
        for legacy_review, voter, value in votes:
            review_id = review_ids.get(legacy_review)
            user_id = self.users.get(legacy_user_key(voter) or "")
            if review_id is None or user_id is None or authors.get(review_id) == user_id or (review_id, user_id) in seen:
                continue
            seen.add((review_id, user_id))
            self.session.add(ReviewVote(review_id=review_id, user_id=user_id, value=1 if value == "up" else -1))
            self.stats["votes"] += 1
        self.session.flush()

    # ── Social ───────────────────────────────────────────────────────────────
    def migrate_social(self) -> None:
        seen_watch: set[tuple[int, int]] = set()
        for doc in self.mongo["watchlist"].find():
            user_id, movie_id = self.users.get(legacy_user_key(doc.get("userId")) or ""), self.resolve_movie(doc.get("movieId"))
            if user_id and movie_id and (user_id, movie_id) not in seen_watch:
                seen_watch.add((user_id, movie_id))
                self.session.add(WatchlistEntry(user_id=user_id, movie_id=movie_id,
                                                added_at=parse_datetime(doc.get("added_at")) or utcnow()))
                self.stats["watchlist"] += 1
        seen_follow: set[tuple[int, int]] = set()
        for doc in self.mongo["followers"].find():
            a, b = self.users.get(legacy_user_key(doc.get("follower")) or ""), self.users.get(legacy_user_key(doc.get("followed")) or "")
            if a and b and a != b and (a, b) not in seen_follow:
                seen_follow.add((a, b))
                self.session.add(Follow(follower_id=a, followed_id=b))
                self.stats["follows"] += 1
        for doc in self.mongo["usuarios"].find({"fav_movies": {"$exists": True}}):
            user_id = self.users.get(str(doc["_id"]).lower())
            position = 0
            used: set[int] = set()
            for title in doc.get("fav_movies") or []:
                clean, _ = split_title_year(str(title), None)
                movie_id = self.movies_by_title.get(clean.casefold())
                if user_id and movie_id and movie_id not in used:
                    used.add(movie_id)
                    self.session.add(FavoriteMovie(user_id=user_id, movie_id=movie_id, position=position))
                    position += 1
                    self.stats["favorites"] += 1
                else:
                    self.stats["favorites_unmatched"] += 1
        self.session.flush()

    def run(self) -> Counter[str]:
        self.migrate_users()
        self.migrate_movies()
        self.migrate_links()
        self.migrate_reviews()
        self.migrate_social()
        return self.stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-uri", default=os.environ.get("RASCAL_MONGO_URI", "mongodb://localhost:27017"))
    parser.add_argument("--mongo-db", default="Peliculas_database")
    parser.add_argument("--rating-scale", choices=["auto", "x1", "x2"], default="auto",
                        help="auto: ×2 para reseñas con userId numérico (MovieLens 0.5–5)")
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--create-schema", action="store_true", help="crea las tablas (sin Alembic)")
    parser.add_argument("--dry-run", action="store_true", help="ejecuta todo y revierte al final")
    args = parser.parse_args()

    from pymongo import MongoClient

    if args.create_schema:
        create_schema(get_engine())
    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)[args.mongo_db]
    stats: Counter[str] = Counter()
    try:
        with session_scope() as session:
            stats = Migrator(mongo, session, scale_mode=args.rating_scale, batch=args.batch).run()
            if args.dry_run:
                raise DryRun
    except DryRun:
        print("[dry-run] Cambios revertidos.")
    for key, value in sorted(stats.items()):
        print(f"{key:>28}: {value}")


if __name__ == "__main__":
    main()
