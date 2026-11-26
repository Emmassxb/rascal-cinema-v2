"""RascalCinema · lanzador de escritorio (también es el punto de entrada del .exe de Windows).

Qué hace, en orden:
  1. Calcula las carpetas: en modo ``.exe`` los datos se guardan JUNTO al ejecutable (``var/``) y los
     recursos de solo lectura (migraciones, plantillas) están dentro del paquete.
  2. Comprueba dependencias. En modo desarrollo puede instalarlas con ``--install-deps``. En el ``.exe``
     NO se pueden instalar paquetes (todo va empaquetado): si falta algo, se indica cómo recompilar.
  3. Crea ``var/``, ``var/subtitles/``, ``var/avatars/``, ``var/torrents/``, ``var/downloads/``...
  4. Genera secretos locales la primera vez (``var/secrets.json``) si no hay ``.env`` que los defina.
  5. Ejecuta ``alembic upgrade head`` para crear o actualizar la base SQLite.
  6. Elige el primer puerto libre desde 8001 y arranca el servidor (sólo en 127.0.0.1).
  7. Abre el navegador en http://localhost:<puerto>/ cuando el servidor responde.

Uso:
    python launcher.py                  # desarrollo
    python launcher.py --install-deps   # desarrollo: instala requirements.txt si falta algo
    RascalCinema.exe                    # ejecutable compilado con scripts/build_exe.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PREFERRED_PORT = 8001
PORT_TRIES = 20
REQUIRED_MODULES = (
    "fastapi", "uvicorn", "sqlalchemy", "alembic", "pydantic", "pydantic_settings",
    "jinja2", "httpx", "argon2", "email_validator", "multipart",
)
DATA_SUBDIRS = ("subtitles", "avatars", "torrents", "downloads", "resume", "uploads")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Carpeta donde viven los datos del usuario (var/, .env)."""
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent


def bundle_root() -> Path:
    """Carpeta de recursos de solo lectura (migraciones, alembic.ini, plantillas)."""
    return Path(getattr(sys, "_MEIPASS", app_root()))


def log(message: str) -> None:
    print(f"[RascalCinema] {message}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2) Dependencias
# ─────────────────────────────────────────────────────────────────────────────
def missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def ensure_dependencies(install: bool) -> None:
    missing = missing_modules()
    if not missing:
        return
    if is_frozen():
        # Un ejecutable no puede instalar paquetes: deben ir dentro del build.
        log(f"Faltan módulos en el ejecutable: {', '.join(missing)}. Recompila con scripts/build_exe.py.")
        raise SystemExit(2)
    if not install:
        log(f"Faltan dependencias: {', '.join(missing)}.")
        log("Ejecuta de nuevo con --install-deps, o: pip install -r requirements.txt")
        raise SystemExit(2)
    log("Instalando dependencias desde requirements.txt …")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(app_root() / "requirements.txt")])
    still = missing_modules()
    if still:
        log(f"No se pudieron instalar: {', '.join(still)}")
        raise SystemExit(2)


def libtorrent_available() -> bool:
    return importlib.util.find_spec("libtorrent") is not None


# ─────────────────────────────────────────────────────────────────────────────
# 3-4) Carpetas, configuración y secretos
# ─────────────────────────────────────────────────────────────────────────────
def prepare_environment(port: int) -> Path:
    root = app_root()
    os.chdir(root)  # las rutas relativas del proyecto (var/, .env) se resuelven aquí
    data = root / "var"
    for sub in DATA_SUBDIRS:
        (data / sub).mkdir(parents=True, exist_ok=True)

    env_file = root / ".env"
    # Si hay .env, ÉL manda: las variables de entorno tienen prioridad sobre el archivo en pydantic,
    # así que sólo se fijan valores por defecto cuando el usuario no ha creado un .env.
    if not env_file.exists():
        os.environ.setdefault("RASCAL_DATA_DIR", str(data))
        os.environ.setdefault("RASCAL_DATABASE_URL", f"sqlite:///{(data / 'rascal.db').as_posix()}")
        os.environ.setdefault("RASCAL_STREAMER_INTERNAL_URL", f"http://127.0.0.1:{port}")
        os.environ.setdefault("RASCAL_STREAMER_PUBLIC_URL", f"http://localhost:{port}")
        os.environ.setdefault("RASCAL_UI_PUBLIC_URL", f"http://localhost:{port}")
        _load_or_create_secrets(data)
    if not libtorrent_available():
        log("libtorrent no está disponible: se usa el motor 'local' (sin P2P).")
        os.environ["RASCAL_STREAM_ENGINE"] = "local"
        os.environ.setdefault("RASCAL_LOCAL_MEDIA_DIR", str(data / "downloads"))
    return root


