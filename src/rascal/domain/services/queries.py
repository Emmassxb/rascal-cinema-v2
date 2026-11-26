"""Modelos de lectura para la UI (ficha de película, perfiles de entidad, home, búsqueda).

Cada método devuelve DTOs inmutables listos para ``st.cache_data``. Las vistas de entidad
(actor, director, productora, género) comparten una sola implementación parametrizada por
``kind`` + ``slug``, que es exactamente lo que codifican los ``st.query_params``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from rascal.auth.rbac import Principal, can_edit_movie, can_manage_sources
from rascal.data.enums import CreditRole, MediaType, SourceStatus
from rascal.data.models import Company, Genre, Movie, MovieCredit, Person
from rascal.data.repositories.catalog import CardQuery, CatalogRepository, MovieCardRow
from rascal.data.repositories.reviews import ReviewRepository, ReviewRow
from rascal.data.repositories.users import SocialRepository, UserRepository
from rascal.domain.metrics import entity_metrics, movie_rating_summary
from rascal.domain.services.people import can_edit_person
from rascal.domain.services.series import SeriesQueries
from rascal.domain.services.watch import list_watch_links
from rascal.domain.ratings import star_label, to_stars
from rascal.domain.schemas import (
    CreditView,
    EntityKind,
    EntityProfile,
    EntityRef,
    FilmographyFilters,
    MovieCard,
    MovieDetail,
    ReviewView,
    RoleCount,
    SearchHit,
    SourceView,
    TechSpecsView,
    UserPublicProfile,
    UserRef,
)

GLOBAL_MEAN_TTL_S = 300
_GLOBAL_MEAN_CACHE: dict[str, tuple[float, float]] = {}

SearchKind = str  # "movies" | "directors" | "cast" | "people" | "companies" | "genres" | "users"


@dataclass(frozen=True, slots=True)
class HomeFeed:
    top_rated: list[MovieCard]
    popular: list[MovieCard]
    recent: list[MovieCard]
    popular_among_followed: list[MovieCard]


def card_from_row(row: MovieCardRow, roles: list[tuple[CreditRole, str | None]] | None = None) -> MovieCard:
    return MovieCard(
        id=row.id,
        slug=row.slug,
        title=row.title,
        media_type=row.media_type,
        release_year=row.release_year,
        poster_path=row.poster_path,
        average=row.average,
        average_stars=to_stars(row.average),
        review_count=row.review_count,
        watchlist_count=row.watchlist_count,
        roles=sorted({role for role, _ in roles or []}, key=lambda r: list(CreditRole).index(r)),
        characters=[c for role, c in roles or [] if role is CreditRole.CAST and c],
    )


def review_view(row: ReviewRow) -> ReviewView:
    review = row.review
    stars = to_stars(review.rating) if review.rating is not None else None
    return ReviewView(
        id=review.id,
        user=UserRef(
            id=row.user.id,
            username=row.user.username,
            display_name=row.user.display_name,
            avatar_path=row.user.avatar_path,
        ),
        movie_id=review.movie_id,
        movie_slug=row.movie_slug,
        movie_title=row.movie_title,
        rating=review.rating,
        stars=stars,
        star_label=star_label(stars),
        body=review.body,
        watched_on=review.watched_on,
        is_rewatch=review.is_rewatch,
        contains_spoilers=review.contains_spoilers,
        created_at=review.created_at,
        votes_up=row.votes_up,
        votes_down=row.votes_down,
    )


def _ref(kind: EntityKind, entity: Person | Company | Genre) -> EntityRef:
    return EntityRef(kind=kind, id=entity.id, slug=entity.slug, name=entity.name)


class CatalogQueries:
    def __init__(self, session: Session, viewer: Principal | None = None) -> None:
        self.session = session
        self.viewer = viewer
        self.catalog = CatalogRepository(session)
        self.reviews = ReviewRepository(session)
        self.social = SocialRepository(session)

    # ── Ficha de película ────────────────────────────────────────────────────
    def movie_detail(self, *, slug: str | None = None, movie_id: int | None = None) -> MovieDetail | None:
        movie = self.catalog.load_movie_full(movie_id=movie_id, slug=slug)
        if movie is None:
            return None
        if not movie.is_published and not can_edit_movie(self.viewer, movie.owner_id):
            return None

        directors = [
            _ref("person", c.person)
            for c in sorted(movie.credits, key=lambda c: c.billing_order)
            if c.role is CreditRole.DIRECTOR
        ]

        def credit_view(credit: MovieCredit) -> CreditView:
            return CreditView(
                person=_ref("person", credit.person),
                role=credit.role,
                character_name=credit.character_name,
                billing_order=credit.billing_order,
            )

        cast = [credit_view(c) for c in sorted(movie.credits, key=lambda c: c.billing_order) if c.role is CreditRole.CAST]
        crew = [
            credit_view(c)
            for c in sorted(movie.credits, key=lambda c: (list(CreditRole).index(c.role), c.billing_order))
            if c.role not in (CreditRole.CAST, CreditRole.DIRECTOR)
        ]
        sources = self.title_sources(movie.id, movie.owner_id)
        return MovieDetail(
            id=movie.id,
            media_type=movie.media_type,
            slug=movie.slug,
            title=movie.title,
            original_title=movie.original_title,
            tagline=movie.tagline,
            synopsis=movie.synopsis,
            release_year=movie.release_year,
            runtime_min=movie.runtime_min,
            poster_path=movie.poster_path,
            backdrop_path=movie.backdrop_path,
            trailer_url=movie.trailer_url,
            trailer_path=movie.trailer_path,
            imdb_id=movie.imdb_id,
            tmdb_id=movie.tmdb_id,
            owner_id=movie.owner_id,
            is_published=movie.is_published,
            genres=[_ref("genre", g) for g in movie.genres],
            companies=[_ref("company", c) for c in movie.companies],
            directors=directors,
            cast=cast,
            crew=crew,
            tech=TechSpecsView.model_validate(movie.tech_specs, from_attributes=True) if movie.tech_specs else None,
            sources=sources,
            rating=movie_rating_summary(self.session, movie.id),
            watchlist_count=movie.watchlist_count,
            watch_links=list_watch_links(self.session, movie.id),
            seasons=SeriesQueries(self.session).seasons(movie.id) if movie.media_type is MediaType.SERIES else [],
        )

    def title_sources(self, movie_id: int, owner_id: int | None, *, episode_id: int | None = None) -> list[SourceView]:
        """Fuentes visibles para el espectador (las desactivadas, sólo para quien las gestiona)."""
        show_all = can_manage_sources(self.viewer, owner_id)
        return [
            SourceView.model_validate(s, from_attributes=True)
            for s in self.catalog.title_sources(movie_id, episode_id=episode_id)
            if show_all or s.status is not SourceStatus.DISABLED
        ]

    def movie_reviews(self, movie_id: int, *, order: str = "recent", limit: int = 50, offset: int = 0) -> list[ReviewView]:
        return [review_view(r) for r in self.reviews.list_for_movie(movie_id, order=order, limit=limit, offset=offset)]

    def viewer_state(self, movie_id: int) -> dict[str, bool]:
        """Estado del usuario actual respecto a la película (watchlist)."""
        if self.viewer is None:
            return {"in_watchlist": False}
        return {"in_watchlist": self.social.in_watchlist(self.viewer.user_id, movie_id)}

    # ── Perfiles de entidad (actor / director / productora / género) ────────
    def entity_profile(self, kind: EntityKind, slug: str, filters: FilmographyFilters | None = None) -> EntityProfile | None:
        filters = filters or FilmographyFilters()
        entity = self.catalog.get_entity_by_slug(kind, slug)
        if entity is None:
            return None

        role = filters.role if kind == "person" else None
        movie_ids = self.catalog.movie_ids_for_entity(kind, entity.id, role)
        rows, total = self.catalog.movie_cards(
            CardQuery(
                movie_ids=movie_ids,
                genre_slug=filters.genre_slug if kind != "genre" else None,
                year_from=filters.year_from,
                year_to=filters.year_to,
                min_average=filters.min_stars * 2 if filters.min_stars else None,
                sort=filters.sort,
                limit=filters.limit,
                offset=filters.offset,
            )
        )
        roles_by_movie = (
            self.catalog.person_roles_by_movie(entity.id, [r.id for r in rows]) if isinstance(entity, Person) else {}
        )
        filmography = [card_from_row(r, roles_by_movie.get(r.id)) for r in rows]

        # Métricas sobre TODA la filmografía (con el filtro de rol), no sólo la página actual.
        _, films_total = self.catalog.movie_cards(CardQuery(movie_ids=movie_ids, limit=1))
        metrics = entity_metrics(self.session, kind, entity.id, role=role, films=films_total)

        profile = EntityProfile(
            ref=_ref(kind, entity),
            metrics=metrics,
            filmography=filmography,
            filmography_total=total,
            description=getattr(entity, "bio", None),
            image_path=getattr(entity, "photo_path", None) or getattr(entity, "logo_path", None),
            country=getattr(entity, "country", None),
            birth_date=getattr(entity, "birth_date", None),
            death_date=getattr(entity, "death_date", None),
            place_of_birth=getattr(entity, "place_of_birth", None),
            known_for_department=getattr(entity, "known_for_department", None),
            tmdb_id=getattr(entity, "tmdb_id", None),
            can_edit=isinstance(entity, Person) and can_edit_person(self.session, self.viewer, entity.id),
            roles=[RoleCount(role=r, films=n) for r, n in self.catalog.person_role_counts(entity.id)]
            if isinstance(entity, Person)
            else [],
        )
        return profile

    # ── Home ─────────────────────────────────────────────────────────────────
    def home(self, *, limit: int = 12) -> HomeFeed:
        prior = self._cached_global_mean()
        top, _ = self.catalog.movie_cards(CardQuery(sort="weighted", prior_mean=prior, limit=limit, with_total=False))
        popular, _ = self.catalog.movie_cards(CardQuery(sort="popularity", limit=limit, with_total=False))
        recent, _ = self.catalog.movie_cards(CardQuery(sort="recent", limit=limit, with_total=False))
        followed: list[MovieCard] = []
        if self.viewer is not None:
            ranked = self.reviews.popular_among_followed(self.viewer.user_id, limit)
            if ranked:
                ids = [movie_id for movie_id, _ in ranked]
                rows, _ = self.catalog.movie_cards(
                    CardQuery(movie_ids=select(Movie.id).where(Movie.id.in_(ids)), limit=limit)
                )
                by_id = {r.id: r for r in rows}
                followed = [card_from_row(by_id[i]) for i in ids if i in by_id]
        return HomeFeed(
            top_rated=[card_from_row(r) for r in top],
            popular=[card_from_row(r) for r in popular],
            recent=[card_from_row(r) for r in recent],
            popular_among_followed=followed,
        )

    def _cached_global_mean(self) -> float:
        """Media global de calificaciones (prior bayesiano del «Top»), cacheada 5 min por base de datos:
        cambia muy despacio y calcularla recorre todos los títulos con reseñas."""
        key = str(self.session.get_bind().url)
        hit = _GLOBAL_MEAN_CACHE.get(key)
        now = time.monotonic()
        if hit is None or now - hit[1] > GLOBAL_MEAN_TTL_S:
            hit = (self.reviews.global_mean() or 6.0, now)
            _GLOBAL_MEAN_CACHE[key] = hit
        return hit[0]

    def browse(
        self, filters: FilmographyFilters, *, title: str | None = None, media_type: MediaType | None = None
    ) -> tuple[list[MovieCard], int]:
        rows, total = self.catalog.movie_cards(
            CardQuery(
                media_type=media_type,
                genre_slug=filters.genre_slug,
                year_from=filters.year_from,
                year_to=filters.year_to,
                min_average=filters.min_stars * 2 if filters.min_stars else None,
                title_contains=title,
                sort=filters.sort,
                limit=filters.limit,
                offset=filters.offset,
            )
        )
        return [card_from_row(r) for r in rows], total

    def genres(self) -> list[EntityRef]:
        return [_ref("genre", g) for g in self.catalog.all_genres()]

    # ── Búsqueda (mismos tipos que el buscador anterior, ahora navegables) ──
    def search(self, q: str, kind: SearchKind = "movies", *, limit: int = 50) -> list[SearchHit]:
        q = q.strip()
        if not q:
            return []
        if kind == "movies":
            return [
                SearchHit(kind="series" if m.media_type is MediaType.SERIES else "movie", id=m.id, slug=m.slug,
                          title=m.name, subtitle=str(m.year) if m.year else None, image_path=m.image_path)
                for m in self.catalog.search_movies(q, limit)
            ]
        if kind in ("directors", "cast", "people"):
            role = {"directors": CreditRole.DIRECTOR, "cast": CreditRole.CAST}.get(kind)
            return [
                SearchHit(kind="person", id=p.id, slug=p.slug, title=p.name, subtitle=f"{p.films} títulos",
                          image_path=p.image_path)
                for p in self.catalog.search_people(q, limit, role)
            ]
        if kind == "companies":
            return [
                SearchHit(kind="company", id=c.id, slug=c.slug, title=c.name, subtitle=f"{c.films} títulos",
                          image_path=c.image_path)
                for c in self.catalog.search_companies(q, limit)
            ]
        if kind == "genres":
            return [
                SearchHit(kind="genre", id=g.id, slug=g.slug, title=g.name, subtitle=f"{g.films} títulos")
                for g in self.catalog.search_genres(q, limit)
            ]
        if kind == "users":
            return [
                SearchHit(kind="user", id=u.id, slug=u.username, title=u.username, subtitle=u.display_name,
                          image_path=u.avatar_path)
                for u in UserRepository(self.session).search(q, limit)
            ]
        raise ValueError(f"Tipo de búsqueda desconocido: {kind}")

    # ── Perfil público de usuario ────────────────────────────────────────────
    def user_profile(self, username: str, *, reviews_limit: int = 30) -> UserPublicProfile | None:
        user = UserRepository(self.session).by_username(username)
        if user is None or not user.is_active:
            return None
        followers, following = self.social.follow_counts(user.id)
        films, avg_given = self.reviews.user_stats(user.id)

        favorite_ids = self.social.favorite_movie_ids(user.id)
        favorites: list[MovieCard] = []
        if favorite_ids:
            rows, _ = self.catalog.movie_cards(
                CardQuery(movie_ids=select(Movie.id).where(Movie.id.in_(favorite_ids)), limit=len(favorite_ids))
            )
            by_id = {r.id: r for r in rows}
            favorites = [card_from_row(by_id[i]) for i in favorite_ids if i in by_id]

        watch_rows, _ = self.catalog.movie_cards(
            CardQuery(movie_ids=self.social.watchlist_movie_ids(user.id), sort="title", limit=200)
        )
        is_self = self.viewer is not None and self.viewer.user_id == user.id
        return UserPublicProfile(
            user=UserRef(id=user.id, username=user.username, display_name=user.display_name, avatar_path=user.avatar_path),
            bio=user.bio,
            role=user.role,
            joined_at=user.created_at,
            followers=followers,
            following=following,
            is_following=bool(self.viewer and not is_self and self.social.is_following(self.viewer.user_id, user.id)),
            is_self=is_self,
            films_logged=films,
            average_given=avg_given,
            favorites=favorites,
            watchlist=[card_from_row(r) for r in watch_rows],
            recent_reviews=[review_view(r) for r in self.reviews.list_for_user(user.id, limit=reviews_limit)],
        )
