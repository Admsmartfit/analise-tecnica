"""Central de Serviços: status/controle das tarefas agendadas, configuração local e testes de conexão.

Tudo o que antes exigia PowerShell (Start-ScheduledTask, `ts cerebro config`, testes com curl) fica acessível pelo
terminal. Só mexe em tarefas da lista ``TASKS`` (prefixo TS_) e só grava em config/settings.local.yaml (fora do git).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

TASKS: dict[str, dict[str, str]] = {
    "TS_EOD": {"label": "Fim do dia (EOD)", "when": "19:30 em dias úteis",
               "what": "Baixa cotações, processa o pregão no paper e planeja as ordens de amanhã"},
    "TS_PreOpen": {"label": "Pré-abertura", "when": "08:45 em dias úteis",
                   "what": "Reconcilia a carteira e libera as ordens do dia"},
    "TS_Watchdog": {"label": "Watchdog", "when": "a cada 15 min, 09:30–18:30",
                    "what": "Confere conexão, idade das cotações e se o terminal está vivo"},
    "TS_DeadMan": {"label": "Dead-man", "when": "20:30 em dias úteis", "what": "Avisa se o EOD não terminou"},
    "TS_Backup": {"label": "Backup", "when": "23:00", "what": "Copia o banco de dados e confere os arquivos"},
    "TS_Update": {"label": "Atualização automática", "when": "a cada poucos minutos",
                  "what": "Baixa a versão nova do GitHub, roda os testes e volta atrás se falhar"},
    "TS_Cerebro": {"label": "Webhook do Cérebro", "when": "sempre ligado (sobe no logon)",
                   "what": "Recebe as decisões do TradingAgents e reenvia pedidos/resultados pendentes"},
}
ACTIONS = {"run": "Start-ScheduledTask", "stop": "Stop-ScheduledTask",
           "enable": "Enable-ScheduledTask", "disable": "Disable-ScheduledTask"}
RESULT_PT = {0: "OK", 1: "erro (código 1)", 2: "configuração inválida", 3: "erro de conexão/execução",
             4: "dados insuficientes (falha segura)", 5: "outra rotina rodando",
             267009: "rodando agora", 267011: "ainda não rodou", 267014: "encerrada no fim do horário (normal)",
             2147750687: "já estava rodando", 2147946720: "já estava rodando (nova execução ignorada)",
             3221225786: "interrompida (Ctrl+C / fechamento)"}
OK_RESULTS = {0, 267009, 267014, 2147750687, 2147946720}
STATE_PT = {"Ready": "pronta", "Running": "rodando", "Disabled": "desativada", "Queued": "na fila",
            "Missing": "não instalada", "Unknown": "desconhecida"}


def _ps(script: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def tasks_status() -> list[dict[str, Any]]:
    """One PowerShell call for every task. Never raises (non-Windows / no scheduler -> 'Unknown')."""
    names = ",".join(f"'{n}'" for n in TASKS)
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        f"$r = foreach ($n in @({names})) {{ $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; [pscustomobject]@{ name=$n; state=\"$($t.State)\"; "
        "last=$(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('s') } else { $null }); "
        "result=[int64]$i.LastTaskResult; "
        "next=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null }) } } "
        "else { [pscustomobject]@{ name=$n; state='Missing'; last=$null; result=$null; next=$null } } }; "
        "$r | ConvertTo-Json -Compress"
    )
    try:
        proc = _ps(script)
        rows = json.loads(proc.stdout.strip() or "[]")
        rows = rows if isinstance(rows, list) else [rows]
    except (OSError, subprocess.SubprocessError, ValueError):
        rows = [{"name": n, "state": "Unknown", "last": None, "result": None, "next": None} for n in TASKS]
    out = []
    for r in rows:
        meta = TASKS.get(r.get("name"), {})
        res = r.get("result")
        if r.get("name") == "TS_DeadMan" and res == 1:
            r = {**r, "result": res}
            meta = {**meta, "_deadman_alert": "1"}
        out.append({**r, **meta, "state_pt": STATE_PT.get(r.get("state"), r.get("state")),
                    "result_pt": None if res is None or r.get("last") is None
                    else "avisou que o EOD não terminou" if meta.get("_deadman_alert")
                    else RESULT_PT.get(res, f"código {res}"),
                    "ok": res in OK_RESULTS if r.get("last") else None})
    return out


def task_action(name: str, action: str) -> tuple[bool, str]:
    if name not in TASKS or action not in ACTIONS:
        return False, "Serviço ou ação desconhecidos"
    script = f"{ACTIONS[action]} -TaskName '{name}' -ErrorAction Stop | Out-Null"
    if name == "TS_Cerebro" and action in ("stop", "disable"):
        # the task runs pythonw directly; make sure the listener process is really gone
        script += ("; Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                   "Where-Object { $_.CommandLine -match 'cerebro serve' } | "
                   "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        p = _ps(script)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Falha ao executar: {e}"
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        return False, (err[0] if err else f"código {p.returncode}")[:300]
    verb = {"run": "iniciado", "stop": "parado", "enable": "ativado", "disable": "desativado"}[action]
    return True, f"{TASKS[name]['label']}: {verb}."


def restart_task(name: str) -> tuple[bool, str]:
    task_action(name, "stop")
    ok, msg = task_action(name, "run")
    return ok, (f"{TASKS[name]['label']}: reiniciado." if ok else msg)


def register_tasks(root: Path) -> tuple[bool, str]:
    script = root / "deploy" / "scripts" / "register_tasks.ps1"
    if not script.exists():
        return False, f"Script não encontrado: {script}"
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                            "-Root", str(root)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Falha ao registrar: {e}"
    if p.returncode != 0:
        return False, ((p.stderr or p.stdout).strip().splitlines() or ["erro"])[-1][:300]
    return True, "Tarefas agendadas registradas/atualizadas."


# ------------------------------------------------------------------------------------------- local settings
def write_local(config_dir: Path, section: str, updates: dict[str, Any]) -> Path:
    """Merge ``updates`` into config/settings.local.yaml[section] (git-ignored, survives updates)."""
    p = config_dir / "settings.local.yaml"
    data = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}
    data.setdefault(section, {}).update({k: v for k, v in updates.items() if v is not None})
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


CEREBRO_EDITABLE = ("enabled", "mode", "base_url", "webhook_port", "min_conviccao", "allow_contingency",
                    "send_feedback", "demand_priority")


def save_cerebro(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Validate (pydantic) and persist only the editable Cérebro fields. Raises ValueError on invalid input."""
    from pydantic import ValidationError

    from ts.core.config import CerebroSettings, load_settings

    clean = {k: updates[k] for k in CEREBRO_EDITABLE if k in updates}
    if "base_url" in clean:
        u = str(clean["base_url"]).strip().rstrip("/")
        if not (u.startswith("http://") or u.startswith("https://")):
            raise ValueError("O endereço do Cérebro deve começar com http:// (ex.: http://localhost:8000)")
        clean["base_url"] = u
    current = load_settings(root / "config" / "settings.yaml").cerebro.model_dump()
    try:
        CerebroSettings(**{**current, **clean})
    except ValidationError as e:
        raise ValueError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
    write_local(root / "config", "cerebro", clean)
    return clean


