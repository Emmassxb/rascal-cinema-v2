"""Validación de URLs externas enviadas por usuarios (anti-SSRF y anti-XSS por esquema).

Dónde se aplica
---------------
* Enlaces directos de video (``direct_url`` / ``hls``), enlaces "Dónde ver", pósters, fondos,
  fotos de personas, avatares por URL, tráileres y portafolios de solicitudes Partner.
* El servidor **no descarga** esas URLs (las reproduce o muestra el navegador), pero se validan
  igual: una URL a ``http://192.168.1.1/...`` haría que el navegador de cada visitante golpee
  su propia red interna, y un ``javascript:`` en un ``href`` es XSS.
* Si en el futuro un proveedor necesita *descargar* una URL desde el servidor (sondeo de
  códecs, miniaturas…), debe usar ``assert_public_host(..., resolve=True)``: resuelve DNS y
  rechaza nombres que apunten a rangos privados (defensa contra *DNS rebinding* básica).

Reglas
------
1. Esquemas permitidos explícitamente (por defecto ``https`` y ``http``). Se rechazan
   ``javascript:``, ``data:``, ``file:``, ``vbscript:``, ``blob:``, ``ftp:``, etc.
2. Sin credenciales embebidas (``https://user:pass@host``): ocultan el destino real.
3. Host obligatorio. Se rechazan ``localhost``, ``*.localhost``, ``*.local``, ``*.internal``,
   ``*.lan``, ``*.home.arpa`` y cualquier IP literal privada, loopback, link-local (incluido
   169.254.169.254, metadata de la nube), multicast, reservada o no especificada (IPv4 e IPv6,
   incluidas las formas IPv4-mapped y enteros decimales como ``http://2130706433/``).
4. Sin caracteres de control ni espacios; longitud máxima configurable.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

DANGEROUS_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "ftp", "about", "chrome", "jar"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "0", "broadcasthost"})


class UnsafeUrl(ValueError):
    """La URL no es aceptable (esquema peligroso, red privada, formato inválido)."""


def _ip_from_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP literal en cualquier forma que acepten los navegadores: 127.0.0.1, [::1], 2130706433, 0x7f.0.0.1."""
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    # Formas numéricas alternativas que WHATWG URL normaliza a IPv4 (decimal, hex, octal).
    parts = candidate.split(".")
    if 1 <= len(parts) <= 4 and all(p for p in parts):
        def number(part: str) -> int:
            lowered = part.lower()
            if lowered.startswith("0x"):
                return int(lowered[2:] or "0", 16)
            if len(part) > 1 and part.startswith("0"):
                return int(part[1:], 8)
            return int(part, 10)

        try:
            numbers = [number(p) for p in parts]
        except ValueError:
            return None
        if len(numbers) == 1:
            value = numbers[0]
        else:
            *head, last = numbers
            if any(not 0 <= n <= 255 for n in head):
                return None
            value = 0
            for n in head:
                value = (value << 8) | n
            value = (value << (8 * (4 - len(head)))) | last
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(value)
    return None


def is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or not ip.is_global
    )


def _blocked_hostname(host: str) -> bool:
    host = host.rstrip(".").lower()
    return host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES) or "." not in host


def validate_external_url(
    value: str,
    *,
    schemes: Iterable[str] = ("https", "http"),
    allow_private: bool = False,
    max_length: int = 2000,
) -> str:
    """Devuelve la URL (sin espacios alrededor) o lanza ``UnsafeUrl`` con un mensaje presentable."""
    url = (value or "").strip()
    if not url:
        raise UnsafeUrl("La URL está vacía.")
    if len(url) > max_length:
        raise UnsafeUrl(f"La URL supera {max_length} caracteres.")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        raise UnsafeUrl("La URL contiene espacios o caracteres de control.")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    allowed = {s.lower() for s in schemes}
    if scheme in DANGEROUS_SCHEMES:
        raise UnsafeUrl(f"Esquema no permitido: {scheme}:")
    if scheme not in allowed:
        raise UnsafeUrl("Usa una URL " + " o ".join(sorted(allowed)) + ".")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeUrl("La URL no puede incluir usuario o contraseña.")
    try:
        host = parts.hostname or ""
        _ = parts.port  # valida el puerto (ValueError si no es numérico o está fuera de rango)
    except ValueError as exc:
        raise UnsafeUrl("Host o puerto inválido.") from exc
    if not host:
        raise UnsafeUrl("La URL no tiene dominio.")
    if not allow_private:
        ip = _ip_from_host(host)
        if ip is not None:
            if is_private_ip(ip):
                raise UnsafeUrl("No se permiten direcciones IP privadas, locales o reservadas.")
        elif _blocked_hostname(host):
            raise UnsafeUrl("No se permiten dominios locales (localhost, .local, .internal…).")
    return url


def assert_public_host(url: str, *, resolve: bool = True) -> None:
    """Para descargas hechas POR EL SERVIDOR: todas las IPs del host deben ser públicas."""
    host = urlsplit(url).hostname or ""
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"No se pudo resolver {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if is_private_ip(ip):
            raise UnsafeUrl(f"{host} resuelve a una red privada.")


def is_safe_href(value: str | None) -> bool:
    """Para plantillas: ``True`` si la URL puede ir en un ``href``/``src`` sin riesgo de esquema."""
    if not value:
        return False
    try:
        validate_external_url(value, allow_private=True)
    except UnsafeUrl:
        return False
    return True
