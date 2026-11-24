"""Cliente y mapeo de **The Movie Database (TMDB)**, API v3.

Diseño
------
* ``parse_tmdb_ref`` acepta un ID numérico o una URL de ``themoviedb.org/movie/...``.
* ``build_movie`` convierte la respuesta JSON en ``TmdbMovie`` (dataclass plana). Es **pura**:
  no hace red, así que se prueba con diccionarios de ejemplo.
* ``TmdbClient`` hace la red con ``httpx`` (importado de forma perezosa). Idioma primario
  ``es-MX``; si faltan campos de texto, se rellenan con ``en-US``.
* La clave se lee de ``RASCAL_TMDB_API_KEY``. Acepta la *API Key* v3 (parámetro ``api_key``)
  o el *Read Access Token* v4 (cabecera ``Authorization: Bearer``), detectado por el prefijo JWT.

Los campos devueltos son **sugerencias**: el formulario del panel Partner los deja editables.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

TMDB_API_BASE = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"
BACKDROP_BASE = "https://image.tmdb.org/t/p/original"
PRIMARY_LANGUAGE = "es-MX"
FALLBACK_LANGUAGE = "en-US"
MAX_CAST = 20

_URL_RE = re.compile(r"themoviedb\.org/movie/(\d+)", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d{1,10}$")


class TmdbError(Exception):
    """Error presentable al usuario (clave inválida, película inexistente, red caída...)."""


def parse_tmdb_ref(text: str | None) -> int:
    """Extrae el ID de película de ``12345``, ``12345-slug`` o ``https://www.themoviedb.org/movie/12345-...``."""
    value = (text or "").strip()
    if not value:
        raise TmdbError("Escribe el ID o el enlace de la película en TMDB.")
    if _DIGITS_RE.match(value):
        number = int(value)
    else:
        match = _URL_RE.search(value)
        if match is None:
            raise TmdbError("Enlace no válido: usa una URL de themoviedb.org/movie/... o un ID numérico.")
        number = int(match.group(1))
    if number <= 0:
        raise TmdbError("El ID de TMDB debe ser positivo.")
    return number


@dataclass(frozen=True)
class TmdbMovie:
    tmdb_id: int
    title: str
    original_title: str | None = None
    tagline: str | None = None
    synopsis: str | None = None
    release_year: int | None = None
    runtime_min: int | None = None
    imdb_id: str | None = None
    original_language: str | None = None
    country: str | None = None
    genres: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    cast: tuple[tuple[str, str | None], ...] = ()  # (actor, personaje)
    poster_url: str | None = None
    backdrop_url: str | None = None
    language_used: str = PRIMARY_LANGUAGE
    filled_from_fallback: tuple[str, ...] = field(default_factory=tuple)

    def to_form_fields(self) -> dict[str, str]:
        """Valores de texto listos para los inputs del formulario (mismo formato que ``_parse_*``)."""
        cast_lines = "\n".join(f"{name} : {char}" if char else name for name, char in self.cast)
        return {
            "title": self.title,
            "original_title": self.original_title or "",
            "release_year": str(self.release_year or ""),
            "runtime_min": str(self.runtime_min or ""),
            "tagline": self.tagline or "",
            "synopsis": self.synopsis or "",
            "genres": ", ".join(self.genres),
            "companies": ", ".join(self.companies),
            "directors": ", ".join(self.directors),
            "cast": cast_lines,
            "imdb_id": self.imdb_id or "",
            "tmdb_id": str(self.tmdb_id),
            "poster_path": self.poster_url or "",
            "backdrop_path": self.backdrop_url or "",
            "original_language": self.original_language or "",
            "country": self.country or "",
        }


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _year(date_str: Any) -> int | None:
    text = _text(date_str)
    if text and len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        return year if 1870 <= year <= 2100 else None
    return None


