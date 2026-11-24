"""Lógica de calificación por estrellas (estilo Letterboxd), sin dependencias de BD.

Conserva exactamente la semántica del widget actual (``movie_detail.html``):

* El widget tiene 5 estrellas con mitades; cada clic envía un entero ``1..10``
  (``data-value``), es decir, **medias estrellas**. Ese entero es lo que se persiste.
* El promedio se muestra como ``avg/10`` y como estrellas ``round(avg)/2`` (la función
  JS ``avgToStars`` hacía ``Math.round((avg/2)*2)/2``; aquí se replica el redondeo
  "half-up" de ``Math.round`` en lugar del redondeo bancario de Python).
* Mediana y desviación estándar poblacional redondeadas a 2 decimales; ``stdev = 0.0``
  con una sola reseña (igual que antes).

La diferencia de rendimiento: antes se descargaban hasta 500 reseñas completas para
calcular estas métricas en Python; ahora basta un histograma de 10 filas (``GROUP BY``)
y todas las métricas se derivan de él de forma exacta.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

RATING_MIN = 1
RATING_MAX = 10
RATING_VALUES: tuple[int, ...] = tuple(range(RATING_MIN, RATING_MAX + 1))


def js_round(value: float) -> int:
    """Equivalente a ``Math.round`` de JavaScript (half-up)."""
    return math.floor(value + 0.5)


def to_stars(rating10: float | None) -> float | None:
    """Convierte la escala 0–10 a estrellas 0–5 redondeadas a la media estrella."""
    if rating10 is None:
        return None
    return js_round(rating10) / 2


def star_label(stars: float | None) -> str:
    """``3.5`` → ``"★★★½"``. Cadena vacía si no hay calificación."""
    if stars is None:
        return ""
    full = int(stars)
    half = (stars - full) >= 0.5
    return "★" * full + ("½" if half else "")


def validate_rating(value: int | None) -> int | None:
    """Valida un rating de formulario (0 o ``None`` = sin calificación)."""
    if value is None or value == 0:
        return None
    if not RATING_MIN <= value <= RATING_MAX:
        raise ValueError(f"La calificación debe estar entre {RATING_MIN} y {RATING_MAX} (medias estrellas)")
    return value


def normalize_legacy_rating(raw: float | int | str | None, *, scale_factor: float) -> int | None:
    """Convierte calificaciones heredadas a medias estrellas ``1..10``.

    ``scale_factor`` = 2 para MovieLens (0.5–5.0) y 1 para reseñas creadas en la app (0–10).
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw) * scale_factor
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return max(RATING_MIN, min(RATING_MAX, js_round(value)))


@dataclass(frozen=True, slots=True)
class HistogramStats:
    count: int
    mean: float | None
    median: float | None
    stdev: float | None


def summarize_histogram(histogram: Mapping[int, int]) -> HistogramStats:
    """Media, mediana y desviación poblacional exactas a partir de un histograma."""
    items = sorted((value, count) for value, count in histogram.items() if count > 0)
    total = sum(count for _, count in items)
    if total == 0:
        return HistogramStats(count=0, mean=None, median=None, stdev=None)

    mean = sum(value * count for value, count in items) / total
    variance = sum(count * (value - mean) ** 2 for value, count in items) / total

    def nth(position: int) -> int:  # 0-based sobre la lista expandida
        seen = 0
        for value, count in items:
            seen += count
            if position < seen:
                return value
        raise IndexError(position)

    if total % 2:
        median = float(nth(total // 2))
    else:
        median = (nth(total // 2 - 1) + nth(total // 2)) / 2

    return HistogramStats(
        count=total,
        mean=round(mean, 2),
        median=round(median, 2),
        stdev=round(math.sqrt(variance), 2) if total > 1 else 0.0,
    )


def full_histogram(histogram: Mapping[int, int]) -> dict[int, int]:
    """Rellena con ceros las medias estrellas sin votos (para gráficas de 10 barras)."""
    return {value: int(histogram.get(value, 0)) for value in RATING_VALUES}


def bayesian_average(mean: float | None, count: int, *, prior_mean: float, prior_weight: int) -> float | None:
    """Promedio ponderado tipo IMDb: evita que 1 reseña de 10/10 lidere el ranking."""
    if mean is None or count == 0:
        return None
    return round((count * mean + prior_weight * prior_mean) / (count + prior_weight), 3)
