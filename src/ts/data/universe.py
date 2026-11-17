"""IBX-100 historical universe selector (PRD 1.1.5 survivorship bias, Sprint 1 passo 11).

Files: ``config/universe/ibx100_YYYY_Qn.csv`` with a ``ticker`` column. A file is valid from the first day
of its quarter until the next available file starts. For a date ``d`` we use the most recent file whose
period starts on or before ``d``; using a *later* composition for a historical date would leak survivorship
bias into the backtest, so dates before the first file only fall back to it with a warning.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger("ts.data.universe")

_FILE_RE = re.compile(r"ibx100_(\d{4})_Q([1-4])\.csv$", re.IGNORECASE)


def get_quarter_str(d: date) -> str:
    """Return YYYY_Qn representation for date d."""
    quarter = (d.month - 1) // 3 + 1
    return f"{d.year}_Q{quarter}"


def _period_start(path: Path) -> date | None:
    m = _FILE_RE.search(path.name)
    if not m:
        return None
    year, q = int(m.group(1)), int(m.group(2))
    return date(year, 3 * (q - 1) + 1, 1)


def _resolve_dir(universe_dir: str | Path | None) -> Path:
    if universe_dir is not None:
        return Path(universe_dir)
    candidate = Path("config/universe")
    if not candidate.exists():
        candidate = Path(__file__).resolve().parent.parent.parent.parent / "config" / "universe"
    return candidate


def universe_on(d: date, universe_dir: str | Path | None = None) -> list[str]:
    """Return the IBX-100 tickers in force on date ``d``."""
    udir = _resolve_dir(universe_dir)
    dated = sorted(
        ((start, f) for f in udir.glob("ibx100_*.csv") if (start := _period_start(f)) is not None),
        key=lambda x: x[0],
    )
    if not dated:
        raise FileNotFoundError(f"No universe files (ibx100_YYYY_Qn.csv) found in {udir}")

    eligible = [f for start, f in dated if start <= d]
    if eligible:
        target_file = eligible[-1]
    else:
        target_file = dated[0][1]
        logger.warning(
            "No universe composition on or before %s; using earliest file %s (survivorship risk in backtest)",
            d, target_file.name,
        )

    df = pd.read_csv(target_file)
    return sorted({str(t).strip().upper() for t in df["ticker"].dropna().tolist()})
