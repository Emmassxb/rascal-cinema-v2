"""Casos de uso de reseñas (diario estilo Letterboxd) y votos."""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from rascal.auth.rbac import Permission, PermissionDenied, Principal, require
from rascal.data.models import Review
from rascal.data.repositories.catalog import CatalogRepository
from rascal.data.repositories.reviews import ReviewRepository
from rascal.domain.schemas import ReviewForm
from rascal.domain.services.catalog import NotFound


class ReviewService:
    def __init__(self, session: Session, principal: Principal | None) -> None:
        self.session = session
        self.principal = principal
        self.reviews = ReviewRepository(session)
        self.catalog = CatalogRepository(session)

    def add(self, movie_id: int, form: ReviewForm) -> int:
        actor = require(self.principal, Permission.REVIEW_WRITE)
        movie = self.catalog.get_movie(movie_id)
        if movie is None or not movie.is_published:
            raise NotFound("Película no encontrada")
        review = self.reviews.add(
            Review(
                user_id=actor.user_id,
                movie_id=movie.id,
                rating=form.rating,
                body=form.body,
                watched_on=form.watched_on,
                is_rewatch=form.is_rewatch,
                contains_spoilers=form.contains_spoilers,
            )
        )
        return review.id

    def update(self, review_id: int, form: ReviewForm) -> None:
        review = self._own_review(review_id)
        review.rating = form.rating
        review.body = form.body
        review.watched_on = form.watched_on
        review.is_rewatch = form.is_rewatch
        review.contains_spoilers = form.contains_spoilers
        self.session.flush()

    def delete(self, review_id: int) -> None:
        review = self.reviews.get(review_id)
        if review is None:
            raise NotFound("Reseña no encontrada")
        actor = require(self.principal, Permission.REVIEW_WRITE)
        if review.user_id != actor.user_id and not actor.can(Permission.REVIEW_MODERATE):
            raise PermissionDenied("Sólo puedes borrar tus propias reseñas")
        self.reviews.delete(review)

    def vote(self, review_id: int, value: Literal[-1, 0, 1]) -> tuple[int, int]:
        """Vota una reseña (1 = me gusta, -1 = no útil, 0 = retirar). Devuelve (up, down)."""
        actor = require(self.principal, Permission.REVIEW_WRITE)
        if value not in (-1, 0, 1):
            raise ValueError("Voto inválido")
        review = self.reviews.get(review_id)
        if review is None:
            raise NotFound("Reseña no encontrada")
        if review.user_id == actor.user_id:
            raise PermissionDenied("No puedes votar tu propia reseña")
        self.reviews.set_vote(review.id, actor.user_id, value)
        return self.reviews.vote_totals(review.id)

    def _own_review(self, review_id: int) -> Review:
        actor = require(self.principal, Permission.REVIEW_WRITE)
        review = self.reviews.get(review_id)
        if review is None:
            raise NotFound("Reseña no encontrada")
        if review.user_id != actor.user_id:
            raise PermissionDenied("Sólo puedes editar tus propias reseñas")
        return review