def build_movie(
    primary: Mapping[str, Any],
    fallback: Mapping[str, Any] | None = None,
) -> TmdbMovie:
    """Mapea la respuesta de ``/movie/{id}?append_to_response=credits`` a ``TmdbMovie``.

    ``fallback`` es la misma respuesta en ``en-US``: sólo rellena campos de texto vacíos en
    ``primary``. Los campos que no dependen del idioma (imdb, año, duración, créditos) se toman
    siempre de ``primary`` (y de ``fallback`` si ``primary`` no los trae)."""
    fb = fallback or {}
    filled: list[str] = []

    def pick_text(key: str) -> str | None:
        value = _text(primary.get(key))
        if value is None and _text(fb.get(key)) is not None:
            filled.append(key)
            return _text(fb.get(key))
        return value

    tmdb_id = primary.get("id") or fb.get("id")
    if not isinstance(tmdb_id, int):
        raise TmdbError("La respuesta de TMDB no contiene un id válido.")
    title = pick_text("title") or _text(primary.get("original_title")) or _text(fb.get("original_title"))
    if not title:
        raise TmdbError("TMDB no devolvió título para esta película.")

    credits = primary.get("credits") or fb.get("credits") or {}
    crew = credits.get("crew") or []
    cast_raw = credits.get("cast") or []
    directors = tuple(
        dict.fromkeys(_text(c.get("name")) for c in crew if c.get("job") == "Director" and _text(c.get("name")))
    )
    cast: list[tuple[str, str | None]] = []
    for member in sorted(cast_raw, key=lambda c: c.get("order", 999))[:MAX_CAST]:
        name = _text(member.get("name"))
        if name:
            cast.append((name, _text(member.get("character"))))

    genres = tuple(g["name"] for g in primary.get("genres") or fb.get("genres") or [] if _text(g.get("name")))
    companies = tuple(c["name"] for c in primary.get("production_companies") or [] if _text(c.get("name")))
    countries = primary.get("production_countries") or []
    country = _text(countries[0].get("name")) if countries else None

    poster = _text(primary.get("poster_path")) or _text(fb.get("poster_path"))
    backdrop = _text(primary.get("backdrop_path")) or _text(fb.get("backdrop_path"))
    imdb = _text(primary.get("imdb_id")) or _text(fb.get("imdb_id"))

    return TmdbMovie(
        tmdb_id=tmdb_id,
        title=title,
        original_title=_text(primary.get("original_title")),
        tagline=pick_text("tagline"),
        synopsis=pick_text("overview"),
        release_year=_year(primary.get("release_date")) or _year(fb.get("release_date")),
        runtime_min=primary.get("runtime") or fb.get("runtime") or None,
        imdb_id=imdb if imdb and re.fullmatch(r"tt\d{7,10}", imdb) else None,
        original_language=_text(primary.get("original_language")),
        country=country,
        genres=genres,
        companies=companies,
        directors=directors,
        cast=tuple(cast),
        poster_url=f"{POSTER_BASE}{poster}" if poster else None,
        backdrop_url=f"{BACKDROP_BASE}{backdrop}" if backdrop else None,
        language_used=PRIMARY_LANGUAGE,
        filled_from_fallback=tuple(filled),
    )


Getter = Callable[[str, dict[str, Any]], dict[str, Any]]


class TmdbClient:
    """Cliente mínimo. ``get_json`` es inyectable (pruebas sin red)."""

    def __init__(self, api_key: str, *, get_json: Getter | None = None, timeout_s: float = 10.0) -> None:
        if not api_key:
            raise TmdbError("Falta RASCAL_TMDB_API_KEY: configúrala en .env para importar desde TMDB.")
        self._key = api_key
        self._get_json = get_json or self._http_get(timeout_s)

    def _http_get(self, timeout_s: float) -> Getter:
        import httpx  # importación perezosa: el módulo puro no la necesita

        client = httpx.Client(base_url=TMDB_API_BASE, timeout=timeout_s)
        is_bearer = self._key.startswith("eyJ")  # token de lectura v4 (JWT)

        def get(path: str, params: dict[str, Any]) -> dict[str, Any]:
            headers = {"Accept": "application/json"}
            query = dict(params)
            if is_bearer:
                headers["Authorization"] = f"Bearer {self._key}"
            else:
                query["api_key"] = self._key
            try:
                response = client.get(path, params=query, headers=headers)
            except httpx.HTTPError as exc:
                raise TmdbError("No se pudo contactar con TMDB. Revisa tu conexión e inténtalo de nuevo.") from exc
            if response.status_code == 401:
                raise TmdbError("La clave de TMDB no es válida (RASCAL_TMDB_API_KEY).")
            if response.status_code == 404:
                raise TmdbError("TMDB no tiene ninguna película con ese ID.")
            if response.status_code == 429:
                raise TmdbError("TMDB está limitando las peticiones. Espera un minuto y vuelve a intentarlo.")
            if response.status_code >= 400:
                raise TmdbError(f"TMDB respondió con error {response.status_code}.")
            return response.json()

        return get

    def fetch_movie(self, tmdb_id: int) -> TmdbMovie:
        primary = self._get_json(f"/movie/{tmdb_id}", {"language": PRIMARY_LANGUAGE, "append_to_response": "credits"})
        needs_fallback = not _text(primary.get("overview")) or not _text(primary.get("title"))
        fallback = None
        if needs_fallback:
            fallback = self._get_json(
                f"/movie/{tmdb_id}", {"language": FALLBACK_LANGUAGE, "append_to_response": "credits"}
            )
        return build_movie(primary, fallback)
