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

_WARNED: set[str] = set()
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


B3_PORTFOLIO_URL = "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/{query}"


def fetch_b3_index_portfolio(index: str = "IBXX", client: object | None = None) -> pd.DataFrame:
    """Current theoretical portfolio of a B3 index (official B3 listing service; IBXX = IBX-100)."""
    import base64
    import json

    import httpx

    q = base64.b64encode(json.dumps(
        {"language": "pt-br", "pageNumber": 1, "pageSize": 200, "index": index, "segment": "1"}
    ).encode()).decode()
    http = client or httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0 ts-b3-swing"})
    try:
        r = http.get(B3_PORTFOLIO_URL.format(query=q))  # type: ignore[attr-defined]
        r.raise_for_status()
        body = r.json()
    finally:
        if client is None:
            http.close()  # type: ignore[attr-defined]
    rows = body.get("results") or []
    if len(rows) < 50:
        raise ValueError(f"B3 returned only {len(rows)} constituents for {index}")
    df = pd.DataFrame({
        "ticker": [str(x["cod"]).strip().upper() for x in rows],
        "company_name": [str(x.get("asset", "")).strip() for x in rows],
        "type": [" ".join(str(x.get("type", "")).split()) for x in rows],
        "weight_pct": [float(str(x.get("part", "0")).replace(".", "").replace(",", ".")) for x in rows],
    })
    df.attrs["b3_date"] = (body.get("header") or {}).get("date")
    return df.sort_values("ticker").reset_index(drop=True)


def update_universe_file(d: date, universe_dir: str | Path | None = None, client: object | None = None,
                         overwrite: bool = False) -> Path | None:
    """Write ``ibx100_<quarter of d>.csv`` from the current B3 portfolio. Returns the path (None if kept)."""
    udir = _resolve_dir(universe_dir)
    udir.mkdir(parents=True, exist_ok=True)
    target = udir / f"ibx100_{get_quarter_str(d)}.csv"
    if target.exists() and not overwrite:
        return None
    df = fetch_b3_index_portfolio("IBXX", client)
    # LF line endings, same as the repository: a CRLF copy shows up as "modified" and blocks the auto-update
    df.to_csv(target, index=False, encoding="utf-8", lineterminator="\n")
    logger.info("Universe %s written from B3 (%d tickers, B3 date %s)", target.name, len(df), df.attrs.get("b3_date"))
    return target


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
        if target_file.name not in _WARNED:  # once per run: a backtest calls this for every session
            _WARNED.add(target_file.name)
            logger.warning(
                "No universe composition on or before %s; using earliest file %s for earlier dates "
                "(survivorship risk in backtest)", d, target_file.name,
            )

    df = pd.read_csv(target_file)
    return sorted({str(t).strip().upper() for t in df["ticker"].dropna().tolist()})
