"""Reglas de compatibilidad navegador ↔ contenedor/códec (puras, sin ffmpeg).

Matriz práctica (Chrome, Firefox, Safari, Edge actuales):

=================  =====================  ==========================================
Contenedor         Códecs                 Entrega
=================  =====================  ==========================================
MP4 / MOV          H.264 8-bit + AAC/MP3  DIRECT (Range requests, seek nativo)
WebM               VP8/VP9/AV1 + Opus     DIRECT
MKV / AVI / TS     H.264 8-bit + AAC      REMUX  → fMP4 ``-c copy`` (CPU ≈ 0)
MKV / AVI / TS     H.264 + AC3/DTS/FLAC   REMUX_AUDIO → vídeo copiado, audio a AAC
cualquiera         HEVC, MPEG-4 ASP,      TRANSCODE → H.264 (CPU alto; desactivado por
                   VC-1, H.264 10-bit     defecto, ``RASCAL_ALLOW_FULL_TRANSCODE``)
=================  =====================  ==========================================

HEVC sólo decodifica en Safari/Edge con hardware compatible, por eso no se sirve directo.
"""

from __future__ import annotations

from dataclasses import dataclass

from rascal.data.enums import PlaybackMode

BROWSER_VIDEO_IN_MP4 = {"h264"}
BROWSER_AUDIO_IN_MP4 = {"aac", "mp3"}
BROWSER_VIDEO_IN_WEBM = {"vp8", "vp9", "av1"}
BROWSER_AUDIO_IN_WEBM = {"opus", "vorbis"}
EIGHT_BIT_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}

MIME_BY_CONTAINER = {
    "mp4": "video/mp4",
    "mov": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "mpegts": "video/mp2t",
    "mpeg": "video/mpeg",
    "ogg": "video/ogg",
    "asf": "video/x-ms-asf",
}


@dataclass(frozen=True, slots=True)
class PlaybackDecision:
    mode: PlaybackMode
    reason: str

    @property
    def needs_ffmpeg(self) -> bool:
        return self.mode in (PlaybackMode.REMUX, PlaybackMode.REMUX_AUDIO, PlaybackMode.TRANSCODE)


def guess_mode_from_container(container: str | None) -> PlaybackMode:
    """Estimación previa al *probe* (sólo con la extensión)."""
    if container in ("mp4", "mov", "webm"):
        return PlaybackMode.DIRECT
    if container in ("mkv", "avi", "mpegts", "mpeg", "asf", "ogg"):
        return PlaybackMode.REMUX
    return PlaybackMode.UNKNOWN


def decide_playback(
    container: str | None,
    video_codec: str | None,
    audio_codec: str | None,
    pix_fmt: str | None = None,
    *,
    allow_transcode: bool = False,
) -> PlaybackDecision:
    """Elige la estrategia más barata que el navegador pueda reproducir."""
    v = (video_codec or "").lower()
    a = (audio_codec or "").lower() or None
    eight_bit = pix_fmt is None or pix_fmt in EIGHT_BIT_PIX_FMTS

    if container == "webm" and v in BROWSER_VIDEO_IN_WEBM and (a is None or a in BROWSER_AUDIO_IN_WEBM):
        return PlaybackDecision(PlaybackMode.DIRECT, "WebM compatible")

    if v == "h264" and eight_bit:
        audio_ok = a is None or a in BROWSER_AUDIO_IN_MP4
        if container in ("mp4", "mov") and audio_ok:
            return PlaybackDecision(PlaybackMode.DIRECT, "MP4 H.264/AAC compatible")
        if audio_ok:
            return PlaybackDecision(PlaybackMode.REMUX, f"{container or '?'} con H.264/{a or 'sin audio'}: remux a fMP4")
        return PlaybackDecision(PlaybackMode.REMUX_AUDIO, f"Audio {a} no soportado por navegadores: se convierte a AAC")

    if allow_transcode and v:
        return PlaybackDecision(PlaybackMode.TRANSCODE, f"Vídeo {v} ({pix_fmt or '?'}) requiere transcodificar a H.264")
    return PlaybackDecision(
        PlaybackMode.UNSUPPORTED,
        f"Vídeo {v or 'desconocido'} ({pix_fmt or '?'}) no es reproducible sin transcodificar "
        "(activa RASCAL_ALLOW_FULL_TRANSCODE o sube una versión MP4 H.264/AAC)",
    )
