"""Proveedores de fuentes de reproducción.

* ``P2PStreamProvider``: ``.torrent`` y magnet. Valida e inspecciona (bencode seguro, sin
  libtorrent) y planifica la reproducción a través del microservicio (``needs_engine``).
* ``DirectStreamProvider``: URL directa (MP4/WebM/MKV por HTTPS, enlaces Debrid) y HLS
  (``.m3u8``). Nunca toca libtorrent: el navegador carga la URL en Video.js. Para HLS el
  servidor remoto debe permitir CORS (Video.js descarga la lista y los segmentos con XHR).
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from rascal.config import Settings
from rascal.data.enums import PlaybackMode, SourceKind
from rascal.domain.playback import MIME_BY_CONTAINER, guess_mode_from_container
from rascal.providers.base import PlaybackPlan, PreparedSource, ProviderError
from rascal.security.urls import UnsafeUrl, validate_external_url
from rascal.torrents.ingest import InvalidTorrent, container_for, inspect_torrent
from rascal.torrents.magnet import InvalidMagnet, parse_magnet

HLS_MIME = "application/x-mpegURL"


def url_fingerprint(url: str) -> str:
    """sha256 hex (64) de la URL: clave de deduplicación por título."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


class P2PStreamProvider:
    name = "p2p"
    kinds = (SourceKind.MAGNET, SourceKind.TORRENT_FILE)

    def prepare_magnet(self, magnet_uri: str) -> PreparedSource:
        try:
            magnet = parse_magnet(magnet_uri)
        except InvalidMagnet as exc:
            raise ProviderError(str(exc)) from exc
        return PreparedSource(
            kind=SourceKind.MAGNET,
            values={"magnet_uri": magnet.canonical(), "info_hash": magnet.info_hash, "display_name": magnet.display_name},
            dedupe_key=magnet.info_hash,
        )

    def prepare_torrent(self, data: bytes, *, max_bytes: int) -> PreparedSource:
        try:
            meta = inspect_torrent(data, max_bytes=max_bytes)
        except InvalidTorrent as exc:
            raise ProviderError(str(exc)) from exc
        primary = meta.primary_video()
        if primary is None:
            raise ProviderError("El torrent no contiene archivos de video reproducibles (mp4, mkv, webm, …)")
        return PreparedSource(
            kind=SourceKind.TORRENT_FILE,
            values={
                "torrent_data": data,
                "info_hash": meta.info_hash,
                "display_name": meta.name,
                "total_size": meta.total_size,
                "file_index": primary.index,
                "file_path": primary.path,
                "file_size": primary.size,
                "container": primary.container,
                "playback_mode": guess_mode_from_container(primary.container),
                "_files": len(meta.files),
                "_private": meta.private,
            },
            dedupe_key=meta.info_hash,
        )

    def plan(self, source: Any) -> PlaybackPlan:
        return PlaybackPlan(
            source_id=source.id, source_type=source.kind.source_type, needs_engine=True,
            mode=source.playback_mode, exposes_server_ip=True,
        )


class DirectStreamProvider:
    name = "direct"
    kinds = (SourceKind.DIRECT_URL, SourceKind.HLS)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(self, url: str, *, declared: str | None = None) -> PreparedSource:
        schemes = ("https", "http") if self.settings.allow_insecure_stream_urls else ("https",)
        try:
            clean = validate_external_url(
                url, schemes=schemes, allow_private=self.settings.allow_private_urls, max_length=4000
            )
        except UnsafeUrl as exc:
            raise ProviderError(str(exc)) from exc
        path = unquote(urlsplit(clean).path)
        suffix = PurePosixPath(path).suffix.lower()
        is_hls = declared == "hls" or suffix in (".m3u8", ".m3u")
        if declared == "direct_url" and suffix in (".m3u8", ".m3u"):
            is_hls = True  # la extensión manda: una lista HLS no se reproduce como archivo
        name = PurePosixPath(path).name or urlsplit(clean).hostname or "Enlace directo"
        if is_hls:
            return PreparedSource(
                kind=SourceKind.HLS,
                values={"stream_url": clean, "url_hash": url_fingerprint(clean), "display_name": name[:500],
                        "container": "hls", "playback_mode": PlaybackMode.DIRECT},
                dedupe_key=url_fingerprint(clean),
            )
        container = container_for(path)
        if container in ("avi", "asf", "mpeg"):
            raise ProviderError("Ese formato no se reproduce en el navegador. Usa MP4, WebM, MKV (H.264/AAC) o HLS.")
        return PreparedSource(
            kind=SourceKind.DIRECT_URL,
            values={"stream_url": clean, "url_hash": url_fingerprint(clean), "display_name": name[:500],
                    "container": container, "playback_mode": PlaybackMode.DIRECT},
            dedupe_key=url_fingerprint(clean),
        )

    def plan(self, source: Any) -> PlaybackPlan:
        if source.kind is SourceKind.HLS:
            mime = HLS_MIME
        else:
            mime = MIME_BY_CONTAINER.get(source.container or "", "video/mp4")
            if mime == "video/x-matroska":
                mime = "video/webm"  # Chrome sólo intenta MKV si se anuncia como WebM
        return PlaybackPlan(
            source_id=source.id, source_type=source.kind.source_type, needs_engine=False,
            url=source.stream_url, mime=mime, mode=PlaybackMode.DIRECT,
        )
