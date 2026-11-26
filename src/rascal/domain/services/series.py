"""Temporadas y capítulos (panel Partner) + lectura pública e importación desde TMDB.

Permisos: los mismos que editar la ficha de la serie (dueño o admin). La importación desde
TMDB es **aditiva**: crea temporadas y capítulos que falten y rellena campos vacíos, pero
nunca sobrescribe lo que un partner ya editó.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from rascal.auth.rbac import Principal, require_movie_edit
from rascal.data.enums import MediaType, SourceStatus
from rascal.data.models import Episode, MediaSource, Movie, Season
from rascal.data.repositories.media import AuditRepository
from rascal.domain.schemas import EpisodeForm, EpisodeView, SeasonForm, SeasonView
from rascal.domain.services.catalog import CatalogError, NotFound

MAX_EPISODES_PER_SEASON = 2000


def season_view(season: Season, episode_count: int) -> SeasonView:
    return SeasonView(
        id=season.id, season_number=season.season_number, title=season.title, overview=season.overview,
        poster_path=season.poster_path, air_year=season.air_year, episode_count=episode_count,
    )


def episode_view(episode: Episode, season_number: int, source_count: int = 0) -> EpisodeView:
    return EpisodeView(
        id=episode.id, season_id=episode.season_id, season_number=season_number,
        episode_number=episode.episode_number, title=episode.title, synopsis=episode.synopsis,
        runtime_min=episode.runtime_min, still_path=episode.still_path, air_date=episode.air_date,
        source_count=source_count,
    )


class SeriesQueries:
    """Lectura pública (sin permisos especiales): temporadas y capítulos con conteos."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def seasons(self, series_id: int) -> list[SeasonView]:
        counts = (
            select(Episode.season_id, func.count(Episode.id).label("n")).group_by(Episode.season_id).subquery()
        )
        rows = self.session.execute(
            select(Season, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.season_id == Season.id)
            .where(Season.series_id == series_id)
            .order_by(Season.season_number)
        )
        return [season_view(season, int(n)) for season, n in rows]

    def episodes(self, season_id: int, *, include_disabled_sources: bool = False) -> list[EpisodeView]:
        season = self.session.get(Season, season_id)
        if season is None:
            return []
        source_filter = [MediaSource.episode_id == Episode.id]
        if not include_disabled_sources:
            source_filter.append(MediaSource.status != SourceStatus.DISABLED)
        count = select(func.count(MediaSource.id)).where(*source_filter).scalar_subquery()
        rows = self.session.execute(
            select(Episode, count)
            .where(Episode.season_id == season_id)
            .order_by(Episode.episode_number)
            .limit(MAX_EPISODES_PER_SEASON)
        )
        return [episode_view(ep, season.season_number, int(n or 0)) for ep, n in rows]

    def episode(self, episode_id: int) -> tuple[Episode, Season] | None:
        episode = self.session.get(Episode, episode_id)
        return (episode, episode.season) if episode is not None else None


