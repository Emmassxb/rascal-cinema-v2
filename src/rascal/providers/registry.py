"""Registro de proveedores: punto único de consulta para rutas y servicios."""

from __future__ import annotations

from typing import Any

from rascal.config import Settings
from rascal.data.enums import SourceKind
from rascal.providers.base import MetadataProvider, PlaybackPlan, ProviderError, StreamProvider, SubtitleProvider, WatchProvider
from rascal.providers.metadata import ManualMetadataProvider, TmdbMetadataProvider
from rascal.providers.streams import DirectStreamProvider, P2PStreamProvider
from rascal.providers.subtitles import LocalSubtitleProvider
from rascal.providers.watch import TmdbWatchProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self.metadata: dict[str, MetadataProvider] = {}
        self.watch: dict[str, WatchProvider] = {}
        self.streams: dict[SourceKind, StreamProvider] = {}
        self.subtitles: dict[str, SubtitleProvider] = {}

    # ── Registro ─────────────────────────────────────────────────────────────
    def register_metadata(self, provider: MetadataProvider) -> None:
        self.metadata[provider.name] = provider

    def register_watch(self, provider: WatchProvider) -> None:
        self.watch[provider.name] = provider

    def register_stream(self, provider: StreamProvider) -> None:
        for kind in provider.kinds:
            self.streams[kind] = provider

    def register_subtitles(self, provider: SubtitleProvider) -> None:
        self.subtitles[provider.name] = provider

    # ── Consulta ─────────────────────────────────────────────────────────────
    def stream_for(self, kind: SourceKind) -> StreamProvider:
        try:
            return self.streams[kind]
        except KeyError as exc:
            raise ProviderError(f"No hay proveedor para fuentes de tipo {kind.value}") from exc

    def plan(self, source: Any) -> PlaybackPlan:
        return self.stream_for(source.kind).plan(source)

    @property
    def p2p(self) -> P2PStreamProvider:
        provider = self.streams[SourceKind.MAGNET]
        assert isinstance(provider, P2PStreamProvider)
        return provider

    @property
    def direct(self) -> DirectStreamProvider:
        provider = self.streams[SourceKind.DIRECT_URL]
        assert isinstance(provider, DirectStreamProvider)
        return provider

    @property
    def tmdb(self) -> TmdbMetadataProvider:
        provider = self.metadata["tmdb"]
        assert isinstance(provider, TmdbMetadataProvider)
        return provider

    @property
    def local_subtitles(self) -> LocalSubtitleProvider:
        provider = self.subtitles["local"]
        assert isinstance(provider, LocalSubtitleProvider)
        return provider


def build_registry(settings: Settings) -> ProviderRegistry:
    """Registro con los proveedores incluidos. Se construye por proceso (es barato y sin estado)."""
    registry = ProviderRegistry()
    tmdb = TmdbMetadataProvider(settings.tmdb_api_key.get_secret_value() if settings.tmdb_api_key else None)
    registry.register_metadata(tmdb)
    registry.register_metadata(ManualMetadataProvider())
    registry.register_watch(TmdbWatchProvider(tmdb))
    registry.register_stream(P2PStreamProvider())
    registry.register_stream(DirectStreamProvider(settings))
    registry.register_subtitles(LocalSubtitleProvider(settings.subtitles_dir))
    return registry
