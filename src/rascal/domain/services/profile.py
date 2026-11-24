"""Perfil social estilo Letterboxd: diario, listas de seguidores/seguidos y favoritas fijadas.

* El **diario** ordena por fecha de visionado (``watched_on``) y, si no existe, por la de creación.
  Se calcula en Python sobre las últimas reseñas del usuario: son pocas por persona y así la
  regla de "fecha efectiva" queda en un único sitio, legible y probada.
* Las **favoritas** se guardan en ``favorite_movies.position``; el perfil fija las primeras 4.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rascal.auth.rbac import Permission, Principal, require
from rascal.data.models import Movie, Review, User
from rascal.data.repositories.users import SocialRepository, UserRepository
from rascal.domain.schemas import DiaryEntry, FollowUser, UserRef
from rascal.domain.services.catalog import CatalogError, NotFound

PINNED_FAVORITES = 4
MAX_FAVORITES = 10
MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_STARS = {v: v / 2 for v in range(1, 11)}


def star_label(rating: int | None) -> str:
    """Estrellas en texto: 9 → ★★★★½. Sin calificación → cadena vacía."""
    if rating is None or rating <= 0:
        return ""
    stars = rating / 2
    full, half = int(stars), stars % 1 >= 0.5
    return "★" * full + ("½" if half else "")


def effective_date(watched_on: date | None, created_at: Any) -> date:
    """Fecha que ordena el diario: visionado si existe; si no, la de creación de la reseña."""
    if watched_on is not None:
        return watched_on
    return created_at.date() if hasattr(created_at, "date") else created_at


def diary_entry(review: Review, movie: Movie) -> DiaryEntry:
    when = effective_date(review.watched_on, review.created_at)
    return DiaryEntry(
        review_id=review.id,
        movie_slug=movie.slug,
        movie_title=movie.title,
        poster_path=movie.poster_path,
        watched_on=review.watched_on,
        day=when.day,
        month_short=MONTHS_ES[when.month - 1],
        year=when.year,
        rating=review.rating,
        stars=_STARS.get(review.rating) if review.rating else None,
        star_label=star_label(review.rating),
        is_rewatch=review.is_rewatch,
        has_review=bool(review.body),
    )


class ProfileService:
    def __init__(self, session: Session, viewer: Principal | None) -> None:
        self.session = session
        self.viewer = viewer
        self.users = UserRepository(session)
        self.social = SocialRepository(session)

    # ── Diario ───────────────────────────────────────────────────────────────
    def diary(self, user_id: int, *, limit: int = 200) -> list[DiaryEntry]:
        rows = self.session.execute(
            select(Review, Movie)
            .join(Movie, Movie.id == Review.movie_id)
            .where(Review.user_id == user_id, Movie.is_published.is_(True))
            .order_by(Review.created_at.desc())
            .limit(limit * 3)  # margen: el orden final depende de watched_on, que puede ser anterior
        ).all()
        rows.sort(key=lambda pair: (effective_date(pair[0].watched_on, pair[0].created_at), pair[0].id), reverse=True)
        return [diary_entry(review, movie) for review, movie in rows[:limit]]

    # ── Listas sociales ──────────────────────────────────────────────────────
    def follow_list(self, username: str, kind: str, *, limit: int = 200) -> list[FollowUser]:
        if kind not in ("followers", "following"):
            raise CatalogError("Lista no válida")
        owner = self.users.by_username(username)
        if owner is None or not owner.is_active:
            raise NotFound("Usuario no encontrado")
        people = (self.social.followers if kind == "followers" else self.social.following)(owner.id, limit=limit)
        viewer_id = self.viewer.user_id if self.viewer else None
        return [
            FollowUser(
                user=UserRef(id=u.id, username=u.username, display_name=u.display_name, avatar_path=u.avatar_path),
                bio=u.bio[:140] if u.bio else None,
                is_following=bool(viewer_id and viewer_id != u.id and self.social.is_following(viewer_id, u.id)),
                is_self=viewer_id == u.id,
            )
            for u in people
        ]

    # ── Favoritas ────────────────────────────────────────────────────────────
    def favorites(self, user_id: int) -> list[int]:
        return self.social.favorite_movie_ids(user_id)

    def add_favorite(self, movie_id: int) -> None:
        actor = require(self.viewer, Permission.REVIEW_WRITE)
        movie = self.session.get(Movie, movie_id)
        if movie is None or not movie.is_published:
            raise NotFound("Película no encontrada")
        current = self.favorites(actor.user_id)
        if movie_id in current:
            return
        if len(current) >= MAX_FAVORITES:
            raise CatalogError(f"Puedes tener hasta {MAX_FAVORITES} favoritas; quita alguna antes de añadir otra.")
        self.social.set_favorites(actor.user_id, [*current, movie_id])

    def remove_favorite(self, movie_id: int) -> None:
        actor = require(self.viewer, Permission.REVIEW_WRITE)
        current = self.favorites(actor.user_id)
        self.social.set_favorites(actor.user_id, [m for m in current if m != movie_id])

    def toggle_favorite(self, movie_id: int) -> bool:
        """Devuelve True si la película queda marcada como favorita."""
        actor = require(self.viewer, Permission.REVIEW_WRITE)
        if movie_id in self.favorites(actor.user_id):
            self.remove_favorite(movie_id)
            return False
        self.add_favorite(movie_id)
        return True

    def owner_of(self, username: str) -> User | None:
        return self.users.by_username(username)
