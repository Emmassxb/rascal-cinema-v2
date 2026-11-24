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


class SourceKind(StrEnum):
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"


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
