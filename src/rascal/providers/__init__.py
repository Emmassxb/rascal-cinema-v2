"""Proveedores internos estilo Stremio: metadatos, "Dónde ver", fuentes y subtítulos.

Ver ``rascal.providers.base`` para los contratos y ``build_registry`` para los incluidos.
"""

from rascal.providers.base import (
    MediaTarget,
    MetadataProvider,
    MetadataResult,
    PlaybackPlan,
    PreparedSource,
    ProviderError,
    StreamProvider,
    SubtitleProvider,
    WatchProvider,
    WatchSuggestion,
)
from rascal.providers.registry import ProviderRegistry, build_registry

__all__ = [
    "MediaTarget",
    "MetadataProvider",
    "MetadataResult",
    "PlaybackPlan",
    "PreparedSource",
    "ProviderError",
    "ProviderRegistry",
    "StreamProvider",
    "SubtitleProvider",
    "WatchProvider",
    "WatchSuggestion",
    "build_registry",
]