# ------------------------------------------------------------------------------------------- one-click tests
def test_cerebro_reach(base_url: str) -> tuple[bool, str]:
    import httpx

    try:
        r = httpx.get(base_url.rstrip("/") + "/openapi.json", timeout=4)
        title = r.json().get("info", {}).get("title") if r.status_code == 200 else None
        if title:
            return True, f"Cérebro respondendo em {base_url} ({title})"
        return False, f"{base_url} respondeu HTTP {r.status_code}, mas não parece o TradingAgents"
    except (httpx.HTTPError, ValueError) as e:
        return False, f"Cérebro não responde em {base_url}: {type(e).__name__}. Ele está aberto?"


def test_webhook(cfg: Any) -> tuple[bool, str]:
    import httpx

    host = "127.0.0.1" if cfg.webhook_host in ("0.0.0.0", "") else cfg.webhook_host
    url = f"http://{host}:{cfg.webhook_port}{cfg.webhook_path}"
    try:
        r = httpx.post(url, json={"tipo": "ping_teste", "origem": "Central de Serviços", "mensagem": "teste"},
                       timeout=4)
        if r.status_code == 200 and r.json().get("recebido"):
            return True, f"Webhook respondeu ao ping em {r.elapsed.total_seconds() * 1000:.0f} ms ({url})"
        return False, f"Webhook respondeu HTTP {r.status_code} em {url}"
    except (httpx.HTTPError, ValueError) as e:
        return False, f"Webhook não responde em {url} ({type(e).__name__}). Inicie o serviço 'Webhook do Cérebro'."


def test_telegram(settings: Any) -> tuple[bool, str]:
    if not settings.telegram.enabled:
        return False, "Telegram desligado (telegram.enabled=false); as notificações vão para o arquivo de log."
    try:
        from ts.app.notify import send_telegram

        send_telegram("✅ Teste da Central de Serviços do Trading System B3")
        return True, "Mensagem de teste enviada ao Telegram. Confira no celular."
    except Exception as e:  # noqa: BLE001
        return False, f"Falha ao enviar: {e}"


def test_data_source(settings: Any) -> tuple[bool, str]:
    try:
        from ts.data.yahoo_gateway import make_gateway

        gw = make_gateway(settings)
        gw.connect()
        try:
            h = gw.health()
        finally:
            gw.shutdown()
        ok = bool(h.ok and h.connected)
        src = "Yahoo Finance" if settings.data.provider == "yahoo" else "MetaTrader 5"
        parts = [f"{src}: {'conectado' if ok else 'sem conexão'}"]
        if h.ping_ms is not None:
            parts.append(f"resposta em {h.ping_ms:.0f} ms")
        if h.tick_age_seconds is not None:
            parts.append(f"última cotação do IBOV há {h.tick_age_seconds / 60:.0f} min")
        if h.details.get("error"):
            parts.append(str(h.details["error"])[:200])
        return ok, " · ".join(parts)
    except Exception as e:  # noqa: BLE001
        return False, f"Fonte de cotações indisponível: {e}"


def python_exe(root: Path) -> str:
    venv = root / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable
