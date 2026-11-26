"""Panel Partner: plataformas oficiales ("Dónde ver") por título."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from rascal.auth.rbac import can_edit_movie
from rascal.data.db import session_scope
from rascal.data.models import Movie
from rascal.domain.schemas import WatchLinkForm
from rascal.domain.services.watch import WatchLinkService
from rascal.providers import ProviderError
from rascal.webui.common import error_text, partner


def build_router(ctx: Any) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    factory = ctx.factory

    def page(movie_id: int) -> str:
        return f"/partner/movie/{movie_id}"

    def _form(platform_name: str, url: str, offer_type: str, region: str, logo_path: str) -> WatchLinkForm:
        return WatchLinkForm(platform_name=platform_name, url=url, offer_type=offer_type,  # type: ignore[arg-type]
                             region=region or ctx.cfg.tmdb_region, logo_path=logo_path or None)

    @router.post("/partner/movie/{movie_id}/watch-links")
    def add_link(
        request: Request, movie_id: int, platform_name: str = Form(...), url: str = Form(...),
        offer_type: str = Form("subscription"), region: str = Form(""), logo_path: str = Form(""),
        platform_slug: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                WatchLinkService(session, viewer).add(
                    movie_id, _form(platform_name, url, offer_type, region, logo_path), platform_slug=platform_slug or None
                )
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(page(movie_id), err=error_text(exc), anchor="#donde-ver")
        return ctx.back(page(movie_id), msg=f"{platform_name.strip()} añadida a «Dónde ver»", anchor="#donde-ver")

    @router.post("/partner/watch-link/{link_id}/update")
    def update_link(
        request: Request, link_id: int, movie_id: int = Form(...), platform_name: str = Form(...),
        url: str = Form(...), offer_type: str = Form("subscription"), region: str = Form(""),
        logo_path: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                WatchLinkService(session, viewer).update(link_id, _form(platform_name, url, offer_type, region, logo_path))
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(page(movie_id), err=error_text(exc), anchor="#donde-ver")
        return ctx.back(page(movie_id), msg="Plataforma actualizada", anchor="#donde-ver")

    @router.post("/partner/watch-link/{link_id}/delete")
    def delete_link(request: Request, link_id: int, movie_id: int = Form(...)):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                WatchLinkService(session, viewer).delete(link_id)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(page(movie_id), err=error_text(exc), anchor="#donde-ver")
        return ctx.back(page(movie_id), msg="Plataforma eliminada", anchor="#donde-ver")

    @router.get("/partner/movie/{movie_id}/watch-suggest")
    def suggest(request: Request, movie_id: int, region: str = ""):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            movie = session.get(Movie, movie_id)
            if viewer is None or movie is None or not can_edit_movie(viewer, movie.owner_id):
                return JSONResponse({"ok": False, "error": "No puedes editar este título."}, status_code=403)
            media_type, tmdb_id = movie.media_type.value, movie.tmdb_id
        region = (region or ctx.cfg.tmdb_region).upper()[:2]
        try:
            offers = ctx.providers.watch["tmdb-watch"].suggest(media_type=media_type, tmdb_id=tmdb_id, region=region)
        except ProviderError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({
            "ok": True,
            "region": region,
            "offers": [
                {"platform_name": o.platform_name, "platform_slug": o.platform_slug, "offer_type": o.offer_type,
                 "url": o.url, "logo_url": o.logo_url}
                for o in offers
            ],
            "note": "TMDB no publica enlaces directos por plataforma: sustituye la URL por la del título en cada servicio.",
        })

    return router
