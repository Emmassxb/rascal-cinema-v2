"""Proveedor de subtítulos local: .srt/.vtt subidos por partners, convertidos a WebVTT."""

from __future__ import annotations

from pathlib import Path

from rascal.media.subtitles import episode_folder, list_subtitles_in, save_subtitle_in
from rascal.providers.base import MediaTarget


class LocalSubtitleProvider:
    name = "local"

    def __init__(self, base_dir: Path, url_prefix: str = "/subtitles") -> None:
        self.base_dir = Path(base_dir)
        self.url_prefix = url_prefix

    def folder(self, target: MediaTarget) -> Path:
        if target.episode_id is not None:
            return episode_folder(self.base_dir, target.episode_id)
        return self.base_dir / str(int(target.movie_id))

    def url_base(self, target: MediaTarget) -> str:
        if target.episode_id is not None:
            return f"{self.url_prefix}/episode/{int(target.episode_id)}"
        return f"{self.url_prefix}/{int(target.movie_id)}"

    def list_tracks(self, target: MediaTarget) -> list[dict[str, object]]:
        return list_subtitles_in(self.folder(target), self.url_base(target))

    def save(self, target: MediaTarget, raw: bytes, label: str) -> str:
        return save_subtitle_in(self.folder(target), raw, label)

    def file_path(self, target: MediaTarget, filename: str) -> Path | None:
        """Ruta segura de un .vtt existente (``None`` si no existe o el nombre es sospechoso)."""
        safe = Path(filename).name
        if not safe.lower().endswith(".vtt") or safe != filename:
            return None
        path = self.folder(target) / safe
        return path if path.is_file() else None
