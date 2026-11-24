"""Generación de slugs estables para el enrutamiento por ``st.query_params``."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LENGTH = 100


def slugify(text: str, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """``"Amélie (2001)"`` → ``"amelie-2001"``. Nunca devuelve cadena vacía."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub("-", normalized.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "item"


def movie_slug_base(title: str, year: int | None) -> str:
    return slugify(f"{title} {year}" if year else title)


def next_free_slug(base: str, taken: Iterable[str] | Callable[[str], bool]) -> str:
    """Devuelve ``base`` o ``base-2``, ``base-3``… según disponibilidad.

    ``taken`` puede ser un conjunto de slugs ocupados o una función ``exists(slug)``.
    """
    exists: Callable[[str], bool]
    if callable(taken):
        exists = taken
    else:
        taken_set = set(taken)
        exists = taken_set.__contains__
    if not exists(base):
        return base
    suffix = 2
    while True:
        candidate = f"{base[: MAX_SLUG_LENGTH - len(str(suffix)) - 1]}-{suffix}"
        if not exists(candidate):
            return candidate
        suffix += 1
