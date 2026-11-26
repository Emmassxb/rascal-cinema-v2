"""Panel Partner de series: temporadas, capítulos y fuentes/subtítulos por capítulo."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from rascal.data.db import session_scope
from rascal.data.models import Movie
from rascal.domain.schemas import (
    AUDIO_LANGUAGE_SUGGESTIONS,
    QUALITY_SUGGESTIONS,
    DirectSourceForm,
    EpisodeForm,
    MagnetSourceForm,
    SeasonForm,
    TorrentSourceForm,
)
from rascal.domain.services.catalog import CatalogService
from rascal.domain.services.series import SeriesService
from rascal.media.subtitles import SubtitleError
from rascal.providers import MediaTarget
from rascal.webui.common import error_text, partner, to_date, to_int


def build_router(ctx: Any) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    factory, cfg = ctx.factory, ctx.cfg

    def series_page(movie_id: int) -> str:
        return f"/partner/movie/{movie_id}"

    def episode_page(episode_id: int) -> str:
        return f"/partner/episode/{episode_id}"

    # ── Temporadas ───────────────────────────────────────────────────────────
    @router.post("/partner/series/{movie_id}/seasons")
    def add_season(
        request: Request, movie_id: int, season_number: str = Form(...), title: str = Form(""),
        overview: str = Form(""), poster_path: str = Form(""), air_year: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = SeasonForm(season_number=to_int(season_number), title=title, overview=overview,
                                  poster_path=poster_path, air_year=to_int(air_year))
                SeriesService(session, viewer).add_season(movie_id, form)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(series_page(movie_id), err=error_text(exc), anchor="#temporadas")
        return ctx.back(series_page(movie_id), msg="Temporada creada", anchor="#temporadas")

    @router.post("/partner/season/{season_id}/update")
    def update_season(
        request: Request, season_id: int, movie_id: int = Form(...), season_number: str = Form(...),
        title: str = Form(""), overview: str = Form(""), poster_path: str = Form(""), air_year: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = SeasonForm(season_number=to_int(season_number), title=title, overview=overview,
                                  poster_path=poster_path, air_year=to_int(air_year))
                SeriesService(session, viewer).update_season(season_id, form)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(series_page(movie_id), err=error_text(exc), anchor="#temporadas")
        return ctx.back(series_page(movie_id), msg="Temporada actualizada", anchor="#temporadas")

    @router.post("/partner/season/{season_id}/delete")
    def delete_season(request: Request, season_id: int, movie_id: int = Form(...)):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                SeriesService(session, viewer).delete_season(season_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(series_page(movie_id), err=error_text(exc), anchor="#temporadas")
        return ctx.back(series_page(movie_id), msg="Temporada eliminada con sus capítulos", anchor="#temporadas")

    @router.post("/partner/series/{movie_id}/import-tmdb")
    def import_tmdb(request: Request, movie_id: int):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            movie = session.get(Movie, movie_id)
            if movie is None or not movie.tmdb_id:
                return ctx.back(series_page(movie_id), err="Guarda primero el ID de TMDB de la serie.",
                                anchor="#temporadas")
            if not SeriesService(session, viewer).can_edit(movie_id):
                return ctx.back(series_page(movie_id), err="No puedes editar esta serie.", anchor="#temporadas")
            note = ctx.import_series_seasons(session, viewer, movie_id, movie.tmdb_id)
        failed = note.startswith("temporadas no importadas")
        return ctx.back(series_page(movie_id), **({"err": note} if failed else {"msg": note}), anchor="#temporadas")

    # ── Capítulos ────────────────────────────────────────────────────────────
    def _episode_form(number: str, title: str, synopsis: str, runtime_min: str, still_path: str, air_date: str) -> EpisodeForm:
        return EpisodeForm(episode_number=to_int(number), title=title, synopsis=synopsis,
                           runtime_min=to_int(runtime_min), still_path=still_path, air_date=to_date(air_date))

    @router.post("/partner/season/{season_id}/episodes")
    def add_episode(
        request: Request, season_id: int, movie_id: int = Form(...), episode_number: str = Form(...),
        title: str = Form(""), synopsis: str = Form(""), runtime_min: str = Form(""), still_path: str = Form(""),
        air_date: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _episode_form(episode_number, title, synopsis, runtime_min, still_path, air_date)
                episode_id = SeriesService(session, viewer).add_episode(season_id, form)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(series_page(movie_id), err=error_text(exc), anchor="#temporadas")
        return ctx.back(episode_page(episode_id), msg="Capítulo creado. Añade sus fuentes y subtítulos.")

    @router.get("/partner/episode/{episode_id}", response_class=HTMLResponse)
    def episode_editor(request: Request, episode_id: int, msg: str | None = None, err: str | None = None):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                episode, season, series = SeriesService(session, viewer).episode_for_edit(episode_id)
                sources = CatalogService(session, viewer, cfg).list_sources(series.id, episode_id=episode.id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back("/partner", err=error_text(exc))
            subtitles = ctx.providers.local_subtitles.list_tracks(MediaTarget(series.id, episode.id))
            return ctx.render(
                "partner_episode.html", title=f"{series.title} · T{season.season_number}E{episode.episode_number}",
                viewer=viewer, nav="partner", msg=msg, err=err, episode=episode, season=season, series=series,
                sources=sources, subtitles=subtitles, audio_suggestions=AUDIO_LANGUAGE_SUGGESTIONS,
                quality_suggestions=QUALITY_SUGGESTIONS,
            )

    @router.post("/partner/episode/{episode_id}")
    def update_episode(
        request: Request, episode_id: int, episode_number: str = Form(...), title: str = Form(""),
        synopsis: str = Form(""), runtime_min: str = Form(""), still_path: str = Form(""), air_date: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _episode_form(episode_number, title, synopsis, runtime_min, still_path, air_date)
                SeriesService(session, viewer).update_episode(episode_id, form)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(episode_page(episode_id), msg="Capítulo actualizado")

    @router.post("/partner/episode/{episode_id}/delete")
    def delete_episode(request: Request, episode_id: int):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                series_id = SeriesService(session, viewer).delete_episode(episode_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(series_page(series_id), msg="Capítulo eliminado", anchor="#temporadas")

    # ── Fuentes y subtítulos del capítulo ────────────────────────────────────
    def _series_of(session: Any, viewer: Any, episode_id: int) -> int:
        _, _, series = SeriesService(session, viewer).episode_for_edit(episode_id)
        return series.id

    @router.post("/partner/episode/{episode_id}/upload-torrent")
    async def episode_torrent(
        request: Request, episode_id: int, torrent_file: UploadFile = File(...), label: str = Form("Principal"),
        audio_language: str = Form(""), quality: str = Form(""), rights_statement: str = Form(...),
    ):
        raw = await torrent_file.read(cfg.max_torrent_file_bytes + 1)
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                series_id = _series_of(session, viewer, episode_id)
                form = TorrentSourceForm(label=label, audio_language=audio_language or None,
                                         quality=quality or None, rights_statement=rights_statement)
                CatalogService(session, viewer, cfg).add_torrent_source(series_id, form, raw, episode_id=episode_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(episode_page(episode_id), msg="Archivo .torrent añadido al capítulo")

    @router.post("/partner/episode/{episode_id}/add-magnet")
    def episode_magnet(
        request: Request, episode_id: int, magnet_uri: str = Form(...), label: str = Form("Principal"),
        audio_language: str = Form(""), quality: str = Form(""), rights_statement: str = Form(...),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                series_id = _series_of(session, viewer, episode_id)
                form = MagnetSourceForm(magnet_uri=magnet_uri, label=label, audio_language=audio_language or None,
                                        quality=quality or None, rights_statement=rights_statement)
                CatalogService(session, viewer, cfg).add_magnet_source(series_id, form, episode_id=episode_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(episode_page(episode_id), msg="Magnet añadido al capítulo")

    @router.post("/partner/episode/{episode_id}/add-direct")
    def episode_direct(
        request: Request, episode_id: int, stream_url: str = Form(...), source_type: str = Form("auto"),
        label: str = Form("Enlace directo"), audio_language: str = Form(""), quality: str = Form(""),
        rights_statement: str = Form(...),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                series_id = _series_of(session, viewer, episode_id)
                form = DirectSourceForm(stream_url=stream_url, source_type=source_type, label=label,  # type: ignore[arg-type]
                                        audio_language=audio_language or None, quality=quality or None,
                                        rights_statement=rights_statement)
                CatalogService(session, viewer, cfg).add_direct_source(series_id, form, episode_id=episode_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(episode_page(episode_id), msg="Enlace directo añadido al capítulo")

    @router.post("/partner/episode/{episode_id}/upload-subtitle")
    async def episode_subtitle(
        request: Request, episode_id: int, subtitle_file: UploadFile = File(...), subtitle_label: str = Form("Español"),
    ):
        raw = await subtitle_file.read()
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                series_id = _series_of(session, viewer, episode_id)  # valida permiso antes de escribir en disco
                ctx.providers.local_subtitles.save(MediaTarget(series_id, episode_id), raw, subtitle_label)
            except (SubtitleError, Exception) as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(episode_page(episode_id), err=error_text(exc))
        return ctx.back(episode_page(episode_id), msg="Subtítulos del capítulo guardados")

    return router
