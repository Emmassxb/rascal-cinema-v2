#!/usr/bin/env bash
# Respaldo diario (cron/systemd timer como usuario rascal). Retención: 14 días locales.
#   0 4 * * *  /srv/rascal/app/deploy/linux/backup-rascal.sh
set -euo pipefail
umask 077
VAR=/srv/rascal/var
DEST=/srv/rascal/backups
STAMP=$(date +%F)
mkdir -p "$DEST"

DB_URL=$(grep -E '^RASCAL_DATABASE_URL=' /etc/rascal/rascal.env | cut -d= -f2-)
if [[ "$DB_URL" == sqlite* ]]; then
  # .backup usa la API de respaldo en línea de SQLite: copia coherente aunque la app esté escribiendo (WAL).
  sqlite3 "$VAR/rascal.db" ".backup '$DEST/rascal-$STAMP.db'"
  sqlite3 "$DEST/rascal-$STAMP.db" "PRAGMA integrity_check;" | grep -qx ok
  gzip -f "$DEST/rascal-$STAMP.db"
else
  pg_dump --format=custom --no-owner --file="$DEST/rascal-$STAMP.dump" "${DB_URL/+psycopg/}"
fi

# Archivos que no se regeneran solos (las descargas P2P sí se regeneran desde el enjambre).
tar -czf "$DEST/files-$STAMP.tar.gz" -C "$VAR" subtitles avatars torrents resume 2>/dev/null || true

find "$DEST" -type f -mtime +14 -delete
# Copia fuera del servidor (recomendado): rclone/restic/borg hacia otro proveedor, cifrado.
# restic -r sftp:backup@otro-host:/rascal backup "$DEST"
