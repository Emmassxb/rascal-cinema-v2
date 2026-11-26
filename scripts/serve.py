#!/usr/bin/env python3
"""Arranque de producción: un proceso uvicorn con la configuración de ``RASCAL_*`` (.env).

    python scripts/serve.py            # usa RASCAL_HOST / RASCAL_PORT (por defecto 127.0.0.1:8001)

Siempre ``--workers 1``: un único proceso es dueño de la sesión libtorrent y del rate limiter
en memoria. Detrás de Nginx, ``--proxy-headers`` + ``--forwarded-allow-ips`` (RASCAL_TRUSTED_PROXIES)
hacen que la IP real del cliente llegue al rate limit y a los logs sin poder falsificarse.
Lo usa ``deploy/linux/rascal-cinema.service``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    import uvicorn

    from rascal.config import get_settings

    settings = get_settings()
    if settings.host not in ("127.0.0.1", "::1", "localhost"):
        logging.getLogger("rascal").warning(
            "RASCAL_HOST=%s expone la app sin proxy. En un servidor usa 127.0.0.1 detrás de Nginx.", settings.host
        )
    uvicorn.run(
        "rascal.streaming.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        server_header=False,
        date_header=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
