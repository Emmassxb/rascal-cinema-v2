"""Servicio de escritura del catálogo (panel Partner / Admin).

* Toda operación valida permisos RBAC y propiedad antes de tocar datos.
* Todo ocurre dentro de la unidad de trabajo del llamador (``session_scope``): película,
  relaciones N:M, créditos, especificaciones técnicas, fuentes y auditoría se confirman
  juntos o no se confirma nada.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rascal.auth.rbac import (
    Permission,
    PermissionDenied,
    Principal,
    can_manage_sources,
    require,
    require_movie_edit,
)
from rascal.config import Settings
from rascal.data.enums import CreditRole, MediaType, SourceStatus
from rascal.data.models import Episode, MediaSource, Movie, Season
from rascal.data.repositories.catalog import CardQuery, CatalogRepository
from rascal.data.repositories.media import AuditRepository, SourceRepository
from rascal.domain.schemas import (
    CastInput,
    CrewInput,
    DirectSourceForm,
    MagnetSourceForm,
    MovieCard,
    MovieForm,
    PersonInput,
    SourceView,
    TechSpecsForm,
    TorrentSourceForm,
)
from rascal.domain.slugs import movie_slug_base
from rascal.domain.ratings import to_stars
from rascal.providers import PreparedSource, ProviderError, build_registry

_SCALAR_FIELDS = (
    "title",
    "original_title",
    "tagline",
    "synopsis",
    "release_year",
    "runtime_min",
    "poster_path",
    "backdrop_path",
    "trailer_url",
    "trailer_path",
    "imdb_id",
    "tmdb_id",
    "is_published",
    "media_type",
)


class CatalogError(ValueError):
    """Error de negocio presentable al usuario (campo duplicado, fuente inválida, etc.)."""


class NotFound(LookupError):
    pass


def source_view(source: MediaSource) -> SourceView:
    return SourceView.model_validate(source, from_attributes=True)


class CatalogService:
    def __init__(self, session: Session, principal: Principal | None, settings: Settings) -> None:
        self.session = session
        self.principal = principal
        self.settings = settings
        self.catalog = CatalogRepository(session)
        self.sources = SourceRepository(session)
        self.audit = AuditRepository(session)
        self.providers = build_registry(settings)

    # ── Películas ────────────────────────────────────────────────────────────
    def create_movie(self, form: MovieForm, *, owner_id: int | None = None) -> int:
        actor = require(self.principal, Permission.MOVIE_CREATE)
        owner = owner_id if (owner_id is not None and actor.is_admin) else actor.user_id
        self._check_external_ids(form, exclude_id=None)

        movie: Movie | None = None
        for attempt in range(3):  # reintento ante colisión de slug por concurrencia
            slug = self.catalog.unique_slug(Movie, movie_slug_base(form.title, form.release_year))
            try:
                with self.session.begin_nested():
                    movie = Movie(slug=slug, owner_id=owner, **self._scalars(form))
                    self.session.add(movie)
                    self.session.flush()
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        assert movie is not None

        self._apply_relations(movie, form)
        self.audit.record(
            actor_id=actor.user_id,
            action="movie.create",
            entity_type="movie",
            entity_id=movie.id,
            payload={"title": movie.title, "slug": movie.slug},
        )
        return movie.id

    def update_movie(self, movie_id: int, form: MovieForm) -> None:
        """Reemplaza todos los atributos y relaciones. El slug se mantiene estable
        (no rompe enlaces compartidos aunque cambie el título)."""
        movie = self._get_movie(movie_id)
        actor = require_movie_edit(self.principal, movie.owner_id)
        self._check_external_ids(form, exclude_id=movie.id)
        if movie.media_type is MediaType.SERIES and form.media_type is MediaType.MOVIE:
            has_seasons = self.session.scalar(select(Season.id).where(Season.series_id == movie.id).limit(1))
            if has_seasons is not None:
                raise CatalogError("Esta serie tiene temporadas: elimínalas antes de convertirla en película")

        changes: dict[str, Any] = {}
        for field, value in self._scalars(form).items():
            if getattr(movie, field) != value:
                changes[field] = {"from": _jsonable(getattr(movie, field)), "to": _jsonable(value)}
                setattr(movie, field, value)

        self._apply_relations(movie, form)
        self.session.flush()
        self.audit.record(
            actor_id=actor.user_id,
            action="movie.update",
            entity_type="movie",
            entity_id=movie.id,
            payload={"fields": changes, "relations_replaced": True},
        )

    def set_published(self, movie_id: int, published: bool) -> None:
        movie = self._get_movie(movie_id)
        actor = require_movie_edit(self.principal, movie.owner_id)
        movie.is_published = published
        self.audit.record(
            actor_id=actor.user_id, action="movie.publish" if published else "movie.unpublish",
            entity_type="movie", entity_id=movie.id,
        )

    def delete_movie(self, movie_id: int) -> None:
        actor = require(self.principal, Permission.MOVIE_DELETE)
        movie = self._get_movie(movie_id)
        self.audit.record(
            actor_id=actor.user_id, action="movie.delete", entity_type="movie", entity_id=movie.id,
            payload={"title": movie.title, "slug": movie.slug},
        )
        self.session.delete(movie)
        self.session.flush()

    def assign_owner(self, movie_id: int, owner_id: int | None) -> None:
        actor = require(self.principal, Permission.MOVIE_EDIT_ANY)
        movie = self._get_movie(movie_id)
        previous = movie.owner_id
        movie.owner_id = owner_id
        self.audit.record(
            actor_id=actor.user_id, action="movie.assign_owner", entity_type="movie", entity_id=movie.id,
            payload={"from": previous, "to": owner_id},
        )

    def movie_form(self, movie_id: int) -> MovieForm:
        """Formulario pre-rellenado para la vista de edición."""
        movie = self.catalog.load_movie_full(movie_id=movie_id)
        if movie is None:
            raise NotFound("Película no encontrada")
        require_movie_edit(self.principal, movie.owner_id)
        credits = sorted(movie.credits, key=lambda c: c.billing_order)
        tech = movie.tech_specs
        return MovieForm(
            **{field: getattr(movie, field) for field in _SCALAR_FIELDS},
            genres=[g.name for g in movie.genres],
            companies=[c.name for c in movie.companies],
            directors=[PersonInput(id=c.person_id, name=c.person.name) for c in credits if c.role is CreditRole.DIRECTOR],
            cast=[
                CastInput(id=c.person_id, name=c.person.name, character_name=c.character_name)
                for c in credits
                if c.role is CreditRole.CAST
            ],
            crew=[
                CrewInput(id=c.person_id, name=c.person.name, role=c.role)
                for c in credits
                if c.role not in (CreditRole.CAST, CreditRole.DIRECTOR)
            ],
            tech=TechSpecsForm.model_validate(tech, from_attributes=True) if tech else TechSpecsForm(),
        )

    def manageable_movies(self, *, q: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[MovieCard], int]:
        actor = require(self.principal, Permission.PARTNER_PANEL)
        rows, total = self.catalog.movie_cards(
            CardQuery(
                owner_id=None if actor.can(Permission.MOVIE_EDIT_ANY) else actor.user_id,
                title_contains=q or None,
                include_unpublished=True,
                sort="recent",
                limit=limit,
                offset=offset,
            )
        )
        cards = [
            MovieCard(
                id=r.id, slug=r.slug, title=r.title, media_type=r.media_type, release_year=r.release_year,
                poster_path=r.poster_path,
                average=r.average, average_stars=to_stars(r.average), review_count=r.review_count,
                watchlist_count=r.watchlist_count, is_published=r.is_published,
            )
            for r in rows
        ]
        return cards, total

    # ── Fuentes (P2P y directas) ─────────────────────────────────────────────
    def list_sources(self, movie_id: int, *, episode_id: int | None = None, all_episodes: bool = False) -> list[SourceView]:
        """Fuentes de la película (``episode_id=None``) o de un capítulo concreto."""
        movie = self._get_movie(movie_id)
        self._require_sources(movie)
        return [
            source_view(s)
            for s in self.sources.list_for_movie(movie.id)
            if all_episodes or s.episode_id == episode_id
        ]

    def add_magnet_source(self, movie_id: int, form: MagnetSourceForm, *, episode_id: int | None = None) -> SourceView:
        prepared = self._prepare(lambda: self.providers.p2p.prepare_magnet(form.magnet_uri))
        return self._add_source(movie_id, episode_id, prepared, form, action="source.add_magnet")

    def add_torrent_source(
        self, movie_id: int, form: TorrentSourceForm, data: bytes, *, episode_id: int | None = None
    ) -> SourceView:
        prepared = self._prepare(
            lambda: self.providers.p2p.prepare_torrent(data, max_bytes=self.settings.max_torrent_file_bytes)
        )
        extra = {"files": prepared.values.get("_files"), "private": prepared.values.get("_private")}
        return self._add_source(movie_id, episode_id, prepared, form, action="source.add_torrent", extra=extra)

    def add_direct_source(self, movie_id: int, form: DirectSourceForm, *, episode_id: int | None = None) -> SourceView:
        """URL directa o HLS: se reproduce en el navegador sin pasar por libtorrent."""
        declared = None if form.source_type == "auto" else form.source_type
        prepared = self._prepare(lambda: self.providers.direct.prepare(form.stream_url, declared=declared))
        return self._add_source(movie_id, episode_id, prepared, form, action="source.add_direct")

    def _prepare(self, build: Any) -> PreparedSource:
        try:
            return build()
        except ProviderError as exc:
            raise CatalogError(str(exc)) from exc

    def _add_source(
        self,
        movie_id: int,
        episode_id: int | None,
        prepared: PreparedSource,
        form: Any,
        *,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> SourceView:
        movie = self._get_movie(movie_id)
        actor = self._require_sources(movie)
        if episode_id is not None:
            self._require_episode_of(movie, episode_id)
        elif movie.media_type is MediaType.SERIES:
            raise CatalogError("En una serie, las fuentes se añaden a cada capítulo")
        self._ensure_new_source(movie.id, prepared)
        values = {k: v for k, v in prepared.values.items() if not k.startswith("_")}
        source = self.sources.add(
            MediaSource(
                movie_id=movie.id,
                episode_id=episode_id,
                kind=prepared.kind,
                label=form.label,
                audio_language=form.audio_language,
                quality=form.quality,
                rights_statement=form.rights_statement,
                added_by_id=actor.user_id,
                **values,
            )
        )
        self._audit_source(actor, action, source, extra=extra)
        return source_view(source)

    def update_source(
        self,
        source_id: int,
        *,
        label: str | None = None,
        enabled: bool | None = None,
        audio_language: str | None = None,
        quality: str | None = None,
    ) -> SourceView:
        source = self._get_source(source_id)
        actor = self._require_sources(source.movie)
        if audio_language is not None:
            source.audio_language = audio_language.strip()[:40] or None
        if quality is not None:
            source.quality = quality.strip()[:20] or None
        if label is not None:
            label = label.strip()
            if not 1 <= len(label) <= 120:
                raise CatalogError("La etiqueta debe tener entre 1 y 120 caracteres")
            source.label = label
        if enabled is not None:
            if not enabled:
                source.status = SourceStatus.DISABLED
            elif source.status is SourceStatus.DISABLED:
                source.status = SourceStatus.PENDING
                source.status_message = None
        self.session.flush()
        self._audit_source(
            actor, "source.update", source,
            extra={"label": label, "enabled": enabled, "audio_language": audio_language, "quality": quality},
        )
        return source_view(source)

    def remove_source(self, source_id: int) -> None:
        source = self._get_source(source_id)
        actor = self._require_sources(source.movie)
        self._audit_source(actor, "source.remove", source)
        self.sources.delete(source)

    # ── Internos ─────────────────────────────────────────────────────────────
    def _get_movie(self, movie_id: int) -> Movie:
        movie = self.catalog.get_movie(movie_id)
        if movie is None:
            raise NotFound("Película no encontrada")
        return movie

    def _get_source(self, source_id: int) -> MediaSource:
        source = self.sources.get(source_id)
        if source is None:
            raise NotFound("Fuente no encontrada")
        return source

    def _require_sources(self, movie: Movie) -> Principal:
        if not can_manage_sources(self.principal, movie.owner_id):
            raise PermissionDenied("No puedes gestionar las fuentes de esta película")
        assert self.principal is not None
        return self.principal

    def _ensure_new_source(self, movie_id: int, prepared: PreparedSource) -> None:
        if prepared.kind.is_p2p:
            if self.sources.exists_for_movie(movie_id, prepared.dedupe_key):
                raise CatalogError("Esta fuente (mismo info-hash) ya está vinculada a este título")
        elif self.sources.url_exists_for_movie(movie_id, prepared.dedupe_key):
            raise CatalogError("Este enlace ya está vinculado a este título")

    def _require_episode_of(self, movie: Movie, episode_id: int) -> Episode:
        episode = self.session.get(Episode, episode_id)
        if episode is None or episode.season.series_id != movie.id:
            raise NotFound("Capítulo no encontrado en esta serie")
        return episode

    def _audit_source(self, actor: Principal, action: str, source: MediaSource, extra: dict[str, Any] | None = None) -> None:
        self.audit.record(
            actor_id=actor.user_id,
            action=action,
            entity_type="media_source",
            entity_id=source.id,
            payload={
                "movie_id": source.movie_id,
                "episode_id": source.episode_id,
                "source_type": source.source_type,
                "info_hash": source.info_hash,
                "url_hash": source.url_hash,
                **(extra or {}),
            },
        )

    @staticmethod
    def _scalars(form: MovieForm) -> dict[str, Any]:
        return {field: getattr(form, field) for field in _SCALAR_FIELDS}

    def _check_external_ids(self, form: MovieForm, *, exclude_id: int | None) -> None:
        for column, value, label in ((Movie.imdb_id, form.imdb_id, "IMDb"), (Movie.tmdb_id, form.tmdb_id, "TMDB")):
            if value is None:
                continue
            stmt = select(Movie.id).where(column == value)
            if column is Movie.tmdb_id:  # TMDB numera películas y series por separado
                stmt = stmt.where(Movie.media_type == form.media_type)
            if exclude_id is not None:
                stmt = stmt.where(Movie.id != exclude_id)
            if self.session.scalar(stmt) is not None:
                raise CatalogError(f"Ya existe otro título con ese id de {label}")

    def _resolve_person(self, person: PersonInput) -> int:
        if person.id is not None:
            existing = self.catalog.get_person(person.id)
            if existing is None:
                raise CatalogError(f"La persona con id {person.id} no existe")
            return existing.id
        return self.catalog.get_or_create_person(person.name).id

    def _apply_relations(self, movie: Movie, form: MovieForm) -> None:
        movie.genres = [self.catalog.get_or_create_genre(name) for name in form.genres]
        movie.companies = [self.catalog.get_or_create_company(name) for name in form.companies]

        credits: list[tuple[int, CreditRole, str | None, int]] = []
        seen: set[tuple[int, CreditRole]] = set()

        def push(person_id: int, role: CreditRole, character: str | None, order: int) -> None:
            if (person_id, role) not in seen:  # id y nombre pueden resolver a la misma persona
                seen.add((person_id, role))
                credits.append((person_id, role, character, order))

        for order, director in enumerate(form.directors):
            push(self._resolve_person(director), CreditRole.DIRECTOR, None, order)
        for order, member in enumerate(form.cast):
            push(self._resolve_person(member), CreditRole.CAST, member.character_name, order)
        for order, crew in enumerate(form.crew):
            push(self._resolve_person(crew), crew.role, None, order)
        self.catalog.replace_credits(movie, credits)

        self.catalog.set_tech_specs(movie, None if form.tech.is_empty() else form.tech.model_dump())


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
