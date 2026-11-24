# Rascal Does Not Dream of Cinema · v2

Plataforma de reseñas y analítica de cine estilo Letterboxd con streaming P2P secuencial.
Arquitectura por capas (datos → negocio → servicios), RBAC (Admin / Partner / User) y un
microservicio de streaming independiente basado en libtorrent + FastAPI.

**Documentación completa:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (diagnóstico,
esquema, streaming, RBAC, rutas y despliegue) · DDL PostgreSQL: [`docs/schema.sql`](docs/schema.sql).

## Arranque rápido

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt                         # + ffmpeg/ffprobe en el PATH
cp .env.example .env                                    # edita RASCAL_SECRET_KEY y RASCAL_SERVICE_KEY
alembic upgrade head
python scripts/seed_demo.py --password "elige-una-clave-1"   # o scripts/create_user.py / migrate_from_mongo.py
python scripts/run_all.py                               # streamer en :8001 (+ UI en :8501)
```

Pruebas: `pip install -r requirements-dev.txt && pytest` (112 pruebas, incluida una descarga
BitTorrent real en loopback con streaming por Range, probe y remux).

## Estado

| Capa | Estado |
|---|---|
| Datos (ORM normalizado, repositorios, Alembic, migración Mongo) | ✅ |
| Negocio (métricas, analítica, estrellas, formularios Pydantic, servicios RBAC) | ✅ |
| Ingesta P2P (.torrent v1/v2/híbrido, magnet) | ✅ |
| Microservicio de streaming (libtorrent, Range/206, probe, remux, Video.js) | ✅ |
| UI Streamlit (`src/rascal/ui/`) | ⏳ siguiente entrega |

> Seguridad: el código anterior contenía credenciales en claro (Gmail, MongoDB Atlas, TMDB).
> Rótalas aunque migres a esta versión.
