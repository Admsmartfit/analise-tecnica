"""The desktop shortcut must never reattach to a terminal server older than the installed code."""
from __future__ import annotations

import io
import json

import pytest

from ts.app import desktop


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


@pytest.mark.parametrize(("status", "stale"), [
    ({"mode": "paper"}, True),                                     # server from before version reporting
    ({"server": {"stale": True, "started": "a", "disk": "b"}}, True),  # updated while open
    ({"server": {"stale": False, "started": "a", "disk": "a"}}, False),
])
def test_server_is_stale(monkeypatch: pytest.MonkeyPatch, status: dict, stale: bool) -> None:
    monkeypatch.setattr(desktop.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(json.dumps(status).encode("utf-8")))
    assert desktop._server_is_stale(8765) is stale


def test_unreachable_status_is_never_treated_as_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float = 0) -> None:
        raise OSError("refused")

    monkeypatch.setattr(desktop.urllib.request, "urlopen", boom)
    assert desktop._server_is_stale(8765) is False  # never kill something we cannot identify


def test_run_ui_replaces_stale_server_before_opening(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    calls: list[str] = []
    monkeypatch.setattr(desktop, "_running", lambda port: "replaced" not in calls)
    monkeypatch.setattr(desktop, "_server_is_stale", lambda port: True)
    monkeypatch.setattr(desktop, "_replace_stale_server", lambda port: calls.append("replaced") or True)
    monkeypatch.setattr(desktop, "_port_free", lambda port: False)  # stop right after the replacement step
    with pytest.raises(SystemExit):
        desktop.run_ui(tmp_path, port=8799)
    assert calls == ["replaced"]
