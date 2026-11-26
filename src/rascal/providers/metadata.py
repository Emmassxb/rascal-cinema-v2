"""Proveedores de metadatos: TMDB (películas, series y personas) y captura manual."""

from __future__ import annotations

from collections.abc import Callable

from rascal.integrations.tmdb import TmdbClient, TmdbError, parse_tmdb_media_ref
from rascal.providers.base import MetadataResult, ProviderError

_KIND_ALIASES = {"movie": "movie", "series": "tv", "tv": "tv", "person": "person"}


class TmdbMetadataProvider:
    """Fichas desde TMDB en es-MX con respaldo en-US. Todo lo devuelto es una sugerencia editable."""

    name = "tmdb"
    kinds = ("movie", "series", "person")

    def __init__(self, api_key: str | None, *, client_factory: Callable[[str], TmdbClient] | None = None) -> None:
        self.api_key = api_key or ""
        self._factory = client_factory or (lambda key: TmdbClient(key))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def client(self) -> TmdbClient:
        try:
            return self._factory(self.api_key)
        except TmdbError as exc:
            raise ProviderError(str(exc)) from exc

    def lookup(self, kind: str, ref: str) -> MetadataResult:
        """``kind`` es una pista: si ``ref`` es una URL de /tv/ o /movie/, manda la URL."""
        try:
            tmdb_kind, tmdb_id = parse_tmdb_media_ref(ref, default_kind=_KIND_ALIASES.get(kind, "movie"))
            client = self.client()
            if tmdb_kind == "movie":
                movie = client.fetch_movie(tmdb_id)
                notes = ["Algunos textos se tomaron de la versión en inglés."] if movie.filled_from_fallback else []
                return MetadataResult("movie", str(tmdb_id), movie.to_form_fields(), notes)
            if tmdb_kind == "tv":
                series, seasons = client.fetch_series(tmdb_id)
                notes = [f"{len(seasons)} temporada(s) disponibles en TMDB; se importan al guardar."]
                if series.filled_from_fallback:
                    notes.append("Algunos textos se tomaron de la versión en inglés.")
                return MetadataResult("series", str(tmdb_id), series.to_form_fields(), notes, seasons)
            person = client.fetch_person(tmdb_id)
            notes = ["Biografía tomada de la versión en inglés (no hay traducción)."] if person.filled_from_fallback else []
            return MetadataResult("person", str(tmdb_id), person.to_form_fields(), notes)
        except TmdbError as exc:
            raise ProviderError(str(exc)) from exc


class ManualMetadataProvider:
    """Captura manual: no consulta nada; existe para que la UI liste el origen 'Manual'."""

    name = "manual"
    kinds = ("movie", "series", "person")

    def lookup(self, kind: str, ref: str) -> MetadataResult:
        return MetadataResult(kind, None, {}, ["Captura manual: rellena los campos del formulario."])
