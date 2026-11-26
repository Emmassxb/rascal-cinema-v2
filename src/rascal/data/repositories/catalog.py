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
from sqlalchemy.orm import Session, defer, joinedload, selectinload

from rascal.data.enums import CreditRole, MediaType
from rascal.data.models import (
    Company,
    Genre,
    MediaSource,
    Movie,
    MovieCredit,
    MovieTechSpecs,
    Person,
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
    media_type: MediaType = MediaType.MOVIE
    is_published: bool = True


@dataclass(frozen=True, slots=True)
class SearchRow:
    """Proyección mínima para resultados de búsqueda (sin sinopsis ni biografías)."""

    id: int
    slug: str
    name: str
    image_path: str | None
    year: int | None = None
    films: int = 0
    media_type: MediaType | None = None


MAX_PAGE_SIZE = 200  # ninguna consulta de listado devuelve más filas que esto


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
    media_type: MediaType | None = None
    sort: str = "popularity"  # popularity | rating | weighted | year_desc | year_asc | title | recent
    prior_mean: float = 6.0  # media bayesiana (sort="weighted"), escala 0–10
    prior_weight: int = 5
    limit: int = 60
    offset: int = 0
    with_total: bool = True  # False en carruseles: ahorra el COUNT(*) sobre el catálogo


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Películas ────────────────────────────────────────────────────────────
    def get_movie(self, movie_id: int) -> Movie | None:
        return self.session.get(Movie, movie_id)

    def get_movie_by_slug(self, slug: str) -> Movie | None:
        return self.session.scalars(select(Movie).where(Movie.slug == slug)).first()

    def load_movie_full(self, *, movie_id: int | None = None, slug: str | None = None) -> Movie | None:
        """Película con sus relaciones en consultas fijas (selectin), sin N+1.

        Las fuentes NO se cargan aquí (una serie puede tener miles, y ``torrent_data`` es un
        BLOB): ver ``title_sources``. La biografía de las personas tampoco (``defer``)."""
        stmt = select(Movie).options(
            selectinload(Movie.genres),
            selectinload(Movie.companies),
            selectinload(Movie.credits).joinedload(MovieCredit.person).defer(Person.bio),
            joinedload(Movie.tech_specs),
        )
        if movie_id is not None:
            stmt = stmt.where(Movie.id == movie_id)
        elif slug is not None:
            stmt = stmt.where(Movie.slug == slug)
        else:
            raise ValueError("movie_id o slug requerido")
        return self.session.scalars(stmt).unique().first()

    def title_sources(self, movie_id: int, *, episode_id: int | None = None) -> list[MediaSource]:
        """Fuentes de la película (o de un capítulo) sin cargar el BLOB del .torrent."""
        stmt = (
            select(MediaSource)
            .options(defer(MediaSource.torrent_data))
            .where(MediaSource.movie_id == movie_id)
            .order_by(MediaSource.created_at, MediaSource.id)
            .limit(MAX_PAGE_SIZE)
        )
        stmt = stmt.where(MediaSource.episode_id.is_(None) if episode_id is None else MediaSource.episode_id == episode_id)
        return list(self.session.scalars(stmt))

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

    # ── Listados de tarjetas ────────────────────────────────────────────────
    def movie_cards(self, query: CardQuery) -> tuple[list[MovieCardRow], int]:
        """Listado filtrable/ordenable con métricas en UNA consulta + un conteo.

        Lee los agregados desnormalizados de ``movies`` (ver ``rascal.data.stats``) y proyecta
        sólo las columnas de la tarjeta. Nunca devuelve más de ``MAX_PAGE_SIZE`` filas.
        """
        rated = Movie.rating_count
        weighted = (rated * func.coalesce(Movie.rating_avg, query.prior_mean) + query.prior_weight * query.prior_mean) / (
            rated + query.prior_weight
        )

        conditions: list[Any] = []
        if not query.include_unpublished:
            conditions.append(Movie.is_published.is_(True))
        if query.media_type is not None:
            conditions.append(Movie.media_type == query.media_type)
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
            conditions.append(Movie.rating_avg >= query.min_average)
        if query.owner_id is not None:
            conditions.append(Movie.owner_id == query.owner_id)
        if query.title_contains:
            conditions.append(Movie.title.icontains(query.title_contains, autoescape=True))
        if query.sort == "weighted":
            # «Mejor valoradas»: sólo títulos con al menos una calificación. Sin esto, la expresión
            # bayesiana se evaluaría sobre los 86 000 títulos en cada carga del inicio.
            conditions.append(Movie.rating_count > 0)

        order_by: list[Any] = {
            "popularity": [Movie.popularity.desc(), Movie.title],
            "rating": [case((Movie.rating_avg.is_(None), 1), else_=0), Movie.rating_avg.desc(), Movie.review_count.desc()],
            "weighted": [weighted.desc(), Movie.review_count.desc()],
            "year_desc": [case((Movie.release_year.is_(None), 1), else_=0), Movie.release_year.desc(), Movie.title],
            "year_asc": [case((Movie.release_year.is_(None), 1), else_=0), Movie.release_year.asc(), Movie.title],
            "title": [Movie.title],
            "recent": [Movie.created_at.desc()],
        }.get(query.sort, [Movie.title])

        total = int(self.session.scalar(select(func.count(Movie.id)).where(*conditions)) or 0) if query.with_total else -1
        limit = max(1, min(query.limit, MAX_PAGE_SIZE))
        stmt = (
            select(
                Movie.id, Movie.slug, Movie.title, Movie.release_year, Movie.poster_path,
                Movie.rating_avg, Movie.review_count, Movie.watchlist_count, Movie.media_type, Movie.is_published,
            )
            .where(*conditions)
            .order_by(*order_by, Movie.id)
            .limit(limit)
            .offset(max(0, query.offset))
        )
        rows = [
            MovieCardRow(
                id=r[0], slug=r[1], title=r[2], release_year=r[3], poster_path=r[4],
                average=round(float(r[5]), 2) if r[5] is not None else None,
                review_count=int(r[6] or 0), watchlist_count=int(r[7] or 0), media_type=r[8], is_published=bool(r[9]),
            )
            for r in self.session.execute(stmt)
        ]
        return rows, total

    # ── Búsqueda (ILIKE escapado; en PostgreSQL acelerar con pg_trgm) ───────
    def search_movies(self, q: str, limit: int) -> list[SearchRow]:
        rows = self.session.execute(
            select(Movie.id, Movie.slug, Movie.title, Movie.poster_path, Movie.release_year, Movie.media_type)
            .where(Movie.is_published.is_(True), Movie.title.icontains(q, autoescape=True))
            .order_by(Movie.popularity.desc(), Movie.release_year.desc(), Movie.title)
            .limit(min(limit, MAX_PAGE_SIZE))
        )
        return [SearchRow(id=r[0], slug=r[1], name=r[2], image_path=r[3], year=r[4], media_type=r[5]) for r in rows]

    def search_people(self, q: str, limit: int, role: CreditRole | None = None) -> list[SearchRow]:
        credit_filter = [MovieCredit.person_id == Person.id]
        if role is not None:
            credit_filter.append(MovieCredit.role == role)
        films = func.count(func.distinct(MovieCredit.movie_id))
        stmt = (
            select(Person.id, Person.slug, Person.name, Person.photo_path, films)
            .join(MovieCredit, and_(*credit_filter))
            .where(Person.name.icontains(q, autoescape=True))
            .group_by(Person.id, Person.slug, Person.name, Person.photo_path)
            .order_by(films.desc(), Person.name)
            .limit(min(limit, MAX_PAGE_SIZE))
        )
        return [SearchRow(id=r[0], slug=r[1], name=r[2], image_path=r[3], films=int(r[4])) for r in self.session.execute(stmt)]

    def search_companies(self, q: str, limit: int) -> list[SearchRow]:
        films = func.count(movie_companies.c.movie_id)
        stmt = (
            select(Company.id, Company.slug, Company.name, Company.logo_path, films)
            .outerjoin(movie_companies, movie_companies.c.company_id == Company.id)
            .where(Company.name.icontains(q, autoescape=True))
            .group_by(Company.id, Company.slug, Company.name, Company.logo_path)
            .order_by(films.desc(), Company.name)
            .limit(min(limit, MAX_PAGE_SIZE))
        )
        return [SearchRow(id=r[0], slug=r[1], name=r[2], image_path=r[3], films=int(r[4])) for r in self.session.execute(stmt)]

    def search_genres(self, q: str, limit: int) -> list[SearchRow]:
        films = func.count(movie_genres.c.movie_id)
        stmt = (
            select(Genre.id, Genre.slug, Genre.name, films)
            .outerjoin(movie_genres, movie_genres.c.genre_id == Genre.id)
            .where(Genre.name.icontains(q, autoescape=True))
            .group_by(Genre.id, Genre.slug, Genre.name)
            .order_by(films.desc(), Genre.name)
            .limit(min(limit, MAX_PAGE_SIZE))
        )
        return [SearchRow(id=r[0], slug=r[1], name=r[2], image_path=None, films=int(r[3])) for r in self.session.execute(stmt)]

    def all_genres(self) -> list[Genre]:
        return list(self.session.scalars(select(Genre).order_by(Genre.name)))
