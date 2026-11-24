"""Eliminación de la descarga local de una fuente P2P (liberar disco sin borrar el registro).

Qué hace ``purge_local_download``:
  1. Quita el torrent de la sesión activa (libtorrent ``remove_torrent`` con ``delete_files``).
  2. Borra ``<downloads_dir>/<info_hash>/`` tras comprobar que la ruta real queda DENTRO de
     ``downloads_dir`` (nunca se borra nada fuera de esa carpeta, aunque el hash se manipule).
  3. Borra el fastresume del torrent para que no se "resucite" la descarga al reiniciar.

Qué NO hace: no toca la fila de ``media_sources`` (el ``.torrent`` o el magnet se conservan para
volver a descargarlo). Quien llama debe marcar la fuente como ``pending`` con un mensaje.

Con el motor ``local`` (archivos de ``RASCAL_LOCAL_MEDIA_DIR``) no hay carpeta de descarga, así que
el borrado de disco es no-op: la biblioteca local del usuario jamás se elimina desde aquí.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rascal.config import Settings

log = logging.getLogger(__name__)


class PurgeError(RuntimeError):
    """La purga no es segura o no se puede completar."""


@dataclass(frozen=True)
class PurgeResult:
    source_id: int
    info_hash: str
    freed_bytes: int
    removed_dir: bool


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def download_dir_for(settings: Settings, info_hash: str) -> Path:
    """Ruta de la descarga de un torrent, validada para que quede dentro de ``downloads_dir``."""
    if not info_hash or not all(ch in "0123456789abcdef" for ch in info_hash.lower()) or len(info_hash) not in (40, 64):
        raise PurgeError("Infohash inválido")
    root = settings.downloads_dir.resolve()
    target = (settings.downloads_dir / info_hash).resolve()
    if target == root or root not in target.parents:
        raise PurgeError("Ruta de descarga fuera del almacenamiento permitido")
    return target


def purge_local_download(*, engine: Any, settings: Settings, source_id: int, info_hash: str) -> PurgeResult:
    """Detiene la descarga y borra sus archivos del disco. Idempotente."""
    if engine is not None and engine.has(source_id):
        engine.remove(source_id, delete_files=True)

    target = download_dir_for(settings, info_hash)
    freed = 0
    removed = False
    if target.is_dir():
        freed = _dir_size(target)
        # libtorrent borra en segundo plano: en Windows un archivo puede seguir bloqueado un instante.
        # ignore_errors evita una carrera; la comprobación final decide si la carpeta quedó eliminada.
        shutil.rmtree(target, ignore_errors=True)
        removed = not target.exists()
        if not removed:
            raise PurgeError("No se pudieron borrar todos los archivos (¿siguen en uso?). Reintenta en unos segundos.")
    resume = settings.resume_dir / f"{info_hash}.fastresume"
    resume.unlink(missing_ok=True)
    log.info("Descarga %s purgada: %d bytes liberados", info_hash, freed)
    return PurgeResult(source_id=source_id, info_hash=info_hash, freed_bytes=freed, removed_dir=removed)