class SeriesService:
    def __init__(self, session: Session, principal: Principal | None) -> None:
        self.session = session
        self.principal = principal
        self.audit = AuditRepository(session)

    # ── Permisos ─────────────────────────────────────────────────────────────
    def _series(self, series_id: int) -> tuple[Movie, Principal]:
        movie = self.session.get(Movie, series_id)
        if movie is None:
            raise NotFound("Serie no encontrada")
        actor = require_movie_edit(self.principal, movie.owner_id)
        if movie.media_type is not MediaType.SERIES:
            raise CatalogError("Este título es una película: cambia su tipo a «Serie» para añadir temporadas")
        return movie, actor

    def _season(self, season_id: int) -> tuple[Season, Principal]:
        season = self.session.get(Season, season_id)
        if season is None:
            raise NotFound("Temporada no encontrada")
        _, actor = self._series(season.series_id)
        return season, actor

    def _episode(self, episode_id: int) -> tuple[Episode, Principal]:
        episode = self.session.get(Episode, episode_id)
        if episode is None:
            raise NotFound("Capítulo no encontrado")
        _, actor = self._series(episode.season.series_id)
        return episode, actor

    def can_edit(self, series_id: int) -> bool:
        try:
            self._series(series_id)
        except Exception:
            return False
        return True

    # ── Temporadas ───────────────────────────────────────────────────────────
    def add_season(self, series_id: int, form: SeasonForm) -> int:
        movie, actor = self._series(series_id)
        if self.session.scalar(
            select(Season.id).where(Season.series_id == movie.id, Season.season_number == form.season_number)
        ):
            raise CatalogError(f"La temporada {form.season_number} ya existe")
        season = Season(series_id=movie.id, **form.model_dump())
        self.session.add(season)
        self.session.flush()
        self._log(actor, "season.create", "season", season.id, {"series_id": movie.id, "number": form.season_number})
        return season.id

    def update_season(self, season_id: int, form: SeasonForm) -> None:
        season, actor = self._season(season_id)
        if form.season_number != season.season_number and self.session.scalar(
            select(Season.id).where(Season.series_id == season.series_id, Season.season_number == form.season_number)
        ):
            raise CatalogError(f"La temporada {form.season_number} ya existe")
        for key, value in form.model_dump().items():
            setattr(season, key, value)
        self.session.flush()
        self._log(actor, "season.update", "season", season.id, {"series_id": season.series_id})

    def delete_season(self, season_id: int) -> int:
        season, actor = self._season(season_id)
        series_id = season.series_id
        self._log(actor, "season.delete", "season", season.id, {"series_id": series_id, "number": season.season_number})
        self.session.delete(season)
        self.session.flush()
        return series_id

    # ── Capítulos ────────────────────────────────────────────────────────────
    def add_episode(self, season_id: int, form: EpisodeForm) -> int:
        season, actor = self._season(season_id)
        if self.session.scalar(
            select(Episode.id).where(Episode.season_id == season.id, Episode.episode_number == form.episode_number)
        ):
            raise CatalogError(f"El capítulo {form.episode_number} ya existe en esta temporada")
        episode = Episode(season_id=season.id, **form.model_dump())
        self.session.add(episode)
        self.session.flush()
        self._log(actor, "episode.create", "episode", episode.id, {"season_id": season.id})
        return episode.id

    def update_episode(self, episode_id: int, form: EpisodeForm) -> None:
        episode, actor = self._episode(episode_id)
        if form.episode_number != episode.episode_number and self.session.scalar(
            select(Episode.id).where(Episode.season_id == episode.season_id, Episode.episode_number == form.episode_number)
        ):
            raise CatalogError(f"El capítulo {form.episode_number} ya existe en esta temporada")
        for key, value in form.model_dump().items():
            setattr(episode, key, value)
        self.session.flush()
        self._log(actor, "episode.update", "episode", episode.id, {"season_id": episode.season_id})

    def delete_episode(self, episode_id: int) -> int:
        episode, actor = self._episode(episode_id)
        series_id = episode.season.series_id
        self._log(actor, "episode.delete", "episode", episode.id, {"season_id": episode.season_id})
        # Las fuentes del capítulo se borran por cascada (FK ON DELETE CASCADE + ORM).
        for source in list(episode.sources):
            self.session.delete(source)
        self.session.delete(episode)
        self.session.flush()
        return series_id

    def episode_for_edit(self, episode_id: int) -> tuple[Episode, Season, Movie]:
        episode, _ = self._episode(episode_id)
        season = episode.season
        return episode, season, season.series

    def seasons_with_episodes(self, series_id: int) -> list[Season]:
        movie, _ = self._series(series_id)
        return list(
            self.session.scalars(
                select(Season)
                .options(selectinload(Season.episodes))
                .where(Season.series_id == movie.id)
                .order_by(Season.season_number)
            )
        )

    # ── TMDB ─────────────────────────────────────────────────────────────────
    def import_tmdb_seasons(self, series_id: int, seasons: list[Any]) -> dict[str, int]:
        """Aplica temporadas ya descargadas de TMDB (``TmdbSeason``). Aditivo: no pisa ediciones."""
        movie, actor = self._series(series_id)
        existing = {s.season_number: s for s in self.session.scalars(select(Season).where(Season.series_id == movie.id))}
        stats = {"seasons_created": 0, "episodes_created": 0, "fields_filled": 0}
        for tmdb_season in seasons:
            season = existing.get(tmdb_season.season_number)
            if season is None:
                season = Season(series_id=movie.id, season_number=tmdb_season.season_number)
                self.session.add(season)
                self.session.flush()
                stats["seasons_created"] += 1
            stats["fields_filled"] += _fill(season, {
                "title": tmdb_season.title, "overview": tmdb_season.overview, "poster_path": tmdb_season.poster_url,
                "air_year": tmdb_season.air_year, "tmdb_season_id": tmdb_season.tmdb_season_id,
            })
            episodes = {e.episode_number: e for e in self.session.scalars(select(Episode).where(Episode.season_id == season.id))}
            for tmdb_ep in tmdb_season.episodes:
                episode = episodes.get(tmdb_ep.episode_number)
                if episode is None:
                    episode = Episode(season_id=season.id, episode_number=tmdb_ep.episode_number)
                    self.session.add(episode)
                    stats["episodes_created"] += 1
                stats["fields_filled"] += _fill(episode, {
                    "title": tmdb_ep.title, "synopsis": tmdb_ep.synopsis, "runtime_min": tmdb_ep.runtime_min,
                    "still_path": tmdb_ep.still_url, "air_date": _parse_date(tmdb_ep.air_date),
                    "tmdb_episode_id": tmdb_ep.tmdb_episode_id,
                })
            self.session.flush()
        self._log(actor, "series.import_tmdb", "movie", movie.id, stats)
        return stats

    def _log(self, actor: Principal, action: str, entity: str, entity_id: int, payload: dict[str, Any]) -> None:
        self.audit.record(actor_id=actor.user_id, action=action, entity_type=entity, entity_id=entity_id, payload=payload)


def _fill(obj: Any, values: dict[str, Any]) -> int:
    filled = 0
    for key, value in values.items():
        if value is not None and getattr(obj, key) in (None, ""):
            setattr(obj, key, value)
            filled += 1
    return filled


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
