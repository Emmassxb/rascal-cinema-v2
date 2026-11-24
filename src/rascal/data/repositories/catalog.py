"""Repositorio del catálogo: películas, personas, productoras, géneros y créditos.

Reemplaza los ``$regex`` sin índice y los bucles ``find_one`` por película (N+1) del
código anterior por consultas agregadas con ``JOIN`` + ``GROUP BY`` sobre índices.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from rascal.data.enums import CreditRole
from rascal.data.models import (
    Company,
    Genre,
    Movie,
    MovieCredit,
    MovieTechSpecs,
    Person,
    Review,
    WatchlistEntry,
    movie_companies,
    movie_genres,
)
from rascal.domain.slugs import next_free_slug, slugify

T = TypeVar("T")
SlugModel = type[Movie] | type[Person] | type[Company] | type[Genre]


@dataclass(frozen=True, slots=True)
class MovieCardRow:
    """Fila plana para construir ``MovieCard`` (sin objetos ORM)."""

    id: int
    slug: str
    title: str
    release_year: int | None
    poster_path: str | None
    average: float | None
    review_count: int
    watchlist_count: int


@dataclass(frozen=True, slots=True)
class CardQuery:
    """Filtros de listados de películas (filmografías, home, catálogo del partner)."""

    movie_ids: Select[Any] | None = None
    genre_slug: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    min_average: float | None = None  # escala 0–10
    owner_id: int | None = None
    title_contains: str | None = None
    include_unpublished: bool = False
    sort: str = "popularity"  # popularity | rating | weighted | year_desc | year_asc | title | recent
    prior_mean: float = 6.0  # media bayesiana (sort="weighted"), escala 0–10
    prior_weight: int = 5
    limit: int = 60
    offset: int = 0


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Películas ────────────────────────────────────────────────────────────
    def get_movie(self, movie_id: int) -> Movie | None:
        return self.session.get(Movie, movie_id)

    def get_movie_by_slug(self, slug: str) -> Movie | None:
        return self.session.scalars(select(Movie).where(Movie.slug == slug)).first()

    def load_movie_full(self, *, movie_id: int | None = None, slug: str | None = None) -> Movie | None:
        """Película con todas sus relaciones en 5 consultas fijas (selectin), sin N+1."""
        stmt = select(Movie).options(
            selectinload(Movie.genres),
            selectinload(Movie.companies),
            selectinload(Movie.credits).joinedload(MovieCredit.person),
            joinedload(Movie.tech_specs),
            selectinload(Movie.sources),
        )
        if movie_id is not None:
            stmt = stmt.where(Movie.id == movie_id)
        elif slug is not None:
            stmt = stmt.where(Movie.slug == slug)
        else:
            raise ValueError("movie_id o slug requerido")
        return self.session.scalars(stmt).unique().first()

    def add_movie(self, movie: Movie) -> Movie:
        self.session.add(movie)
        self.session.flush()
        return movie

    def set_tech_specs(self, movie: Movie, values: dict[str, Any] | None) -> None:
        if values is None:
            movie.tech_specs = None
            return
        if movie.tech_specs is None:
            movie.tech_specs = MovieTechSpecs(**values)
        else:
            for key, value in values.items():
                setattr(movie.tech_specs, key, value)

    # ── Slugs ────────────────────────────────────────────────────────────────
    def unique_slug(self, model: SlugModel, base: str, *, exclude_id: int | None = None) -> str:
        stmt = select(model.slug).where(model.slug.like(f"{base}%"))
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        taken = set(self.session.scalars(stmt))
        return next_free_slug(base, taken)

    # ── Entidades (get-or-create a prueba de carreras) ──────────────────────
    def _get_or_create(self, lookup: Select[tuple[T]], factory: Callable[[], T]) -> T:
        existing = self.session.scalars(lookup).first()
        if existing is not None:
            return existing
        try:
            with self.session.begin_nested():  # SAVEPOINT: si otra transacción ganó, reintenta
                obj = factory()
                self.session.add(obj)
                self.session.flush()
            return obj
        except IntegrityError:
            return self.session.scalars(lookup).one()

    def get_or_create_genre(self, name: str) -> Genre:
        slug = slugify(name)
        return self._get_or_create(
            select(Genre).where((Genre.slug == slug) | (func.lower(Genre.name) == name.casefold())),
            lambda: Genre(name=name, slug=slug),
        )

    def get_or_create_company(self, name: str) -> Company:
        return self._get_or_create(
            select(Company).where(func.lower(Company.name) == name.casefold()).order_by(Company.id),
            lambda: Company(name=name, slug=self.unique_slug(Company, slugify(name))),
        )

    def get_or_create_person(self, name: str) -> Person:
        return self._get_or_create(
            select(Person).where(func.lower(Person.name) == name.casefold()).order_by(Person.id),
            lambda: Person(name=name, slug=self.unique_slug(Person, slugify(name))),
        )

    def get_person(self, person_id: int) -> Person | None:
        return self.session.get(Person, person_id)

    def get_entity_by_slug(self, kind: str, slug: str) -> Person | Company | Genre | None:
        model = {"person": Person, "company": Company, "genre": Genre}[kind]
        return self.session.scalars(select(model).where(model.slug == slug)).first()

    # ── Créditos ─────────────────────────────────────────────────────────────
    def replace_credits(
        self, movie: Movie, credits: Sequence[tuple[int, CreditRole, str | None, int]]
    ) -> None:
        """Sustituye todos los créditos (borrado explícito antes de insertar para no
        violar ``uq_movie_credits_movie_person_role`` dentro del mismo flush)."""
        self.session.query(MovieCredit).filter(MovieCredit.movie_id == movie.id).delete(
            synchronize_session=False
        )
        self.session.flush()
        self.session.expire(movie, ["credits"])
        for person_id, role, character, order in credits:
            self.session.add(
                MovieCredit(
                    movie_id=movie.id,
                    person_id=person_id,
                    role=role,
                    character_name=character,
                    billing_order=order,
                )
            )
        self.session.flush()

    def person_roles_by_movie(self, person_id: int, movie_ids: Iterable[int]) -> dict[int, list[tuple[CreditRole, str | None]]]:
        ids = list(movie_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(MovieCredit.movie_id, MovieCredit.role, MovieCredit.character_name)
            .where(MovieCredit.person_id == person_id, MovieCredit.movie_id.in_(ids))
            .order_by(MovieCredit.movie_id, MovieCredit.role)
        )
        result: dict[int, list[tuple[CreditRole, str | None]]] = {}
        for movie_id, role, character in rows:
            result.setdefault(movie_id, []).append((role, character))
        return result

    def person_role_counts(self, person_id: int) -> list[tuple[CreditRole, int]]:
        rows = self.session.execute(
            select(MovieCredit.role, func.count(func.distinct(MovieCredit.movie_id)))
            .join(Movie, Movie.id == MovieCredit.movie_id)
            .where(MovieCredit.person_id == person_id, Movie.is_published.is_(True))
            .group_by(MovieCredit.role)
            .order_by(func.count(func.distinct(MovieCredit.movie_id)).desc())
        )
        return [(role, int(count)) for role, count in rows]

    # ── Selección de películas por entidad (base de las vistas dinámicas) ───
    @staticmethod
    def movie_ids_for_entity(kind: str, entity_id: int, role: CreditRole | None = None) -> Select[Any]:
        if kind == "person":
            stmt = select(MovieCredit.movie_id).where(MovieCredit.person_id == entity_id)
            if role is not None:
                stmt = stmt.where(MovieCredit.role == role)
            return stmt.distinct()
        if kind == "company":
            return select(movie_companies.c.movie_id).where(movie_companies.c.company_id == entity_id)
        if kind == "genre":
            return select(movie_genres.c.movie_id).where(movie_genres.c.genre_id == entity_id)
        raise ValueError(f"Tipo de entidad desconocido: {kind}")

    # ── Agregados por película (subconsultas reutilizables) ─────────────────
    @staticmethod
    def review_aggregates_subquery() -> Any:
        return (
            select(
                Review.movie_id.label("movie_id"),
                func.avg(Review.rating).label("avg_rating"),
                func.count(Review.id).label("review_count"),
                func.count(Review.rating).label("rated_count"),
            )
            .group_by(Review.movie_id)
            .subquery("review_agg")
        )

    @staticmethod
    def watchlist_aggregates_subquery() -> Any:
        return (
            select(WatchlistEntry.movie_id.label("movie_id"), func.count().label("watchlist_count"))
            .group_by(WatchlistEntry.movie_id)
            .subquery("watch_agg")
        )

    def movie_cards(self, query: CardQuery) -> tuple[list[MovieCardRow], int]:
        """Listado filtrable/ordenable de películas con métricas en UNA consulta + conteo."""
        reviews = self.review_aggregates_subquery()
        watch = self.watchlist_aggregates_subquery()
        review_count = func.coalesce(reviews.c.review_count, 0)
        rated_count = func.coalesce(reviews.c.rated_count, 0)
        watch_count = func.coalesce(watch.c.watchlist_count, 0)
        weighted = (
            rated_count * func.coalesce(reviews.c.avg_rating, query.prior_mean) + query.prior_weight * query.prior_mean
        ) / (rated_count + query.prior_weight)

        conditions: list[Any] = []
        if not query.include_unpublished:
            conditions.append(Movie.is_published.is_(True))
        if query.movie_ids is not None:
            conditions.append(Movie.id.in_(query.movie_ids))
        if query.genre_slug:
            conditions.append(
                Movie.id.in_(
                    select(movie_genres.c.movie_id)
                    .join(Genre, Genre.id == movie_genres.c.genre_id)
                    .where(Genre.slug == query.genre_slug)
                )
            )
        if query.year_from is not None:
            conditions.append(Movie.release_year >= query.year_from)
        if query.year_to is not None:
            conditions.append(Movie.release_year <= query.year_to)
        if query.min_average is not None:
            conditions.append(reviews.c.avg_rating >= query.min_average)
        if query.owner_id is not None:
            conditions.append(Movie.owner_id == query.owner_id)
        if query.title_contains:
            conditions.append(Movie.title.icontains(query.title_contains, autoescape=True))

        order_by: list[Any] = {
            "popularity": [(review_count + watch_count).desc(), Movie.title],
            "rating": [
                case((reviews.c.avg_rating.is_(None), 1), else_=0),
                reviews.c.avg_rating.desc(),
                review_count.desc(),
            ],
            "weighted": [weighted.desc(), review_count.desc()],
            "year_desc": [case((Movie.release_year.is_(None), 1), else_=0), Movie.release_year.desc(), Movie.title],
            "year_asc": [case((Movie.release_year.is_(None), 1), else_=0), Movie.release_year.asc(), Movie.title],
            "title": [Movie.title],
            "recent": [Movie.created_at.desc()],
        }.get(query.sort, [Movie.title])

        base = (
            select(Movie.id)
            .outerjoin(reviews, reviews.c.movie_id == Movie.id)
            .outerjoin(watch, watch.c.movie_id == Movie.id)
            .where(*conditions)
        )
        total = int(self.session.scalar(select(func.count()).select_from(base.subquery())) or 0)

        stmt = (
            select(
                Movie.id,
                Movie.slug,
                Movie.title,
                Movie.release_year,
                Movie.poster_path,
                reviews.c.avg_rating,
                review_count,
                watch_count,
            )
            .outerjoin(reviews, reviews.c.movie_id == Movie.id)
            .outerjoin(watch, watch.c.movie_id == Movie.id)
            .where(*conditions)
            .order_by(*order_by, Movie.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = [
            MovieCardRow(
                id=r[0],
                slug=r[1],
                title=r[2],
                release_year=r[3],
                poster_path=r[4],
                average=round(float(r[5]), 2) if r[5] is not None else None,
                review_count=int(r[6]),
                watchlist_count=int(r[7]),
            )
            for r in self.session.execute(stmt)
        ]
        return rows, total

    # ── Búsqueda (ILIKE escapado; en PostgreSQL acelerar con pg_trgm) ───────
    def search_movies(self, q: str, limit: int) -> list[Movie]:
        return list(
            self.session.scalars(
                select(Movie)
                .where(Movie.is_published.is_(True), Movie.title.icontains(q, autoescape=True))
                .order_by(Movie.release_year.desc(), Movie.title)
                .limit(limit)
            )
        )

    def search_people(self, q: str, limit: int, role: CreditRole | None = None) -> list[tuple[Person, int]]:
        credit_filter = [MovieCredit.person_id == Person.id]
        if role is not None:
            credit_filter.append(MovieCredit.role == role)
        films = func.count(func.distinct(MovieCredit.movie_id))
        stmt = (
            select(Person, films)
            .join(MovieCredit, and_(*credit_filter))
            .where(Person.name.icontains(q, autoescape=True))
            .group_by(Person.id)
            .order_by(films.desc(), Person.name)
            .limit(limit)
        )
        return [(person, int(count)) for person, count in self.session.execute(stmt)]

    def search_companies(self, q: str, limit: int) -> list[tuple[Company, int]]:
        films = func.count(movie_companies.c.movie_id)
        stmt = (
            select(Company, films)
            .outerjoin(movie_companies, movie_companies.c.company_id == Company.id)
            .where(Company.name.icontains(q, autoescape=True))
            .group_by(Company.id)
            .order_by(films.desc(), Company.name)
            .limit(limit)
        )
        return [(company, int(count)) for company, count in self.session.execute(stmt)]

    def search_genres(self, q: str, limit: int) -> list[tuple[Genre, int]]:
        films = func.count(movie_genres.c.movie_id)
        stmt = (
            select(Genre, films)
            .outerjoin(movie_genres, movie_genres.c.genre_id == Genre.id)
            .where(Genre.name.icontains(q, autoescape=True))
            .group_by(Genre.id)
            .order_by(films.desc(), Genre.name)
            .limit(limit)
        )
        return [(genre, int(count)) for genre, count in self.session.execute(stmt)]

    def all_genres(self) -> list[Genre]:
        return list(self.session.scalars(select(Genre).order_by(Genre.name)))
