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
UI_ENTRY = ROOT / "src" / "rascal" / "ui" / "app.py"


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    # SIGTERM (systemd, docker stop, kill) se trata igual que Ctrl+C.
    signal.signal(signal.SIGTERM, _raise_interrupt)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    port = env.get("RASCAL_STREAMER_PORT", "8001")
    commands = {
        "streamer": [sys.executable, "-m", "uvicorn", "rascal.streaming.api:create_app", "--factory",
                     "--host", "0.0.0.0", "--port", port, "--workers", "1"],
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
