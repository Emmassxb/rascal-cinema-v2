"""Motores de streaming.

``LibtorrentEngine`` vive dentro del microservicio (un único proceso dueño de la sesión
libtorrent), así que los *reruns* de Streamlit no pueden destruir el estado de descarga.

Estrategia de piezas para reproducir mientras se descarga:

1. ``sequential_download``: el resto del archivo llega en orden.
2. Sólo se descarga el archivo de video elegido (prioridad 0 al resto: subtítulos,
   muestras, NFO…).
3. Cabecera y cola del archivo con prioridad 7 **y** deadlines: el átomo ``moov`` de un MP4
   sin *faststart* o los ``Cues`` de un MKV están al final y el reproductor los pide en el
   primer segundo.
4. Durante la reproducción, el lector HTTP fija deadlines sobre la ventana que está
   leyendo, de modo que un *seek* reprioriza inmediatamente esa zona.
5. *Resume data* periódica y al apagar: reiniciar el servicio no vuelve a pedir metadata
   ni a verificar piezas ya descargadas.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rascal.config import Settings
from rascal.data.enums import SourceKind
from rascal.streaming.pieces import AlwaysAvailable, FileSpan, PieceSource
from rascal.torrents.ingest import TorrentFile, container_for, pick_primary_video

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Contratos
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Lo que el motor necesita de una ``MediaSource`` (sin ORM)."""

    source_id: int
    info_hash: str
    kind: SourceKind
    magnet_uri: str | None = None
    torrent_data: bytes | None = None
    file_index_hint: int | None = None
    file_path_hint: str | None = None


@dataclass(frozen=True, slots=True)
class StreamTarget:
    source_id: int
    path: Path
    rel_path: str
    file_index: int
    span: FileSpan
    pieces: PieceSource

    @property
    def container(self) -> str | None:
        return container_for(self.rel_path)


@dataclass(frozen=True, slots=True)
class EngineStatus:
    source_id: int
    info_hash: str
    state: str
    has_metadata: bool
    progress: float  # 0..1 sobre el archivo elegido (o el torrent si aún no hay metadata)
    download_rate: int  # bytes/s
    upload_rate: int
    num_peers: int
    num_seeds: int
    head_ready: bool
    tail_ready: bool
    file_path: str | None
    file_size: int | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class MetadataTimeout(TimeoutError):
    pass


class EngineListener(Protocol):
    """Eventos que el motor publica (la capa API los persiste en la BD)."""

    def on_metadata(self, source_ids: Sequence[int], *, file_index: int, file_path: str, file_size: int,
                    total_size: int, display_name: str) -> None: ...

    def on_complete(self, source_ids: Sequence[int]) -> None: ...

    def on_error(self, source_ids: Sequence[int], message: str) -> None: ...


class NullListener:
    def on_metadata(self, source_ids: Sequence[int], **_: Any) -> None:
        return None

    def on_complete(self, source_ids: Sequence[int]) -> None:
        return None

    def on_error(self, source_ids: Sequence[int], message: str) -> None:
        return None


