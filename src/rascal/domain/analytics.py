"""Analítica del panel de administración.

Conserva los 5 análisis existentes (top géneros, películas por año, top directores, top
actores, distribución de calificaciones) y la regresión año → calificación media.

Cambios de implementación:
* Las agregaciones se ejecutan en SQL (índices) y el resultado tabular viaja como
  ``polars.DataFrame`` (columnar, sin copia a pandas; Streamlit/Altair lo aceptan directo).
* PySpark (JVM de 4 GB para unos miles de filas) se elimina.
* ``sklearn.LinearRegression`` para una sola variable → OLS cerrado con NumPy (misma
  salida: coeficiente, intercepto, MSE, R², n) sin arrastrar SciPy/scikit-learn.
* Se corrige el filtro de años: antes se comparaban **cadenas** (``"$gte": str(min_year)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl
from sqlalchemy.orm import Session

from rascal.data.enums import CreditRole
from rascal.data.repositories.media import AnalyticsRepository

ANALYSES: dict[str, str] = {
    "top_genres": "Top géneros",
    "movies_per_year": "Películas por año",
    "top_directors": "Top directores",
    "top_actors": "Top actores",
    "ratings_distribution": "Distribución de calificaciones",
}


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    id: str
    title: str
    summary: str
    data: pl.DataFrame
    x: str
    y: str
    chart: Literal["bar", "line"]

    def records(self) -> list[dict[str, object]]:
        return self.data.to_dicts()


def run_analysis(session: Session, analysis_id: str, q: str | None = None) -> AnalysisResult:
    """Ejecuta un análisis por id (los mismos ids que la versión Flask)."""
    repo = AnalyticsRepository(session)
    q = (q or "").strip() or None

    if analysis_id == "top_genres":
        rows = repo.genre_counts(q, limit=50)
        df = pl.DataFrame(rows, schema={"genre": pl.String, "count": pl.Int64}, orient="row")
        return AnalysisResult(analysis_id, ANALYSES[analysis_id], f"Top {df.height} géneros", df, "genre", "count", "bar")

    if analysis_id == "movies_per_year":
        rows = repo.movies_per_year(q)
        df = pl.DataFrame(rows, schema={"year": pl.Int64, "count": pl.Int64}, orient="row")
        return AnalysisResult(analysis_id, ANALYSES[analysis_id], "Conteo anual", df, "year", "count", "line")

    if analysis_id in ("top_directors", "top_actors"):
        role = CreditRole.DIRECTOR if analysis_id == "top_directors" else CreditRole.CAST
        rows = repo.people_counts(role, q, limit=20)
        df = pl.DataFrame(rows, schema={"name": pl.String, "slug": pl.String, "count": pl.Int64}, orient="row")
        summary = "Directores con más películas" if role is CreditRole.DIRECTOR else "Actores con más películas"
        return AnalysisResult(analysis_id, ANALYSES[analysis_id], summary, df, "name", "count", "bar")

    if analysis_id == "ratings_distribution":
        rows = repo.rating_counts(q)
        df = pl.DataFrame(rows, schema={"rating": pl.Int64, "count": pl.Int64}, orient="row").with_columns(
            (pl.col("rating") / 2).alias("stars")
        )
        return AnalysisResult(
            analysis_id, ANALYSES[analysis_id], "Histograma de calificaciones (medias estrellas)", df, "stars", "count", "bar"
        )

    raise KeyError(f"Análisis desconocido: {analysis_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Regresión lineal año → calificación media
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RegressionMetrics:
    coef: float
    intercept: float
    mse: float
    r2: float
    n: int


@dataclass(frozen=True, slots=True)
class RegressionResult:
    metrics: RegressionMetrics | None
    points: pl.DataFrame  # columnas: year, avg_rating, reviews, predicted
    message: str | None = None


def ordinary_least_squares(x: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    """OLS de una variable: devuelve (pendiente, intercepto, predicciones)."""
    x_mean = float(x.mean())
    y_mean = float(y.mean())
    sxx = float(((x - x_mean) ** 2).sum())
    slope = 0.0 if sxx == 0 else float(((x - x_mean) * (y - y_mean)).sum()) / sxx
    intercept = y_mean - slope * x_mean
    return slope, intercept, intercept + slope * x


def r_squared(y: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = float(((y - y_pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot == 0:
        return 1.0 if ss_res == 0 else 0.0
    return 1.0 - ss_res / ss_tot


def year_rating_regression(
    session: Session, *, min_year: int = 1900, max_year: int = 2100, min_reviews: int = 1
) -> RegressionResult:
    rows = AnalyticsRepository(session).year_vs_average(min_year, max_year, max(1, min_reviews))
    empty = pl.DataFrame(
        schema={"year": pl.Int64, "avg_rating": pl.Float64, "reviews": pl.Int64, "predicted": pl.Float64}
    )
    if not rows:
        return RegressionResult(None, empty, "No se encontraron datos válidos para la regresión.")

    x = np.array([r[0] for r in rows], dtype=float)
    y = np.array([r[1] for r in rows], dtype=float)
    slope, intercept, predicted = ordinary_least_squares(x, y)
    metrics = RegressionMetrics(
        coef=round(slope, 4),
        intercept=round(intercept, 2),
        mse=round(float(np.mean((y - predicted) ** 2)), 4),
        r2=round(r_squared(y, predicted), 4),
        n=len(rows),
    )
    points = pl.DataFrame(
        {
            "year": [r[0] for r in rows],
            "avg_rating": y.round(3).tolist(),
            "reviews": [r[2] for r in rows],
            "predicted": predicted.round(3).tolist(),
        }
    ).sort("year")
    message = "Sólo hay un año distinto: la pendiente no es informativa." if len(set(x.tolist())) < 2 else None
    return RegressionResult(metrics, points, message)
