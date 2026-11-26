"""Utilidades compartidas por los routers del panel."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import ValidationError

from rascal.auth.rbac import Principal
from rascal.data.enums import Role

PARTNER_ROLES = (Role.PARTNER, Role.ADMIN)


def partner(ctx: Any, request: Any, session: Any) -> Principal | None:
    viewer = ctx.viewer(request, session)
    return viewer if viewer and viewer.role in PARTNER_ROLES else None


def error_text(exc: Exception) -> str:
    """Mensaje legible: el primer error de Pydantic sin el prefijo técnico."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            field = ".".join(str(x) for x in first.get("loc", ()) if x != "__root__")
            msg = str(first.get("msg", "Dato inválido")).removeprefix("Value error, ")
            return f"{field}: {msg}" if field else msg
    return str(exc)


def to_int(value: str | None) -> int | None:
    try:
        return int(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def to_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None
