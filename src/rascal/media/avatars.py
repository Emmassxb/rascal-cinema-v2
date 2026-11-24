"""Fotos de perfil: validación por firma binaria (no por extensión) y almacenamiento local.

Se aceptan PNG, JPEG y WebP de hasta 2 MB. El nombre del archivo lo genera el servidor
(``<usuario>-<hex>.<ext>``), así que el cliente nunca controla la ruta de destino.
"""

from __future__ import annotations

import secrets
from pathlib import Path

MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_DIRNAME = "avatars"
_EXT_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
)


class AvatarError(ValueError):
    """Imagen no aceptable como foto de perfil."""


def detect_image_ext(raw: bytes) -> str:
    for magic, ext in _EXT_BY_MAGIC:
        if raw.startswith(magic):
            return ext
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    raise AvatarError("Formato no admitido: sube una imagen PNG, JPG o WebP.")


def save_avatar(data_dir: Path, username: str, raw: bytes, *, previous: str | None = None) -> str:
    """Guarda la imagen y devuelve la ruta relativa a ``data_dir`` (p. ej. ``avatars/ana-1f2e.png``).

    Si ``previous`` apunta a un avatar local anterior, se elimina para no acumular archivos."""
    if not raw:
        raise AvatarError("El archivo de imagen está vacío.")
    if len(raw) > MAX_AVATAR_BYTES:
        raise AvatarError("La imagen supera el máximo de 2 MB.")
    ext = detect_image_ext(raw)
    folder = Path(data_dir) / AVATAR_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    safe_user = "".join(ch for ch in username.lower() if ch.isalnum() or ch in "-_")[:20] or "usuario"
    filename = f"{safe_user}-{secrets.token_hex(6)}.{ext}"
    (folder / filename).write_bytes(raw)
    if previous and previous.startswith(f"{AVATAR_DIRNAME}/"):
        delete_avatar(data_dir, previous)
    return f"{AVATAR_DIRNAME}/{filename}"


def delete_avatar(data_dir: Path, relative_path: str) -> None:
    """Borra un avatar local, validando que siga dentro de ``avatars/``."""
    target = (Path(data_dir) / relative_path).resolve()
    root = (Path(data_dir) / AVATAR_DIRNAME).resolve()
    if root in target.parents and target.is_file():
        target.unlink()


def avatar_url(path: str | None) -> str | None:
    """URL pública de un avatar: http(s) tal cual, local ``avatars/x`` → ``/avatars/x``."""
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith(f"{AVATAR_DIRNAME}/"):
        return "/" + path
    return None
