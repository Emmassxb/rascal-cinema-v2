"""Purga de descargas locales: borra sólo dentro de downloads_dir y conserva el registro."""

from __future__ import annotations

from pathlib import Path

import pytest

from rascal.streaming.downloads import PurgeError, download_dir_for, purge_local_download

HASH = "ab" * 20


class FakeEngine:
    def __init__(self) -> None:
        self.active: set[int] = set()
        self.removed: list[tuple[int, bool]] = []

    def has(self, source_id: int) -> bool:
        return source_id in self.active

    def remove(self, source_id: int, *, delete_files: bool = False) -> None:
        self.removed.append((source_id, delete_files))
        self.active.discard(source_id)


@pytest.fixture
def layout(settings):  # type: ignore[no-untyped-def]
    settings.ensure_dirs()
    folder = settings.downloads_dir / HASH
    (folder / "pelicula").mkdir(parents=True)
    (folder / "pelicula" / "video.mp4").write_bytes(b"x" * 4096)
    settings.resume_dir.mkdir(parents=True, exist_ok=True)
    (settings.resume_dir / f"{HASH}.fastresume").write_bytes(b"resume")
    return settings


def test_purge_removes_files_resume_and_stops_engine(layout) -> None:  # type: ignore[no-untyped-def]
    engine = FakeEngine()
    engine.active.add(5)
    result = purge_local_download(engine=engine, settings=layout, source_id=5, info_hash=HASH)
    assert engine.removed == [(5, True)]
    assert result.removed_dir is True and result.freed_bytes == 4096
    assert not (layout.downloads_dir / HASH).exists()
    assert not (layout.resume_dir / f"{HASH}.fastresume").exists()


def test_purge_is_idempotent(layout) -> None:  # type: ignore[no-untyped-def]
    purge_local_download(engine=None, settings=layout, source_id=1, info_hash=HASH)
    again = purge_local_download(engine=None, settings=layout, source_id=1, info_hash=HASH)
    assert again.removed_dir is False and again.freed_bytes == 0


@pytest.mark.parametrize("bad", ["", "../../etc", "zz" * 20, "ab" * 19, "/" + "a" * 39])
def test_rejects_invalid_hashes(layout, bad: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PurgeError):
        download_dir_for(layout, bad)


def test_never_touches_files_outside_downloads(layout) -> None:  # type: ignore[no-untyped-def]
    precious = layout.data_dir / "biblioteca"
    precious.mkdir(parents=True)
    (precious / "original.mkv").write_bytes(b"tu biblioteca")
    purge_local_download(engine=None, settings=layout, source_id=1, info_hash=HASH)
    assert (precious / "original.mkv").read_bytes() == b"tu biblioteca"
