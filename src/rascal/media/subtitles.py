"""Subtítulos: conversión a WebVTT, guardado seguro y listado por película.

Los archivos viven en ``<data_dir>/subtitles/<movie_id>/<Etiqueta>.vtt``. El navegador los
consume a través de ``/subtitles/<movie_id>/<archivo>``, así que el nombre se sanea aquí
para que nunca contenga separadores de ruta ni caracteres raros.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
_TIMESTAMP_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")
# \w es Unicode: conserva "ñ" y acentos. Lo que no sea letra/dígito/_/- (separadores, comillas,
# dos puntos...) se sustituye, así que nunca aparecen rutas en el nombre.
_LABEL_SAFE_RE = re.compile(r"[^\w-]+")

# Idioma BCP-47 aproximado a partir de la etiqueta libre (sólo para el atributo ``srclang``).
_LANG_HINTS: tuple[tuple[str, str], ...] = (
    ("latino", "es"), ("español", "es"), ("espanol", "es"), ("spanish", "es"), ("castellano", "es"),
    ("inglés", "en"), ("ingles", "en"), ("english", "en"),
    ("japonés", "ja"), ("japones", "ja"), ("japanese", "ja"),
    ("coreano", "ko"), ("korean", "ko"),
    ("francés", "fr"), ("frances", "fr"), ("french", "fr"),
    ("portugués", "pt"), ("portugues", "pt"), ("portuguese", "pt"),
)


class SubtitleError(ValueError):
    """Archivo de subtítulos no aceptable (vacío, demasiado grande, sin marcas de tiempo)."""


def decode_text(raw: bytes) -> str:
    """UTF-8 (con o sin BOM); si falla, Windows-1252, que es lo habitual en subtítulos en español."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def to_webvtt(text: str) -> str:
    """Convierte SRT o VTT a WebVTT válido (cabecera ``WEBVTT`` y punto decimal)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if "-->" not in text:
        raise SubtitleError("El archivo no contiene marcas de tiempo (-->): ¿es un .srt o .vtt válido?")
    body = _TIMESTAMP_RE.sub(r"\1.\2", text).strip()
    if body.startswith("WEBVTT"):
        return body + "\n"
    return "WEBVTT\n\n" + body + "\n"


def safe_label(label: str | None, fallback: str = "subtitulo") -> str:
    """Etiqueta apta para nombre de archivo: letras (también acentuadas), dígitos, ``_`` y ``-``."""
    cleaned = _LABEL_SAFE_RE.sub("_", (label or "").strip()).strip("_")
    return cleaned[:60] or fallback


def srclang_for(label: str) -> str:
    lowered = label.lower().replace("_", " ")
    for hint, code in _LANG_HINTS:
        if hint in lowered:
            return code
    return "und"


def save_subtitle(base_dir: Path, movie_id: int, raw: bytes, label: str) -> str:
    """Guarda el subtítulo ya convertido y devuelve el nombre de archivo resultante."""
    if not raw:
        raise SubtitleError("El archivo de subtítulos está vacío.")
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise SubtitleError("El subtítulo supera el máximo de 2 MB.")
    vtt = to_webvtt(decode_text(raw))
    folder = Path(base_dir) / str(int(movie_id))
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{safe_label(label)}.vtt"
    (folder / name).write_text(vtt, encoding="utf-8", newline="\n")
    return name


def list_subtitles(base_dir: Path, movie_id: int, url_prefix: str = "/subtitles") -> list[dict[str, object]]:
    """Pistas disponibles para una película, en orden alfabético estable."""
    folder = Path(base_dir) / str(int(movie_id))
    if not folder.is_dir():
        return []
    tracks: list[dict[str, object]] = []
    for index, path in enumerate(sorted(folder.glob("*.vtt"))):
        label = path.stem.replace("_", " ")
        tracks.append(
            {
                "filename": path.name,
                "label": label,
                "srclang": srclang_for(label),
                "url": f"{url_prefix}/{int(movie_id)}/{quote(path.name)}",
                "default": index == 0,
            }
        )
    return tracks
