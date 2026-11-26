"""Agregados desnormalizados de ``movies`` (rating_avg, rating_count, review_count,
watchlist_count, popularity).

Por qué
-------
Antes, cada listado (inicio, catálogo, filmografías, búsqueda) unía ``movies`` con un
``GROUP BY`` sobre **toda** la tabla ``reviews`` y otro sobre ``watchlist``. Con 86 000
títulos y cientos de miles de reseñas eso es un escaneo completo por petición. Ahora las
tarjetas leen columnas de ``movies`` y las ordenaciones usan índices compuestos
(``ix_movies_published_popularity``, ``ix_movies_published_rating``…).

Cómo se mantienen
-----------------
Un *listener* de sesión anota qué películas tocaron reseñas o entradas de watchlist durante
el ``flush`` y, al terminar ese ``flush``, recalcula SÓLO esas filas con un ``UPDATE`` por
subconsulta (usa ``ix_reviews_movie_rating`` e ``ix_watchlist_movie_id``). Ocurre dentro de la
misma transacción: si la reseña se revierte, el agregado también.

Escrituras que no pasan por el ORM (``insert(Review)`` masivo en la migración desde Mongo,
borrados en cascada a nivel de base de datos al eliminar un usuario) se reparan con
``rebuild_all_stats`` o ``python scripts/rebuild_stats.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import event, func, inspect, select, update
from sqlalchemy.orm import Session

from rascal.data.models import Movie, Review, WatchlistEntry

_PENDING_KEY = "rascal_stats_pending"
_BATCH = 500


def _stat_values() -> dict[str, Any]:
    rated = select(func.count(Review.rating)).where(Review.movie_id == Movie.id).scalar_subquery()
    reviews = select(func.count(Review.id)).where(Review.movie_id == Movie.id).scalar_subquery()
    watch = select(func.count()).select_from(WatchlistEntry).where(WatchlistEntry.movie_id == Movie.id).scalar_subquery()
    avg = (
        select(func.avg(Review.rating))
        .where(Review.movie_id == Movie.id, Review.rating.is_not(None))
        .scalar_subquery()
    )
    return {
        Movie.rating_avg: avg,
        Movie.rating_count: rated,
        Movie.review_count: reviews,
        Movie.watchlist_count: watch,
        Movie.popularity: reviews + watch,
    }


def refresh_movie_stats(session: Session, movie_ids: Iterable[int]) -> None:
    """Recalcula los agregados de las películas indicadas (en lotes de 500 ids)."""
    ids = sorted({int(i) for i in movie_ids if i is not None})
    for start in range(0, len(ids), _BATCH):
        chunk = ids[start:start + _BATCH]
        session.execute(
            update(Movie).where(Movie.id.in_(chunk)).values(_stat_values()).execution_options(synchronize_session=False)
        )
        # Objetos Movie ya cargados en esta sesión: que relean los valores nuevos.
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Movie) and obj.id in chunk:
                session.expire(obj, ["rating_avg", "rating_count", "review_count", "watchlist_count", "popularity"])


def rebuild_all_stats(session: Session) -> int:
    """Recalcula todos los títulos. Devuelve cuántos se procesaron."""
    ids = list(session.scalars(select(Movie.id)))
    refresh_movie_stats(session, ids)
    return len(ids)


def _movie_id_of(obj: Any) -> int | None:
    if isinstance(obj, (Review, WatchlistEntry)):
        return obj.movie_id
    return None


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _flush_context: Any) -> None:
    touched: set[int] = session.info.setdefault(_PENDING_KEY, set())
    for collection in (session.new, session.dirty, session.deleted):
        for obj in collection:
            movie_id = _movie_id_of(obj)
            if movie_id is not None:
                touched.add(movie_id)
    # Si una reseña cambió de película (no ocurre en la app, pero es posible), cuenta la anterior.
    for obj in session.dirty:
        if isinstance(obj, Review):
            history = inspect(obj).attrs.movie_id.history
            touched.update(v for v in (history.deleted or ()) if v is not None)


@event.listens_for(Session, "after_flush_postexec")
def _apply(session: Session, _flush_context: Any) -> None:
    touched = session.info.pop(_PENDING_KEY, None)
    if touched:
        refresh_movie_stats(session, touched)
