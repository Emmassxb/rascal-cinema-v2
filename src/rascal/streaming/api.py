"""Microservicio de streaming (FastAPI). Ejecutar con **un solo worker**::

    uvicorn rascal.streaming.api:create_app --factory --host 0.0.0.0 --port 8001 --workers 1

(o ``python scripts/run_all.py``). Un único proceso es dueño de la sesión libtorrent; las peticiones son asíncronas, así que un
worker atiende muchos streams concurrentes (la espera de piezas no bloquea el event loop).

Endpoints
---------
Control (cabecera ``X-Service-Key``, sólo backend de la UI):
    POST /api/sources/{id}/start · POST /api/sources/{id}/stop · POST /api/sources/{id}/probe
Estado (service key o token de reproducción):
    GET  /api/sources/{id}/status
Reproducción (token firmado ``?token=``, lo emite la UI para un usuario con permiso):
    GET  /player/{id}            página Video.js embebible (iframe)
    GET|HEAD /stream/{id}        bytes con Range → 206 Partial Content
    GET  /remux/{id}?start=s     fMP4 al vuelo (MKV/AVI/TS o audio incompatible)
Interno (token ``internal``, usado por ffprobe/ffmpeg en loopback):
    GET|HEAD /internal/raw/{id}
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib import resources
from string import Template
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from rascal.auth.tokens import TokenError, internal_token, playback_token, verify_token
from rascal.config import Settings, get_settings
from rascal.data.db import get_sessionmaker, session_scope
from rascal.data.enums import PlaybackMode, SourceStatus
from rascal.data.models import MediaSource, Movie
from rascal.data.repositories.media import SourceRepository
from rascal.domain.playback import MIME_BY_CONTAINER, decide_playback, guess_mode_from_container
from rascal.media.subtitles import list_subtitles
from rascal.streaming.downloads import PurgeError, purge_local_download
from rascal.streaming.engine import EngineListener, MetadataTimeout, SourceSpec, StreamingEngine, build_engine
from rascal.streaming.http_range import RangeNotSatisfiable, parse_range
from rascal.streaming.media import build_ffmpeg_command, ffmpeg_stream, ffprobe, sniff_container
from rascal.streaming.pieces import PieceTimeout, stream_file_range, wait_for_pieces

log = logging.getLogger(__name__)
FFMPEG_MODES = (PlaybackMode.REMUX, PlaybackMode.REMUX_AUDIO, PlaybackMode.TRANSCODE)


def _read_head(path: Any, size: int = 512) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia de eventos del motor
# ─────────────────────────────────────────────────────────────────────────────
class DbListener(EngineListener):
    """Refleja en ``media_sources`` lo que descubre el motor (archivo elegido, errores…)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def _update(self, source_ids: Sequence[int], **values: Any) -> None:
        try:
            with session_scope(self.factory) as session:
                for source_id in source_ids:
                    source = session.get(MediaSource, source_id)
                    if source is None:
                        continue
                    for key, value in values.items():
                        setattr(source, key, value)
        except Exception:  # nunca propagar al hilo de alertas
            log.exception("No se pudo persistir el estado de las fuentes %s", list(source_ids))

    def on_metadata(self, source_ids: Sequence[int], *, file_index: int, file_path: str, file_size: int,
                    total_size: int, display_name: str) -> None:
        from rascal.torrents.ingest import container_for

        container = container_for(file_path)
        with session_scope(self.factory) as session:
            for source_id in source_ids:
                source = session.get(MediaSource, source_id)
                if source is None:
                    continue
                source.file_index, source.file_path, source.file_size = file_index, file_path, file_size
                source.total_size, source.display_name = total_size, display_name
                source.container = container
                if source.playback_mode is PlaybackMode.UNKNOWN:
                    source.playback_mode = guess_mode_from_container(container)
                if source.status is not SourceStatus.COMPLETE:
                    source.status = SourceStatus.DOWNLOADING

    def on_complete(self, source_ids: Sequence[int]) -> None:
        self._update(source_ids, status=SourceStatus.COMPLETE, status_message=None)

    def on_error(self, source_ids: Sequence[int], message: str) -> None:
        self._update(source_ids, status=SourceStatus.ERROR, status_message=message[:500])


