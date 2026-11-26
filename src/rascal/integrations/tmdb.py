"""Cliente y mapeo de **The Movie Database (TMDB)**, API v3.

Diseño
------
* ``parse_tmdb_ref`` acepta un ID numérico o una URL de ``themoviedb.org/movie/...``.
  ``parse_tmdb_media_ref`` además distingue ``/tv/`` (series) y ``/person/``.
* ``build_series``, ``build_season``, ``build_person`` y ``build_watch_offers`` son puras como
  ``build_movie``; ``TmdbClient`` añade ``fetch_series``, ``fetch_season``, ``fetch_person``,
  ``search_person`` y ``fetch_watch_providers``.
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
VIDEO_LANGUAGES = "es,en,null"

_URL_RE = re.compile(r"themoviedb\.org/movie/(\d+)", re.IGNORECASE)
_MEDIA_URL_RE = re.compile(r"themoviedb\.org/(movie|tv|person)/(\d+)", re.IGNORECASE)
STILL_BASE = "https://image.tmdb.org/t/p/w300"
PROFILE_BASE = "https://image.tmdb.org/t/p/w500"
LOGO_BASE = "https://image.tmdb.org/t/p/w92"
_GENERIC_EPISODE_RE = re.compile(r"^(episodio|episode|capítulo)\s*\d+$", re.IGNORECASE)
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


def parse_tmdb_media_ref(text: str | None, default_kind: str = "movie") -> tuple[str, int]:
    """``(kind, id)`` con kind ∈ {movie, tv, person}. Un ID suelto usa ``default_kind``."""
    value = (text or "").strip()
    if not value:
        raise TmdbError("Escribe el ID o el enlace de TMDB.")
    if _DIGITS_RE.match(value):
        number, kind = int(value), default_kind
    else:
        match = _MEDIA_URL_RE.search(value)
        if match is None:
            raise TmdbError("Enlace no válido: usa themoviedb.org/movie/…, /tv/… o /person/…, o un ID numérico.")
        kind, number = match.group(1).lower(), int(match.group(2))
    if number <= 0:
        raise TmdbError("El ID de TMDB debe ser positivo.")
    if kind not in ("movie", "tv", "person"):
        raise TmdbError("Tipo de TMDB desconocido.")
    return kind, number


def pick_trailer(*video_payloads: Mapping[str, Any] | None) -> str | None:
    """Mejor tráiler de YouTube: oficial > tráiler > teaser, en el orden de idiomas recibido."""
    for payload in video_payloads:
        results = [v for v in (payload or {}).get("results") or [] if v.get("site") == "YouTube" and _text(v.get("key"))]
        ranked = sorted(
            results,
            key=lambda v: (v.get("type") != "Trailer", not v.get("official", False), v.get("type") != "Teaser"),
        )
        if ranked:
            return f"https://www.youtube.com/watch?v={ranked[0]['key']}"
    return None


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
    trailer_url: str | None = None
    media_type: str = "movie"

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
            "trailer_url": self.trailer_url or "",
            "media_type": self.media_type,
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
        trailer_url=pick_trailer(primary.get("videos"), fb.get("videos")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Series
# ─────────────────────────────────────────────────────────────────────────────
def build_series(primary: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> TmdbMovie:
    """``/tv/{id}?append_to_response=credits,videos`` → ``TmdbMovie`` con ``media_type='series'``.

    Se reutiliza la misma dataclass para que el formulario del panel sea uno solo: los
    creadores (``created_by``) ocupan el lugar de los directores y ``episode_run_time`` el de
    la duración."""
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
    title = pick_text("name") or _text(primary.get("original_name")) or _text(fb.get("original_name"))
    if not title:
        raise TmdbError("TMDB no devolvió título para esta serie.")
    credits = primary.get("credits") or fb.get("credits") or {}
    cast: list[tuple[str, str | None]] = []
    for member in sorted(credits.get("cast") or [], key=lambda c: c.get("order", 999))[:MAX_CAST]:
        name = _text(member.get("name"))
        if name:
            cast.append((name, _text(member.get("character"))))
    creators = tuple(dict.fromkeys(_text(c.get("name")) for c in primary.get("created_by") or [] if _text(c.get("name"))))
    runtimes = [r for r in primary.get("episode_run_time") or fb.get("episode_run_time") or [] if isinstance(r, int) and r > 0]
    countries = primary.get("production_countries") or []
    poster = _text(primary.get("poster_path")) or _text(fb.get("poster_path"))
    backdrop = _text(primary.get("backdrop_path")) or _text(fb.get("backdrop_path"))
    return TmdbMovie(
        tmdb_id=tmdb_id,
        title=title,
        original_title=_text(primary.get("original_name")),
        tagline=pick_text("tagline"),
        synopsis=pick_text("overview"),
        release_year=_year(primary.get("first_air_date")) or _year(fb.get("first_air_date")),
        runtime_min=runtimes[0] if runtimes else None,
        imdb_id=None,
        original_language=_text(primary.get("original_language")),
        country=_text(countries[0].get("name")) if countries else None,
        genres=tuple(g["name"] for g in primary.get("genres") or fb.get("genres") or [] if _text(g.get("name"))),
        companies=tuple(c["name"] for c in primary.get("production_companies") or [] if _text(c.get("name"))),
        directors=creators,
        cast=tuple(cast),
        poster_url=f"{POSTER_BASE}{poster}" if poster else None,
        backdrop_url=f"{BACKDROP_BASE}{backdrop}" if backdrop else None,
        filled_from_fallback=tuple(filled),
        trailer_url=pick_trailer(primary.get("videos"), fb.get("videos")),
        media_type="series",
    )


@dataclass(frozen=True)
class TmdbEpisode:
    episode_number: int
    title: str | None
    synopsis: str | None
    runtime_min: int | None
    still_url: str | None
    air_date: str | None  # ISO yyyy-mm-dd
    tmdb_episode_id: int | None


@dataclass(frozen=True)
class TmdbSeason:
    season_number: int
    title: str | None
    overview: str | None
    poster_url: str | None
    air_year: int | None
    tmdb_season_id: int | None
    episodes: tuple[TmdbEpisode, ...] = ()


def _season_numbers(series_payload: Mapping[str, Any], *, include_specials: bool) -> list[int]:
    numbers = [s.get("season_number") for s in series_payload.get("seasons") or []]
    return sorted({n for n in numbers if isinstance(n, int) and (n > 0 or include_specials)})


def build_season(primary: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> TmdbSeason:
    """``/tv/{id}/season/{n}`` → ``TmdbSeason``. TMDB rellena títulos sin traducir con
    "Episodio N": en ese caso se usa el título en inglés si existe."""
    fb = fallback or {}
    fb_eps = {e.get("episode_number"): e for e in fb.get("episodes") or []}
    number = primary.get("season_number")
    if not isinstance(number, int):
        raise TmdbError("La temporada de TMDB no trae número.")
    episodes: list[TmdbEpisode] = []
    for ep in primary.get("episodes") or []:
        n = ep.get("episode_number")
        if not isinstance(n, int):
            continue
        alt = fb_eps.get(n, {})
        title = _text(ep.get("name"))
        if (title is None or _GENERIC_EPISODE_RE.match(title)) and _text(alt.get("name")) \
                and not _GENERIC_EPISODE_RE.match(_text(alt.get("name")) or ""):
            title = _text(alt.get("name"))
        still = _text(ep.get("still_path")) or _text(alt.get("still_path"))
        runtime = ep.get("runtime") or alt.get("runtime")
        episodes.append(
            TmdbEpisode(
                episode_number=n,
                title=title,
                synopsis=_text(ep.get("overview")) or _text(alt.get("overview")),
                runtime_min=runtime if isinstance(runtime, int) and runtime > 0 else None,
                still_url=f"{STILL_BASE}{still}" if still else None,
                air_date=_text(ep.get("air_date")),
                tmdb_episode_id=ep.get("id") if isinstance(ep.get("id"), int) else None,
            )
        )
    poster = _text(primary.get("poster_path")) or _text(fb.get("poster_path"))
    return TmdbSeason(
        season_number=number,
        title=_text(primary.get("name")) or _text(fb.get("name")),
        overview=_text(primary.get("overview")) or _text(fb.get("overview")),
        poster_url=f"{POSTER_BASE}{poster}" if poster else None,
        air_year=_year(primary.get("air_date")) or _year(fb.get("air_date")),
        tmdb_season_id=primary.get("id") if isinstance(primary.get("id"), int) else None,
        episodes=tuple(sorted(episodes, key=lambda e: e.episode_number)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Personas
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TmdbPerson:
    tmdb_person_id: int
    name: str
    bio: str | None
    birth_date: str | None
    death_date: str | None
    place_of_birth: str | None
    known_for_department: str | None
    photo_url: str | None
    filled_from_fallback: bool = False

    def to_form_fields(self) -> dict[str, str]:
        return {
            "name": self.name,
            "bio": self.bio or "",
            "birth_date": self.birth_date or "",
            "death_date": self.death_date or "",
            "place_of_birth": self.place_of_birth or "",
            "known_for_department": self.known_for_department or "",
            "photo_path": self.photo_url or "",
            "tmdb_person_id": str(self.tmdb_person_id),
        }


def _iso_date(value: Any) -> str | None:
    text = _text(value)
    return text if text and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def build_person(primary: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> TmdbPerson:
    fb = fallback or {}
    pid = primary.get("id") or fb.get("id")
    if not isinstance(pid, int):
        raise TmdbError("La respuesta de TMDB no contiene un id de persona válido.")
    name = _text(primary.get("name")) or _text(fb.get("name"))
    if not name:
        raise TmdbError("TMDB no devolvió nombre para esta persona.")
    bio = _text(primary.get("biography"))
    used_fallback = False
    if bio is None and _text(fb.get("biography")):
        bio, used_fallback = _text(fb.get("biography")), True
    photo = _text(primary.get("profile_path")) or _text(fb.get("profile_path"))
    return TmdbPerson(
        tmdb_person_id=pid,
        name=name,
        bio=bio,
        birth_date=_iso_date(primary.get("birthday") or fb.get("birthday")),
        death_date=_iso_date(primary.get("deathday") or fb.get("deathday")),
        place_of_birth=_text(primary.get("place_of_birth")) or _text(fb.get("place_of_birth")),
        known_for_department=_text(primary.get("known_for_department")),
        photo_url=f"{PROFILE_BASE}{photo}" if photo else None,
        filled_from_fallback=used_fallback,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Plataformas ("Dónde ver")
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WatchOffer:
    platform_name: str
    platform_slug: str
    offer_type: str  # subscription | rent | buy | free
    logo_url: str | None
    url: str  # página de TMDB/JustWatch para la región: el partner la sustituye por el enlace directo


_OFFER_KEYS = (("flatrate", "subscription"), ("free", "free"), ("ads", "free"), ("rent", "rent"), ("buy", "buy"))


def build_watch_offers(payload: Mapping[str, Any], region: str) -> list[WatchOffer]:
    """``/movie|tv/{id}/watch/providers`` → ofertas de la región. TMDB no da enlaces directos
    por plataforma, sólo la página agregada de la región (``link``)."""
    from rascal.domain.slugs import slugify  # import local: este módulo no depende del dominio

    data = (payload.get("results") or {}).get(region.upper()) or {}
    link = _text(data.get("link")) or f"https://www.themoviedb.org/?region={region.upper()}"
    offers: list[WatchOffer] = []
    seen: set[tuple[str, str]] = set()
    for key, offer_type in _OFFER_KEYS:
        for item in sorted(data.get(key) or [], key=lambda p: p.get("display_priority", 999)):
            name = _text(item.get("provider_name"))
            if not name:
                continue
            slug = slugify(name)[:80] or "plataforma"
            if (slug, offer_type) in seen:
                continue
            seen.add((slug, offer_type))
            logo = _text(item.get("logo_path"))
            offers.append(WatchOffer(name[:80], slug, offer_type, f"{LOGO_BASE}{logo}" if logo else None, link))
    return offers


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
                raise TmdbError("TMDB no tiene nada con ese ID (revisa si es película, serie o persona).")
            if response.status_code == 429:
                raise TmdbError("TMDB está limitando las peticiones. Espera un minuto y vuelve a intentarlo.")
            if response.status_code >= 400:
                raise TmdbError(f"TMDB respondió con error {response.status_code}.")
            return response.json()

        return get

    def fetch_movie(self, tmdb_id: int) -> TmdbMovie:
        # include_video_language: tráileres en español, inglés o sin idioma en UNA sola petición.
        params = {"append_to_response": "credits,videos", "include_video_language": VIDEO_LANGUAGES}
        primary = self._get_json(f"/movie/{tmdb_id}", {"language": PRIMARY_LANGUAGE, **params})
        needs_fallback = not _text(primary.get("overview")) or not _text(primary.get("title"))
        fallback = None
        if needs_fallback:
            fallback = self._get_json(f"/movie/{tmdb_id}", {"language": FALLBACK_LANGUAGE, **params})
        return build_movie(primary, fallback)

    def fetch_series(self, tmdb_id: int) -> tuple[TmdbMovie, list[int]]:
        """Ficha de la serie + números de temporada disponibles (sin especiales)."""
        params = {"append_to_response": "credits,videos", "include_video_language": VIDEO_LANGUAGES}
        primary = self._get_json(f"/tv/{tmdb_id}", {"language": PRIMARY_LANGUAGE, **params})
        fallback = None
        if not _text(primary.get("overview")) or not _text(primary.get("name")):
            fallback = self._get_json(f"/tv/{tmdb_id}", {"language": FALLBACK_LANGUAGE, **params})
        return build_series(primary, fallback), _season_numbers(primary, include_specials=False)

    def fetch_season(self, tmdb_id: int, season_number: int) -> TmdbSeason:
        primary = self._get_json(f"/tv/{tmdb_id}/season/{season_number}", {"language": PRIMARY_LANGUAGE})
        incomplete = any(
            not _text(e.get("overview")) or _GENERIC_EPISODE_RE.match(_text(e.get("name")) or "Episodio 0")
            for e in primary.get("episodes") or []
        )
        fallback = (
            self._get_json(f"/tv/{tmdb_id}/season/{season_number}", {"language": FALLBACK_LANGUAGE})
            if incomplete else None
        )
        return build_season(primary, fallback)

    def fetch_person(self, person_id: int) -> TmdbPerson:
        primary = self._get_json(f"/person/{person_id}", {"language": PRIMARY_LANGUAGE})
        fallback = None if _text(primary.get("biography")) else self._get_json(
            f"/person/{person_id}", {"language": FALLBACK_LANGUAGE}
        )
        return build_person(primary, fallback)

    def search_person(self, name: str, *, limit: int = 5) -> list[dict[str, Any]]:
        data = self._get_json("/search/person", {"language": PRIMARY_LANGUAGE, "query": name[:100]})
        hits = []
        for item in (data.get("results") or [])[:limit]:
            if isinstance(item.get("id"), int) and _text(item.get("name")):
                known = [_text(k.get("title") or k.get("name")) for k in item.get("known_for") or []]
                photo = _text(item.get("profile_path"))
                hits.append({
                    "id": item["id"], "name": item["name"],
                    "department": _text(item.get("known_for_department")),
                    "known_for": [k for k in known if k][:3],
                    "photo_url": f"{LOGO_BASE.replace('w92', 'w185')}{photo}" if photo else None,
                })
        return hits

    def fetch_watch_providers(self, kind: str, tmdb_id: int, region: str) -> list[WatchOffer]:
        if kind not in ("movie", "tv"):
            raise TmdbError("Tipo inválido para plataformas.")
        return build_watch_offers(self._get_json(f"/{kind}/{tmdb_id}/watch/providers", {}), region)
