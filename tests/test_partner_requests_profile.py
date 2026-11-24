"""Solicitudes Partner, diario, seguidores y favoritas (requiere BD SQLite temporal y fixtures de conftest)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from rascal.auth.rbac import PermissionDenied
from rascal.data.db import session_scope
from rascal.data.enums import PartnerRequestStatus, Role
from rascal.data.models import PartnerRequest, Review
from rascal.domain.schemas import PartnerRequestForm, ReviewForm
from rascal.domain.services.catalog import CatalogError, CatalogService
from rascal.domain.services.partner_requests import PartnerRequestService
from rascal.domain.services.profile import MAX_FAVORITES, ProfileService, effective_date, star_label
from rascal.domain.services.reviews import ReviewService
from rascal.auth.service import AuthService

from test_catalog import movie_form  # reutiliza el formulario de película de prueba

REASON = "Llevo cinco años escribiendo fichas y traducciones de cine para blogs independientes."


def _auth(session, settings, mailer):  # type: ignore[no-untyped-def]
    return AuthService(session, settings, mailer)


# ─────────────────────────────────────────────────────────────────────────────
# Solicitudes Partner
# ─────────────────────────────────────────────────────────────────────────────
def test_user_submits_once_and_cannot_duplicate(factory, settings, users, mailer) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = PartnerRequestService(s, users["beto"], _auth(s, settings, mailer))
        svc.submit(PartnerRequestForm(reason=REASON))
        with pytest.raises(CatalogError):
            svc.submit(PartnerRequestForm(reason=REASON))


def test_partner_or_admin_cannot_request(factory, settings, users, mailer) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        svc = PartnerRequestService(s, users["partner"], _auth(s, settings, mailer))
        with pytest.raises(CatalogError):
            svc.submit(PartnerRequestForm(reason=REASON))


def test_only_admin_approves_and_role_changes(factory, settings, users, mailer) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        PartnerRequestService(s, users["beto"], _auth(s, settings, mailer)).submit(PartnerRequestForm(reason=REASON))
        req_id = s.scalar(select(PartnerRequest.id))
    with session_scope(factory) as s:  # un usuario normal NO puede aprobar
        with pytest.raises(PermissionDenied):
            PartnerRequestService(s, users["ana"], _auth(s, settings, mailer)).approve(req_id)
    with session_scope(factory) as s:  # el admin sí
        PartnerRequestService(s, users["admin"], _auth(s, settings, mailer)).approve(req_id)
    with session_scope(factory) as s:
        req = s.get(PartnerRequest, req_id)
        assert req.status is PartnerRequestStatus.APPROVED and req.reviewed_by == users["admin"].user_id
        assert s.get(type(req.user), req.user_id).role is Role.PARTNER


def test_reject_allows_new_request(factory, settings, users, mailer) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        PartnerRequestService(s, users["beto"], _auth(s, settings, mailer)).submit(PartnerRequestForm(reason=REASON))
        req_id = s.scalar(select(PartnerRequest.id))
    with session_scope(factory) as s:
        PartnerRequestService(s, users["admin"], _auth(s, settings, mailer)).reject(req_id)
    with session_scope(factory) as s:  # tras rechazar se puede volver a pedir
        PartnerRequestService(s, users["beto"], _auth(s, settings, mailer)).submit(PartnerRequestForm(reason=REASON))
    with session_scope(factory) as s:
        assert len(list(s.scalars(select(PartnerRequest)))) == 2


def test_database_blocks_two_pending_requests(factory, users) -> None:  # type: ignore[no-untyped-def]
    """El índice único parcial protege ante peticiones simultáneas (la comprobación de servicio es la primera barrera)."""
    from sqlalchemy.exc import IntegrityError

    with session_scope(factory) as s:
        s.add(PartnerRequest(user_id=users["beto"].user_id, reason=REASON, status=PartnerRequestStatus.PENDING))
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(PartnerRequest(user_id=users["beto"].user_id, reason=REASON, status=PartnerRequestStatus.PENDING))


# ─────────────────────────────────────────────────────────────────────────────
# Diario y utilidades
# ─────────────────────────────────────────────────────────────────────────────
def test_star_label_and_effective_date() -> None:
    assert star_label(10) == "★★★★★"
    assert star_label(9) == "★★★★½"
    assert star_label(1) == "½"
    assert star_label(None) == ""
    created = datetime(2026, 3, 1, 22, 0)
    assert effective_date(None, created) == date(2026, 3, 1)
    assert effective_date(date(2025, 12, 24), created) == date(2025, 12, 24)


def test_diary_orders_by_watch_date_not_creation(factory, settings, users, movie_id) -> None:  # type: ignore[no-untyped-def]
    """Una reseña creada hoy pero de una película vista hace un año debe quedar en su fecha de visionado."""
    with session_scope(factory) as s:
        ReviewService(s, users["ana"]).add(movie_id, ReviewForm(rating=8, watched_on=date(2024, 1, 5)))
    with session_scope(factory) as s:
        other = CatalogService(s, users["partner"], settings).create_movie(movie_form(title="Otra Peli", release_year=2020, imdb_id=None, tmdb_id=None))
        ReviewService(s, users["ana"]).add(other, ReviewForm(rating=6, watched_on=date(2026, 6, 1)))
    with session_scope(factory) as s:
        entries = ProfileService(s, users["ana"]).diary(users["ana"].user_id)
    assert [e.movie_title for e in entries] == ["Otra Peli", "Big Buck Bunny"]
    assert entries[0].month_short == "jun" and entries[0].day == 1 and entries[0].star_label == "★★★"


def test_follow_lists_are_paginated_per_direction(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    from rascal.domain.services.social import SocialService

    with session_scope(factory) as s:
        SocialService(s, users["ana"]).follow("beto")
    with session_scope(factory) as s:
        seguidores = ProfileService(s, users["beto"]).follow_list("beto", "followers")
        seguidos = ProfileService(s, users["beto"]).follow_list("beto", "following")
        ana_view = ProfileService(s, users["ana"]).follow_list("beto", "followers")
    assert [p.user.username for p in seguidores] == ["ana"]
    assert seguidos == []
    assert ana_view[0].is_self is True and ana_view[0].is_following is False  # ana es la propia seguidora


# ─────────────────────────────────────────────────────────────────────────────
# Favoritas
# ─────────────────────────────────────────────────────────────────────────────
def test_favorites_toggle_and_limit(factory, settings, users) -> None:  # type: ignore[no-untyped-def]
    ids: list[int] = []
    with session_scope(factory) as s:
        for n in range(MAX_FAVORITES + 1):
            ids.append(CatalogService(s, users["partner"], settings).create_movie(movie_form(title=f"Film {n}", release_year=2000 + n, imdb_id=None, tmdb_id=None)))
    with session_scope(factory) as s:
        profile = ProfileService(s, users["ana"])
        assert profile.toggle_favorite(ids[0]) is True
        assert profile.toggle_favorite(ids[0]) is False
        for movie_id in ids[:MAX_FAVORITES]:
            profile.add_favorite(movie_id)
    with session_scope(factory) as s, pytest.raises(CatalogError):
        ProfileService(s, users["ana"]).add_favorite(ids[MAX_FAVORITES])


@pytest.fixture
def movie_id(factory, settings, users) -> int:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        return CatalogService(s, users["partner"], settings).create_movie(movie_form())
