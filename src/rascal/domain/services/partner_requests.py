"""Solicitudes para pasar de ``user`` a ``partner``.

Reglas
------
* Sólo un usuario con rol ``user`` puede solicitarlo, y sólo tiene UNA solicitud pendiente a la vez
  (además lo garantiza el índice único parcial de la base de datos ante peticiones simultáneas).
* Sólo un administrador (``USER_MANAGE``) aprueba o rechaza. La aprobación usa
  ``AuthService.set_role``, que ya valida permisos y deja registro en la auditoría.
* Tras rechazar, el usuario puede volver a solicitarlo más adelante.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rascal.auth.rbac import Permission, Principal, require
from rascal.auth.service import AuthService
from rascal.data.enums import PartnerRequestStatus, Role
from rascal.data.models import PartnerRequest, User, utcnow
from rascal.data.repositories.media import AuditRepository
from rascal.domain.schemas import PartnerRequestForm, PartnerRequestView, UserRef
from rascal.domain.services.catalog import CatalogError, NotFound


def _view(req: PartnerRequest, user: User) -> PartnerRequestView:
    return PartnerRequestView(
        id=req.id,
        user=UserRef(id=user.id, username=user.username, display_name=user.display_name, avatar_path=user.avatar_path),
        status=req.status,
        display_name=req.display_name,
        reason=req.reason,
        portfolio_url=req.portfolio_url,
        created_at=req.created_at,
        reviewed_at=req.reviewed_at,
    )


class PartnerRequestService:
    def __init__(self, session: Session, principal: Principal | None, auth: AuthService) -> None:
        self.session = session
        self.principal = principal
        self.auth = auth
        self.audit = AuditRepository(session)

    # ── Usuario ──────────────────────────────────────────────────────────────
    def submit(self, form: PartnerRequestForm) -> int:
        actor = require(self.principal, Permission.REVIEW_WRITE)  # cualquier usuario autenticado
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise NotFound("Usuario no encontrado")
        if user.role is not Role.USER:
            raise CatalogError("Tu cuenta ya tiene permisos de Partner o Administrador.")
        if self.pending_for(user.id) is not None:
            raise CatalogError("Ya tienes una solicitud pendiente de revisión.")
        req = PartnerRequest(
            user_id=user.id,
            status=PartnerRequestStatus.PENDING,
            display_name=form.display_name,
            reason=form.reason,
            portfolio_url=form.portfolio_url,
            created_at=utcnow(),
        )
        try:
            with self.session.begin_nested():
                self.session.add(req)
                self.session.flush()
        except IntegrityError as exc:  # carrera: otra petición creó la pendiente a la vez
            raise CatalogError("Ya tienes una solicitud pendiente de revisión.") from exc
        self.audit.record(actor_id=user.id, action="partner_request.submit", entity_type="partner_request",
                          entity_id=req.id, payload={"user": user.username})
        return req.id

    def latest_for(self, user_id: int) -> PartnerRequest | None:
        return self.session.scalar(
            select(PartnerRequest).where(PartnerRequest.user_id == user_id).order_by(PartnerRequest.id.desc()).limit(1)
        )

    def pending_for(self, user_id: int) -> PartnerRequest | None:
        return self.session.scalar(
            select(PartnerRequest).where(
                PartnerRequest.user_id == user_id, PartnerRequest.status == PartnerRequestStatus.PENDING
            )
        )

    # ── Administración ───────────────────────────────────────────────────────
    def list_pending(self, limit: int = 100) -> list[PartnerRequestView]:
        require(self.principal, Permission.USER_MANAGE)
        rows = self.session.execute(
            select(PartnerRequest, User)
            .join(User, User.id == PartnerRequest.user_id)
            .where(PartnerRequest.status == PartnerRequestStatus.PENDING)
            .order_by(PartnerRequest.created_at)
            .limit(limit)
        ).all()
        return [_view(req, user) for req, user in rows]

    def approve(self, request_id: int) -> None:
        actor = require(self.principal, Permission.USER_MANAGE)
        req = self._pending(request_id)
        self.auth.set_role(actor, req.user_id, Role.PARTNER)  # valida permisos y audita el cambio de rol
        self._close(req, PartnerRequestStatus.APPROVED, actor.user_id)

    def reject(self, request_id: int) -> None:
        actor = require(self.principal, Permission.USER_MANAGE)
        req = self._pending(request_id)
        self._close(req, PartnerRequestStatus.REJECTED, actor.user_id)

    # ── Internos ─────────────────────────────────────────────────────────────
    def _pending(self, request_id: int) -> PartnerRequest:
        req = self.session.get(PartnerRequest, request_id)
        if req is None:
            raise NotFound("Solicitud no encontrada")
        if req.status is not PartnerRequestStatus.PENDING:
            raise CatalogError("Esta solicitud ya fue revisada.")
        return req

    def _close(self, req: PartnerRequest, status: PartnerRequestStatus, reviewer_id: int) -> None:
        req.status = status
        req.reviewed_at = utcnow()  # UTC naive, igual que el resto del modelo
        req.reviewed_by = reviewer_id
        self.session.flush()
        self.audit.record(actor_id=reviewer_id, action=f"partner_request.{status.value}", entity_type="partner_request",
                          entity_id=req.id, payload={"user_id": req.user_id})

