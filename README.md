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

## Novedades v2.1

- **Ficha de película con orden fijo:** información → tráiler (desplegable) → reproductor P2P y descargas → calificaciones y reseñas.
- **Inicio manual:** abrir la ficha nunca descarga nada. Pulsa *Iniciar descarga y reproducir* en la versión que quieras.
- **Idioma y calidad** por fuente (`audio_language`, `quality`), visibles y editables desde el panel Partner.
- **Subtítulos** como pistas nativas del reproductor (botón CC). Se convierten de `.srt` a WebVTT automáticamente.
- **Selector de versión** dentro del reproductor para cambiar de calidad o idioma sin salir de la ficha.
- **Eliminar descarga del dispositivo:** para la sesión y borra los archivos de la descarga; el `.torrent` o magnet se conserva.
- **Importación desde TMDB** en el panel Partner (requiere `RASCAL_TMDB_API_KEY`). Todo queda editable.
- **Perfil estilo Letterboxd:** pestañas Perfil, Diario, Watchlist, Favoritas, Seguidores y Seguidos; foto de perfil propia.
- **Ajustes (`/settings`):** perfil, foto, favoritas, contraseña y solicitud para ser Partner.
- **Solicitudes Partner:** los administradores aprueban o rechazan desde el panel.

### Migrar una instalación existente

```bash
alembic upgrade head        # aplica 0002: columnas de fuentes y tabla partner_requests
```

### Ejecutable de Windows

```bash
pip install -r requirements-build.txt
python scripts/build_exe.py          # genera dist/RascalCinema.exe
```

El ejecutable crea `var/` junto a él (base de datos, descargas, subtítulos, fotos) y abre el navegador en `http://localhost:8001/`.
Para desarrollo: `python launcher.py` (con `--install-deps` instala `requirements.txt` si falta algo).
