"""Validación y normalización de enlaces ``magnet:`` (v1 BEP 9, v2 BEP 52, híbridos)."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlparse

_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32 = re.compile(r"^[A-Za-z2-7]{32}$")
_BTMH = re.compile(r"^1220([0-9a-fA-F]{64})$")  # multihash: 0x12 = sha2-256, 0x20 = 32 bytes
_TRACKER_SCHEMES = {"udp", "http", "https", "ws", "wss"}
_SEED_SCHEMES = {"http", "https"}
MAX_MAGNET_LENGTH = 8192
MAX_TRACKERS = 50


class InvalidMagnet(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MagnetLink:
    info_hash_v1: str | None
    info_hash_v2: str | None
    display_name: str | None
    trackers: tuple[str, ...]
    web_seeds: tuple[str, ...]

    @property
    def info_hash(self) -> str:
        """Identificador principal: v1 si existe (compatibilidad), si no v2."""
        value = self.info_hash_v1 or self.info_hash_v2
        assert value is not None
        return value

    def canonical(self) -> str:
        parts: list[str] = []
        if self.info_hash_v1:
            parts.append(f"xt=urn:btih:{self.info_hash_v1}")
        if self.info_hash_v2:
            parts.append(f"xt=urn:btmh:1220{self.info_hash_v2}")
        if self.display_name:
            parts.append(f"dn={quote(self.display_name, safe='')}")
        parts.extend(f"tr={quote(tracker, safe='')}" for tracker in self.trackers)
        parts.extend(f"ws={quote(seed, safe='')}" for seed in self.web_seeds)
        return "magnet:?" + "&".join(parts)


def _valid_url(value: str, schemes: set[str]) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in schemes and bool(parsed.netloc) and len(value) <= 1024


def parse_magnet(uri: str) -> MagnetLink:
    """Valida un magnet y devuelve su forma normalizada.

    Raises:
        InvalidMagnet: si no es un magnet BitTorrent válido.
    """
    uri = uri.strip()
    if len(uri) > MAX_MAGNET_LENGTH:
        raise InvalidMagnet("El enlace magnet es demasiado largo")
    if not uri.lower().startswith("magnet:?"):
        raise InvalidMagnet("El enlace debe comenzar con 'magnet:?'")

    try:
        params = parse_qsl(uri[len("magnet:?") :], keep_blank_values=False, max_num_fields=300)
    except ValueError as exc:
        raise InvalidMagnet("Parámetros del magnet inválidos") from exc

    v1: str | None = None
    v2: str | None = None
    name: str | None = None
    trackers: list[str] = []
    seeds: list[str] = []

    for raw_key, value in params:
        key = raw_key.split(".", 1)[0].lower()  # admite xt.1, tr.2...
        value = value.strip()
        if key == "xt":
            lowered = value.lower()
            if lowered.startswith("urn:btih:"):
                digest = value[len("urn:btih:") :]
                if _HEX40.match(digest):
                    candidate = digest.lower()
                elif _BASE32.match(digest):
                    try:
                        candidate = base64.b32decode(digest.upper()).hex()
                    except binascii.Error as exc:
                        raise InvalidMagnet("Info-hash base32 inválido") from exc
                else:
                    raise InvalidMagnet("Info-hash v1 inválido (se esperan 40 hex o 32 base32)")
                if v1 and v1 != candidate:
                    raise InvalidMagnet("El magnet contiene dos info-hash v1 distintos")
                v1 = candidate
            elif lowered.startswith("urn:btmh:"):
                match = _BTMH.match(value[len("urn:btmh:") :])
                if not match:
                    raise InvalidMagnet("Info-hash v2 inválido (multihash sha2-256 esperado)")
                v2 = match.group(1).lower()
        elif key == "dn" and value:
            name = value[:500]
        elif key == "tr" and _valid_url(value, _TRACKER_SCHEMES) and value not in trackers:
            trackers.append(value)
        elif key == "ws" and _valid_url(value, _SEED_SCHEMES) and value not in seeds:
            seeds.append(value)

    if v1 is None and v2 is None:
        raise InvalidMagnet("El magnet no contiene un info-hash BitTorrent (xt=urn:btih / urn:btmh)")

    return MagnetLink(
        info_hash_v1=v1,
        info_hash_v2=v2,
        display_name=name,
        trackers=tuple(trackers[:MAX_TRACKERS]),
        web_seeds=tuple(seeds[:MAX_TRACKERS]),
    )
