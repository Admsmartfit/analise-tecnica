"""Version check / in-app update (dashboard button) against real temporary git repositories."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ts.app.version import UPDATE_EXIT_CODES, check_version, run_update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    origin, dev, inst = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "inst"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(dev)], check=True)
    (dev / "a.txt").write_text("1", encoding="utf-8")
    git(dev, "add", "-A")
    git(dev, "commit", "-qm", "v1")
    git(dev, "remote", "add", "origin", str(origin))
    git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(inst)], check=True)
    return dev, inst


def test_up_to_date_then_update_available(repos: tuple[Path, Path]) -> None:
    dev, inst = repos
    v = check_version(inst)
    assert v.error is None and not v.update_available and v.pending == []
    (dev / "a.txt").write_text("2", encoding="utf-8")
    git(dev, "commit", "-qam", "Nova regra de saída")
    git(dev, "push", "-q", "origin", "main")
    v = check_version(inst)
    assert v.update_available and v.pending and v.pending[0].endswith("Nova regra de saída")


def test_check_version_reports_errors(tmp_path: Path) -> None:
    v = check_version(tmp_path)  # not a git repository
    assert v.error and not v.update_available


@pytest.mark.skipif(sys.platform != "win32", reason="update.ps1 is Windows-only")
@pytest.mark.parametrize("code", [0, 2, 4])
def test_run_update_maps_exit_codes(tmp_path: Path, code: int) -> None:
    script = tmp_path / "deploy" / "scripts" / "update.ps1"
    script.parent.mkdir(parents=True)
    script.write_text(f"param($Root, $Branch, [switch]$Force)\nif (-not $Force) {{ exit 99 }}\nexit {code}\n", encoding="utf-8")
    rc, msg = run_update(tmp_path)
    assert rc == code and msg == UPDATE_EXIT_CODES[code]
