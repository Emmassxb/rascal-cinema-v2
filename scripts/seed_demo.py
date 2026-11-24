#!/usr/bin/env python3
"""Carga datos de demostración: usuarios por rol, catálogo de dominio público / Creative Commons,
reseñas, seguidores y dos fuentes magnet CC-BY (Blender Foundation).

    python scripts/seed_demo.py                 # contraseñas aleatorias impresas al final
    python scripts/seed_demo.py --password demo-pass-123
"""

from __future__ import annotations

import argparse
import random
import secrets
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import func, select  # noqa: E402

from rascal.auth.mailer import ConsoleMailer  # noqa: E402
from rascal.auth.service import AuthService  # noqa: E402
from rascal.config import get_settings  # noqa: E402
from rascal.data.db import create_schema, get_engine, session_scope  # noqa: E402
from rascal.data.enums import Role  # noqa: E402
from rascal.data.models import Review, User, utcnow  # noqa: E402
from rascal.domain.schemas import MagnetSourceForm, MovieForm, RegisterForm, ReviewForm  # noqa: E402
from rascal.domain.services.catalog import CatalogService  # noqa: E402
from rascal.domain.services.reviews import ReviewService  # noqa: E402
from rascal.domain.services.social import SocialService  # noqa: E402

CC_BY = "Creative Commons Attribution 3.0 — © Blender Foundation (www.blender.org)"
WEBSEED = "&ws=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2F"
TRACKERS = "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce&tr=udp%3A%2F%2Fexplodie.org%3A6969"
# Torrents públicos de demostración de WebTorrent (contenido CC-BY). Verifica el info-hash antes de producción.
MAGNETS = {
    "Big Buck Bunny": f"magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny{TRACKERS}{WEBSEED}",
    "Sintel": f"magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel{TRACKERS}{WEBSEED}",
}

