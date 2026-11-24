"""Bencode (BEP 3) en Python puro con límites defensivos.

Se usa para validar archivos ``.torrent`` subidos por partners **antes** de que lleguen al
motor libtorrent. Además de decodificar, registra el rango de bytes exacto del diccionario
``info`` de nivel superior: el info-hash es el SHA-1/SHA-256 de esos bytes *tal cual*,
no de una re-codificación (que podría diferir si el archivo no es canónico).
"""

from __future__ import annotations

from dataclasses import dataclass

BValue = int | bytes | list["BValue"] | dict[bytes, "BValue"]

MAX_DEPTH = 64
MAX_STRING = 64 * 1024 * 1024


class BencodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedTorrent:
    value: BValue
    info_span: tuple[int, int] | None  # [inicio, fin) del dict "info" en los bytes originales


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.info_span: tuple[int, int] | None = None

    def _peek(self) -> int:
        if self.pos >= len(self.data):
            raise BencodeError("Fin de datos inesperado")
        return self.data[self.pos]

    def decode(self, depth: int = 0) -> BValue:
        if depth > MAX_DEPTH:
            raise BencodeError("Anidamiento excesivo")
        token = self._peek()
        if token == ord("i"):
            return self._int()
        if token == ord("l"):
            self.pos += 1
            items: list[BValue] = []
            while self._peek() != ord("e"):
                items.append(self.decode(depth + 1))
            self.pos += 1
            return items
        if token == ord("d"):
            return self._dict(depth)
        if ord("0") <= token <= ord("9"):
            return self._bytes()
        raise BencodeError(f"Token inválido en posición {self.pos}")

    def _int(self) -> int:
        end = self.data.find(b"e", self.pos)
        if end == -1:
            raise BencodeError("Entero sin terminar")
        raw = self.data[self.pos + 1 : end]
        if not raw or raw == b"-" or raw == b"-0" or (raw.startswith(b"0") and raw != b"0") or raw.startswith(b"-0"):
            raise BencodeError("Entero no canónico")
        if len(raw) > 32:
            raise BencodeError("Entero demasiado grande")
        try:
            value = int(raw)
        except ValueError as exc:
            raise BencodeError("Entero inválido") from exc
        self.pos = end + 1
        return value

    def _bytes(self) -> bytes:
        colon = self.data.find(b":", self.pos)
        if colon == -1 or colon - self.pos > 10:
            raise BencodeError("Longitud de cadena inválida")
        raw_len = self.data[self.pos : colon]
        if not raw_len.isdigit() or (raw_len.startswith(b"0") and raw_len != b"0"):
            raise BencodeError("Longitud de cadena no canónica")
        length = int(raw_len)
        if length > MAX_STRING:
            raise BencodeError("Cadena demasiado larga")
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise BencodeError("Cadena truncada")
        self.pos = end
        return self.data[start:end]

    def _dict(self, depth: int) -> dict[bytes, BValue]:
        self.pos += 1
        result: dict[bytes, BValue] = {}
        while self._peek() != ord("e"):
            if not (ord("0") <= self._peek() <= ord("9")):
                raise BencodeError("Las claves de diccionario deben ser cadenas")
            key = self._bytes()
            value_start = self.pos
            value = self.decode(depth + 1)
            if depth == 0 and key == b"info":
                self.info_span = (value_start, self.pos)
            result[key] = value
        self.pos += 1
        return result


def bdecode(data: bytes) -> DecodedTorrent:
    """Decodifica exactamente un valor y rechaza basura al final."""
    decoder = _Decoder(data)
    value = decoder.decode()
    if decoder.pos != len(data):
        raise BencodeError("Datos sobrantes tras el valor bencode")
    return DecodedTorrent(value=value, info_span=decoder.info_span)


def bencode(value: BValue | str) -> bytes:
    """Codificador canónico (claves ordenadas). Útil para tests y herramientas."""
    if isinstance(value, bool):
        raise TypeError("bool no es representable en bencode")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = {(k.encode("utf-8") if isinstance(k, str) else k): v for k, v in value.items()}
        return b"d" + b"".join(bencode(k) + bencode(items[k]) for k in sorted(items)) + b"e"
    raise TypeError(f"Tipo no soportado: {type(value).__name__}")
