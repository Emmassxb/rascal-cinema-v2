"""Repositorio de reseñas y votos.

Las métricas se calculan con agregaciones en la base de datos (``GROUP BY rating``) y
los listados resuelven usuario + votos en una sola consulta, eliminando el bucle de
``users_col.find_one`` por reseña que tenía ``movie_detail``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, delete, func, select
from sqlalchemy.orm import Session

from rascal.data.models import Follow, Movie, Review, ReviewVote, User


@dataclass(frozen=True, slots=True)
class ReviewRow:
    review: Review
    user: User
    movie_slug: str
    movie_title: str
    votes_up: int
    votes_down: int


@dataclass(frozen=True, slots=True)
class ReviewerStats:
    total_reviews: int
    distinct_reviewers: int
    reviews_since: int


class ReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── CRUD ─────────────────────────────────────────────────────────────────
    def add(self, review: Review) -> Review:
        self.session.add(review)
        self.session.flush()
        return review

    def get(self, review_id: int) -> Review | None:
        return self.session.get(Review, review_id)

    def delete(self, review: Review) -> None:
        self.session.delete(review)
        self.session.flush()

    # ── Histogramas (base de todas las métricas de calificación) ────────────
    def histogram_for_movie(self, movie_id: int) -> dict[int, int]:
        rows = self.session.execute(
            select(Review.rating, func.count())
            .where(Review.movie_id == movie_id, Review.rating.is_not(None))
            .group_by(Review.rating)
        )
        return {int(rating): int(count) for rating, count in rows}

    def histogram_for_movies(self, movie_ids: Select[Any]) -> dict[int, int]:
        rows = self.session.execute(
            select(Review.rating, func.count())
            .join(Movie, Movie.id == Review.movie_id)
            .where(Review.movie_id.in_(movie_ids), Review.rating.is_not(None), Movie.is_published.is_(True))
            .group_by(Review.rating)
        )
        return {int(rating): int(count) for rating, count in rows}

    def average_of_film_averages(self, movie_ids: Select[Any]) -> float | None:
        per_movie = (
            select(func.avg(Review.rating).label("avg_rating"))
            .join(Movie, Movie.id == Review.movie_id)
            .where(Review.movie_id.in_(movie_ids), Review.rating.is_not(None), Movie.is_published.is_(True))
            .group_by(Review.movie_id)
            .subquery()
        )
        value = self.session.scalar(select(func.avg(per_movie.c.avg_rating)))
        return round(float(value), 2) if value is not None else None

    def reviewer_stats(self, movie_ids: Select[Any], since: datetime) -> ReviewerStats:
        row = self.session.execute(
            select(
                func.count(Review.id),
                func.count(func.distinct(Review.user_id)),
                func.coalesce(func.sum(case((Review.created_at >= since, 1), else_=0)), 0),
            )
            .join(Movie, Movie.id == Review.movie_id)
            .where(Review.movie_id.in_(movie_ids), Movie.is_published.is_(True))
        ).one()
        return ReviewerStats(total_reviews=int(row[0]), distinct_reviewers=int(row[1]), reviews_since=int(row[2]))

    # ── Listados ─────────────────────────────────────────────────────────────
    @staticmethod
    def _votes_subquery() -> Any:
        return (
            select(
                ReviewVote.review_id.label("review_id"),
                func.sum(case((ReviewVote.value == 1, 1), else_=0)).label("up"),
                func.sum(case((ReviewVote.value == -1, 1), else_=0)).label("down"),
            )
            .group_by(ReviewVote.review_id)
            .subquery("vote_agg")
        )

    def _list(self, *conditions: Any, order: str, limit: int, offset: int) -> list[ReviewRow]:
        votes = self._votes_subquery()
        up = func.coalesce(votes.c.up, 0)
        down = func.coalesce(votes.c.down, 0)
        order_clause = {
            "recent": [Review.created_at.desc(), Review.id.desc()],
            "popular": [(up - down).desc(), Review.created_at.desc()],
            "rating_desc": [Review.rating.desc(), Review.created_at.desc()],
            "rating_asc": [Review.rating.asc(), Review.created_at.desc()],
        }[order]
        stmt = (
            select(Review, User, Movie.slug, Movie.title, up, down)
            .join(User, User.id == Review.user_id)
            .join(Movie, Movie.id == Review.movie_id)
            .outerjoin(votes, votes.c.review_id == Review.id)
            .where(*conditions)
            .order_by(*order_clause)
            .limit(limit)
            .offset(offset)
        )
        return [
            ReviewRow(review=r, user=u, movie_slug=slug, movie_title=title, votes_up=int(vu), votes_down=int(vd))
            for r, u, slug, title, vu, vd in self.session.execute(stmt)
        ]

    def list_for_movie(self, movie_id: int, *, order: str = "recent", limit: int = 50, offset: int = 0) -> list[ReviewRow]:
        return self._list(Review.movie_id == movie_id, order=order, limit=limit, offset=offset)

    def list_for_user(self, user_id: int, *, limit: int = 50, offset: int = 0) -> list[ReviewRow]:
        return self._list(Review.user_id == user_id, order="recent", limit=limit, offset=offset)

    def count_for_movie(self, movie_id: int) -> int:
        return int(self.session.scalar(select(func.count()).where(Review.movie_id == movie_id)) or 0)

    def user_stats(self, user_id: int) -> tuple[int, float | None]:
        films, avg = self.session.execute(
            select(func.count(func.distinct(Review.movie_id)), func.avg(Review.rating)).where(Review.user_id == user_id)
        ).one()
        return int(films), (round(float(avg), 2) if avg is not None else None)

    # ── Votos (corrige la ruta /review/<id>/vote que nunca se registraba) ───
    def set_vote(self, review_id: int, user_id: int, value: int) -> None:
        """``value`` ∈ {-1, 0, 1}; 0 elimina el voto. Idempotente."""
        existing = self.session.get(ReviewVote, (review_id, user_id))
        if value == 0:
            if existing is not None:
                self.session.delete(existing)
        elif existing is None:
            self.session.add(ReviewVote(review_id=review_id, user_id=user_id, value=value))
        else:
            existing.value = value
        self.session.flush()

    def vote_totals(self, review_id: int) -> tuple[int, int]:
        up, down = self.session.execute(
            select(
                func.coalesce(func.sum(case((ReviewVote.value == 1, 1), else_=0)), 0),
                func.coalesce(func.sum(case((ReviewVote.value == -1, 1), else_=0)), 0),
            ).where(ReviewVote.review_id == review_id)
        ).one()
        return int(up), int(down)

    def delete_votes_for_users(self, user_ids: Iterable[int]) -> None:
        self.session.execute(delete(ReviewVote).where(ReviewVote.user_id.in_(list(user_ids))))

    # ── Rankings ─────────────────────────────────────────────────────────────
    def global_mean(self) -> float | None:
        value = self.session.scalar(select(func.avg(Review.rating)).where(Review.rating.is_not(None)))
        return float(value) if value is not None else None

    def popular_among_followed(self, follower_id: int, limit: int) -> list[tuple[int, int]]:
        """(movie_id, nº de reseñas de cuentas seguidas) — "Popular entre tus seguidos"."""
        followed = select(Follow.followed_id).where(Follow.follower_id == follower_id)
        stmt = (
            select(Review.movie_id, func.count().label("n"))
            .join(Movie, Movie.id == Review.movie_id)
            .where(Review.user_id.in_(followed), Movie.is_published.is_(True))
            .group_by(Review.movie_id)
            .order_by(func.count().desc(), Review.movie_id)
            .limit(limit)
        )
        return [(int(movie_id), int(n)) for movie_id, n in self.session.execute(stmt)]
