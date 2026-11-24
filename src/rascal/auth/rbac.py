"""RBAC: roles → permisos, más políticas de propiedad (un Partner sólo edita lo suyo).

Las comprobaciones viven en la capa de servicios, **no** en la UI: aunque alguien
manipule ``st.query_params`` para abrir el panel Partner, el servicio rechaza la operación.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rascal.data.enums import Role


class Permission(StrEnum):
    REVIEW_WRITE = "review:write"
    REVIEW_MODERATE = "review:moderate"
    STREAM_WATCH = "stream:watch"
    PARTNER_PANEL = "partner:panel"
    MOVIE_CREATE = "movie:create"
    MOVIE_EDIT_OWN = "movie:edit_own"
    MOVIE_EDIT_ANY = "movie:edit_any"
    MOVIE_DELETE = "movie:delete"
    SOURCE_MANAGE_OWN = "source:manage_own"
    SOURCE_MANAGE_ANY = "source:manage_any"
    ANALYTICS_VIEW = "analytics:view"
    USER_MANAGE = "user:manage"
    AUDIT_VIEW = "audit:view"


_USER: frozenset[Permission] = frozenset({Permission.REVIEW_WRITE, Permission.STREAM_WATCH})
_PARTNER: frozenset[Permission] = _USER | {
    Permission.PARTNER_PANEL,
    Permission.MOVIE_CREATE,
    Permission.MOVIE_EDIT_OWN,
    Permission.SOURCE_MANAGE_OWN,
}
_ADMIN: frozenset[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.USER: _USER,
    Role.PARTNER: frozenset(_PARTNER),
    Role.ADMIN: _ADMIN,
}


class AuthError(Exception):
    """Error de autenticación (credenciales, bloqueo, OTP)."""


class PermissionDenied(Exception):
    """El principal no tiene permiso para la operación."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Identidad autenticada que viaja por la capa de servicios (y en ``st.session_state``)."""

    user_id: int
    username: str
    role: Role

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def is_partner(self) -> bool:
        return self.role is Role.PARTNER


def require(principal: Principal | None, permission: Permission) -> Principal:
    if principal is None:
        raise PermissionDenied("Debes iniciar sesión")
    if not principal.can(permission):
        raise PermissionDenied(f"Permiso requerido: {permission.value}")
    return principal


def can_edit_movie(principal: Principal | None, owner_id: int | None) -> bool:
    if principal is None:
        return False
    if principal.can(Permission.MOVIE_EDIT_ANY):
        return True
    return principal.can(Permission.MOVIE_EDIT_OWN) and owner_id == principal.user_id


def require_movie_edit(principal: Principal | None, owner_id: int | None) -> Principal:
    if not can_edit_movie(principal, owner_id):
        raise PermissionDenied("No puedes editar esta película")
    assert principal is not None
    return principal


def can_manage_sources(principal: Principal | None, owner_id: int | None) -> bool:
    if principal is None:
        return False
    if principal.can(Permission.SOURCE_MANAGE_ANY):
        return True
    return principal.can(Permission.SOURCE_MANAGE_OWN) and owner_id == principal.user_id
