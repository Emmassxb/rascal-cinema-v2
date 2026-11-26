"""series (temporadas/capítulos), "Dónde ver", fuentes directas/HLS, biografías e índices

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24

Cambios (no destructivos; las filas de v2.1 quedan intactas):

* ``movies``: ``media_type`` ('movie' por defecto), agregados desnormalizados
  (``rating_avg``, ``rating_count``, ``review_count``, ``watchlist_count``, ``popularity``)
  rellenados aquí mismo, ``UNIQUE(tmdb_id)`` → ``UNIQUE(media_type, tmdb_id)`` (TMDB numera
  películas y series por separado) e índices compuestos para listados de 86 000+ títulos.
* ``seasons`` y ``episodes`` con unicidad (serie, nº temporada) y (temporada, nº capítulo).
* ``media_sources``: ``episode_id`` (opcional), ``stream_url`` + ``url_hash`` para enlaces
  directos/HLS, ``info_hash`` pasa a opcional y los CHECK aceptan los nuevos tipos.
* ``watch_links``: plataformas oficiales por título, región y modalidad.
* ``people``: ``place_of_birth``, ``known_for_department``, ``updated_at``.
* ``users``: crea los índices únicos ``lower(username)`` / ``lower(email)`` que el modelo
  declaraba desde v2.0 pero que la migración 0001 nunca creó (en bases creadas con Alembic
  podían coexistir ``Ana`` y ``ana``). Si ya hay duplicados, la migración se detiene y
  los lista para resolverlos a mano.

En SQLite las tablas ``movies`` y ``media_sources`` se recrean (modo batch); ``env.py``
desactiva ``foreign_keys`` durante la migración y verifica la integridad al terminar.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEDIA_TYPES = ("movie", "series")
OLD_KINDS = ("magnet", "torrent_file")
NEW_KINDS = ("magnet", "torrent_file", "direct_url", "hls")
OFFER_TYPES = ("subscription", "rent", "buy", "free")
NEW_PAYLOAD_CHECK = (
    "(kind = 'magnet' AND magnet_uri IS NOT NULL AND info_hash IS NOT NULL)"
    " OR (kind = 'torrent_file' AND torrent_data IS NOT NULL AND info_hash IS NOT NULL)"
    " OR (kind IN ('direct_url', 'hls') AND stream_url IS NOT NULL AND url_hash IS NOT NULL)"
)
OLD_PAYLOAD_CHECK = (
    "(kind = 'magnet' AND magnet_uri IS NOT NULL) OR (kind = 'torrent_file' AND torrent_data IS NOT NULL)"
)


def _enum(values: Sequence[str], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=24)


def _case_insensitive_user_indexes() -> None:
    bind = op.get_bind()
    for column in ("username", "email"):
        dupes = bind.execute(
            sa.text(
                f"SELECT lower({column}) AS k, COUNT(*) FROM users WHERE {column} IS NOT NULL "
                f"GROUP BY lower({column}) HAVING COUNT(*) > 1"
            )
        ).fetchall()
        if dupes:
            listed = ", ".join(str(row[0]) for row in dupes[:10])
            raise RuntimeError(
                f"Hay usuarios con {column} duplicado sin distinguir mayúsculas ({listed}). "
                "Renómbralos y vuelve a ejecutar `alembic upgrade head`."
            )
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_users_{column}_lower ON users (lower({column}))")


def upgrade() -> None:
    _case_insensitive_user_indexes()

    # ── movies ───────────────────────────────────────────────────────────────
    with op.batch_alter_table("movies", schema=None, recreate="auto") as batch_op:
        batch_op.add_column(
            sa.Column("media_type", _enum(MEDIA_TYPES, "media_type"), nullable=False, server_default="movie")
        )
        batch_op.add_column(sa.Column("rating_avg", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("rating_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("watchlist_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("popularity", sa.Integer(), nullable=False, server_default="0"))
        batch_op.drop_constraint("uq_movies_tmdb_id", type_="unique")
        batch_op.create_unique_constraint("uq_movies_media_type_tmdb_id", ["media_type", "tmdb_id"])
        batch_op.create_index("ix_movies_tmdb_id", ["tmdb_id"], unique=False)
        batch_op.create_index("ix_movies_type_published_year", ["media_type", "is_published", "release_year"])
        batch_op.create_index("ix_movies_published_popularity", ["is_published", "popularity"])
        batch_op.create_index("ix_movies_published_rating", ["is_published", "rating_avg"])
        batch_op.create_index("ix_movies_published_created", ["is_published", "created_at"])
        batch_op.create_index("ix_movies_published_title", ["is_published", "title", "id"])

    # Agregados iniciales (SQL estándar: válido en SQLite y PostgreSQL).
    op.execute(
        """
        UPDATE movies SET
          rating_avg = (SELECT AVG(r.rating) FROM reviews r WHERE r.movie_id = movies.id AND r.rating IS NOT NULL),
          rating_count = (SELECT COUNT(r.rating) FROM reviews r WHERE r.movie_id = movies.id),
          review_count = (SELECT COUNT(r.id) FROM reviews r WHERE r.movie_id = movies.id),
          watchlist_count = (SELECT COUNT(*) FROM watchlist w WHERE w.movie_id = movies.id)
        """
    )
    op.execute("UPDATE movies SET popularity = review_count + watchlist_count")

    # ── people ───────────────────────────────────────────────────────────────
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("place_of_birth", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("known_for_department", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))

    # ── seasons / episodes ───────────────────────────────────────────────────
    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("season_number", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("poster_path", sa.String(length=500), nullable=True),
        sa.Column("air_year", sa.SmallInteger(), nullable=True),
        sa.Column("tmdb_season_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("season_number BETWEEN 0 AND 999", name=op.f("ck_seasons_season_number_range")),
        sa.CheckConstraint("air_year IS NULL OR air_year BETWEEN 1870 AND 2100", name=op.f("ck_seasons_air_year_range")),
        sa.ForeignKeyConstraint(["series_id"], ["movies.id"], name=op.f("fk_seasons_series_id_movies"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seasons")),
        sa.UniqueConstraint("series_id", "season_number", name="uq_seasons_series_number"),
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("runtime_min", sa.SmallInteger(), nullable=True),
        sa.Column("still_path", sa.String(length=500), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("tmdb_episode_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("episode_number BETWEEN 0 AND 9999", name=op.f("ck_episodes_episode_number_range")),
        sa.CheckConstraint("runtime_min IS NULL OR runtime_min > 0", name=op.f("ck_episodes_episode_runtime_positive")),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name=op.f("fk_episodes_season_id_seasons"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
        sa.UniqueConstraint("season_id", "episode_number", name="uq_episodes_season_number"),
    )

    # ── media_sources ────────────────────────────────────────────────────────
    with op.batch_alter_table("media_sources", schema=None, recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("episode_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("stream_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("url_hash", sa.String(length=64), nullable=True))
        batch_op.alter_column("info_hash", existing_type=sa.String(length=64), nullable=True)
        batch_op.alter_column(
            "kind", existing_type=_enum(OLD_KINDS, "source_kind"), type_=_enum(NEW_KINDS, "source_kind"),
            existing_nullable=False,
        )
        batch_op.drop_constraint(op.f("ck_media_sources_payload_matches_kind"), type_="check")
        batch_op.create_check_constraint(op.f("ck_media_sources_payload_matches_kind"), NEW_PAYLOAD_CHECK)
        batch_op.create_foreign_key(
            "fk_media_sources_episode_id_episodes", "episodes", ["episode_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_unique_constraint("uq_media_sources_movie_url_hash", ["movie_id", "url_hash"])
        batch_op.create_index("ix_media_sources_episode_id", ["episode_id"], unique=False)

    # ── watch_links ──────────────────────────────────────────────────────────
    op.create_table(
        "watch_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=False),
        sa.Column("platform_name", sa.String(length=80), nullable=False),
        sa.Column("platform_slug", sa.String(length=80), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("offer_type", _enum(OFFER_TYPES, "offer_type"), nullable=False),
        sa.Column("region", sa.String(length=2), nullable=False),
        sa.Column("logo_path", sa.String(length=500), nullable=True),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], name=op.f("fk_watch_links_movie_id_movies"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["added_by_id"], ["users.id"], name=op.f("fk_watch_links_added_by_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_links")),
        sa.UniqueConstraint("movie_id", "platform_slug", "offer_type", "region", name="uq_watch_links_offer"),
    )
    op.create_index("ix_watch_links_movie_id", "watch_links", ["movie_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_watch_links_movie_id", table_name="watch_links")
    op.drop_table("watch_links")

    # Las fuentes directas y las de capítulos no existen en 0002: se eliminan antes de restringir.
    op.execute("DELETE FROM media_sources WHERE kind IN ('direct_url', 'hls') OR episode_id IS NOT NULL")
    with op.batch_alter_table("media_sources", schema=None, recreate="auto") as batch_op:
        batch_op.drop_index("ix_media_sources_episode_id")
        batch_op.drop_constraint("uq_media_sources_movie_url_hash", type_="unique")
        batch_op.drop_constraint("fk_media_sources_episode_id_episodes", type_="foreignkey")
        batch_op.drop_constraint(op.f("ck_media_sources_payload_matches_kind"), type_="check")
        batch_op.create_check_constraint(op.f("ck_media_sources_payload_matches_kind"), OLD_PAYLOAD_CHECK)
        batch_op.alter_column(
            "kind", existing_type=_enum(NEW_KINDS, "source_kind"), type_=_enum(OLD_KINDS, "source_kind"),
            existing_nullable=False,
        )
        batch_op.alter_column("info_hash", existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_column("url_hash")
        batch_op.drop_column("stream_url")
        batch_op.drop_column("episode_id")

    op.drop_table("episodes")
    op.drop_table("seasons")

    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("known_for_department")
        batch_op.drop_column("place_of_birth")

    # Las series no existen en 0002: se eliminan con todo lo que cuelga de ellas. Se borra
    # explícitamente porque durante la migración las FKs de SQLite están desactivadas.
    series = "(SELECT id FROM movies WHERE media_type = 'series')"
    op.execute(f"DELETE FROM review_votes WHERE review_id IN (SELECT id FROM reviews WHERE movie_id IN {series})")
    for table in ("reviews", "watchlist", "favorite_movies", "movie_genres", "movie_companies", "movie_credits",
                  "movie_tech_specs", "media_sources"):
        op.execute(f"DELETE FROM {table} WHERE movie_id IN {series}")
    op.execute("DELETE FROM movies WHERE media_type = 'series'")
    with op.batch_alter_table("movies", schema=None, recreate="auto") as batch_op:
        batch_op.drop_index("ix_movies_published_title")
        batch_op.drop_index("ix_movies_published_created")
        batch_op.drop_index("ix_movies_published_rating")
        batch_op.drop_index("ix_movies_published_popularity")
        batch_op.drop_index("ix_movies_type_published_year")
        batch_op.drop_index("ix_movies_tmdb_id")
        batch_op.drop_constraint("uq_movies_media_type_tmdb_id", type_="unique")
        batch_op.create_unique_constraint("uq_movies_tmdb_id", ["tmdb_id"])
        batch_op.drop_column("popularity")
        batch_op.drop_column("watchlist_count")
        batch_op.drop_column("review_count")
        batch_op.drop_column("rating_count")
        batch_op.drop_column("rating_avg")
        batch_op.drop_constraint(op.f("ck_movies_media_type"), type_="check")
        batch_op.drop_column("media_type")
