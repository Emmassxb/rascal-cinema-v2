# Despliegue y seguridad en un servidor Linux (Ubuntu 22.04/24.04, Debian 12)

Arquitectura final: Internet → **Nginx** (443, TLS de Let's Encrypt) → **uvicorn** en
`127.0.0.1:8001` (un proceso, systemd) → SQLite en `/srv/rascal/var` o PostgreSQL local.
libtorrent, si se usa, escucha en su propio puerto (6881) y **nunca** comparte puerto con HTTP.

Archivos de referencia en `deploy/linux/`: `rascal-cinema.service`, `nginx-rascal.conf`,
`rascal.env.example` y `backup-rascal.sh`.

## 1. Sistema y usuario sin privilegios

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-dev build-essential ffmpeg sqlite3 nginx certbot python3-certbot-nginx ufw unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades            # parches de seguridad automáticos

sudo adduser --system --group --home /srv/rascal --shell /usr/sbin/nologin rascal
sudo install -d -o rascal -g rascal -m 750 /srv/rascal/var /srv/rascal/backups
sudo -u rascal git clone https://github.com/TU_USUARIO/rascal-cinema-v2 /srv/rascal/app
sudo -u rascal python3 -m venv /srv/rascal/venv
sudo -u rascal /srv/rascal/venv/bin/pip install -r /srv/rascal/app/requirements.txt
```

## 2. Configuración y secretos

```bash
sudo install -d -m 750 -o root -g rascal /etc/rascal
sudo install -m 640 -o root -g rascal /srv/rascal/app/deploy/linux/rascal.env.example /etc/rascal/rascal.env
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # → RASCAL_SECRET_KEY
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # → RASCAL_SERVICE_KEY
sudo nano /etc/rascal/rascal.env
```

Claves imprescindibles: `RASCAL_ENV=prod` (la app se niega a arrancar con secretos por defecto),
`RASCAL_HOST=127.0.0.1`, `RASCAL_SECURE_COOKIES=true`, `RASCAL_PUBLIC_URL=https://tu-dominio`,
`RASCAL_DATABASE_URL` absoluta. En `prod` la protección CSRF es estricta: todo POST debe traer
`Origin` o `Referer` de tu dominio (los navegadores lo hacen solos).

Protección de credenciales:

- Los secretos viven sólo en `/etc/rascal/rascal.env` (`640 root:rascal`), nunca en git. El
  repositorio antiguo tenía en claro la contraseña de Gmail, la URI de MongoDB Atlas y la clave de
  TMDB: **rótalas** aunque ya no se usen.
- Cambiar `RASCAL_SECRET_KEY` invalida todas las sesiones y tokens de reproducción (útil si se filtra).
- SMTP: usa una contraseña de aplicación o un proveedor transaccional, no la contraseña de tu cuenta.
- PostgreSQL: usuario propio con contraseña larga, escuchando sólo en `127.0.0.1`
  (`listen_addresses = 'localhost'`), y `scram-sha-256` en `pg_hba.conf`.

## 3. Servicio systemd

```bash
sudo cp /srv/rascal/app/deploy/linux/rascal-cinema.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rascal-cinema
sudo systemctl status rascal-cinema
curl -s http://127.0.0.1:8001/health
sudo -u rascal /srv/rascal/venv/bin/python /srv/rascal/app/scripts/create_user.py --username admin --role admin --email tu@correo.com
```

La unidad aplica `alembic upgrade head` antes de arrancar, corre como `rascal` sin shell, con
`ProtectSystem=strict` (sólo puede escribir en `/srv/rascal/var`), `NoNewPrivileges`, filtro de
llamadas al sistema y sin capacidades. `scripts/serve.py` arranca uvicorn con **un solo worker**
(dueño único de la sesión libtorrent) y `--proxy-headers` restringido a `RASCAL_TRUSTED_PROXIES`.

## 4. Nginx + HTTPS con Certbot

```bash
sudo cp /srv/rascal/app/deploy/linux/nginx-rascal.conf /etc/nginx/sites-available/rascal-cinema
sudo sed -i 's/cine.ejemplo.com/TU-DOMINIO/g' /etc/nginx/sites-available/rascal-cinema
sudo ln -s /etc/nginx/sites-available/rascal-cinema /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
# Primera vez: comenta el bloque «server { listen 443 … }», recarga, y deja que Certbot emita el certificado:
sudo certbot certonly --nginx -d TU-DOMINIO
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl list-timers | grep certbot     # renovación automática
```

Lo que hace la configuración (validada con `nginx -t` y probada de extremo a extremo):

- HTTP → HTTPS (301); TLS 1.2/1.3 con los parámetros de Certbot.
- `X-Forwarded-For` se **sobrescribe** con la IP real (no se concatena), así nadie falsifica su IP
  para saltarse el rate limit.
- Rate limit en Nginx (10 peticiones/min en login/registro/2FA → 429) además del de la app.
- `/stream` y `/remux` sin buffer y con tiempos largos (video por Range).
- `/internal/*`, las acciones de servicio de `/api/sources` y `/docs` devuelven 404 desde fuera:
  la app las usa por loopback.
- Log sin cadena de consulta: los tokens `?token=` del reproductor no quedan registrados.
- Las cabeceras de seguridad (CSP con nonce, `X-Frame-Options`, `nosniff`, `Referrer-Policy`,
  HSTS) las pone la app; Nginx no las duplica.

## 5. Firewall (UFW) y aislamiento de puertos

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp comment 'SSH con limitación de intentos'
sudo ufw allow 80,443/tcp comment 'Nginx'
# Sólo si usas P2P en este servidor (ver §6):
sudo ufw allow 6881/tcp comment 'libtorrent'
sudo ufw allow 6881/udp comment 'libtorrent DHT/uTP'
sudo ufw enable
sudo ufw status verbose
```

- **8001 no se abre nunca**: uvicorn escucha en `127.0.0.1`, así que ni siquiera un error de UFW lo
  expondría. Compruébalo con `ss -ltnp | grep 8001` (debe decir `127.0.0.1:8001`).
- HTTP (80/443, Nginx) y BitTorrent (6881, libtorrent) son puertos y procesos distintos: cerrar
  6881 desactiva las conexiones entrantes de peers sin afectar a la web.
- `RASCAL_LT_ENABLE_PORT_MAPPING=false` en servidores: UPnP/NAT-PMP no tienen sentido en un VPS.
- SSH: sólo claves (`PasswordAuthentication no`, `PermitRootLogin no` en `/etc/ssh/sshd_config`).
  Opcional: `fail2ban` con las cárceles `sshd` y `nginx-limit-req`.

## 6. Privacidad de los usuarios y del servidor

**Qué expone el P2P.** El motor libtorrent corre en el servidor: es la **IP del servidor** la que
aparece en el enjambre, no la de los espectadores (ellos sólo hablan HTTPS con tu dominio). La
ficha y el reproductor lo avisan al elegir una fuente torrent/magnet. Opciones, de más a menos
privada para el servidor:

1. `RASCAL_STREAM_ENGINE=local` y sólo fuentes directas HTTPS/HLS y plataformas oficiales: el
   servidor no participa en ningún enjambre. Cierra 6881 en UFW.
2. P2P por una VPN: levanta el túnel (p. ej. WireGuard `wg0`), fija
   `RASCAL_LT_LISTEN_INTERFACES=<IP del túnel>:6881` y enruta por política el tráfico de esa IP
   por el túnel. La web sigue saliendo por la IP normal.
3. P2P directo (como está): sólo con contenido de distribución autorizada (cada fuente exige una
   declaración de derechos, que queda en la auditoría).

**Datos personales.** Contraseñas con Argon2id; códigos 2FA guardados como HMAC con caducidad e
intentos limitados; cookies `HttpOnly`, `SameSite=Lax` y `Secure`; la CSP impide cargar scripts de
terceros salvo Video.js desde jsDelivr. Reduce lo que guardas: rota los logs de Nginx a 7–14 días
(`/etc/logrotate.d/nginx`: `rotate 14`, `daily`) y `journalctl --vacuum-time=14d`. Los respaldos
contienen correos y reseñas: cífralos fuera del servidor.

**URLs enviadas por usuarios.** Pósters, fotos, tráileres, enlaces directos, plataformas, avatares
y portafolios se validan: sólo `https`/`http` (enlaces de video sólo `https`), sin `javascript:`,
`data:` ni `file:`, sin credenciales embebidas y sin IPs privadas, loopback, link-local
(169.254.169.254, metadata de la nube) ni dominios `.local`/`.internal`.

## 7. Respaldos

Programa `deploy/linux/backup-rascal.sh` como el usuario `rascal` (`crontab -u rascal -e`):

```cron
0 4 * * * /srv/rascal/app/deploy/linux/backup-rascal.sh
```

Detalles de ubicación, restauración y rendimiento en [`BASE_DE_DATOS.md`](BASE_DE_DATOS.md).

## 8. Actualizar

```bash
sudo -u rascal git -C /srv/rascal/app pull
sudo -u rascal /srv/rascal/venv/bin/pip install -r /srv/rascal/app/requirements.txt
/srv/rascal/app/deploy/linux/backup-rascal.sh          # respaldo ANTES de migrar
sudo systemctl restart rascal-cinema                   # aplica alembic upgrade head al arrancar
journalctl -u rascal-cinema -n 50
```

## 9. Lista de comprobación

- [ ] `RASCAL_ENV=prod`, secretos nuevos, `.env` fuera del repositorio y `640`.
- [ ] `ss -ltnp` muestra uvicorn sólo en `127.0.0.1:8001`.
- [ ] `ufw status`: 22 (limit), 80, 443 y, sólo si hay P2P, 6881.
- [ ] `curl -I https://TU-DOMINIO/login` muestra `strict-transport-security` y `content-security-policy`.
- [ ] Un POST con `Origin: https://otro-sitio` devuelve 403.
- [ ] Respaldo diario + copia externa + una restauración de prueba.
- [ ] Credenciales del código antiguo (Gmail, MongoDB Atlas, TMDB) rotadas.
