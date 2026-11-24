"""Central de Serviços: overview, task control (whitelisted), Cérebro settings form and one-click tests."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ts.app import services as sv  # noqa: E402
from ts.app.api import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
H = {"X-TS-Token": "tok"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, list]:
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("settings.local.yaml"))
    calls: list = []
    monkeypatch.setattr(sv, "tasks_status", lambda: [
        {"name": "TS_EOD", "state": "Ready", "state_pt": "pronta", "last": "2026-11-23T19:31:31", "result": 4,
         "result_pt": "dados insuficientes (falha segura)", "ok": False, "next": "2026-11-24T19:30:00", **sv.TASKS["TS_EOD"]},
        {"name": "TS_Cerebro", "state": "Running", "state_pt": "rodando", "last": None, "result": None, "result_pt": None,
         "ok": None, "next": None, **sv.TASKS["TS_Cerebro"]}])
    monkeypatch.setattr(sv, "restart_task", lambda name: (calls.append(("restart", name)) or (True, "reiniciado")))
    real_action = sv.task_action

    def fake_action(name: str, action: str) -> tuple[bool, str]:
        if name not in sv.TASKS or action not in sv.ACTIONS:  # keep the whitelist check of the real function
            return real_action(name, action)
        calls.append((action, name))
        return True, f"{name} {action}"

    monkeypatch.setattr(sv, "task_action", fake_action)
    client = TestClient(create_app(tmp_path, token="tok"))
    return client, tmp_path, calls


def test_overview(env: tuple) -> None:
    client, root, _ = env
    d = client.get("/api/services").json()
    assert [t["name"] for t in d["tasks"]] == ["TS_EOD", "TS_Cerebro"]
    assert d["cerebro"]["config"]["mode"] == "required" and d["root"] == str(root)


def test_task_control_requires_token_and_whitelist(env: tuple) -> None:
    client, _, calls = env
    assert client.post("/api/services/task?name=TS_EOD&action=run").status_code == 403
    assert client.post("/api/services/task?name=TS_EOD&action=run", headers=H).json()["ok"] is True
    assert calls == [("run", "TS_EOD")]
    r = client.post("/api/services/task?name=Malware&action=run", headers=H).json()
    assert r["ok"] is False and len(calls) == 1  # never forwarded to PowerShell
    assert client.post("/api/services/task?name=TS_EOD&action=format-c", headers=H).json()["ok"] is False


def test_save_cerebro_settings_from_the_form(env: tuple) -> None:
    client, root, calls = env
    body = {"enabled": True, "mode": "advisory", "base_url": "http://localhost:8000/", "webhook_port": 5001,
            "min_conviccao": 0.7, "allow_contingency": False, "send_feedback": True, "demand_priority": "alta",
            "webhook_host": "0.0.0.0"}  # not editable from the form -> ignored
    assert client.post("/api/settings/cerebro", json=body).status_code == 403
    r = client.post("/api/settings/cerebro", json=body, headers=H).json()
    assert r["ok"] and r["config"]["mode"] == "advisory" and r["config"]["base_url"] == "http://localhost:8000"
    assert ("restart", "TS_Cerebro") in calls  # applied right away
    local = yaml.safe_load((root / "config" / "settings.local.yaml").read_text(encoding="utf-8"))
    assert local["cerebro"]["mode"] == "advisory" and "webhook_host" not in local["cerebro"]
    assert client.get("/api/services").json()["cerebro"]["config"]["webhook_port"] == 5001  # settings reloaded
    bad = client.post("/api/settings/cerebro", json={"base_url": "localhost:8000"}, headers=H)
    assert bad.status_code == 400 and "http://" in bad.json()["detail"]
    bad = client.post("/api/settings/cerebro", json={"min_conviccao": 3}, headers=H)
    assert bad.status_code == 400
    r = client.post("/api/settings/cerebro", json={"enabled": False}, headers=H).json()
    assert ("stop", "TS_Cerebro") in calls and "desligada" in r["message"]


def test_one_click_tests(env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = env
    monkeypatch.setattr(sv, "test_webhook", lambda cfg: (True, f"ping ok na porta {cfg.webhook_port}"))
    r = client.post("/api/services/test?kind=webhook", headers=H).json()
    assert r == {"ok": True, "message": "ping ok na porta 5000"}
    assert client.post("/api/services/test?kind=rm", headers=H).status_code == 404
    assert client.post("/api/services/test?kind=webhook").status_code == 403


def test_cerebro_reach_message_when_down() -> None:
    ok, msg = sv.test_cerebro_reach("http://127.0.0.1:1")  # nothing listens on port 1
    assert ok is False and "não responde" in msg


def test_static_services_module_is_served(env: tuple) -> None:
    client, _, _ = env
    r = client.get("/static/services.js")
    assert r.status_code == 200 and "setupServices" in r.text
    assert 'id="dlg-services"' in client.get("/").text
