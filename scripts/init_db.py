#!/usr/bin/env python3
"""Crea el esquema directamente (desarrollo rápido). En producción usa ``alembic upgrade head``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rascal.config import get_settings  # noqa: E402
from rascal.data.db import create_schema, get_engine  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    settings.ensure_dirs()
    create_schema(get_engine())
    print(f"Esquema creado en {settings.database_url}")
