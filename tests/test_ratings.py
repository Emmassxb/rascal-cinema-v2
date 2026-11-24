import random
import statistics

import pytest

from rascal.domain.ratings import (
    bayesian_average,
    full_histogram,
    js_round,
    normalize_legacy_rating,
    star_label,
    summarize_histogram,
    to_stars,
    validate_rating,
)


@pytest.mark.parametrize("seed", range(25))
def test_histogram_stats_match_statistics_module(seed: int) -> None:
    rng = random.Random(seed)
    values = [rng.randint(1, 10) for _ in range(rng.randint(1, 300))]
    histogram: dict[int, int] = {}
    for v in values:
        histogram[v] = histogram.get(v, 0) + 1
    stats = summarize_histogram(histogram)
    assert stats.count == len(values)
    assert stats.mean == round(statistics.mean(values), 2)
    assert stats.median == round(statistics.median(values), 2)
    expected_stdev = round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0
    assert stats.stdev == expected_stdev


def test_empty_histogram() -> None:
    stats = summarize_histogram({})
    assert (stats.count, stats.mean, stats.median, stats.stdev) == (0, None, None, None)


def test_js_rounding_semantics_preserved() -> None:
    # Python redondea 8.5 → 8 (bancario); el widget JS hacía Math.round → 9.
    assert js_round(8.5) == 9 and js_round(6.5) == 7 and js_round(6.49) == 6
    assert to_stars(8.5) == 4.5
    assert to_stars(7.0) == 3.5
    assert to_stars(None) is None


def test_star_label() -> None:
    assert star_label(3.5) == "★★★½"
    assert star_label(5.0) == "★★★★★"
    assert star_label(None) == ""


def test_validate_rating() -> None:
    assert validate_rating(0) is None
    assert validate_rating(7) == 7
    with pytest.raises(ValueError):
        validate_rating(11)


def test_legacy_normalization() -> None:
    assert normalize_legacy_rating(3.5, scale_factor=2) == 7  # MovieLens
    assert normalize_legacy_rating(0.5, scale_factor=2) == 1
    assert normalize_legacy_rating("8", scale_factor=1) == 8  # app Flask (0–10)
    assert normalize_legacy_rating(0, scale_factor=1) is None
    assert normalize_legacy_rating(12, scale_factor=1) == 10
    assert normalize_legacy_rating("abc", scale_factor=1) is None


def test_full_histogram_and_bayesian() -> None:
    assert list(full_histogram({7: 2}).values()) == [0, 0, 0, 0, 0, 0, 2, 0, 0, 0]
    assert bayesian_average(10.0, 1, prior_mean=6.0, prior_weight=5) == pytest.approx(6.667, abs=1e-3)
    assert bayesian_average(None, 0, prior_mean=6.0, prior_weight=5) is None
