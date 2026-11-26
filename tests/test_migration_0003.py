"""Migración 0003 sobre una base v2.1 (0002) CON datos: nada se pierde y los agregados se rellenan.

Es la prueba que protege contra el riesgo real del modo batch de SQLite: al recrear ``movies``
con ``foreign_keys=ON``, el DROP en cascada borraría reseñas, watchlist y fuentes.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "RASCAL_DATABASE_URL": url, "PYTHONPATH": str(ROOT / "src")}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)


def _seed_v21(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("INSERT INTO users(id,username,role,is_active,mfa_email_enabled,failed_login_count,created_at) "
               "VALUES (1,'ana','user',1,0,0,'2026-01-01'), (2,'beto','user',1,0,0,'2026-01-01')")
    db.execute("INSERT INTO movies(id,slug,title,is_published,created_at,updated_at,tmdb_id) VALUES "
               "(1,'m1','M1',1,'2026-01-01','2026-01-01',10), (2,'m2','M2',1,'2026-01-01','2026-01-01',NULL)")
    db.execute("INSERT INTO reviews(id,user_id,movie_id,rating,is_rewatch,contains_spoilers,created_at,updated_at) VALUES "
               "(1,1,1,8,0,0,'2026-01-01','2026-01-01'), (2,2,1,5,0,0,'2026-01-01','2026-01-01'), "
               "(3,1,2,NULL,0,0,'2026-01-01','2026-01-01')")
    db.execute("INSERT INTO review_votes(review_id,user_id,value,created_at) VALUES (1,2,1,'2026-01-01')")
    db.execute("INSERT INTO watchlist(user_id,movie_id,added_at) VALUES (1,1,'2026-01-01'), (2,1,'2026-01-01')")
    db.execute("INSERT INTO media_sources(id,movie_id,kind,label,magnet_uri,info_hash,playback_mode,status,"
               "rights_statement,created_at,updated_at,audio_language) VALUES (1,1,'magnet','x','magnet:?xt=urn:btih:a',"
               "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','unknown','pending','CC','2026-01-01','2026-01-01','Inglés')")
    db.commit()
    db.close()


def test_0003_preserves_v21_data_and_round_trips(tmp_path: Path) -> None:
    pytest.importorskip("alembic")
    path = tmp_path / "v21.db"
    url = f"sqlite:///{path.as_posix()}"
    _alembic(url, "upgrade", "0002")
    _seed_v21(path)

    _alembic(url, "upgrade", "head")
    db = sqlite3.connect(path)
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("reviews", "review_votes", "watchlist", "media_sources")}
    assert counts == {"reviews": 3, "review_votes": 1, "watchlist": 2, "media_sources": 1}
    assert db.execute("SELECT media_type, rating_avg, rating_count, review_count, watchlist_count, popularity "
                      "FROM movies WHERE id=1").fetchone() == ("movie", 6.5, 2, 2, 2, 4)
    assert db.execute("SELECT kind, audio_language, info_hash IS NOT NULL FROM media_sources").fetchone() == ("magnet", "Inglés", 1)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"seasons", "episodes", "watch_links"} <= tables
    indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_movies_type_published_year", "ix_movies_published_popularity", "ix_movies_tmdb_id",
            "uq_users_username_lower", "uq_users_email_lower", "ix_media_sources_episode_id"} <= indexes
    # Película y serie pueden compartir número de TMDB; dos películas no.
    db.execute("INSERT INTO movies(slug,title,is_published,created_at,updated_at,tmdb_id,media_type) "
               "VALUES ('s1','S1',1,'2026-01-01','2026-01-01',10,'series')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO movies(slug,title,is_published,created_at,updated_at,tmdb_id) "
                   "VALUES ('m3','M3',1,'2026-01-01','2026-01-01',10)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO users(username,role,is_active,mfa_email_enabled,failed_login_count,created_at) "
                   "VALUES ('ANA','user',1,0,0,'2026-01-01')")
    db.commit()
    db.close()

    _alembic(url, "downgrade", "0002")
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 2  # la serie no existe en 0002
    db.close()
    _alembic(url, "upgrade", "head")
