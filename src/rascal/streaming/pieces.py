"""Mapeo bytes de archivo ↔ piezas del torrent y lectura "piece-aware" del disco.

Un archivo en descarga es *sparse*: los huecos de piezas aún no recibidas se leen como
ceros. Servir esos ceros corrompe la reproducción, así que cada trozo sólo se lee del disco
cuando ``have_piece`` confirma que todas las piezas que lo cubren pasaron la verificación
de hash. Mientras tanto se fijan *deadlines* sobre las piezas pedidas (y una ventana de
lectura anticipada) para que libtorrent las trate como críticas en tiempo.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class FileSpan:
    """Posición de un archivo dentro del espacio lineal de piezas del torrent."""

    offset: int  # byte inicial del archivo dentro del torrent
    size: int
    piece_length: int

    def piece_at(self, file_pos: int) -> int:
        return (self.offset + file_pos) // self.piece_length

    def pieces_for(self, start: int, end: int) -> range:
        """Piezas que cubren los bytes ``[start, end]`` (coordenadas del archivo)."""
        return range(self.piece_at(start), self.piece_at(end) + 1)

    def piece_end_in_file(self, piece: int) -> int:
        """Último byte (coordenadas del archivo) que pertenece a ``piece``."""
        return min(self.size - 1, (piece + 1) * self.piece_length - self.offset - 1)

    @property
    def first_piece(self) -> int:
        return self.piece_at(0)

    @property
    def last_piece(self) -> int:
        return self.piece_at(max(0, self.size - 1))

    def head_pieces(self, nbytes: int) -> range:
        return self.pieces_for(0, max(0, min(self.size, nbytes) - 1))

    def tail_pieces(self, nbytes: int) -> range:
        return self.pieces_for(max(0, self.size - nbytes), self.size - 1)

    def window(self, from_pos: int, nbytes: int) -> range:
        return self.pieces_for(from_pos, min(self.size - 1, from_pos + max(1, nbytes) - 1))


class PieceSource(Protocol):
    """Lo mínimo que el lector necesita del motor P2P."""

    def have_piece(self, piece: int) -> bool: ...

    def prioritize(self, pieces: Sequence[int], *, urgent: bool) -> None: ...


class PieceTimeout(TimeoutError):
    pass


async def wait_for_pieces(source: PieceSource, pieces: Iterable[int], *, timeout: float) -> None:
    """Espera (sin bloquear el event loop) a que todas las piezas estén verificadas."""
    pending = [p for p in pieces if not source.have_piece(p)]
    if not pending:
        return
    source.prioritize(pending, urgent=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.02
    while pending:
        if loop.time() >= deadline:
            raise PieceTimeout(f"Piezas no disponibles a tiempo: {pending[:8]}{'…' if len(pending) > 8 else ''}")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.25)
        pending = [p for p in pending if not source.have_piece(p)]


def _read_at(handle: BinaryIO, position: int, size: int) -> bytes:
    handle.seek(position)
    return handle.read(size)


async def stream_file_range(
    path: Path,
    span: FileSpan,
    source: PieceSource,
    start: int,
    end: int,
    *,
    chunk_size: int,
    readahead_bytes: int,
    timeout: float,
) -> AsyncIterator[bytes]:
    """Genera los bytes ``[start, end]`` del archivo a medida que sus piezas llegan."""
    handle: BinaryIO | None = None
    last_window_piece = -1
    try:
        position = start
        while position <= end:
            piece = span.piece_at(position)
            if piece != last_window_piece:  # re-prioriza la ventana sólo al cambiar de pieza
                window = [p for p in span.window(position, readahead_bytes) if not source.have_piece(p)]
                if window:
                    source.prioritize(window, urgent=False)
                last_window_piece = piece
            chunk_end = min(end, position + chunk_size - 1, span.piece_end_in_file(piece))
            await wait_for_pieces(source, span.pieces_for(position, chunk_end), timeout=timeout)
            if handle is None:
                handle = await asyncio.to_thread(open, path, "rb")
            data = await asyncio.to_thread(_read_at, handle, position, chunk_end - position + 1)
            if not data:
                raise OSError(f"Lectura vacía en {path} @ {position}")
            yield data
            position += len(data)
    finally:
        if handle is not None:
            handle.close()


class AlwaysAvailable:
    """``PieceSource`` para archivos ya completos en disco (motor local / dev)."""

    def have_piece(self, piece: int) -> bool:
        return True

    def prioritize(self, pieces: Sequence[int], *, urgent: bool) -> None:
        return None
