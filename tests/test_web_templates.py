"""Plantillas Jinja2 de la interfaz: sintaxis válida y orden de secciones de la ficha.

Sólo necesita Jinja2 (no levanta la aplicación), así que se ejecuta en cualquier entorno.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "src" / "rascal" / "templates" / "web"


class TemplateSyntaxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

    def test_every_template_parses(self) -> None:
        names = sorted(p.name for p in TEMPLATE_DIR.glob("*.html"))
        # 13 de v2.1 + login_verify, partner_episode y partner_person (v2.2)
        self.assertEqual(len(names), 16)
        for name in names:
            with self.subTest(template=name):
                self.env.parse(self.env.loader.get_source(self.env, name)[0])  # type: ignore[union-attr]

    def test_expected_pages_exist(self) -> None:
        for name in ("base.html", "macros.html", "movie.html", "user.html", "settings.html",
                     "partner_list.html", "partner_edit.html", "login.html", "login_verify.html",
                     "partner_episode.html", "partner_person.html"):
            self.assertTrue((TEMPLATE_DIR / name).is_file(), name)


class MovieLayoutOrderTests(unittest.TestCase):
    """Orden vertical obligatorio de /movie/{slug}: información → tráiler → P2P → reseñas."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (TEMPLATE_DIR / "movie.html").read_text(encoding="utf-8")

    def position(self, marker: str) -> int:
        index = self.source.find(marker)
        self.assertNotEqual(index, -1, f"no aparece el marcador {marker!r}")
        self.assertEqual(self.source.count(marker), 1, f"el marcador {marker!r} debe aparecer una sola vez")
        return index

    def test_sections_are_in_the_required_order(self) -> None:
        info = self.position('<section class="hero')
        trailer = self.position('<details class="panel trailer">')
        p2p = self.position('<section class="panel p2p" id="p2p">')
        reviews = self.position('<section class="panel" id="resenas">')
        self.assertLess(info, trailer, "el tráiler debe ir DEBAJO de la información")
        self.assertLess(trailer, p2p, "el P2P debe ir DEBAJO del tráiler")
        self.assertLess(p2p, reviews, "las reseñas deben ir DEBAJO del P2P")

    def test_trailer_is_collapsible(self) -> None:
        self.assertIn('<details class="panel trailer">', self.source)

    def test_p2p_start_is_manual_link_not_autoplay(self) -> None:
        self.assertIn("play=1", self.source)
        self.assertNotIn("autoplay=1", self.source)
        self.assertIn("Iniciar descarga y reproducir", self.source)

    def test_purge_button_is_wired_to_endpoint(self) -> None:
        self.assertIn("/sources/{{ s.id }}/purge", self.source)
        self.assertIn("Eliminar descarga del dispositivo", self.source)


if __name__ == "__main__":
    unittest.main()
