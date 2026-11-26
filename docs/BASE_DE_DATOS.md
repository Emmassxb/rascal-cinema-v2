# Base de datos: ubicación, respaldos y rendimiento con 86 000+ títulos

## 1. Dónde vive la base de datos

| Modo | `RASCAL_DATABASE_URL` | Archivo / servicio |
|---|---|---|
| Desarrollo (`python launcher.py`) | sin `.env`: lo fija el lanzador | `var/rascal.db` junto al repositorio |
| Ejecutable Windows (`RascalCinema.exe`) | lo fija el lanzador | `var\rascal.db` **junto al .exe** |
| Servidor con SQLite | `sqlite:////srv/rascal/var/rascal.db` (ruta absoluta: cuatro barras) | `/srv/rascal/var/rascal.db` |
| Servidor con PostgreSQL | `postgresql+psycopg://rascal:CLAVE@127.0.0.1:5432/rascal` | el clúster de PostgreSQL |

`sqlite:///var/rascal.db` (tres barras) es una ruta **relativa al directorio de trabajo**: funciona
con el lanzador porque hace `chdir` a su carpeta, pero en un servidor usa siempre la absoluta o
fija `WorkingDirectory=` en systemd (como hace `deploy/linux/rascal-cinema.service`).

SQLite trabaja en modo WAL: junto a `rascal.db` verás `rascal.db-wal` y `rascal.db-shm`. Los tres
forman la base mientras el servicio corre. **No copies `rascal.db` con `cp` en caliente**: puedes
obtener un archivo incoherente.

Además de la base, `var/` contiene datos que conviene respaldar: `subtitles/` (WebVTT de películas
y `subtitles/episodes/<id>/` de capítulos), `avatars/`, `torrents/` y `resume/` (estado de
libtorrent). `downloads/` no hace falta: se vuelve a descargar del enjambre.

### Cuándo pasar a PostgreSQL

SQLite aguanta bien un servidor con tráfico moderado (lecturas concurrentes ilimitadas, una
escritura a la vez). Pasa a PostgreSQL si: hay más de una instancia de la app, muchas escrituras
simultáneas (reseñas/watchlist en picos), o necesitas búsqueda por subcadena rápida con `pg_trgm`
(ver §3). El esquema y las migraciones son los mismos en ambos motores; la 0003 se probó en los dos
(upgrade → downgrade → upgrade con datos).

