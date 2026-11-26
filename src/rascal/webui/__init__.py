"""Routers web de la v2.2, separados de ``rascal.web`` para no sobrecargarlo.

* ``series``   — temporadas y capítulos (panel Partner) y fuentes/subtítulos por capítulo.
* ``watch``    — plataformas oficiales "Dónde ver" (alta, edición, sugerencias de TMDB).
* ``people``   — biografías editables (``/partner/person/{id}``) con importación de TMDB.
* ``sources``  — enlaces directos (MP4/MKV/WebM por HTTPS, Debrid) y HLS para películas.

Todos reciben el ``WebContext`` de ``rascal.web`` (configuración, sesiones, plantillas y
registro de proveedores) y delegan la lógica en los servicios de ``rascal.domain``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter


def register_extensions(router: APIRouter, ctx: Any) -> None:
    from rascal.webui import people, series, sources, watch

    for module in (series, watch, people, sources):
        router.include_router(module.build_router(ctx))
