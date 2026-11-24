"""Detección de contenedor, sondeo de códecs (ffprobe) y remux/transcodificación al vuelo.

ffprobe/ffmpeg leen el video **a través del propio endpoint HTTP interno con Range**:
así ffmpeg pide el índice (``moov`` al final, ``Cues`` de MKV) y el servidor espera a que
esas piezas lleguen por P2P, en lugar de leer ceros de un archivo incompleto.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from rascal.config import Settings
from rascal.data.enums import PlaybackMode

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MediaProbe:
    format_name: str | None
    video_codec: str | None
    audio_codec: str | None
    pix_fmt: str | None
    width: int | None
    height: int | None
    duration_s: float | None


def sniff_container(head: bytes) -> str | None:
    """Identifica el contenedor por *magic bytes* (no confía en la extensión)."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mov" if head[8:10] == b"qt" else "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm" if b"webm" in head[:64] else "mkv"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    if len(head) >= 377 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return "mpegts"
    return None


def parse_ffprobe(payload: dict) -> MediaProbe:
    streams = payload.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format", {})
    duration = fmt.get("duration") or (video or {}).get("duration")
    return MediaProbe(
        format_name=fmt.get("format_name"),
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        pix_fmt=(video or {}).get("pix_fmt"),
        width=(video or {}).get("width"),
        height=(video or {}).get("height"),
        duration_s=float(duration) if duration not in (None, "N/A") else None,
    )


async def ffprobe(url: str, settings: Settings, *, timeout: float = 180.0) -> MediaProbe:
    cmd = [
        settings.ffprobe_path, "-v", "error", "-rw_timeout", str(int(settings.stream_piece_timeout_s * 1e6)),
        "-print_format", "json", "-show_format", "-show_streams", url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falló: {stderr.decode(errors='replace').strip()[-500:]}")
    return parse_ffprobe(json.loads(stdout or b"{}"))


def build_ffmpeg_command(settings: Settings, input_url: str, mode: PlaybackMode, *, start_s: float = 0.0) -> list[str]:
    """fMP4 fragmentado (``empty_moov``) apto para reproducción progresiva en <video>."""
    cmd = [
        settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rw_timeout", str(int(settings.stream_piece_timeout_s * 1e6)),
    ]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]  # antes de -i: seek por índice (usa Cues/moov)
    cmd += ["-i", input_url, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if mode is PlaybackMode.REMUX:
        cmd += ["-c:v", "copy", "-c:a", "copy"]
    elif mode is PlaybackMode.REMUX_AUDIO:
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    elif mode is PlaybackMode.TRANSCODE:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        ]
    else:
        raise ValueError(f"Modo sin ffmpeg: {mode}")
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd


async def ffmpeg_stream(cmd: list[str], *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Ejecuta ffmpeg y emite su stdout; mata el proceso si el cliente se desconecta."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    tail: deque[str] = deque(maxlen=20)

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            tail.append(line.decode(errors="replace").rstrip())

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
        await proc.wait()
        if proc.returncode not in (0, None):
            log.warning("ffmpeg terminó con código %s: %s", proc.returncode, " | ".join(tail))
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
