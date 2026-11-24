"""La migración 0002 aplica y revierte limpiamente en una base SQLite nueva (proceso aislado)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "RASCAL_DATABASE_URL": url, "PYTHONPATH": str(ROOT / "src")}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)


def test_upgrade_adds_columns_and_table_then_downgrades(tmp_path: Path) -> None:
    pytest.importorskip("alembic")
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    _alembic(url, "upgrade", "head")
    insp = inspect(create_engine(url))
    assert {"audio_language", "quality"} <= {c["name"] for c in insp.get_columns("media_sources")}
    assert "partner_requests" in insp.get_table_names()
    _alembic(url, "downgrade", "0001")
    insp = inspect(create_engine(url))
    assert "partner_requests" not in insp.get_table_names()
    assert "quality" not in {c["name"] for c in insp.get_columns("media_sources")}
