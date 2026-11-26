#!/usr/bin/env python3
"""Recalcula los agregados desnormalizados de ``movies`` (rating_avg, rating_count, review_count,
watchlist_count, popularity).

Se mantienen solos en cada reseña/watchlist hecha por la app. Ejecútalo después de:
  * importar datos con SQL directo o ``insert`` masivo,
  * borrar usuarios directamente en la base (las reseñas caen por ON DELETE CASCADE sin pasar por el ORM),
  * restaurar un respaldo parcial.

    python scripts/rebuild_stats.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rascal.data.db import session_scope  # noqa: E402
from rascal.data.stats import rebuild_all_stats  # noqa: E402


def main() -> int:
    started = time.perf_counter()
    with session_scope() as session:
        count = rebuild_all_stats(session)
    print(f"Agregados recalculados para {count} títulos en {time.perf_counter() - started:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
