"""Métricas de negocio construidas sobre agregados del repositorio.

La fórmula de popularidad de una entidad (actor, director, productora, género) es
explícita y fácil de ajustar::

    popularidad = usuarios_distintos + 0.5 · watchlist + 2 · reseñas_últimos_30_días

es decir: alcance (cuántas personas distintas reseñaron su filmografía), intención de
ver (watchlist) y tendencia (actividad reciente con más peso).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy.orm import Session

from rascal.data.enums import CreditRole
from rascal.data.models import utcnow
from rascal.data.repositories.catalog import CatalogRepository
from rascal.data.repositories.reviews import ReviewRepository
from rascal.data.repositories.users import SocialRepository
from rascal.domain.ratings import full_histogram, summarize_histogram, to_stars
from rascal.domain.schemas import EntityMetrics, RatingBin, RatingSummary

POPULARITY_WEIGHTS = {"reviewers": 1.0, "watchlist": 0.5, "recent": 2.0}
TRENDING_WINDOW = timedelta(days=30)


def rating_summary_from_histogram(histogram: Mapping[int, int]) -> RatingSummary:
    stats = summarize_histogram(histogram)
    bins = [
        RatingBin(value=value, stars=value / 2, count=count) for value, count in full_histogram(histogram).items()
    ]
    return RatingSummary(
        count=stats.count,
        average=stats.mean,
        average_stars=to_stars(stats.mean),
        median=stats.median,
        stdev=stats.stdev,
        distribution=bins,
    )


def movie_rating_summary(session: Session, movie_id: int) -> RatingSummary:
    """Promedio, mediana, desviación, conteo y distribución de una película (1 consulta)."""
    return rating_summary_from_histogram(ReviewRepository(session).histogram_for_movie(movie_id))


def popularity_score(distinct_reviewers: int, watchlist: int, recent_reviews: int) -> float:
    return round(
        POPULARITY_WEIGHTS["reviewers"] * distinct_reviewers
        + POPULARITY_WEIGHTS["watchlist"] * watchlist
        + POPULARITY_WEIGHTS["recent"] * recent_reviews,
        2,
    )


def entity_metrics(
    session: Session, kind: str, entity_id: int, *, role: CreditRole | None = None, films: int
) -> EntityMetrics:
    """Métricas agregadas de la filmografía de una entidad (5 consultas, todas agregadas)."""
    movie_ids = CatalogRepository.movie_ids_for_entity(kind, entity_id, role)
    reviews = ReviewRepository(session)
    histogram = reviews.histogram_for_movies(movie_ids)
    summary = rating_summary_from_histogram(histogram)
    stats = reviews.reviewer_stats(movie_ids, since=utcnow() - TRENDING_WINDOW)
    watchlist = SocialRepository(session).watchlist_count_for_movies(movie_ids)
    return EntityMetrics(
        films=films,
        total_reviews=stats.total_reviews,
        average=summary.average,
        average_stars=summary.average_stars,
        average_of_film_averages=reviews.average_of_film_averages(movie_ids),
        distinct_reviewers=stats.distinct_reviewers,
        watchlist_count=watchlist,
        reviews_last_30d=stats.reviews_since,
        popularity_score=popularity_score(stats.distinct_reviewers, watchlist, stats.reviews_since),
        distribution=summary.distribution,
    )
