"""Panel Partner: enlaces directos (HTTPS/Debrid) y HLS para películas."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from rascal.data.db import session_scope
from rascal.domain.schemas import DirectSourceForm
from rascal.domain.services.catalog import CatalogService
from rascal.webui.common import error_text, partner


def build_router(ctx: Any) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post("/partner/movie/{movie_id}/add-direct")
    def add_direct(
        request: Request, movie_id: int, stream_url: str = Form(...), source_type: str = Form("auto"),
        label: str = Form("Enlace directo"), audio_language: str = Form(""), quality: str = Form(""),
        rights_statement: str = Form(...),
    ):
        with session_scope(ctx.factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = DirectSourceForm(stream_url=stream_url, source_type=source_type, label=label,  # type: ignore[arg-type]
                                        audio_language=audio_language or None, quality=quality or None,
                                        rights_statement=rights_statement)
                CatalogService(session, viewer, ctx.cfg).add_direct_source(movie_id, form)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(f"/partner/movie/{movie_id}", err=error_text(exc), anchor="#fuentes")
        text = "Enlace directo añadido. Se reproduce en el navegador sin P2P al pulsar «Reproducir»."
        return ctx.back(f"/partner/movie/{movie_id}", msg=text, anchor="#fuentes")

    return router
