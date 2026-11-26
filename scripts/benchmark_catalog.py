#!/usr/bin/env python3
"""Benchmark del catálogo con datos sintéticos (por defecto 86 000 títulos).

Crea una base NUEVA (nunca toca la tuya), la llena con títulos, personas, créditos, reseñas y
watchlist, y mide las consultas que usan las páginas: inicio, catálogo (página 1 y página
profunda), filtro por género, búsqueda, ficha, filmografía y panel de administración. Compara
además con la consulta de v2.1 (GROUP BY sobre toda la tabla de reseñas en cada listado).

    python scripts/benchmark_catalog.py --db /tmp/bench.db --titles 86000 --reviews 600000
    python scripts/benchmark_catalog.py --db "postgresql+psycopg://rascal:x@127.0.0.1/rascal_bench"
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import func, insert, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from rascal.data.db import build_engine, create_schema, session_scope  # noqa: E402
from rascal.data.enums import CreditRole, MediaType, Role  # noqa: E402
from rascal.data.models import (  # noqa: E402
    Genre, Movie, MovieCredit, Person, Review, User, WatchlistEntry, movie_genres,
)
from rascal.data.stats import rebuild_all_stats  # noqa: E402
from rascal.domain.schemas import FilmographyFilters  # noqa: E402
from rascal.domain.services.queries import CatalogQueries  # noqa: E402

WORDS = ("amor noche ciudad sombra guerra mar último regreso secreto camino fuego viaje silencio tiempo "
         "rey río luna sangre casa jardín ángel hielo tormenta lobo verano invierno sueño cielo").split()


def chunks(rows: list[dict], size: int = 20_000):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def seed(factory, titles: int, reviews: int, users: int, people: int) -> None:  # type: ignore[no-untyped-def]
    rnd = random.Random(42)
    with session_scope(factory) as s:
        s.execute(insert(User), [{"id": i, "username": f"u{i}", "role": Role.USER} for i in range(1, users + 1)])
        s.execute(insert(Genre), [{"id": i, "slug": f"genero-{i}", "name": f"Género {i}"} for i in range(1, 41)])
        movie_rows = []
        for i in range(1, titles + 1):
            title = " ".join(rnd.sample(WORDS, 3)).capitalize() + f" {i}"
            movie_rows.append({
                "id": i, "slug": f"t-{i}", "title": title, "release_year": rnd.randint(1920, 2026),
                "media_type": MediaType.SERIES if i % 20 == 0 else MediaType.MOVIE,
                "synopsis": "Sinopsis " * 40, "poster_path": f"https://image.tmdb.org/t/p/w500/{i}.jpg",
                "tmdb_id": i, "is_published": i % 50 != 0,
            })
        for part in chunks(movie_rows):
            s.execute(insert(Movie), part)
        for part in chunks([{"movie_id": m, "genre_id": g} for m in range(1, titles + 1)
                            for g in {rnd.randint(1, 40), rnd.randint(1, 40)}]):
            s.execute(insert(movie_genres), part)
        for part in chunks([{"id": i, "slug": f"p-{i}", "name": f"Persona {i}", "bio": "Bio " * 200}
                            for i in range(1, people + 1)]):
            s.execute(insert(Person), part)
        credits = []
        for m in range(1, titles + 1):
            cast = rnd.sample(range(1, people + 1), 6)
            credits.append({"movie_id": m, "person_id": cast[0], "role": CreditRole.DIRECTOR, "billing_order": 0})
            credits += [{"movie_id": m, "person_id": p, "role": CreditRole.CAST, "billing_order": k}
                        for k, p in enumerate(cast[1:])]
        for part in chunks(credits):
            s.execute(insert(MovieCredit), part)
        # Distribución sesgada: pocas películas muy reseñadas (como en la vida real).
        popular = list(range(1, titles + 1))
        weights = [1 / (r ** 0.8) for r in range(1, titles + 1)]
        rnd.shuffle(popular)
        picks = rnd.choices(popular, weights=weights, k=reviews)
        for part in chunks([{"user_id": rnd.randint(1, users), "movie_id": m, "rating": rnd.randint(1, 10)} for m in picks]):
            s.execute(insert(Review), part)
        seen: set[tuple[int, int]] = set()
        watch = []
        for m in rnd.choices(popular, weights=weights, k=reviews // 5):
            key = (rnd.randint(1, users), m)
            if key not in seen:
                seen.add(key)
                watch.append({"user_id": key[0], "movie_id": key[1]})
        for part in chunks(watch):
            s.execute(insert(WatchlistEntry), part)
        rebuild_all_stats(s)


def timed(fn, repeat: int = 7) -> float:  # type: ignore[no-untyped-def]
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/tmp/rascal-bench.db", help="ruta SQLite o URL SQLAlchemy")
    parser.add_argument("--titles", type=int, default=86_000)
    parser.add_argument("--reviews", type=int, default=600_000)
    parser.add_argument("--users", type=int, default=20_000)
    parser.add_argument("--people", type=int, default=120_000)
    parser.add_argument("--reuse", action="store_true", help="no regenera los datos (sólo mide)")
    args = parser.parse_args()
    url = args.db if "://" in args.db else f"sqlite:///{Path(args.db).as_posix()}"
    if url.startswith("sqlite") and not args.reuse:
        Path(args.db).unlink(missing_ok=True)
    engine = build_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if not args.reuse:
        create_schema(engine)
        started = time.perf_counter()
        seed(factory, args.titles, args.reviews, args.users, args.people)
        print(f"Datos sintéticos: {args.titles} títulos, {args.reviews} reseñas, {args.people} personas "
              f"({time.perf_counter() - started:.0f} s)\n")
    # Estadísticas del planificador; en PostgreSQL VACUUM además habilita los index-only scans.
    if engine.dialect.name == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM ANALYZE")
    else:
        with engine.connect() as conn:
            conn.exec_driver_sql("ANALYZE")
            conn.commit()

    with session_scope(factory) as s:
        q = CatalogQueries(s, None)
        genre = s.scalar(select(Genre.slug).limit(1))
        person = s.scalar(select(Person.slug).where(Person.id == 77))
        hot = s.scalar(select(Movie.slug).order_by(Movie.popularity.desc()).limit(1))
        old_listing = text("""
            SELECT m.id, m.slug, m.title, m.release_year, m.poster_path, ra.avg_rating, COALESCE(ra.n, 0), COALESCE(wa.n, 0)
            FROM movies m
            LEFT JOIN (SELECT movie_id, AVG(rating) AS avg_rating, COUNT(id) AS n FROM reviews GROUP BY movie_id) ra ON ra.movie_id = m.id
            LEFT JOIN (SELECT movie_id, COUNT(*) AS n FROM watchlist GROUP BY movie_id) wa ON wa.movie_id = m.id
            WHERE m.is_published ORDER BY COALESCE(ra.n, 0) + COALESCE(wa.n, 0) DESC, m.title LIMIT 60""")
        cases = {
            "v2.1 · listado por popularidad (GROUP BY reseñas)": lambda: s.execute(old_listing).all(),
            "Inicio (3 carruseles + media global)": lambda: q.home(),
            "Catálogo · popularidad, página 1": lambda: q.browse(FilmographyFilters(limit=60)),
            "Catálogo · título, página 1 000": lambda: q.browse(FilmographyFilters(sort="title", limit=60, offset=59_940)),
            "Catálogo · sólo series, por año": lambda: q.browse(FilmographyFilters(year_from=1990, year_to=2010, limit=60),
                                                               media_type=MediaType.SERIES),
            "Catálogo · por género + mejor calificación": lambda: q.browse(FilmographyFilters(genre_slug=genre, sort="rating", limit=60)),
            "Búsqueda «sombra» (frecuente)": lambda: q.search("sombra", limit=40),
            "Búsqueda sin resultados (peor caso)": lambda: q.search("zzqx", limit=40),
            "Búsqueda de personas «Persona 123»": lambda: q.search("Persona 123", kind="people", limit=20),
            "Ficha del título más reseñado": lambda: q.movie_detail(slug=hot),
            "Reseñas de la ficha (30)": lambda: q.movie_reviews(s.scalar(select(Movie.id).where(Movie.slug == hot)), limit=30),
            "Filmografía de una persona": lambda: q.entity_profile("person", person, FilmographyFilters(limit=60)),
            "Conteo total publicado": lambda: s.scalar(select(func.count(Movie.id)).where(Movie.is_published.is_(True))),
        }
        width = max(len(k) for k in cases)
        print(f"{'Consulta'.ljust(width)}  mediana")
        for name, fn in cases.items():
            print(f"{name.ljust(width)}  {timed(fn):7.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
