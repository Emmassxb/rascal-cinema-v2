"""Inspección de ``.torrent`` y selección del archivo de video a reproducir.

Se ejecuta en el proceso de la UI/servicio de negocio (sin libtorrent): rechaza archivos
malformados, con rutas peligrosas o sin video reproducible antes de persistirlos.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import PurePosixPath

from rascal.torrents.bencode import BencodeError, BValue, bdecode

VIDEO_CONTAINERS: dict[str, str] = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mov",
    ".mkv": "mkv",
    ".webm": "webm",
    ".avi": "avi",
    ".ts": "mpegts",
    ".m2ts": "mpegts",
    ".mpg": "mpeg",
    ".mpeg": "mpeg",
    ".ogv": "ogg",
    ".wmv": "asf",
}
MIN_V2_PIECE = 16 * 1024
MAX_PIECE = 128 * 1024 * 1024


class InvalidTorrent(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentFile:
    index: int
    path: str  # relativa al save_path, como la reporta libtorrent (incluye la carpeta raíz)
    size: int
    is_pad: bool = False

    @property
    def container(self) -> str | None:
        return container_for(self.path)


@dataclass(frozen=True, slots=True)
class TorrentMeta:
    info_hash_v1: str | None
    info_hash_v2: str | None
    name: str
    piece_length: int
    total_size: int
    files: tuple[TorrentFile, ...]
    trackers: tuple[str, ...]
    private: bool

    @property
    def info_hash(self) -> str:
        value = self.info_hash_v1 or self.info_hash_v2
        assert value is not None
        return value

    def video_files(self) -> list[TorrentFile]:
        return [f for f in self.files if not f.is_pad and f.container is not None]

    def primary_video(self) -> TorrentFile | None:
        return pick_primary_video(self.files)


def container_for(path: str) -> str | None:
    return VIDEO_CONTAINERS.get(PurePosixPath(path).suffix.lower())


def pick_primary_video(files: tuple[TorrentFile, ...] | list[TorrentFile]) -> TorrentFile | None:
    """El video más grande, descartando *samples* si hay alternativas."""
    videos = [f for f in files if not f.is_pad and f.container is not None and f.size > 0]
    if not videos:
        return None
    non_samples = [f for f in videos if "sample" not in PurePosixPath(f.path).name.lower()]
    return max(non_samples or videos, key=lambda f: f.size)


def _text(info: dict[bytes, BValue], key: bytes) -> str | None:
    utf8 = info.get(key + b".utf-8")
    raw = utf8 if isinstance(utf8, bytes) else info.get(key)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return None


def _safe_component(component: BValue) -> str:
    if not isinstance(component, bytes) or not component:
        raise InvalidTorrent("Componente de ruta vacío o inválido")
    text = component.decode("utf-8", errors="replace")
    if text in (".", "..") or "/" in text or "\\" in text or "\x00" in text:
        raise InvalidTorrent(f"Ruta peligrosa en el torrent: {text!r}")
    return text


def _v1_files(info: dict[bytes, BValue], name: str) -> list[TorrentFile]:
    if b"files" not in info:
        length = info.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise InvalidTorrent("Torrent de un archivo sin 'length' válido")
        return [TorrentFile(index=0, path=name, size=length)]
    entries = info[b"files"]
    if not isinstance(entries, list) or not entries:
        raise InvalidTorrent("Lista 'files' vacía o inválida")
    files: list[TorrentFile] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InvalidTorrent("Entrada de archivo inválida")
        length = entry.get(b"length")
        path = entry.get(b"path.utf-8") or entry.get(b"path")
        if not isinstance(length, int) or length < 0 or not isinstance(path, list) or not path:
            raise InvalidTorrent("Entrada de archivo sin 'length'/'path' válidos")
        attr = entry.get(b"attr", b"")
        is_pad = isinstance(attr, bytes) and b"p" in attr
        parts = [_safe_component(c) for c in path]
        files.append(TorrentFile(index=index, path="/".join([name, *parts]), size=length, is_pad=is_pad))
    return files


def _v2_files(tree: BValue, name: str) -> list[TorrentFile]:
    files: list[TorrentFile] = []

    def walk(node: BValue, prefix: list[str], depth: int) -> None:
        if depth > 64 or not isinstance(node, dict):
            raise InvalidTorrent("'file tree' inválido")
        for key in sorted(node):
            child = node[key]
            if key == b"":
                if not isinstance(child, dict) or not isinstance(child.get(b"length"), int):
                    raise InvalidTorrent("Hoja de 'file tree' sin 'length'")
                files.append(TorrentFile(index=len(files), path="/".join(prefix), size=int(child[b"length"])))  # type: ignore[arg-type]
            else:
                walk(child, [*prefix, _safe_component(key)], depth + 1)

    walk(tree, [], 0)
    if len(files) == 1 and files[0].path == name:
        return files
    return [TorrentFile(index=f.index, path=f"{name}/{f.path}", size=f.size) for f in files]


def _trackers(root: dict[bytes, BValue]) -> tuple[str, ...]:
    found: list[str] = []
    announce = root.get(b"announce")
    if isinstance(announce, bytes):
        found.append(announce.decode("utf-8", errors="replace"))
    tiers = root.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, list):
                found.extend(t.decode("utf-8", errors="replace") for t in tier if isinstance(t, bytes))
    unique: list[str] = []
    for tracker in found:
        if tracker and tracker not in unique:
            unique.append(tracker)
    return tuple(unique[:50])


def inspect_torrent(data: bytes, *, max_bytes: int = 10 * 1024 * 1024) -> TorrentMeta:
    """Valida un ``.torrent`` y extrae su metadata.

    Raises:
        InvalidTorrent: con un mensaje apto para mostrar en el formulario del Partner.
    """
    if not data:
        raise InvalidTorrent("El archivo está vacío")
    if len(data) > max_bytes:
        raise InvalidTorrent(f"El .torrent supera el máximo de {max_bytes // (1024 * 1024)} MiB")
    try:
        decoded = bdecode(data)
    except BencodeError as exc:
        raise InvalidTorrent(f"No es un archivo .torrent válido: {exc}") from exc

    root = decoded.value
    if not isinstance(root, dict) or decoded.info_span is None:
        raise InvalidTorrent("Falta el diccionario 'info'")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise InvalidTorrent("'info' debe ser un diccionario")

    name = _text(info, b"name")
    if not name:
        raise InvalidTorrent("El torrent no tiene nombre")
    name = _safe_component(name.encode("utf-8"))

    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or not 0 < piece_length <= MAX_PIECE:
        raise InvalidTorrent("'piece length' inválido")

    info_bytes = data[decoded.info_span[0] : decoded.info_span[1]]
    is_v1 = b"pieces" in info
    is_v2 = info.get(b"meta version") == 2 and b"file tree" in info
    if not is_v1 and not is_v2:
        raise InvalidTorrent("Formato de torrent no reconocido (ni v1 ni v2)")
    if is_v2 and (piece_length < MIN_V2_PIECE or piece_length & (piece_length - 1)):
        raise InvalidTorrent("Torrent v2 con 'piece length' que no es potencia de 2 ≥ 16 KiB")

    files = _v1_files(info, name) if is_v1 else _v2_files(info[b"file tree"], name)
    total = sum(f.size for f in files)
    if total <= 0:
        raise InvalidTorrent("El torrent no contiene datos")

    if is_v1:
        pieces = info[b"pieces"]
        if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
            raise InvalidTorrent("Campo 'pieces' corrupto")
        expected = math.ceil(total / piece_length)
        if len(pieces) // 20 != expected:
            raise InvalidTorrent("El número de piezas no coincide con el tamaño total")

    return TorrentMeta(
        info_hash_v1=hashlib.sha1(info_bytes).hexdigest() if is_v1 else None,  # noqa: S324 (BEP 3)
        info_hash_v2=hashlib.sha256(info_bytes).hexdigest() if is_v2 else None,
        name=name,
        piece_length=piece_length,
        total_size=total,
        files=tuple(files),
        trackers=_trackers(root),
        private=info.get(b"private") == 1,
    )
