"""Installed vs available version (git) and the in-app "update now" action used by the dashboard and CLI."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # src/ts/app/version.py -> repository root

UPDATE_EXIT_CODES = {
    0: "Atualizado (ou já estava na última versão).",
    1: "Atualização abortada: há arquivos versionados modificados nesta máquina.",
    2: "A nova versão falhou nos testes: o sistema foi mantido na versão anterior (rollback).",
    3: "Erro durante a atualização (veja logs/update-*.log).",
    4: "Uma rotina (EOD/preopen/backup) está rodando agora. Tente de novo em alguns minutos.",
}


def _git(args: list[str], root: Path = ROOT, timeout: float = 30.0) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out.stdout.strip()


@dataclass
class VersionInfo:
    installed: str | None
    installed_date: str | None
    available: str | None
    branch: str
    pending: list[str]
    error: str | None = None
    modified: list[str] | None = None  # tracked files changed on this machine (update.ps1 refuses to run)

    @property
    def update_available(self) -> bool:
        return bool(self.installed and self.available and self.installed != self.available)


def check_version(root: Path = ROOT, branch: str = "main", fetch: bool = True) -> VersionInfo:
    """Compare HEAD with origin/<branch>. ``pending`` lists the commit subjects that would be installed."""
    try:
        head = _git(["rev-parse", "HEAD"], root)
        head_date = _git(["log", "-1", "--format=%cd", "--date=format:%d/%m/%Y %H:%M"], root)
        if fetch:
            _git(["fetch", "--quiet", "origin", branch], root, timeout=60)
        remote = _git(["rev-parse", f"origin/{branch}"], root)
        pending = _git(["log", "--format=%h %s", f"HEAD..origin/{branch}"], root).splitlines() if remote != head else []
        modified = [ln[3:] for ln in _git(["status", "--porcelain", "--untracked-files=no"], root).splitlines() if ln.strip()]
        return VersionInfo(head, head_date, remote, branch, pending, modified=modified)
    except (OSError, subprocess.SubprocessError) as e:
        return VersionInfo(None, None, None, branch, [], error=str(e))


def run_update(root: Path = ROOT, branch: str = "main", timeout: float = 1200.0) -> tuple[int, str]:
    """Run deploy/scripts/update.ps1 now (ignoring the time window; still honours the routines' lock)."""
    script = root / "deploy" / "scripts" / "update.ps1"
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-Root", str(root), "-Branch", branch, "-Force"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, UPDATE_EXIT_CODES.get(proc.returncode, f"Código {proc.returncode}")
