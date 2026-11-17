"""Desktop launcher for the terminal: local API (uvicorn) + native window (pywebview, Edge WebView2).

``python -m ts ui`` (or the "Trading System B3" shortcut, via pythonw). If pywebview is unavailable the
terminal opens in the default browser instead. Only one server runs per machine: a second launch just
opens another window on the running instance.
"""
from __future__ import annotations

import socket
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


def run_ui(root: Path | None = None, port: int = DEFAULT_PORT, window: bool = True) -> int:
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
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
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
