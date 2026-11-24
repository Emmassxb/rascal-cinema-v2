"""Parseo de cabeceras ``Range`` (RFC 9110 §14) para respuestas 206 Partial Content."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int  # inclusivo

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, total: int) -> str:
        return f"bytes {self.start}-{self.end}/{total}"


class RangeNotSatisfiable(Exception):
    """El rango pedido cae fuera del recurso → 416 con ``Content-Range: bytes */total``."""


def parse_range(header: str | None, total: int) -> ByteRange | None:
    """Devuelve el rango a servir o ``None`` para responder el recurso completo (200).

    * Admite ``bytes=a-b``, ``bytes=a-`` y ``bytes=-n`` (sufijo).
    * Si llegan varios rangos se atiende sólo el primero (los navegadores nunca piden
      multi-rango para video y ``multipart/byteranges`` no aporta nada aquí).
    * Cabeceras sintácticamente inválidas se ignoran (RFC 9110 lo permite) → 200.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_text, end_text = (part.strip() for part in spec.split("-", 1))
    if total <= 0:
        raise RangeNotSatisfiable
    if start_text == "":
        if not end_text.isdigit():
            return None
        suffix = int(end_text)
        if suffix == 0:
            raise RangeNotSatisfiable
        return ByteRange(max(0, total - suffix), total - 1)
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        return None
    start = int(start_text)
    if start >= total:
        raise RangeNotSatisfiable
    end = int(end_text) if end_text else total - 1
    if end < start:
        return None
    return ByteRange(start, min(end, total - 1))
