"""Plataformas oficiales ("Dónde ver") de un título. Permisos: editar la ficha."""

from __future__ import annotations

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from rascal.auth.rbac import Principal, require_movie_edit
from rascal.data.models import Movie, WatchLink
from rascal.data.repositories.media import AuditRepository
from rascal.domain.schemas import WatchLinkForm, WatchLinkView
from rascal.domain.services.catalog import CatalogError, NotFound
from rascal.domain.slugs import slugify

MAX_LINKS_PER_TITLE = 60


def watch_link_view(link: WatchLink) -> WatchLinkView:
    return WatchLinkView.model_validate(link, from_attributes=True)


# Orden de presentación: suscripción → gratis → renta → compra (como JustWatch/TMDB).
OFFER_ORDER = case(
    {"subscription": 0, "free": 1, "rent": 2, "buy": 3}, value=WatchLink.offer_type, else_=9
)


def list_watch_links(session: Session, movie_id: int, region: str | None = None) -> list[WatchLinkView]:
    stmt = select(WatchLink).where(WatchLink.movie_id == movie_id).order_by(OFFER_ORDER, WatchLink.platform_name)
    if region:
        stmt = stmt.where(WatchLink.region == region.upper())
    return [watch_link_view(link) for link in session.scalars(stmt.limit(MAX_LINKS_PER_TITLE))]


class WatchLinkService:
    def __init__(self, session: Session, principal: Principal | None) -> None:
        self.session = session
        self.principal = principal
        self.audit = AuditRepository(session)

    def _movie(self, movie_id: int) -> tuple[Movie, Principal]:
        movie = self.session.get(Movie, movie_id)
        if movie is None:
            raise NotFound("Título no encontrado")
        return movie, require_movie_edit(self.principal, movie.owner_id)

    def _link(self, link_id: int) -> tuple[WatchLink, Principal]:
        link = self.session.get(WatchLink, link_id)
        if link is None:
            raise NotFound("Enlace no encontrado")
        _, actor = self._movie(link.movie_id)
        return link, actor

    def add(self, movie_id: int, form: WatchLinkForm, *, platform_slug: str | None = None) -> WatchLinkView:
        movie, actor = self._movie(movie_id)
        slug = (platform_slug or slugify(form.platform_name))[:80] or "plataforma"
        count = len(self.session.scalars(select(WatchLink.id).where(WatchLink.movie_id == movie.id)).all())
        if count >= MAX_LINKS_PER_TITLE:
            raise CatalogError(f"Máximo {MAX_LINKS_PER_TITLE} plataformas por título")
        duplicate = self.session.scalar(
            select(WatchLink.id).where(
                WatchLink.movie_id == movie.id, WatchLink.platform_slug == slug,
                WatchLink.offer_type == form.offer_type, WatchLink.region == form.region,
            )
        )
        if duplicate:
            raise CatalogError(f"{form.platform_name} ({form.offer_type.value}, {form.region}) ya está en la lista")
        link = WatchLink(
            movie_id=movie.id, platform_name=form.platform_name, platform_slug=slug, url=form.url,
            offer_type=form.offer_type, region=form.region, logo_path=form.logo_path, added_by_id=actor.user_id,
        )
        self.session.add(link)
        self.session.flush()
        self.audit.record(actor_id=actor.user_id, action="watch_link.add", entity_type="watch_link",
                          entity_id=link.id, payload={"movie_id": movie.id, "platform": slug})
        return watch_link_view(link)

    def update(self, link_id: int, form: WatchLinkForm) -> WatchLinkView:
        link, actor = self._link(link_id)
        link.platform_name, link.url, link.offer_type = form.platform_name, form.url, form.offer_type
        link.region = form.region
        if form.logo_path:
            link.logo_path = form.logo_path
        self.session.flush()
        self.audit.record(actor_id=actor.user_id, action="watch_link.update", entity_type="watch_link",
                          entity_id=link.id, payload={"movie_id": link.movie_id})
        return watch_link_view(link)

    def delete(self, link_id: int) -> int:
        link, actor = self._link(link_id)
        movie_id = link.movie_id
        self.audit.record(actor_id=actor.user_id, action="watch_link.delete", entity_type="watch_link",
                          entity_id=link.id, payload={"movie_id": movie_id, "platform": link.platform_slug})
        self.session.delete(link)
        self.session.flush()
        return movie_id
