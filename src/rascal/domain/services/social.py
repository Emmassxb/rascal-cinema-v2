"""Casos de uso sociales: seguir usuarios, watchlist, favoritos y perfil."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rascal.auth.rbac import PermissionDenied, Principal
from rascal.data.models import Movie
from rascal.data.repositories.catalog import CatalogRepository
from rascal.data.repositories.users import SocialRepository, UserRepository
from rascal.domain.schemas import ProfileForm
from rascal.domain.services.catalog import CatalogError, NotFound


class SocialService:
    def __init__(self, session: Session, principal: Principal | None) -> None:
        self.session = session
        self.principal = principal
        self.users = UserRepository(session)
        self.social = SocialRepository(session)
        self.catalog = CatalogRepository(session)

    def _actor(self) -> Principal:
        if self.principal is None:
            raise PermissionDenied("Debes iniciar sesión")
        return self.principal

    def follow(self, username: str) -> bool:
        actor = self._actor()
        target = self.users.by_username(username)
        if target is None or not target.is_active:
            raise NotFound("Usuario no encontrado")
        if target.id == actor.user_id:
            raise CatalogError("No puedes seguirte a ti mismo")
        return self.social.follow(actor.user_id, target.id)

    def unfollow(self, username: str) -> bool:
        actor = self._actor()
        target = self.users.by_username(username)
        if target is None:
            raise NotFound("Usuario no encontrado")
        return self.social.unfollow(actor.user_id, target.id)

    def toggle_watchlist(self, movie_id: int) -> bool:
        actor = self._actor()
        movie = self.catalog.get_movie(movie_id)
        if movie is None or not movie.is_published:
            raise NotFound("Película no encontrada")
        return self.social.toggle_watchlist(actor.user_id, movie.id)

    def update_profile(self, form: ProfileForm) -> None:
        actor = self._actor()
        user = self.users.get(actor.user_id)
        if user is None:
            raise NotFound("Usuario no encontrado")
        user.display_name = form.display_name
        user.bio = form.bio
        user.avatar_path = form.avatar_path
        if form.favorite_movie_ids:
            found = self.session.scalar(
                select(func.count()).where(Movie.id.in_(form.favorite_movie_ids), Movie.is_published.is_(True))
            )
            if found != len(set(form.favorite_movie_ids)):
                raise CatalogError("Alguna película favorita no existe")
        self.social.set_favorites(user.id, form.favorite_movie_ids)
