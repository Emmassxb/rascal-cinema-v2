"""Repositorios de fuentes P2P, bitácora de auditoría y consultas analíticas."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rascal.data.enums import CreditRole, SourceStatus
from rascal.data.models import AuditLog, Genre, MediaSource, Movie, MovieCredit, Person, Review, movie_genres


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, source_id: int) -> MediaSource | None:
        return self.session.get(MediaSource, source_id)

    def add(self, source: MediaSource) -> MediaSource:
        self.session.add(source)
        self.session.flush()
        return source

    def delete(self, source: MediaSource) -> None:
        self.session.delete(source)
        self.session.flush()

    def list_for_movie(self, movie_id: int, *, include_disabled: bool = True) -> list[MediaSource]:
        stmt = select(MediaSource).where(MediaSource.movie_id == movie_id).order_by(MediaSource.created_at)
        if not include_disabled:
            stmt = stmt.where(MediaSource.status != SourceStatus.DISABLED)
        return list(self.session.scalars(stmt))

    def exists_for_movie(self, movie_id: int, info_hash: str) -> bool:
        return (
            self.session.scalar(
                select(func.count()).where(MediaSource.movie_id == movie_id, MediaSource.info_hash == info_hash)
            )
            or 0
        ) > 0

    def active(self) -> list[MediaSource]:
        """Fuentes que el motor debe reanudar al arrancar."""
        return list(
            self.session.scalars(
                select(MediaSource).where(MediaSource.status.in_([SourceStatus.DOWNLOADING, SourceStatus.COMPLETE]))
            )
        )


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor_id: int | None,
        action: str,
        entity_type: str,
        entity_id: int | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            AuditLog(actor_id=actor_id, action=action, entity_type=entity_type, entity_id=entity_id, payload=payload)
        )

    def recent(self, limit: int = 100) -> list[AuditLog]:
        return list(self.session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)))


class AnalyticsRepository:
    """Agregaciones del panel de análisis (antes: pipelines $unwind/$group o PySpark)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def genre_counts(self, q: str | None, limit: int) -> list[tuple[str, int]]:
        stmt = (
            select(Genre.name, func.count(movie_genres.c.movie_id))
            .join(movie_genres, movie_genres.c.genre_id == Genre.id)
            .group_by(Genre.id)
            .order_by(func.count(movie_genres.c.movie_id).desc(), Genre.name)
            .limit(limit)
        )
        if q:
            stmt = stmt.where(Genre.name.icontains(q, autoescape=True))
        return [(name, int(n)) for name, n in self.session.execute(stmt)]

    def movies_per_year(self, q: str | None) -> list[tuple[int, int]]:
        stmt = (
            select(Movie.release_year, func.count())
            .where(Movie.release_year.is_not(None))
            .group_by(Movie.release_year)
            .order_by(Movie.release_year)
        )
        if q:
            stmt = stmt.where(Movie.title.icontains(q, autoescape=True))
        return [(int(year), int(n)) for year, n in self.session.execute(stmt)]

    def people_counts(self, role: CreditRole, q: str | None, limit: int) -> list[tuple[str, str, int]]:
        films = func.count(func.distinct(MovieCredit.movie_id))
        stmt = (
            select(Person.name, Person.slug, films)
            .join(MovieCredit, MovieCredit.person_id == Person.id)
            .where(MovieCredit.role == role)
            .group_by(Person.id)
            .order_by(films.desc(), Person.name)
            .limit(limit)
        )
        if q:
            stmt = stmt.where(Person.name.icontains(q, autoescape=True))
        return [(name, slug, int(n)) for name, slug, n in self.session.execute(stmt)]

    def rating_counts(self, q: str | None) -> list[tuple[int, int]]:
        stmt = (
            select(Review.rating, func.count())
            .where(Review.rating.is_not(None))
            .group_by(Review.rating)
            .order_by(Review.rating)
        )
        if q:
            stmt = stmt.where(Review.body.icontains(q, autoescape=True))
        return [(int(r), int(n)) for r, n in self.session.execute(stmt)]

    def year_vs_average(self, min_year: int, max_year: int, min_reviews: int) -> list[tuple[int, float, int]]:
        """(año, rating medio, nº reseñas) por película — insumo de la regresión."""
        n = func.count(Review.rating)
        stmt = (
            select(Movie.release_year, func.avg(Review.rating), n)
            .join(Review, Review.movie_id == Movie.id)
            .where(
                Movie.release_year.between(min_year, max_year),
                Review.rating.is_not(None),
            )
            .group_by(Movie.id, Movie.release_year)
            .having(n >= min_reviews)
        )
        return [(int(year), float(avg), int(count)) for year, avg, count in self.session.execute(stmt)]
