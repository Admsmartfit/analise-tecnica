"""Desktop launcher for the terminal: local API (uvicorn) + native window (pywebview, Edge WebView2).

``python -m ts ui`` (or the "Trading System B3" shortcut, via pythonw). If pywebview is unavailable the
terminal opens in the default browser instead. Only one server runs per machine: a second launch just
opens another window on the running instance.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8765


def _running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return bool(r.status == 200)
    except OSError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _open_window(url: str) -> bool:
    try:
        import webview
    except ImportError:
        return False
    try:
        webview.create_window("Trading System B3", url, width=1520, height=920, min_size=(1100, 680),
                              background_color="#16191e")
        webview.start()
        return True
    except Exception:  # noqa: BLE001 - WebView2 missing or broken: fall back to the browser
        return False


def ensure_streams(root: Path) -> Path | None:
    """Under ``pythonw`` (the desktop shortcut) sys.stdout/sys.stderr are None and uvicorn's logging setup
    crashes before the window opens ("Unable to configure formatter 'default'"). Send them to logs/ui.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log = root / "logs" / "ui.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the whole process
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return log


def _server_is_stale(port: int) -> bool:
    """True when the terminal already running on ``port`` is older than the code on disk.

    An old server (from before version reporting existed) has no ``server`` key in /api/status.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=15) as r:
            st = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return False  # cannot tell: keep it (never kill something we do not understand)
    srv = st.get("server")
    return srv is None or bool(srv.get("stale"))


def _listening_pid(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == f"127.0.0.1:{port}" and parts[3].upper() in ("LISTENING", "OUVINDO"):
            return int(parts[4]) if parts[4].isdigit() else None
    return None


def _replace_stale_server(port: int) -> bool:
    """Stop an outdated terminal server so a fresh one can start. Only ever touches a python process."""
    pid = _listening_pid(port)
    if pid is None:
        return False
    try:
        info = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                              text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        if "python" not in info.lower():
            return False
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(50):
        if _port_free(port):
            return True
        time.sleep(0.2)
    return False


def run_ui(root: Path | None = None, port: int = DEFAULT_PORT, window: bool = True, restart: bool = False) -> int:
    ensure_streams(root or Path.cwd())
    url = f"http://127.0.0.1:{port}/"
    if not restart and _running(port) and _server_is_stale(port):
        # e.g. a terminal left running in the background while the automatic update installed a new version:
        # reusing it would show the new screen on top of the old engine (things silently missing)
        _replace_stale_server(port)
    if restart:
        # launched by the old instance right before it exits (after an update): wait for it to release the port,
        # otherwise we would just open another window on the OLD server
        for _ in range(100):
            if not _running(port) and _port_free(port):
                break
            time.sleep(0.2)
    if _running(port):
        if not (window and _open_window(url)):
            webbrowser.open(url)
        return 0
    if not _port_free(port):
        raise SystemExit(f"Porta {port} ocupada por outro programa")

    import uvicorn

    from ts.app.api import create_app

    app = create_app(root or Path.cwd())
    # log_config=None: keep the app's own logging (uvicorn's colour formatter probes the console)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                                           log_config=None))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    try:
        if not (window and _open_window(url)):
            webbrowser.open(url)
            t.join()  # browser mode: keep serving until the process is stopped
    finally:
        server.should_exit = True
    return 0
