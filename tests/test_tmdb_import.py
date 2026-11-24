"""Importación desde TMDB: extracción de ID, mapeo con respaldo en inglés y cliente con red simulada.

Sólo usa la lógica pura (sin FastAPI, SQLAlchemy ni red): se ejecuta en cualquier entorno.
"""

from __future__ import annotations

import unittest
from typing import Any

from rascal.integrations.tmdb import (
    POSTER_BASE,
    TmdbClient,
    TmdbError,
    build_movie,
    parse_tmdb_ref,
)

PRIMARY: dict[str, Any] = {
    "id": 129,
    "title": "El viaje de Chihiro",
    "original_title": "千と千尋の神隠し",
    "tagline": "",  # vacío en español: debe rellenarse desde inglés
    "overview": "Una niña queda atrapada en un mundo de espíritus.",
    "release_date": "2001-07-20",
    "runtime": 125,
    "imdb_id": "tt0245429",
    "original_language": "ja",
    "genres": [{"name": "Animación"}, {"name": "Fantasía"}],
    "production_companies": [{"name": "Studio Ghibli"}],
    "production_countries": [{"name": "Japón"}],
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    "credits": {
        "cast": [
            {"name": "Rumi Hiiragi", "character": "Chihiro", "order": 1},
            {"name": "Miyu Irino", "character": "Haku", "order": 0},
        ],
        "crew": [
            {"name": "Hayao Miyazaki", "job": "Director"},
            {"name": "Joe Hisaishi", "job": "Composer"},
        ],
    },
}
FALLBACK: dict[str, Any] = {
    "id": 129,
    "title": "Spirited Away",
    "tagline": "The tagline in English.",
    "overview": "A young girl becomes trapped in a strange new world of spirits.",
    "release_date": "2001-07-20",
}


class ParseRefTests(unittest.TestCase):
    def test_accepts_plain_id_and_urls(self) -> None:
        self.assertEqual(parse_tmdb_ref("129"), 129)
        self.assertEqual(parse_tmdb_ref("  129  "), 129)
        self.assertEqual(parse_tmdb_ref("https://www.themoviedb.org/movie/129-spirited-away"), 129)
        self.assertEqual(parse_tmdb_ref("themoviedb.org/movie/42"), 42)

    def test_rejects_other_urls_and_garbage(self) -> None:
        for bad in ("", "   ", None, "https://www.themoviedb.org/tv/1399-game-of-thrones", "abc", "0", "-5"):
            with self.subTest(value=bad), self.assertRaises(TmdbError):
                parse_tmdb_ref(bad)  # type: ignore[arg-type]


class BuildMovieTests(unittest.TestCase):
    def test_maps_fields_with_spanish_primary(self) -> None:
        movie = build_movie(PRIMARY)
        self.assertEqual(movie.title, "El viaje de Chihiro")
        self.assertEqual(movie.release_year, 2001)
        self.assertEqual(movie.runtime_min, 125)
        self.assertEqual(movie.imdb_id, "tt0245429")
        self.assertEqual(movie.directors, ("Hayao Miyazaki",))
        self.assertEqual(movie.genres, ("Animación", "Fantasía"))
        self.assertEqual(movie.companies, ("Studio Ghibli",))
        self.assertEqual(movie.country, "Japón")
        self.assertEqual(movie.poster_url, POSTER_BASE + "/poster.jpg")
        self.assertTrue(movie.backdrop_url and movie.backdrop_url.endswith("/t/p/original/backdrop.jpg"))
        # Reparto ordenado por 'order' y con personaje
        self.assertEqual(movie.cast, (("Miyu Irino", "Haku"), ("Rumi Hiiragi", "Chihiro")))

    def test_text_gaps_are_filled_from_english_and_reported(self) -> None:
        movie = build_movie(PRIMARY, FALLBACK)
        self.assertEqual(movie.tagline, "The tagline in English.")
        self.assertIn("tagline", movie.filled_from_fallback)
        self.assertEqual(movie.synopsis, PRIMARY["overview"])  # el español no se sustituye
        self.assertNotIn("overview", movie.filled_from_fallback)

    def test_fallback_is_used_for_missing_title_only_when_primary_empty(self) -> None:
        movie = build_movie({**PRIMARY, "title": ""}, FALLBACK)
        self.assertEqual(movie.title, "Spirited Away")

    def test_invalid_imdb_is_dropped(self) -> None:
        self.assertIsNone(build_movie({**PRIMARY, "imdb_id": "xx"}).imdb_id)

    def test_form_fields_use_parser_formats(self) -> None:
        fields = build_movie(PRIMARY).to_form_fields()
        self.assertEqual(fields["cast"], "Miyu Irino : Haku\nRumi Hiiragi : Chihiro")
        self.assertEqual(fields["directors"], "Hayao Miyazaki")
        self.assertEqual(fields["genres"], "Animación, Fantasía")
        self.assertEqual(fields["tmdb_id"], "129")
        self.assertEqual(fields["release_year"], "2001")


class ClientTests(unittest.TestCase):
    def test_requests_spanish_then_english_only_when_needed(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append({"path": path, **params})
            if params["language"] == "es-MX":
                return {**PRIMARY, "overview": ""}  # sinopsis vacía: debe activarse el respaldo
            return dict(FALLBACK)

        movie = TmdbClient("clave", get_json=fake_get).fetch_movie(129)
        self.assertEqual([c["language"] for c in calls], ["es-MX", "en-US"])
        self.assertEqual(movie.language_used, "es-MX")

    def test_no_second_request_when_primary_is_complete(self) -> None:
        calls: list[str] = []

        def fake_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(params["language"])
            return dict(PRIMARY)

        TmdbClient("clave", get_json=fake_get).fetch_movie(129)
        self.assertEqual(calls, ["es-MX"])

    def test_missing_key_is_a_clear_error(self) -> None:
        with self.assertRaises(TmdbError) as ctx:
            TmdbClient("")
        self.assertIn("RASCAL_TMDB_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