def _read_dotenv(path: Path) -> None:
    """Sólo RASCAL_HOST y RASCAL_PORT, que se necesitan ANTES de crear Settings (el resto lo lee pydantic)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip().strip('"').strip("'")
        if key in ("RASCAL_HOST", "RASCAL_PORT") and value:
            os.environ.setdefault(key, value)


def _load_or_create_secrets(data: Path) -> None:
    path = data / "secrets.json"
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
    else:
        values = {"secret_key": secrets.token_urlsafe(48), "service_key": secrets.token_urlsafe(32)}
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        log("Se generaron secretos locales en var/secrets.json (no los compartas).")
    os.environ.setdefault("RASCAL_SECRET_KEY", values["secret_key"])
    os.environ.setdefault("RASCAL_SERVICE_KEY", values["service_key"])


# ─────────────────────────────────────────────────────────────────────────────
# 5) Base de datos
# ─────────────────────────────────────────────────────────────────────────────
def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    ini = bundle_root() / "alembic.ini" if is_frozen() else app_root() / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    command.upgrade(cfg, "head")
    log("Base de datos al día (alembic upgrade head).")


# ─────────────────────────────────────────────────────────────────────────────
# 6-7) Puerto, servidor y navegador
# ─────────────────────────────────────────────────────────────────────────────
def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No hay puertos libres entre {preferred} y {preferred + PORT_TRIES - 1}.")


def open_browser_when_ready(url: str, timeout_s: float = 30.0) -> None:
    def worker() -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{url}health", timeout=2) as response:  # noqa: S310 - loopback
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except OSError:
                time.sleep(0.4)
        log(f"No se pudo confirmar el arranque; abre manualmente {url}")

    threading.Thread(target=worker, name="open-browser", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lanza RascalCinema en tu navegador.")
    parser.add_argument("--install-deps", action="store_true", help="instala requirements.txt si falta algo (sólo desarrollo)")
    parser.add_argument("--port", type=int, default=None, help="puerto (por defecto RASCAL_PORT o el primero libre desde 8001)")
    parser.add_argument("--host", default=None, help="interfaz (por defecto RASCAL_HOST o 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    ensure_dependencies(install=args.install_deps)
    os.chdir(app_root())
    _read_dotenv(app_root() / ".env")
    host = args.host or os.environ.get("RASCAL_HOST") or "127.0.0.1"
    fixed_port = args.port or (int(os.environ["RASCAL_PORT"]) if os.environ.get("RASCAL_PORT") else None)
    port = fixed_port if fixed_port else pick_port(PREFERRED_PORT)
    os.environ["RASCAL_HOST"], os.environ["RASCAL_PORT"] = host, str(port)
    prepare_environment(port)
    run_migrations()

    import uvicorn

    from rascal.streaming.api import create_app

    url = f"http://localhost:{port}/"
    log(f"Servidor en {url}  ·  Ctrl+C para cerrar (los torrents se guardan antes de salir).")
    if host not in ("127.0.0.1", "localhost", "::1"):
        log(f"AVISO: escuchando en {host}: otros equipos de la red pueden conectarse. Usa 127.0.0.1 si no lo necesitas.")
    if not args.no_browser:
        open_browser_when_ready(url)
    uvicorn.run(create_app(), host=host, port=port, workers=1, log_level="warning")
    log("Servidor detenido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
