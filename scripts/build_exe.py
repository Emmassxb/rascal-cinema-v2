#!/usr/bin/env python3
"""Compila ``dist/RascalCinema.exe`` (Windows) o el binario equivalente en otros sistemas.

    pip install -r requirements-build.txt
    python scripts/build_exe.py            # genera dist/RascalCinema(.exe)
    python scripts/build_exe.py --onedir   # carpeta en lugar de un solo archivo (arranque más rápido)

Requisitos en la máquina que compila: Python 3.11+ de 64 bits. Para el usuario final NO hace falta
instalar nada: el ejecutable incluye Python, uvicorn, SQLAlchemy, las plantillas, las migraciones y
los binarios nativos de libtorrent.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "RascalCinema"


def add_data(source: str, target: str) -> list[str]:
    """PyInstaller usa ';' como separador en Windows y ':' en el resto."""
    sep = ";" if os.name == "nt" else ":"
    return ["--add-data", f"{ROOT / source}{sep}{target}"]


def build_command(onedir: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--onedir" if onedir else "--onefile",
        "--console",  # la consola muestra el estado del servidor
        "--paths", str(ROOT / "src"),
        "--collect-all", "libtorrent",          # binarios nativos (DLL/pyd) del motor BitTorrent
        "--collect-submodules", "rascal",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "alembic",
        "--collect-submodules", "sqlalchemy.dialects.sqlite",
        "--exclude-module", "streamlit",         # la UI vive en FastAPI; Streamlit no se usa en el exe
        "--exclude-module", "pytest",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        *add_data("src/rascal/templates", "rascal/templates"),
        *add_data("src/rascal/streaming/templates", "rascal/streaming/templates"),
        *add_data("migrations", "migrations"),
        *add_data("alembic.ini", "."),
        str(ROOT / "launcher.py"),
    ]
    return cmd


def preflight() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Se necesita Python 3.11 o superior para compilar.")
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("Falta PyInstaller: pip install -r requirements-build.txt")
    if importlib.util.find_spec("libtorrent") is None:
        print("AVISO: libtorrent no está instalado aquí; el ejecutable se generará sin P2P (motor 'local').")
    for required in ("launcher.py", "alembic.ini", "migrations", "src/rascal/templates/web"):
        if not (ROOT / required).exists():
            raise SystemExit(f"No existe {required}: ejecuta el script desde el repositorio completo.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compila RascalCinema con PyInstaller.")
    parser.add_argument("--onedir", action="store_true", help="carpeta en lugar de un único ejecutable")
    parser.add_argument("--keep-build", action="store_true", help="conserva build/ tras compilar")
    args = parser.parse_args()

    preflight()
    cmd = build_command(onedir=args.onedir)
    print("Ejecutando:", " ".join(cmd[:6]), "…")
    subprocess.check_call(cmd, cwd=ROOT)

    exe_name = f"{NAME}.exe" if os.name == "nt" else NAME
    artifact = ROOT / "dist" / (NAME if args.onedir else exe_name)
    if not args.keep_build:
        shutil.rmtree(ROOT / "build", ignore_errors=True)
    print(f"\nListo: {artifact}")
    print("Copia el ejecutable a una carpeta propia: creará var/ con la base de datos y las descargas junto a él.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