BLENDER = [
    dict(title="Big Buck Bunny", release_year=2008, runtime_min=10, quality=8,
         synopsis="Un conejo gigante y bonachón decide vengarse de tres roedores abusones.",
         genres=["Animación", "Comedia", "Familia"], directors=[{"name": "Sacha Goedegebure"}],
         crew=[{"name": "Jan Morgenstern", "role": "composer"}],
         tech={"aspect_ratio": "16:9", "color": "color", "original_language": "en", "country": "Países Bajos"}),
    dict(title="Elephants Dream", release_year=2006, runtime_min=11, quality=6,
         synopsis="Dos personajes recorren una máquina infinita y surrealista.",
         genres=["Animación", "Ciencia ficción"], directors=[{"name": "Bassam Kurdali"}],
         cast=[{"name": "Tygo Gernandt", "character_name": "Proog"}, {"name": "Cas Jansen", "character_name": "Emo"}],
         crew=[{"name": "Jan Morgenstern", "role": "composer"}],
         tech={"aspect_ratio": "16:9", "color": "color", "original_language": "en"}),
    dict(title="Sintel", release_year=2010, runtime_min=15, quality=9,
         synopsis="Una joven busca al dragón bebé que crió como a un hermano.",
         genres=["Animación", "Fantasía", "Aventura"], directors=[{"name": "Colin Levy"}],
         cast=[{"name": "Halina Reijn", "character_name": "Sintel (voz)"}, {"name": "Thom Hoffman", "character_name": "Chamán (voz)"}],
         crew=[{"name": "Jan Morgenstern", "role": "composer"}],
         tech={"aspect_ratio": "2.35:1", "color": "color", "original_language": "en"}),
    dict(title="Tears of Steel", release_year=2012, runtime_min=12, quality=6,
         synopsis="En una Ámsterdam futurista, científicos intentan salvar el mundo de los robots.",
         genres=["Ciencia ficción", "Acción"], directors=[{"name": "Ian Hubert"}],
         cast=[{"name": "Derek de Lint"}, {"name": "Sergio Hasselbaink"}, {"name": "Vanja Rukavina"}],
         tech={"aspect_ratio": "2.40:1", "color": "color", "original_language": "en"}),
]
CLASSICS = [
    dict(title="Nosferatu", release_year=1922, runtime_min=94, quality=9, companies=["Prana Film"],
         synopsis="Un agente inmobiliario visita al misterioso conde Orlok en los Cárpatos.",
         genres=["Terror", "Fantasía"], directors=[{"name": "F. W. Murnau"}],
         cast=[{"name": "Max Schreck", "character_name": "Conde Orlok"},
               {"name": "Gustav von Wangenheim", "character_name": "Thomas Hutter"},
               {"name": "Greta Schröder", "character_name": "Ellen Hutter"}],
         crew=[{"name": "Henrik Galeen", "role": "writer"}],
         tech={"aspect_ratio": "1.33:1", "color": "bw", "sound_mix": "Muda", "country": "Alemania", "original_language": "de"}),
    dict(title="Metropolis", release_year=1927, runtime_min=153, quality=9, companies=["UFA"],
         synopsis="En una ciudad futurista dividida en clases, el hijo del amo se rebela.",
         genres=["Ciencia ficción", "Drama"], directors=[{"name": "Fritz Lang"}],
         cast=[{"name": "Brigitte Helm", "character_name": "María"}, {"name": "Alfred Abel", "character_name": "Joh Fredersen"},
               {"name": "Gustav Fröhlich", "character_name": "Freder"}],
         crew=[{"name": "Thea von Harbou", "role": "writer"}],
         tech={"aspect_ratio": "1.33:1", "color": "bw", "sound_mix": "Muda", "country": "Alemania", "original_language": "de"}),
    dict(title="The General", release_year=1926, runtime_min=78, quality=8, companies=["Buster Keaton Productions"],
         synopsis="Un maquinista sureño persigue la locomotora que le robaron.",
         genres=["Comedia", "Acción"], directors=[{"name": "Buster Keaton"}, {"name": "Clyde Bruckman"}],
         cast=[{"name": "Buster Keaton", "character_name": "Johnnie Gray"}, {"name": "Marion Mack", "character_name": "Annabelle Lee"}],
         tech={"aspect_ratio": "1.33:1", "color": "bw", "sound_mix": "Muda", "country": "Estados Unidos"}),
    dict(title="Night of the Living Dead", release_year=1968, runtime_min=96, quality=8, companies=["Image Ten"],
         synopsis="Un grupo de desconocidos se atrinchera en una granja ante una plaga de muertos vivientes.",
         genres=["Terror"], directors=[{"name": "George A. Romero"}],
         cast=[{"name": "Duane Jones", "character_name": "Ben"}, {"name": "Judith O'Dea", "character_name": "Barbra"}],
         tech={"aspect_ratio": "1.37:1", "color": "bw", "country": "Estados Unidos", "original_language": "en"}),
    dict(title="His Girl Friday", release_year=1940, runtime_min=92, quality=8, companies=["Columbia Pictures"],
         synopsis="Un editor astuto intenta recuperar a su mejor reportera, que además es su exesposa.",
         genres=["Comedia", "Romance"], directors=[{"name": "Howard Hawks"}],
         cast=[{"name": "Cary Grant", "character_name": "Walter Burns"}, {"name": "Rosalind Russell", "character_name": "Hildy Johnson"},
               {"name": "Ralph Bellamy", "character_name": "Bruce Baldwin"}],
         tech={"aspect_ratio": "1.37:1", "color": "bw", "country": "Estados Unidos", "original_language": "en"}),
    dict(title="Charade", release_year=1963, runtime_min=113, quality=8, companies=["Universal Pictures"],
         synopsis="Una viuda en París descubre que todos buscan la fortuna que escondió su marido.",
         genres=["Comedia", "Misterio", "Romance"], directors=[{"name": "Stanley Donen"}],
         cast=[{"name": "Cary Grant", "character_name": "Peter Joshua"}, {"name": "Audrey Hepburn", "character_name": "Regina Lampert"},
               {"name": "Walter Matthau", "character_name": "Hamilton Bartholomew"}],
         crew=[{"name": "Henry Mancini", "role": "composer"}],
         tech={"aspect_ratio": "1.85:1", "color": "color", "country": "Estados Unidos", "original_language": "en"}),
]
COMMENTS = ["Imprescindible.", "Envejeció mejor de lo que esperaba.", "Visualmente impecable.",
            "El ritmo decae a la mitad, pero el final lo compensa.", "La vi de nuevo y gana con cada visionado.", None]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", help="contraseña común para las cuentas demo (por defecto aleatoria)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    create_schema(get_engine())
    password = args.password or f"demo-{secrets.token_urlsafe(9)}"
    rng = random.Random(args.seed)

    with session_scope() as session:
        if session.scalar(select(func.count()).select_from(User)):
            print("La base ya tiene usuarios; no se siembra nada (usa una BD nueva).")
            return 1
        auth = AuthService(session, settings, ConsoleMailer())
        accounts = {
            "admin": Role.ADMIN, "blender_studio": Role.PARTNER, "archivo_publico": Role.PARTNER,
            "ana": Role.USER, "beto": Role.USER, "carla": Role.USER, "dario": Role.USER,
        }
        principals = {
            name: auth.register(RegisterForm(username=name, password=password), role=role)  # type: ignore[arg-type]
            for name, role in accounts.items()
        }
        movies: dict[str, tuple[int, int]] = {}
        for owner, catalog in (("blender_studio", BLENDER), ("archivo_publico", CLASSICS)):
            service = CatalogService(session, principals[owner], settings)
            for spec in catalog:
                data = dict(spec)
                quality = int(data.pop("quality"))
                if owner == "blender_studio":
                    data.setdefault("companies", ["Blender Institute"])
                movie_id = service.create_movie(MovieForm.model_validate(data))
                movies[data["title"]] = (movie_id, quality)
                if data["title"] in MAGNETS:
                    service.add_magnet_source(movie_id, MagnetSourceForm(
                        magnet_uri=MAGNETS[data["title"]], label="1080p (WebTorrent)", rights_statement=CC_BY))

        reviewers = ["ana", "beto", "carla", "dario", "admin"]
        for movie_id, quality in movies.values():
            for name in rng.sample(reviewers, k=rng.randint(2, len(reviewers))):
                rating = max(1, min(10, quality + rng.choice([-2, -1, -1, 0, 0, 0, 1])))
                review_id = ReviewService(session, principals[name]).add(
                    movie_id, ReviewForm(rating=rating, body=rng.choice(COMMENTS), is_rewatch=rng.random() < 0.2))
                session.get(Review, review_id).created_at = utcnow() - timedelta(days=rng.randint(0, 120))
        for follower, followed in [("ana", "beto"), ("ana", "carla"), ("beto", "ana"), ("carla", "dario"), ("dario", "ana")]:
            SocialService(session, principals[follower]).follow(followed)
        for name, title in [("ana", "Metropolis"), ("beto", "Sintel"), ("carla", "Charade"), ("ana", "Sintel")]:
            SocialService(session, principals[name]).toggle_watchlist(movies[title][0])

    print(f"Datos demo creados en {settings.database_url}")
    print(f"Cuentas: {', '.join(accounts)}  ·  contraseña: {password}")
    print("Fuentes magnet CC-BY: Big Buck Bunny y Sintel (requieren conexión a Internet para descargar).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
