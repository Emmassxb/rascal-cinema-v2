#!/usr/bin/env python3
"""Levanta en paralelo el microservicio de streaming (uvicorn) y la UI (Streamlit).

    python scripts/run_all.py            # Ctrl+C detiene ambos
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
UI_ENTRY = ROOT / "src" / "rascal" / "ui" / "app.py"


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    # SIGTERM (systemd, docker stop, kill) se trata igual que Ctrl+C.
    signal.signal(signal.SIGTERM, _raise_interrupt)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    # v2.2: host/puerto desde RASCAL_HOST / RASCAL_PORT (o .env). Por defecto sólo loopback:
    # antes escuchaba en 0.0.0.0 y quedaba expuesto en la red sin proxy ni TLS.
    from rascal.config import get_settings

    settings = get_settings()
    host = env.get("RASCAL_HOST", settings.host)
    port = env.get("RASCAL_PORT", env.get("RASCAL_STREAMER_PORT", str(settings.port)))
    commands = {
        "streamer": [sys.executable, "-m", "uvicorn", "rascal.streaming.api:create_app", "--factory",
                     "--host", host, "--port", port, "--workers", "1", "--proxy-headers",
                     "--forwarded-allow-ips", settings.trusted_proxies],
    }
    if UI_ENTRY.exists():
        commands["ui"] = [sys.executable, "-m", "streamlit", "run", str(UI_ENTRY), "--server.port",
                          env.get("RASCAL_UI_PORT", "8501")]
    else:
        print(f"[run_all] {UI_ENTRY.relative_to(ROOT)} aún no existe: sólo se inicia el streamer.")

    processes = {name: subprocess.Popen(cmd, cwd=ROOT, env=env) for name, cmd in commands.items()}
    print("[run_all] Iniciado:", ", ".join(f"{n} (pid {p.pid})" for n, p in processes.items()))
    exit_code = 0
    try:
        while True:
            for name, proc in processes.items():
                if proc.poll() is not None:
                    print(f"[run_all] '{name}' terminó con código {proc.returncode}; deteniendo el resto.")
                    exit_code = proc.returncode or 0
                    raise KeyboardInterrupt
            time.sleep(0.5)
    except KeyboardInterrupt:
        for proc in processes.values():
            if proc.poll() is None:
                # SIGTERM = apagado ordenado en uvicorn/streamlit: el lifespan guarda el resume data
                # de libtorrent. (SIGINT puede llegar ignorado a procesos lanzados en segundo plano.)
                proc.terminate()
        deadline = time.monotonic() + 15
        for proc in processes.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
