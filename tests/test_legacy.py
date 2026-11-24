from datetime import datetime

from rascal.data.legacy import (
    imdb_from_links,
    legacy_user_key,
    parse_datetime,
    parse_genres,
    poster_from_doc,
    rating_scale,
    safe_username,
    split_title_year,
    trailer_from_doc,
)


def test_title_year_variants() -> None:
    assert split_title_year("Toy Story (1995)", None) == ("Toy Story", 1995)
    assert split_title_year("Heat", "1995") == ("Heat", 1995)
    assert split_title_year("Alien (1979)", 1980) == ("Alien", 1980)  # el campo explícito manda
    assert split_title_year("Sin año", "N/A") == ("Sin año", None)


def test_genres_and_keys() -> None:
    assert parse_genres("Adventure|Animation|adventure") == ["Adventure", "Animation"]
    assert parse_genres(["(no genres listed)"]) == []
    assert legacy_user_key(42) == "ml:42" and legacy_user_key("42") == "ml:42"
    assert legacy_user_key("68D7359B0F7DE459C3BBC707") == "68d7359b0f7de459c3bbc707"
    assert legacy_user_key("Ana") == "name:ana"


def test_rating_scale_auto() -> None:
    assert rating_scale(17, "auto") == 2.0  # MovieLens
    assert rating_scale("68d7359b0f7de459c3bbc707", "auto") == 1.0  # app Flask
    assert rating_scale(17, "x1") == 1.0


def test_dates_posters_usernames() -> None:
    assert parse_datetime(964982703) == datetime(2000, 7, 30, 18, 45, 3)
    assert parse_datetime("2025-11-20T21:06:00Z") == datetime(2025, 11, 20, 21, 6)
    assert poster_from_doc({"poster_path": "posters/a.jpg"}) == "posters/a.jpg"
    assert poster_from_doc({"poster_path": "/static/posters/a.jpg"}) == "posters/a.jpg"
    assert poster_from_doc({"tmdb_data": {"poster_path": "/x.jpg"}}) == "https://image.tmdb.org/t/p/w500/x.jpg"
    assert trailer_from_doc({"trailer_url": "https://youtu.be/x", "trailer_path": "trailers/1_a.mp4"}) == (
        "https://youtu.be/x", "trailers/1_a.mp4")
    taken: set[str] = set()
    assert safe_username("Ana María", taken) == "Ana_Mar_a"
    assert safe_username("ana maría", taken) == "ana_mar_a_2"
    assert imdb_from_links(114709) == "tt0114709"
