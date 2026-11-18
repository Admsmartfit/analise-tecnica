"""Desktop launcher for the terminal: local API (uvicorn) + native window (pywebview, Edge WebView2).

``python -m ts ui`` (or the "Trading System B3" shortcut, via pythonw). If pywebview is unavailable the
terminal opens in the default browser instead. Only one server runs per machine: a second launch just
opens another window on the running instance.
"""
from __future__ import annotations

import socket
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


def run_ui(root: Path | None = None, port: int = DEFAULT_PORT, window: bool = True) -> int:
    ensure_streams(root or Path.cwd())
    url = f"http://127.0.0.1:{port}/"
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
