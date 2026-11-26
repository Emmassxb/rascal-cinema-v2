"""Contratos de los proveedores internos (add-ons estilo Stremio).

Stremio separa *catalog/meta*, *subtitles* y *streams* en add-ons independientes que la app
consulta por tipo de recurso. Aquí se replica esa separación dentro del proceso:

==================  ==========================================  ==============================
Recurso             Contrato                                     Implementaciones incluidas
==================  ==========================================  ==============================
Metadatos           ``MetadataProvider``                         TMDB (película, serie, persona),
                                                                 captura manual
Dónde ver           ``WatchProvider``                            enlaces guardados, sugerencias TMDB
Fuentes             ``StreamProvider``                           torrent, magnet, URL directa, HLS
Subtítulos          ``SubtitleProvider``                         archivos locales .srt/.vtt → WebVTT
==================  ==========================================  ==============================

Las rutas web y el microservicio de streaming sólo hablan con el ``ProviderRegistry``; para
añadir un origen nuevo (otro catálogo, OpenSubtitles, un CDN propio) se implementa el
contrato y se registra, sin tocar ``web.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rascal.data.enums import PlaybackMode, SourceKind


class ProviderError(ValueError):
    """Error presentable al usuario (entrada inválida, servicio externo caído…)."""


@dataclass(frozen=True, slots=True)
class MediaTarget:
    """A qué se asocia un recurso: una película/serie y, opcionalmente, un capítulo."""

    movie_id: int
    episode_id: int | None = None

    @property
    def is_episode(self) -> bool:
        return self.episode_id is not None


# ── Metadatos ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MetadataResult:
    kind: str  # movie | series | person
    external_id: str | None
    fields: dict[str, str]  # valores de texto para el formulario (siempre editables)
    notes: list[str] = field(default_factory=list)
    seasons: list[int] = field(default_factory=list)  # series: números de temporada disponibles


@runtime_checkable
class MetadataProvider(Protocol):
    name: str
    kinds: tuple[str, ...]

    def lookup(self, kind: str, ref: str) -> MetadataResult: ...


# ── Dónde ver ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class WatchSuggestion:
    platform_name: str
    platform_slug: str
    offer_type: str
    url: str
    logo_url: str | None = None
    region: str = "MX"


@runtime_checkable
class WatchProvider(Protocol):
    name: str

    def suggest(self, *, media_type: str, tmdb_id: int | None, region: str) -> list[WatchSuggestion]: ...


# ── Fuentes ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PreparedSource:
    """Columnas de ``media_sources`` que produce un proveedor al validar una entrada."""

    kind: SourceKind
    values: dict[str, Any]
    dedupe_key: str  # info_hash o url_hash


@dataclass(frozen=True, slots=True)
class PlaybackPlan:
    """Cómo debe reproducirse una fuente en el navegador."""

    source_id: int
    source_type: str  # torrent | magnet | direct_url | hls
    needs_engine: bool  # True → arrancar libtorrent (StreamerClient.start) antes del reproductor
    url: str | None = None  # fuentes directas: la URL que carga Video.js
    mime: str | None = None
    mode: PlaybackMode = PlaybackMode.UNKNOWN
    exposes_server_ip: bool = False  # P2P: la IP del equipo que corre libtorrent participa en el enjambre


@runtime_checkable
class StreamProvider(Protocol):
    name: str
    kinds: tuple[SourceKind, ...]

    def plan(self, source: Any) -> PlaybackPlan: ...


# ── Subtítulos ───────────────────────────────────────────────────────────────
@runtime_checkable
class SubtitleProvider(Protocol):
    name: str

    def list_tracks(self, target: MediaTarget) -> list[dict[str, object]]: ...

    def save(self, target: MediaTarget, raw: bytes, label: str) -> str: ...