Para migrar de SQLite a PostgreSQL: `alembic upgrade head` contra la base nueva vacía y copia los
datos con [pgloader](https://pgloader.io) (`pgloader sqlite:///srv/rascal/var/rascal.db
postgresql://rascal@localhost/rascal` con la opción `data only`), luego `python scripts/rebuild_stats.py`.

## 2. Respaldos

**SQLite** (copia coherente aunque la app esté escribiendo, gracias a la API de respaldo en línea):

```bash
sqlite3 /srv/rascal/var/rascal.db ".backup '/srv/rascal/backups/rascal-$(date +%F).db'"
sqlite3 /srv/rascal/backups/rascal-$(date +%F).db "PRAGMA integrity_check;"   # debe responder: ok
```

**PostgreSQL**:

```bash
pg_dump --format=custom --no-owner --file=rascal-$(date +%F).dump "postgresql://rascal@127.0.0.1/rascal"
pg_restore --clean --if-exists --no-owner -d rascal rascal-2026-11-24.dump     # restaurar
```

`deploy/linux/backup-rascal.sh` hace ambas cosas según `RASCAL_DATABASE_URL`, empaqueta
subtítulos/avatares/torrents/resume, verifica la integridad y borra copias de más de 14 días.
Prográmalo a diario y **copia fuera del servidor** (restic, borg o rclone, cifrado): un respaldo en
el mismo disco no protege de perder el VPS. Prueba una restauración al menos una vez.

Tras restaurar un respaldo parcial o importar datos con SQL directo, recalcula los agregados:
`python scripts/rebuild_stats.py`.

## 3. Qué se hizo para que siga rápida con 86 000+ títulos

### Agregados desnormalizados

En v2.1 cada listado (inicio, catálogo, filmografía, panel) unía `movies` con un `GROUP BY` sobre
**toda** la tabla `reviews` y otro sobre `watchlist`. Ahora `movies` guarda `rating_avg`,
`rating_count`, `review_count`, `watchlist_count` y `popularity`. Un *listener* de sesión
(`rascal/data/stats.py`) los recalcula sólo para las películas afectadas, en la misma transacción
que la reseña o la entrada de watchlist. La migración 0003 los rellena para los datos existentes.

### Índices (migración 0003)

| Índice | Para qué |
|---|---|
| `movies(media_type, is_published, release_year)` | catálogo filtrado por tipo (series/películas) y rango de años |
| `movies(is_published, popularity)` | «Popular» y orden por popularidad sin ordenar en memoria |
| `movies(is_published, rating_avg)` | «Mejor calificación» y filtro de estrellas mínimas |
| `movies(is_published, created_at)` | «Recién añadidas» |
| `movies(is_published, title, id)` | catálogo A-Z paginado (cubre ORDER BY title, id) |
| `movies(title)`, `movies(tmdb_id)` | búsqueda por prefijo/igualdad, importación TMDB |
| `UNIQUE movies(media_type, tmdb_id)` | película 1399 y serie 1399 de TMDB conviven |
| `UNIQUE seasons(series_id, season_number)` | pestañas de temporada |
| `UNIQUE episodes(season_id, episode_number)` | lista de capítulos |
| `media_sources(episode_id)` | fuentes de un capítulo |
| `UNIQUE people(slug)` (ya existía) | `/person/{slug}` |
| `UNIQUE lower(users.username)`, `lower(users.email)` | **corrección**: el modelo los declaraba pero la 0001 nunca los creó |

En PostgreSQL añade los índices trigram de `scripts/postgres_trgm.sql` para que la búsqueda por
subcadena (`ILIKE '%texto%'`) no recorra la tabla.

### Paginación estricta y proyecciones ligeras

- Ninguna consulta de listado devuelve más de 200 filas (`MAX_PAGE_SIZE`); las páginas usan 60.
- Catálogo (`/browse?page=`), series (`/browse?type=series`), filmografías (`/person|company|genre/{slug}?page=`),
  reseñas de la ficha (`?rpage=`, 30 por página) y el panel Partner/Admin (`/partner?page=&pq=`)
  están paginados. Antes el panel de administración hacía `SELECT * FROM movies` sin límite.
- Las tarjetas sólo leen `id, slug, title, release_year, poster_path, media_type, rating_avg,
  review_count, watchlist_count, is_published`. La búsqueda proyecta columnas (sin sinopsis ni
  biografías) y la ficha ya no carga el BLOB `.torrent` ni todas las fuentes de una serie.
- Los carruseles del inicio no ejecutan `COUNT(*)`; «Mejor valoradas» sólo evalúa títulos con
  calificaciones, y la media global (prior bayesiano) se cachea 5 minutos.

### Medición (`python scripts/benchmark_catalog.py`)

86 000 títulos (5 % series), 600 000 reseñas, 120 000 personas y 516 000 créditos sintéticos.
Mediana de 7 ejecuciones, en el contenedor de desarrollo (PostgreSQL sin ajustar y sin `pg_trgm`):

| Consulta | SQLite | PostgreSQL |
|---|---:|---:|
| v2.1 · listado por popularidad (GROUP BY reseñas) | 158 ms | 611 ms |
| Inicio (3 carruseles + media global) | 26 ms | 48 ms |
| Catálogo · popularidad, página 1 | 3,9 ms | 6,1 ms |
| Catálogo · A-Z, página 1 000 | 6,5 ms | 28 ms |
| Catálogo · sólo series por rango de años | 2,1 ms | 2,3 ms |
| Catálogo · género + mejor calificación | 10 ms | 16 ms |
| Ficha del título más reseñado | 2,9 ms | 5,2 ms |
| Filmografía de una persona | 3,4 ms | 7,7 ms |
| Búsqueda frecuente («sombra») | 0,6 ms | 1,0 ms |
| Búsqueda sin resultados (peor caso, sin trigram) | 64 ms | 52 ms |

El listado equivalente al de v2.1 cuesta 158–611 ms por petición; el nuevo, 4–6 ms. El peor caso
restante es la búsqueda por subcadena que no encuentra nada (recorre los títulos): en PostgreSQL
se resuelve con `pg_trgm`; en SQLite sigue siendo aceptable a este tamaño.
