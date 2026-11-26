"""Enumeraciones persistidas como VARCHAR + CHECK (portables entre SQLite y PostgreSQL)."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Roles del sistema RBAC."""

    ADMIN = "admin"
    PARTNER = "partner"
    USER = "user"


class CreditRole(StrEnum):
    """Tipo de participación de una persona en una película."""

    CAST = "cast"
    DIRECTOR = "director"
    WRITER = "writer"
    PRODUCER = "producer"
    CINEMATOGRAPHER = "cinematographer"
    COMPOSER = "composer"
    EDITOR = "editor"


class ColorKind(StrEnum):
    COLOR = "color"
    BLACK_AND_WHITE = "bw"
    MIXED = "mixed"


class MediaType(StrEnum):
    """Tipo de título del catálogo: las series comparten tabla con las películas."""

    MOVIE = "movie"
    SERIES = "series"


class SourceKind(StrEnum):
    """Cómo se obtiene el video. Las dos primeras pasan por libtorrent; las dos últimas no."""

    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    DIRECT_URL = "direct_url"  # MP4/WebM/MKV por HTTPS (CDN propio, enlace Debrid…)
    HLS = "hls"  # lista .m3u8

    @property
    def is_p2p(self) -> bool:
        return self in (SourceKind.MAGNET, SourceKind.TORRENT_FILE)

    @property
    def source_type(self) -> str:
        """Nombre público estilo Stremio: torrent | magnet | direct_url | hls."""
        return "torrent" if self is SourceKind.TORRENT_FILE else self.value


P2P_KINDS = (SourceKind.MAGNET, SourceKind.TORRENT_FILE)
DIRECT_KINDS = (SourceKind.DIRECT_URL, SourceKind.HLS)


class OfferType(StrEnum):
    """Modalidad de una plataforma oficial en "Dónde ver"."""

    SUBSCRIPTION = "subscription"
    RENT = "rent"
    BUY = "buy"
    FREE = "free"


class SourceStatus(StrEnum):
    """Ciclo de vida de una fuente P2P."""

    PENDING = "pending"  # validada y guardada, nunca iniciada
    DOWNLOADING = "downloading"  # el motor la tiene activa
    COMPLETE = "complete"  # archivo de video descargado al 100 %
    ERROR = "error"
    DISABLED = "disabled"  # desactivada por un partner/admin


class PlaybackMode(StrEnum):
    """Estrategia de entrega al navegador según contenedor/códecs (ver streaming.media)."""

    UNKNOWN = "unknown"
    DIRECT = "direct"  # MP4 H.264/AAC o WebM VP9/Opus: Range requests directos
    REMUX = "remux"  # MKV H.264/AAC → fMP4 con -c copy (sin recodificar)
    REMUX_AUDIO = "remux_audio"  # video copiado, audio AC3/DTS/FLAC → AAC
    TRANSCODE = "transcode"  # HEVC/MPEG-4/10-bit → H.264 (costoso, opcional)
    UNSUPPORTED = "unsupported"


class PartnerRequestStatus(StrEnum):
    """Ciclo de vida de una solicitud para convertirse en Partner."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
