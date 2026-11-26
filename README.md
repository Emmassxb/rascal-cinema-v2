# Rascal Does Not Dream of Cinema · v2

Plataforma de reseñas y analítica de cine estilo Letterboxd con streaming P2P secuencial.
Arquitectura por capas (datos → negocio → servicios), RBAC (Admin / Partner / User) y un
microservicio de streaming independiente basado en libtorrent + FastAPI.

**Documentación:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (arquitectura, proveedores, streaming, RBAC) ·
[`docs/BASE_DE_DATOS.md`](docs/BASE_DE_DATOS.md) (ubicación, respaldos, índices, benchmark 86 000 títulos) ·
[`docs/DEPLOY_LINUX.md`](docs/DEPLOY_LINUX.md) (systemd, Nginx + HTTPS, UFW, privacidad) ·
DDL PostgreSQL: [`docs/schema.sql`](docs/schema.sql).

## Arranque rápido

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt                         # + ffmpeg/ffprobe en el PATH
cp .env.example .env                                    # edita RASCAL_SECRET_KEY y RASCAL_SERVICE_KEY
alembic upgrade head
python scripts/seed_demo.py --password "elige-una-clave-1"   # o scripts/create_user.py / migrate_from_mongo.py
python launcher.py                                      # o: python scripts/serve.py (RASCAL_HOST/RASCAL_PORT)
```

Pruebas: `pip install -r requirements-dev.txt && pytest` (218 pruebas, incluida una descarga
BitTorrent real en loopback con streaming por Range, probe y remux). Prueba del reproductor en un
navegador real (opcional): `tests/e2e/player_e2e.py`.

## Estado

| Capa | Estado |
|---|---|
| Datos (ORM normalizado, repositorios, Alembic, migración Mongo) | ✅ |
| Negocio (métricas, analítica, estrellas, formularios Pydantic, servicios RBAC) | ✅ |
| Ingesta P2P (.torrent v1/v2/híbrido, magnet) | ✅ |
| Microservicio de streaming (libtorrent, Range/206, probe, remux, Video.js) | ✅ |
| UI web FastAPI + Jinja2 (`rascal/web.py`, `rascal/webui/`) | ✅ |
| Proveedores estilo Stremio, series, "Dónde ver", fuentes directas/HLS | ✅ v2.2 |

> Seguridad: el código anterior contenía credenciales en claro (Gmail, MongoDB Atlas, TMDB).
> Rótalas aunque migres a esta versión.

## Novedades v2.2

- **Proveedores internos estilo Stremio** (`src/rascal/providers/`): metadatos (TMDB/manual), "Dónde ver",
  fuentes (P2P, URL directa, HLS) y subtítulos detrás de contratos comunes.
- **Series**: temporadas y capítulos con pestañas y tarjetas en la ficha; importación desde
  `themoviedb.org/tv/{id}` (es-MX, respaldo en-US) que nunca pisa lo editado; fuentes y subtítulos por capítulo.
- **"Dónde ver"**: plataformas oficiales con enlace directo, modalidad y región; sugerencias de TMDB para MX.
- **Enlaces directos y HLS**: MP4/WebM/MKV por HTTPS (también Debrid) y `.m3u8`, reproducidos sin libtorrent
  y sólo al pulsar «Reproducir».
- **Reproductor**: idioma de audio y calidad arriba y en la barra de Video.js, pistas de audio internas
  (`audioTracks()`), subtítulos sincronizados con CC, cambio de versión conservando la posición y aviso de
  privacidad P2P.
- **Biografías**: `/person/{slug}` con foto, datos y biografía completa; edición en `/partner/person/{id}` con
  importación de TMDB. Botón «Editar bio» junto a cada integrante.
- **Rendimiento**: agregados desnormalizados, índices compuestos, paginación en todos los listados.
- **Seguridad**: anti-SSRF en URLs, CSRF por Origin/Referer, rate limiting, CSP con nonce, cookies `Secure`,
  HSTS; **corregido un bypass del 2FA** en `/login`. Configuración `RASCAL_HOST`, `RASCAL_PORT`,
  `RASCAL_SECURE_COOKIES`. Despliegue en `deploy/linux/`.

### Migrar a v2.2

```bash
sqlite3 var/rascal.db ".backup 'var/rascal-antes-0003.db'"   # respaldo (PostgreSQL: pg_dump -Fc)
alembic upgrade head        # 0003: series, watch_links, fuentes directas, índices y agregados
```

Si la base tiene usuarios que sólo difieren en mayúsculas (`Ana`/`ana`), la 0003 se detiene y los lista:
la unicidad sin distinguir mayúsculas estaba declarada pero nunca se había aplicado.

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
