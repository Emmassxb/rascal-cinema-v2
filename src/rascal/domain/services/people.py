"""Biografías editables de reparto, dirección y equipo (``people``).

Una persona se comparte entre muchas películas, así que el permiso es más estricto que el de
una ficha: el administrador puede editar a cualquiera; un partner sólo a quien aparece en los
créditos de al menos un título suyo.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from rascal.auth.rbac import Permission, PermissionDenied, Principal
from rascal.data.models import Movie, MovieCredit, Person
from rascal.data.repositories.media import AuditRepository
from rascal.domain.schemas import PersonForm
from rascal.domain.services.catalog import CatalogError, NotFound

_FIELDS = ("name", "bio", "birth_date", "death_date", "place_of_birth", "known_for_department", "photo_path",
           "tmdb_person_id")


def can_edit_person(session: Session, principal: Principal | None, person_id: int) -> bool:
    if principal is None:
        return False
    if principal.can(Permission.MOVIE_EDIT_ANY):
        return True
    if not principal.can(Permission.MOVIE_EDIT_OWN):
        return False
    return bool(
        session.scalar(
            select(
                exists().where(
                    MovieCredit.person_id == person_id,
                    MovieCredit.movie_id == Movie.id,
                    Movie.owner_id == principal.user_id,
                )
            )
        )
    )


class PersonService:
    def __init__(self, session: Session, principal: Principal | None) -> None:
        self.session = session
        self.principal = principal
        self.audit = AuditRepository(session)

    def _editable(self, person_id: int) -> tuple[Person, Principal]:
        person = self.session.get(Person, person_id)
        if person is None:
            raise NotFound("Persona no encontrada")
        if not can_edit_person(self.session, self.principal, person.id):
            raise PermissionDenied("Sólo puedes editar a personas que aparecen en tus títulos")
        assert self.principal is not None
        return person, self.principal

    def form(self, person_id: int) -> tuple[Person, PersonForm]:
        person, _ = self._editable(person_id)
        return person, PersonForm.model_validate({f: getattr(person, f) for f in _FIELDS})

    def update(self, person_id: int, form: PersonForm) -> Person:
        person, actor = self._editable(person_id)
        if form.tmdb_person_id and self.session.scalar(
            select(Person.id).where(Person.tmdb_id == form.tmdb_person_id, Person.id != person.id)
        ):
            raise CatalogError("Otra persona ya tiene ese ID de TMDB")
        changed: dict[str, Any] = {}
        for field in _FIELDS:
            value = getattr(form, field)
            if getattr(person, field) != value:
                changed[field] = True
                setattr(person, field, value)
        self.session.flush()
        self.audit.record(actor_id=actor.user_id, action="person.update", entity_type="person",
                          entity_id=person.id, payload={"fields": sorted(changed)})
        return person
