"""Proveedores de "Dónde ver" (plataformas oficiales)."""

from __future__ import annotations

from rascal.integrations.tmdb import TmdbError
from rascal.providers.base import ProviderError, WatchSuggestion
from rascal.providers.metadata import TmdbMetadataProvider


class TmdbWatchProvider:
    """Sugiere plataformas desde ``/movie|tv/{id}/watch/providers`` (datos de JustWatch).

    TMDB no publica enlaces directos por plataforma: la URL sugerida es la página de la
    región en TMDB. El partner la reemplaza por el enlace directo al título en la plataforma.
    """

    name = "tmdb-watch"

    def __init__(self, metadata: TmdbMetadataProvider) -> None:
        self.metadata = metadata

    def suggest(self, *, media_type: str, tmdb_id: int | None, region: str) -> list[WatchSuggestion]:
        if not tmdb_id:
            raise ProviderError("Este título no tiene ID de TMDB: impórtalo o escríbelo primero.")
        kind = "tv" if media_type == "series" else "movie"
        try:
            offers = self.metadata.client().fetch_watch_providers(kind, tmdb_id, region)
        except TmdbError as exc:
            raise ProviderError(str(exc)) from exc
        return [
            WatchSuggestion(o.platform_name, o.platform_slug, o.offer_type, o.url, o.logo_url, region.upper())
            for o in offers
        ]
