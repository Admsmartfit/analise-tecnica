"""Data files the system writes on the production machine must not block the auto-update."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ts.app.version import AUTO_DATA

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=root, check=True, capture_output=True)


def test_auto_data_pattern() -> None:
    assert AUTO_DATA.match("config/universe/ibx100_2026_Q4.csv")
    assert AUTO_DATA.match("config/corporate_actions.csv")
    assert not AUTO_DATA.match("config/strategy.yaml")
    assert not AUTO_DATA.match("config/universe/sub/ibx100_2026_Q4.csv")
    assert not AUTO_DATA.match("src/ts/app/api.py")


def test_crlf_rewrite_of_data_file_is_not_a_blocking_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "config" / "universe").mkdir(parents=True)
    _git(repo, "init", "-q")
    shutil.copy(Path(__file__).resolve().parents[2] / ".gitattributes", repo / ".gitattributes")
    csv = repo / "config" / "universe" / "ibx100_2026_Q4.csv"
    csv.write_bytes(b"ticker\nPETR4\nVALE3\n")
    (repo / "config" / "strategy.yaml").write_text("a: 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
    csv.write_bytes(b"ticker\r\nPETR4\r\nVALE3\r\n")  # what the old writer produced on Windows
    # git still flags the CRLF copy as "M" in `status` (stat mismatch) although the content is identical, which is
    # why update.ps1 must treat these data files itself; .gitattributes only guarantees there is no content change.
    diff = subprocess.run(["git", "-c", "core.autocrlf=false", "diff", "--name-only"],
                          cwd=repo, capture_output=True, text=True, check=True).stdout
    assert diff.strip() == ""
    csv.write_bytes(b"ticker\nPETR4\nITUB4\n")  # real content change (new B3 composition)
    (repo / "config" / "strategy.yaml").write_text("a: 2\n", encoding="utf-8")
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout
    changed = [ln[3:] for ln in out.splitlines() if ln.strip()]
    assert sorted(changed) == ["config/strategy.yaml", "config/universe/ibx100_2026_Q4.csv"]
    assert [f for f in changed if AUTO_DATA.match(f)] == ["config/universe/ibx100_2026_Q4.csv"]


def test_writers_use_lf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    from ts.data import universe

    monkeypatch.setattr(universe, "fetch_b3_index_portfolio", lambda code, client=None: pd.DataFrame({"ticker": ["PETR4", "VALE3"]}))
    from datetime import date

    p = universe.update_universe_file(date(2026, 11, 23), tmp_path)
    assert p is not None and b"\r\n" not in p.read_bytes()