class StreamingEngine(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def ensure(self, spec: SourceSpec) -> None: ...

    def has(self, source_id: int) -> bool: ...

    def remove(self, source_id: int, *, delete_files: bool = False) -> None: ...

    def status(self, source_id: int) -> EngineStatus | None: ...

    async def target(self, source_id: int, *, timeout: float) -> StreamTarget: ...

    def active_count(self) -> int: ...


# ─────────────────────────────────────────────────────────────────────────────
# Motor local (desarrollo / tests / contenido ya descargado)
# ─────────────────────────────────────────────────────────────────────────────
class LocalFileEngine:
    """Sirve archivos ya presentes en ``<local_media_dir>/<info_hash>/`` como si estuvieran
    completos. Permite desarrollar la UI y el reproductor sin red P2P."""

    name = "local"

    def __init__(self, settings: Settings, listener: EngineListener | None = None) -> None:
        self.root = settings.local_media_dir or (settings.data_dir / "media")
        self.listener = listener or NullListener()
        self._specs: dict[int, SourceSpec] = {}
        self._targets: dict[int, StreamTarget] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None

    def ensure(self, spec: SourceSpec) -> None:
        with self._lock:
            if spec.source_id in self._specs:
                return
            self._specs[spec.source_id] = spec
        target = self._resolve(spec)
        if target is not None:
            self.listener.on_metadata(
                [spec.source_id], file_index=target.file_index, file_path=target.rel_path,
                file_size=target.span.size, total_size=target.span.size, display_name=Path(target.rel_path).name,
            )
            self.listener.on_complete([spec.source_id])

    def _resolve(self, spec: SourceSpec) -> StreamTarget | None:
        base = self.root / spec.info_hash
        if not base.is_dir():
            return None
        files = [p for p in sorted(base.rglob("*")) if p.is_file()]
        entries = [TorrentFile(index=i, path=p.relative_to(base).as_posix(), size=p.stat().st_size) for i, p in enumerate(files)]
        chosen = next((e for e in entries if spec.file_path_hint and e.path.endswith(spec.file_path_hint.split("/")[-1])), None)
        chosen = chosen or pick_primary_video(entries)
        if chosen is None:
            return None
        target = StreamTarget(
            source_id=spec.source_id,
            path=base / chosen.path,
            rel_path=chosen.path,
            file_index=chosen.index,
            span=FileSpan(offset=0, size=chosen.size, piece_length=1024 * 1024),
            pieces=AlwaysAvailable(),
        )
        with self._lock:
            self._targets[spec.source_id] = target
        return target

    def has(self, source_id: int) -> bool:
        return source_id in self._specs

    def remove(self, source_id: int, *, delete_files: bool = False) -> None:
        with self._lock:
            self._specs.pop(source_id, None)
            self._targets.pop(source_id, None)

    def status(self, source_id: int) -> EngineStatus | None:
        spec = self._specs.get(source_id)
        if spec is None:
            return None
        target = self._targets.get(source_id)
        return EngineStatus(
            source_id=source_id, info_hash=spec.info_hash, state="seeding" if target else "missing_files",
            has_metadata=target is not None, progress=1.0 if target else 0.0, download_rate=0, upload_rate=0,
            num_peers=0, num_seeds=0, head_ready=target is not None, tail_ready=target is not None,
            file_path=target.rel_path if target else None, file_size=target.span.size if target else None,
            error=None if target else f"No hay archivos en {self.root / spec.info_hash}",
        )

    async def target(self, source_id: int, *, timeout: float) -> StreamTarget:
        target = self._targets.get(source_id)
        if target is None:
            raise MetadataTimeout(f"La fuente {source_id} no tiene archivo local")
        return target

    def active_count(self) -> int:
        return len(self._specs)


# ─────────────────────────────────────────────────────────────────────────────
# Motor libtorrent
# ─────────────────────────────────────────────────────────────────────────────
class LibtorrentPieces:
    """Adaptador ``PieceSource`` sobre un ``torrent_handle`` (métodos thread-safe)."""

    def __init__(self, handle: Any, *, step_ms: int = 150, readahead_base_ms: int = 2000) -> None:
        self.handle = handle
        self.step_ms = step_ms
        self.readahead_base_ms = readahead_base_ms

    def have_piece(self, piece: int) -> bool:
        return bool(self.handle.have_piece(piece))

    def prioritize(self, pieces: Sequence[int], *, urgent: bool) -> None:
        base = 0 if urgent else self.readahead_base_ms
        for i, piece in enumerate(pieces):
            self.handle.set_piece_deadline(piece, base + i * self.step_ms)


@dataclass
class _Managed:
    info_hash: str
    handle: Any
    source_ids: set[int]
    hint_index: int | None
    hint_path: str | None
    save_path: Path
    ready: threading.Event = field(default_factory=threading.Event)
    file_index: int | None = None
    rel_path: str | None = None
    span: FileSpan | None = None
    error: str | None = None


class LibtorrentEngine:
    name = "libtorrent"

    def __init__(self, settings: Settings, listener: EngineListener | None = None) -> None:
        import libtorrent as lt  # import diferido: la UI nunca carga libtorrent

        self.lt = lt
        self.settings = settings
        self.listener = listener or NullListener()
        self._session = lt.session(self._settings_pack())
        self._by_hash: dict[str, _Managed] = {}
        self._by_source: dict[int, _Managed] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_resume: set[str] = set()

    # ── Ciclo de vida ────────────────────────────────────────────────────────
    def _settings_pack(self) -> dict[str, Any]:
        lt, s = self.lt, self.settings
        port = s.lt_listen_port
        return {
            "listen_interfaces": s.lt_listen_interfaces or f"0.0.0.0:{port},[::]:{port}",
            "alert_mask": int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage),
            "alert_queue_size": 10_000,
            "enable_dht": s.lt_enable_dht,
            "enable_lsd": s.lt_enable_dht,
            "enable_upnp": s.lt_enable_port_mapping,
            "enable_natpmp": s.lt_enable_port_mapping,
            "download_rate_limit": s.lt_download_rate_limit,
            "upload_rate_limit": s.lt_upload_rate_limit,
            "user_agent": f"RascalCinema/2.0 libtorrent/{lt.__version__}",
        }

    def start(self) -> None:
        self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.settings.resume_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._alert_loop, name="libtorrent-alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Guarda resume data de todo y detiene la sesión (llamado desde el lifespan)."""
        with self._lock:
            managed = list(self._by_hash.values())
        for mt in managed:
            self._request_resume(mt)
        deadline = time.monotonic() + 10
        while self._pending_resume and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._session.pause()

    def listen_port(self) -> int:
        return int(self._session.listen_port())

    # ── Gestión de torrents ──────────────────────────────────────────────────
    def ensure(self, spec: SourceSpec) -> None:
        with self._lock:
            if spec.source_id in self._by_source:
                return
            existing = self._by_hash.get(spec.info_hash)
            if existing is not None:  # misma fuente vinculada a varias películas
                existing.source_ids.add(spec.source_id)
                self._by_source[spec.source_id] = existing
                return
            save_path = self.settings.downloads_dir / spec.info_hash
            save_path.mkdir(parents=True, exist_ok=True)
            params = self._build_params(spec, save_path)
            handle = self._session.add_torrent(params)
            mt = _Managed(
                info_hash=spec.info_hash, handle=handle, source_ids={spec.source_id},
                hint_index=spec.file_index_hint, hint_path=spec.file_path_hint, save_path=save_path,
            )
            self._by_hash[spec.info_hash] = mt
            self._by_source[spec.source_id] = mt
        if handle.status().has_metadata:
            self._configure(mt)

    def _build_params(self, spec: SourceSpec, save_path: Path) -> Any:
        lt = self.lt
        params = None
        resume_file = self.settings.resume_dir / f"{spec.info_hash}.fastresume"
        if resume_file.exists():
            try:
                params = lt.read_resume_data(resume_file.read_bytes())
            except Exception:  # resume corrupto: se descarta y se reconstruye
                log.warning("Resume data inválida para %s; se ignora", spec.info_hash)
                resume_file.unlink(missing_ok=True)
                params = None
        if params is None:
            if spec.kind is SourceKind.TORRENT_FILE:
                if not spec.torrent_data:
                    raise ValueError("Fuente .torrent sin datos")
                if hasattr(lt, "load_torrent_buffer"):  # libtorrent ≥ 2.0.8
                    params = lt.load_torrent_buffer(spec.torrent_data)
                else:
                    params = lt.add_torrent_params()
                    params.ti = lt.torrent_info(lt.bdecode(spec.torrent_data))
            else:
                if not spec.magnet_uri:
                    raise ValueError("Fuente magnet sin URI")
                params = lt.parse_magnet_uri(spec.magnet_uri)
        params.save_path = str(save_path)
        flags = int(params.flags)
        flags &= ~int(lt.torrent_flags.auto_managed)  # la cola automática no debe pausarla
        flags &= ~int(lt.torrent_flags.paused)
        flags |= int(lt.torrent_flags.sequential_download)
        params.flags = flags
        return params

    def _configure(self, mt: _Managed) -> None:
        """Con metadata disponible: elige archivo, prioriza piezas y notifica."""
        with self._lock:
            if mt.ready.is_set():
                return
            lt = self.lt
            ti = mt.handle.torrent_file()
            fs = ti.layout() if hasattr(ti, "layout") else ti.files()  # files() obsoleto en 2.1
            entries = [
                TorrentFile(index=i, path=fs.file_path(i), size=fs.file_size(i),
                            is_pad=bool(fs.file_flags(i) & fs.flag_pad_file))
                for i in range(fs.num_files())
            ]
            chosen = None
            if mt.hint_path:
                chosen = next((e for e in entries if e.path == mt.hint_path and not e.is_pad), None)
            if chosen is None and mt.hint_index is not None and 0 <= mt.hint_index < len(entries):
                candidate = entries[mt.hint_index]
                chosen = candidate if candidate.container and not candidate.is_pad else None
            chosen = chosen or pick_primary_video(entries)
            if chosen is None:
                mt.error = "El torrent no contiene archivos de video reproducibles"
                mt.ready.set()
                self.listener.on_error(sorted(mt.source_ids), mt.error)
                return

            mt.handle.prioritize_files([4 if e.index == chosen.index else 0 for e in entries])
            mt.handle.set_flags(lt.torrent_flags.sequential_download)
            span = FileSpan(offset=fs.file_offset(chosen.index), size=chosen.size, piece_length=ti.piece_length())

            head = list(span.head_pieces(self.settings.stream_head_bytes))
            tail = [p for p in span.tail_pieces(self.settings.stream_tail_bytes) if p not in head]
            for piece in (*head, *tail):
                mt.handle.piece_priority(piece, 7)
            # Deadlines intercalados: el primer bloque de cabecera, luego la cola (moov/Cues),
            # luego el resto de la cabecera. Así el reproductor obtiene índice y primeros frames.
            step = 120
            for i, piece in enumerate(head):
                mt.handle.set_piece_deadline(piece, (i if i < 2 else i + len(tail)) * step)
            for j, piece in enumerate(tail):
                mt.handle.set_piece_deadline(piece, (2 + j) * step)

            mt.file_index, mt.rel_path, mt.span = chosen.index, chosen.path, span
            mt.ready.set()
            source_ids = sorted(mt.source_ids)
        self.listener.on_metadata(
            source_ids, file_index=chosen.index, file_path=chosen.path, file_size=chosen.size,
            total_size=int(ti.total_size()), display_name=ti.name(),
        )
        self._request_resume(mt)  # persiste la metadata (los magnets no vuelven a pedirla)

    def has(self, source_id: int) -> bool:
        return source_id in self._by_source

    def remove(self, source_id: int, *, delete_files: bool = False) -> None:
        with self._lock:
            mt = self._by_source.pop(source_id, None)
            if mt is None:
                return
            mt.source_ids.discard(source_id)
            if mt.source_ids:
                return
            self._by_hash.pop(mt.info_hash, None)
        flags = self.lt.session.delete_files if delete_files else 0
        self._session.remove_torrent(mt.handle, flags)
        if delete_files:
            (self.settings.resume_dir / f"{mt.info_hash}.fastresume").unlink(missing_ok=True)

    def active_count(self) -> int:
        return len(self._by_hash)

    # ── Consultas ────────────────────────────────────────────────────────────
    def status(self, source_id: int) -> EngineStatus | None:
        mt = self._by_source.get(source_id)
        if mt is None:
            return None
        st = mt.handle.status()
        progress = float(st.progress)
        head_ready = tail_ready = False
        if mt.span is not None and mt.file_index is not None:
            done = mt.handle.file_progress(flags=self.lt.torrent_handle.piece_granularity)
            progress = done[mt.file_index] / mt.span.size if mt.span.size else 1.0
            head_ready = all(mt.handle.have_piece(p) for p in mt.span.head_pieces(self.settings.stream_head_bytes))
            tail_ready = all(mt.handle.have_piece(p) for p in mt.span.tail_pieces(self.settings.stream_tail_bytes))
        error = mt.error or (st.errc.message() if st.errc.value() else None)
        return EngineStatus(
            source_id=source_id, info_hash=mt.info_hash, state=str(st.state).rsplit(".", 1)[-1],
            has_metadata=bool(st.has_metadata), progress=round(min(1.0, progress), 4),
            download_rate=int(st.download_payload_rate), upload_rate=int(st.upload_payload_rate),
            num_peers=int(st.num_peers), num_seeds=int(st.num_seeds), head_ready=head_ready, tail_ready=tail_ready,
            file_path=mt.rel_path, file_size=mt.span.size if mt.span else None, error=error,
        )

    async def target(self, source_id: int, *, timeout: float) -> StreamTarget:
        mt = self._by_source.get(source_id)
        if mt is None:
            raise KeyError(source_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not mt.ready.is_set():
            if loop.time() >= deadline:
                raise MetadataTimeout("Sin metadata del torrent todavía (¿hay peers?)")
            await asyncio.sleep(0.2)
        if mt.error or mt.span is None or mt.rel_path is None or mt.file_index is None:
            raise RuntimeError(mt.error or "Torrent sin archivo de video")
        return StreamTarget(
            source_id=source_id, path=mt.save_path / mt.rel_path, rel_path=mt.rel_path,
            file_index=mt.file_index, span=mt.span, pieces=LibtorrentPieces(mt.handle),
        )

    def connect_peer(self, source_id: int, host: str, port: int) -> None:
        """Conecta manualmente un peer (útil en redes sin DHT/tracker y en pruebas)."""
        mt = self._by_source[source_id]
        mt.handle.connect_peer((host, port))

    # ── Alertas y resume data ────────────────────────────────────────────────
    def _key(self, handle: Any) -> str:
        hashes = handle.info_hashes()
        return str(hashes.v1) if hashes.has_v1() else str(hashes.v2)

    def _managed_for(self, handle: Any) -> _Managed | None:
        try:
            return self._by_hash.get(self._key(handle))
        except RuntimeError:  # handle inválido (torrent eliminado)
            return None

    def _request_resume(self, mt: _Managed) -> None:
        try:
            if mt.handle.is_valid():
                self._pending_resume.add(mt.info_hash)
                mt.handle.save_resume_data(self.lt.save_resume_flags_t.save_info_dict)
        except RuntimeError:
            self._pending_resume.discard(mt.info_hash)

    def _write_resume(self, alert: Any) -> None:
        mt = self._managed_for(alert.handle)
        key = mt.info_hash if mt else self._key(alert.handle)
        target = self.settings.resume_dir / f"{key}.fastresume"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(self.lt.write_resume_data_buf(alert.params))
        os.replace(tmp, target)  # escritura atómica
        self._pending_resume.discard(key)

    def _alert_loop(self) -> None:
        last_resume = time.monotonic()
        while not self._stop.is_set():
            self._session.wait_for_alert(500)
            for alert in self._session.pop_alerts():
                try:
                    self._dispatch(alert)
                except Exception:  # una alerta problemática no debe matar el hilo
                    log.exception("Error procesando alerta %s", type(alert).__name__)
            if time.monotonic() - last_resume >= self.settings.resume_save_interval_s:
                with self._lock:
                    managed = list(self._by_hash.values())
                for mt in managed:
                    if mt.handle.is_valid() and mt.handle.need_save_resume_data():
                        self._request_resume(mt)
                last_resume = time.monotonic()

    def _dispatch(self, alert: Any) -> None:
        lt = self.lt
        if isinstance(alert, lt.metadata_received_alert):
            mt = self._managed_for(alert.handle)
            if mt is not None:
                self._configure(mt)
        elif isinstance(alert, lt.save_resume_data_alert):
            self._write_resume(alert)
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            mt = self._managed_for(alert.handle)
            if mt is not None:
                self._pending_resume.discard(mt.info_hash)
        elif isinstance(alert, lt.torrent_finished_alert):
            mt = self._managed_for(alert.handle)
            if mt is not None and mt.ready.is_set():
                self.listener.on_complete(sorted(mt.source_ids))
                self._request_resume(mt)
        elif isinstance(alert, (lt.torrent_error_alert, lt.file_error_alert)):
            mt = self._managed_for(alert.handle)
            if mt is not None:
                mt.error = alert.message()
                self.listener.on_error(sorted(mt.source_ids), mt.error)


def build_engine(settings: Settings, listener: EngineListener | None = None) -> StreamingEngine:
    if settings.stream_engine == "local":
        return LocalFileEngine(settings, listener)
    with contextlib.suppress(ImportError):
        return LibtorrentEngine(settings, listener)
    raise RuntimeError("libtorrent no está instalado: `pip install libtorrent` o usa RASCAL_STREAM_ENGINE=local")