# ─────────────────────────────────────────────────────────────────────────────
# Fábrica de la aplicación
# ─────────────────────────────────────────────────────────────────────────────
def create_app(
    settings: Settings | None = None,
    *,
    engine: StreamingEngine | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    factory = session_factory or get_sessionmaker()
    secret = settings.secret_key.get_secret_value()
    probe_locks: dict[int, asyncio.Lock] = {}
    background: set[asyncio.Task[None]] = set()  # referencias fuertes a tareas en segundo plano
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    player_template = Template(
        resources.files("rascal.streaming").joinpath("templates/player.html").read_text(encoding="utf-8")
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        eng = engine or build_engine(settings, DbListener(factory))
        app.state.engine = eng
        eng.start()
        await asyncio.to_thread(_resume_active, eng)
        log.info("Motor de streaming '%s' iniciado", eng.name)
        try:
            yield
        finally:
            await asyncio.to_thread(eng.stop)

    app = FastAPI(title="Rascal Streamer", version="2.0.0", lifespan=lifespan)

    # ── Utilidades internas ──────────────────────────────────────────────────
    def _engine(request: Request) -> StreamingEngine:
        return request.app.state.engine

    def _spec_from(source: MediaSource) -> SourceSpec:
        return SourceSpec(
            source_id=source.id, info_hash=source.info_hash, kind=source.kind, magnet_uri=source.magnet_uri,
            torrent_data=source.torrent_data, file_index_hint=source.file_index, file_path_hint=source.file_path,
        )

    def _load_source(source_id: int) -> dict[str, Any]:
        with session_scope(factory) as session:
            source = SourceRepository(session).get(source_id)
            if source is None:
                raise HTTPException(404, "Fuente no encontrada")
            movie = session.get(Movie, source.movie_id)
            return {
                "spec": _spec_from(source),
                "movie_id": source.movie_id,
                "status": source.status,
                "mode": source.playback_mode,
                "container": source.container,
                "duration_s": source.duration_s,
                "title": movie.title if movie else source.display_name or "Video",
                "label": source.label,
            }

    def _resume_active(eng: StreamingEngine) -> None:
        with session_scope(factory) as session:
            specs = [_spec_from(s) for s in SourceRepository(session).active()]
        for spec in specs:
            try:
                eng.ensure(spec)
            except Exception:
                log.exception("No se pudo reanudar la fuente %s", spec.source_id)

    def _set_status(source_id: int, status: SourceStatus, message: str | None = None) -> None:
        with session_scope(factory) as session:
            source = session.get(MediaSource, source_id)
            if source is not None and source.status is not SourceStatus.COMPLETE:
                source.status, source.status_message = status, message

    async def _ensure_running(request: Request, source_id: int) -> dict[str, Any]:
        info = await asyncio.to_thread(_load_source, source_id)
        if info["status"] is SourceStatus.DISABLED:
            raise HTTPException(409, "La fuente está desactivada")
        eng = _engine(request)
        if not eng.has(source_id):
            await asyncio.to_thread(eng.ensure, info["spec"])
            await asyncio.to_thread(_set_status, source_id, SourceStatus.DOWNLOADING)
        return info

    # ── Autenticación ────────────────────────────────────────────────────────
    def require_service(x_service_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.service_key.get_secret_value()
        if not x_service_key or not hmac.compare_digest(x_service_key, expected):
            raise HTTPException(401, "Service key inválida")

    def _claims(scope: str, source_id: int, token: str | None) -> dict[str, Any]:
        if not token:
            raise HTTPException(401, "Token requerido")
        try:
            claims = verify_token(secret, scope, token)  # type: ignore[arg-type]
        except TokenError as exc:
            raise HTTPException(401, str(exc)) from exc
        if int(claims.get("sid", -1)) != source_id:
            raise HTTPException(403, "El token no corresponde a esta fuente")
        return claims

    def require_play(source_id: int, token: Annotated[str | None, Query()] = None) -> dict[str, Any]:
        return _claims("play", source_id, token)

    def require_internal(source_id: int, token: Annotated[str | None, Query()] = None) -> dict[str, Any]:
        return _claims("internal", source_id, token)

    def require_status_access(
        source_id: int,
        token: Annotated[str | None, Query()] = None,
        x_service_key: Annotated[str | None, Header()] = None,
    ) -> None:
        if x_service_key:
            require_service(x_service_key)
        else:
            _claims("play", source_id, token)

    # ── Servicio de bytes con Range ──────────────────────────────────────────
    async def _serve_bytes(request: Request, source_id: int) -> Response:
        info = await _ensure_running(request, source_id)
        try:
            target = await _engine(request).target(source_id, timeout=settings.stream_metadata_timeout_s)
        except MetadataTimeout as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc

        total = target.span.size
        try:
            byte_range = parse_range(request.headers.get("range"), total)
        except RangeNotSatisfiable:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"})

        start, end = (byte_range.start, byte_range.end) if byte_range else (0, total - 1)
        mime = MIME_BY_CONTAINER.get(target.container or info["container"] or "", "application/octet-stream")
        if mime == "video/x-matroska":
            mime = "video/webm"  # Chrome sólo intenta MKV si se anuncia como WebM
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": mime,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        status = 206 if byte_range else 200
        if byte_range:
            headers["Content-Range"] = byte_range.content_range(total)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)

        # Espera la primera pieza ANTES de enviar cabeceras: si no llega, 504 limpio en vez
        # de una respuesta 206 truncada.
        try:
            await wait_for_pieces(
                target.pieces, target.span.pieces_for(start, min(end, start + settings.stream_chunk_bytes - 1)),
                timeout=settings.stream_piece_timeout_s,
            )
        except PieceTimeout as exc:
            return JSONResponse({"detail": str(exc)}, status_code=504, headers={"Retry-After": "5"})

        body = stream_file_range(
            target.path, target.span, target.pieces, start, end,
            chunk_size=settings.stream_chunk_bytes, readahead_bytes=settings.stream_readahead_bytes,
            timeout=settings.stream_piece_timeout_s,
        )
        return StreamingResponse(body, status_code=status, headers=headers, media_type=mime)

    # ── Sondeo de códecs ─────────────────────────────────────────────────────
    async def _probe(request: Request, source_id: int) -> dict[str, Any]:
        lock = probe_locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            info = await _ensure_running(request, source_id)
            target = await _engine(request).target(source_id, timeout=settings.stream_metadata_timeout_s)
            span = target.span
            await wait_for_pieces(
                target.pieces,
                [*span.head_pieces(settings.stream_head_bytes), *span.tail_pieces(settings.stream_tail_bytes)],
                timeout=settings.stream_piece_timeout_s,
            )
            head = await asyncio.to_thread(_read_head, target.path)
            container = sniff_container(head) or target.container or info["container"]
            url = (
                f"{settings.streamer_internal_url}/internal/raw/{source_id}"
                f"?token={quote(internal_token(secret, source_id=source_id))}"
            )
            probe = await ffprobe(url, settings)
            decision = decide_playback(
                container, probe.video_codec, probe.audio_codec, probe.pix_fmt,
                allow_transcode=settings.allow_full_transcode,
            )

            def persist() -> None:
                with session_scope(factory) as session:
                    source = session.get(MediaSource, source_id)
                    if source is not None:
                        source.container = container
                        source.video_codec, source.audio_codec = probe.video_codec, probe.audio_codec
                        source.duration_s = probe.duration_s
                        source.playback_mode = decision.mode
                        if source.status is not SourceStatus.ERROR:
                            source.status_message = decision.reason

            await asyncio.to_thread(persist)
            return {
                "container": container,
                "video_codec": probe.video_codec,
                "audio_codec": probe.audio_codec,
                "pix_fmt": probe.pix_fmt,
                "resolution": f"{probe.width}x{probe.height}" if probe.width else None,
                "duration_s": probe.duration_s,
                "playback_mode": decision.mode.value,
                "reason": decision.reason,
            }

    async def _probe_in_background(request: Request, source_id: int) -> None:
        try:
            await _probe(request, source_id)
        except Exception as exc:
            log.warning("Probe de la fuente %s falló: %s", source_id, exc)

    # ── Rutas ────────────────────────────────────────────────────────────────
    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        eng = _engine(request)
        return {"status": "ok", "engine": eng.name, "active_torrents": eng.active_count()}

    @app.post("/api/sources/{source_id}/start", dependencies=[Depends(require_service)])
    async def start_source(request: Request, source_id: int) -> dict[str, Any]:
        await _ensure_running(request, source_id)
        status = _engine(request).status(source_id)
        return status.as_dict() if status else {"source_id": source_id, "state": "starting"}

    @app.post("/api/sources/{source_id}/stop", dependencies=[Depends(require_service)])
    async def stop_source(request: Request, source_id: int, delete_files: bool = False) -> dict[str, Any]:
        await asyncio.to_thread(_engine(request).remove, source_id, delete_files=delete_files)
        await asyncio.to_thread(_set_status, source_id, SourceStatus.PENDING)
        return {"source_id": source_id, "state": "stopped"}

    @app.post("/api/sources/{source_id}/purge", dependencies=[Depends(require_service)])
    async def purge_source(request: Request, source_id: int) -> dict[str, Any]:
        """Detiene la descarga y borra sus archivos del disco; conserva el .torrent/magnet."""
        def run() -> dict[str, Any]:
            with session_scope(factory) as session:
                source = session.get(MediaSource, source_id)
                if source is None:
                    raise HTTPException(404, "Fuente no encontrada")
                info_hash = source.info_hash
            try:
                result = purge_local_download(engine=_engine(request), settings=settings,
                                              source_id=source_id, info_hash=info_hash)
            except PurgeError as exc:
                raise HTTPException(409, str(exc)) from exc
            with session_scope(factory) as session:
                source = session.get(MediaSource, source_id)
                if source is not None:
                    source.status = SourceStatus.PENDING
                    source.file_size = None
                    source.status_message = "Descarga eliminada del dispositivo"
            return {"source_id": source_id, "freed_bytes": result.freed_bytes, "removed_dir": result.removed_dir}

        return await asyncio.to_thread(run)

    @app.post("/api/sources/{source_id}/probe", dependencies=[Depends(require_service)])
    async def probe_source(request: Request, source_id: int) -> dict[str, Any]:
        try:
            return await _probe(request, source_id)
        except (MetadataTimeout, PieceTimeout) as exc:
            raise HTTPException(504, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sources/{source_id}/status", dependencies=[Depends(require_status_access)])
    async def source_status(request: Request, source_id: int) -> dict[str, Any]:
        info = await asyncio.to_thread(_load_source, source_id)
        status = _engine(request).status(source_id)
        payload: dict[str, Any] = status.as_dict() if status else {"source_id": source_id, "state": "inactive"}
        payload["playback_mode"] = info["mode"].value
        payload["db_status"] = info["status"].value
        return payload

    @app.api_route("/stream/{source_id}", methods=["GET", "HEAD"], dependencies=[Depends(require_play)])
    async def stream(request: Request, source_id: int) -> Response:
        return await _serve_bytes(request, source_id)

    @app.api_route("/internal/raw/{source_id}", methods=["GET", "HEAD"], dependencies=[Depends(require_internal)])
    async def internal_raw(request: Request, source_id: int) -> Response:
        return await _serve_bytes(request, source_id)

    @app.get("/remux/{source_id}", dependencies=[Depends(require_play)])
    async def remux(request: Request, source_id: int, start: Annotated[float, Query(ge=0)] = 0.0) -> Response:
        info = await _ensure_running(request, source_id)
        mode: PlaybackMode = info["mode"]
        if mode in (PlaybackMode.UNKNOWN, PlaybackMode.DIRECT):
            mode = PlaybackMode.REMUX  # intento sin recodificar (seguro para H.264/AAC)
        if mode is PlaybackMode.TRANSCODE and not settings.allow_full_transcode:
            raise HTTPException(422, "Transcodificación completa deshabilitada en este servidor")
        if mode is PlaybackMode.UNSUPPORTED:
            raise HTTPException(422, "Formato no reproducible en navegador")
        try:
            await _engine(request).target(source_id, timeout=settings.stream_metadata_timeout_s)
        except MetadataTimeout as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})
        url = (
            f"{settings.streamer_internal_url}/internal/raw/{source_id}"
            f"?token={quote(internal_token(secret, source_id=source_id))}"
        )
        cmd = build_ffmpeg_command(settings, url, mode, start_s=start)
        return StreamingResponse(
            ffmpeg_stream(cmd), media_type="video/mp4",
            headers={"Cache-Control": "no-store", "X-Playback-Mode": mode.value},
        )

    # Nota: las dependencias locales van en ``dependencies=[...]``; con anotaciones diferidas
    # FastAPI no podría resolver ``Depends(require_play)`` dentro de la anotación del parámetro.
    @app.get("/player/{source_id}", response_class=HTMLResponse, dependencies=[Depends(require_play)])
    async def player(request: Request, source_id: int, token: Annotated[str, Query()]) -> HTMLResponse:
        info = await _ensure_running(request, source_id)
        mode: PlaybackMode = info["mode"]
        if mode is PlaybackMode.UNKNOWN:
            task = asyncio.create_task(_probe_in_background(request, source_id))
            background.add(task)
            task.add_done_callback(background.discard)
            mode = guess_mode_from_container(info["container"])
            if mode is PlaybackMode.UNKNOWN:
                mode = PlaybackMode.DIRECT
        mime = MIME_BY_CONTAINER.get(info["container"] or "", "video/mp4")
        if mime in ("video/x-matroska",):
            mime = "video/webm"
        quoted = quote(token)
        claims = verify_token(secret, "play", token)  # ya validado por la dependencia; aquí se leen los datos
        user_id = int(claims.get("sub", 0))
        with session_scope(factory) as session:
            siblings = SourceRepository(session).list_for_movie(info["movie_id"], include_disabled=False)
            alternatives = [
                {
                    "id": s.id,
                    "label": " · ".join(x for x in (s.label, s.quality, s.audio_language) if x),
                    "url": f"/player/{s.id}?token={quote(playback_token(secret, user_id=user_id, source_id=s.id, ttl_s=settings.playback_token_ttl_s))}",
                    "current": s.id == source_id,
                }
                for s in siblings
            ]
        subtitles = list_subtitles(settings.data_dir / "subtitles", info["movie_id"])
        config = {
            "sourceId": source_id,
            "mode": mode.value,
            "streamUrl": f"/stream/{source_id}?token={quoted}",
            "remuxUrl": f"/remux/{source_id}?token={quoted}",
            "statusUrl": f"/api/sources/{source_id}/status?token={quoted}",
            "mime": mime,
            "durationS": info["duration_s"],
            "alternatives": alternatives,
            "subtitles": subtitles,
        }
        page = player_template.safe_substitute(
            title=html.escape(f"{info['title']} · {info['label']}"),
            config_json=json.dumps(config).replace("</", "<\\/"),
        )
        csp = f"frame-ancestors 'self' {settings.ui_public_url}"
        return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})
    from rascal.web import mount_web
    mount_web(app, settings)
    return app
    

