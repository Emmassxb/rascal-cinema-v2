"""Biografías editables de reparto y equipo (``/partner/person/{id}``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from rascal.data.db import session_scope
from rascal.domain.schemas import PersonForm
from rascal.domain.services.people import PersonService, can_edit_person
from rascal.integrations.tmdb import TmdbError
from rascal.providers import ProviderError
from rascal.webui.common import error_text, partner, to_date, to_int


def build_router(ctx: Any) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    factory = ctx.factory

    @router.get("/partner/person/{person_id}", response_class=HTMLResponse)
    def editor(request: Request, person_id: int, msg: str | None = None, err: str | None = None, back: str = ""):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/login?err=Inicia+sesi%C3%B3n+como+partner", status_code=303)
            try:
                person, form = PersonService(session, viewer).form(person_id)
            except Exception as exc:  # noqa: BLE001
                return ctx.back("/partner", err=error_text(exc))
            safe_back = back if back.startswith(("/partner/", "/movie/", "/person/")) and "//" not in back else ""
            return ctx.render(
                "partner_person.html", title=f"Editar bio: {person.name}", viewer=viewer, nav="partner",
                msg=msg, err=err, person=person, f=form, back=safe_back,
                tmdb_enabled=ctx.providers.tmdb.enabled,
            )

    @router.post("/partner/person/{person_id}")
    def save(
        request: Request, person_id: int, name: str = Form(...), bio: str = Form(""), birth_date: str = Form(""),
        death_date: str = Form(""), place_of_birth: str = Form(""), known_for_department: str = Form(""),
        photo_path: str = Form(""), tmdb_person_id: str = Form(""), back: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = PersonForm(
                    name=name, bio=bio, birth_date=to_date(birth_date), death_date=to_date(death_date),
                    place_of_birth=place_of_birth, known_for_department=known_for_department,
                    photo_path=photo_path, tmdb_person_id=to_int(tmdb_person_id),
                )
                person = PersonService(session, viewer).update(person_id, form)
                slug = person.slug
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                return ctx.back(f"/partner/person/{person_id}", err=error_text(exc))
        return ctx.back(f"/person/{slug}", msg="Biografía guardada")

    @router.get("/partner/person/{person_id}/tmdb")
    def tmdb_lookup(request: Request, person_id: int, ref: str = ""):
        """Con ``ref`` (ID o themoviedb.org/person/…) devuelve los campos; sin él, busca por nombre."""
        with session_scope(factory) as session:
            viewer = partner(ctx, request, session)
            if viewer is None or not can_edit_person(session, viewer, person_id):
                return JSONResponse({"ok": False, "error": "No puedes editar a esta persona."}, status_code=403)
            person, _ = PersonService(session, viewer).form(person_id)
            name = person.name
        try:
            if ref.strip():
                result = ctx.providers.tmdb.lookup("person", ref)
                if result.kind != "person":
                    raise ProviderError("Ese enlace no es de una persona (themoviedb.org/person/…).")
                return JSONResponse({"ok": True, "fields": result.fields, "notes": result.notes})
            hits = ctx.providers.tmdb.client().search_person(name)
            return JSONResponse({"ok": True, "candidates": hits})
        except (ProviderError, TmdbError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return router
