"""Transformaciones puras de documentos MongoDB heredados → valores del esquema relacional.

Se mantienen separadas del script de migración para poder probarlas sin MongoDB.
Cubren las inconsistencias reales del dataset anterior:

* Títulos estilo MovieLens con el año embebido: ``"Toy Story (1995)"``.
* ``year`` como entero, cadena o ausente.
* ``genres`` como lista o como ``"Adventure|Animation"`` (y ``"(no genres listed)"``).
* ``userId`` como ObjectId, cadena (ObjectId o username) o entero (MovieLens).
* Calificaciones en dos escalas: MovieLens 0.5–5 y la app 0–10 (medias estrellas).
* Fechas como ``datetime``, ISO-8601 o epoch (s/ms).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Literal

TITLE_YEAR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$")
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")
NO_GENRES = {"", "(no genres listed)"}
USERNAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
ScaleMode = Literal["auto", "x1", "x2"]


def parse_year(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        year = int(value)
    elif isinstance(value, str) and value.strip()[:4].isdigit():
        year = int(value.strip()[:4])
    else:
        return None
    return year if 1870 <= year <= 2100 else None


def split_title_year(title: str, year: Any) -> tuple[str, int | None]:
    """``("Toy Story (1995)", None)`` → ``("Toy Story", 1995)``."""
    title = (title or "").strip()
    parsed = parse_year(year)
    match = TITLE_YEAR.match(title)
    if match:
        embedded = parse_year(match.group("year"))
        return match.group("title").strip(), parsed or embedded
    return title, parsed


def split_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split("|")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def parse_genres(value: Any) -> list[str]:
    return [g for g in split_list(value) if g.casefold() not in NO_GENRES]


def parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def legacy_user_key(raw: Any) -> str | None:
    """Clave estable para un ``userId`` heredado (ObjectId, username o id MovieLens)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return f"ml:{int(raw)}"
    text = str(raw).strip()
    if not text:
        return None
    if OBJECT_ID.match(text):
        return text.lower()
    if text.isdigit():
        return f"ml:{int(text)}"
    return f"name:{text.casefold()}"


def rating_scale(raw_user_id: Any, mode: ScaleMode) -> float:
    """MovieLens (userId numérico) usa 0.5–5 → ×2; la app Flask ya guardaba 0–10."""
    if mode == "x2":
        return 2.0
    if mode == "x1":
        return 1.0
    key = legacy_user_key(raw_user_id)
    return 2.0 if key is not None and key.startswith("ml:") else 1.0


def poster_from_doc(doc: dict[str, Any]) -> str | None:
    for field in ("poster_path", "poster_url", "poster"):
        value = doc.get(field)
        if isinstance(value, str) and value.strip():
            value = value.strip().lstrip("/")
            if value.startswith("static/"):
                value = value[len("static/") :]
            return value if not value.startswith(("http:/", "https:/")) else doc.get(field).strip()  # type: ignore[union-attr]
    tmdb = doc.get("tmdb_data")
    if isinstance(tmdb, dict) and tmdb.get("poster_path"):
        return f"https://image.tmdb.org/t/p/w500{tmdb['poster_path']}"
    return None


def trailer_from_doc(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    url = doc.get("trailer_url")
    path = doc.get("trailer_path") or doc.get("trailer_file")
    url = url.strip() if isinstance(url, str) and url.strip().startswith(("http://", "https://")) else None
    if isinstance(path, str) and path.strip():
        path = path.strip().lstrip("/")
        path = path[len("static/") :] if path.startswith("static/") else path
    else:
        path = None
    return url, path


def safe_username(raw: str, taken_casefold: set[str]) -> str:
    base = USERNAME_SAFE.sub("_", (raw or "usuario").strip())[:36].strip("._-") or "usuario"
    candidate, suffix = base, 2
    while candidate.casefold() in taken_casefold:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken_casefold.add(candidate.casefold())
    return candidate


def imdb_from_links(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.startswith("tt"):
        return text
    try:
        return f"tt{int(float(text)):07d}"
    except ValueError:
        return None
