"""Atomic Parquet storage for OHLCV bars using pyarrow and zstd."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ts.core.errors import DataError

logger = logging.getLogger("ts.data.ohlcv_store")

# Arrow Schema matching PRD 2.4
ARROW_D1_SCHEMA = pa.schema(
    [
        ("session_date", pa.date32()),
        ("ts_utc", pa.timestamp("s", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("real_volume", pa.int64()),
        ("tick_volume", pa.int64()),
        ("spread", pa.int32()),
        ("adj_factor", pa.float64()),
        ("source", pa.string()),
        ("ingested_at", pa.timestamp("s", tz="UTC")),
    ]
)


class OhlcvStore:
    """Read and atomic upsert of daily OHLCV bars into Parquet format."""

    def __init__(self, data_dir: str | Path = "data/ohlcv/d1") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, ticker: str) -> Path:
        return self.data_dir / f"{ticker.upper()}.parquet"

    def exists(self, ticker: str) -> bool:
        return self._file_path(ticker).exists()

    def read(self, ticker: str, adjusted: bool = False) -> pd.DataFrame:
        """Read Parquet file for ticker. If adjusted=True, adds adj_open, adj_high, etc."""
        fpath = self._file_path(ticker)
        if not fpath.exists():
            return pd.DataFrame(
                columns=[
                    "session_date",
                    "ts_utc",
                    "open",
                    "high",
                    "low",
                    "close",
                    "real_volume",
                    "tick_volume",
                    "spread",
                    "adj_factor",
                    "source",
                    "ingested_at",
                ]
            )

        table = pq.read_table(fpath)
        df = table.to_pandas()

        # Ensure session_date is date objects
        df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
        df = df.sort_values(by="session_date").reset_index(drop=True)

        if adjusted:
            factor = df["adj_factor"].fillna(1.0)
            df["adj_open"] = df["open"] * factor
            df["adj_high"] = df["high"] * factor
            df["adj_low"] = df["low"] * factor
            df["adj_close"] = df["close"] * factor
            # Typically volume is divided by factor when adjusted, but real_volume stays in raw shares
            df["adj_volume"] = df["real_volume"]

        return df

    def get_latest_session_date(self, ticker: str) -> Any | None:
        """Return the most recent session_date stored for ticker, or None."""
        fpath = self._file_path(ticker)
        if not fpath.exists():
            return None
        # Quick read of session_date column
        table = pq.read_table(fpath, columns=["session_date"])
        dates = table.column("session_date").to_pylist()
        if not dates:
            return None
        return max(dates)

    def upsert(self, ticker: str, new_bars: pd.DataFrame) -> tuple[int, int]:
        """Merge new_bars with existing parquet file, saving atomically with replace retry."""
        if new_bars is None or len(new_bars) == 0:
            return 0, 0

        target_file = self._file_path(ticker)
        existing_df = self.read(ticker) if target_file.exists() else pd.DataFrame()

        clean_new = new_bars.copy()
        clean_new["session_date"] = pd.to_datetime(clean_new["session_date"]).dt.date
        clean_new = clean_new.drop_duplicates(subset=["session_date"], keep="last")

        if not existing_df.empty:
            # A bar re-delivered with identical market data keeps the stored row (and its ingested_at),
            # so re-running sync does not touch the file (PRD S1 acceptance: idempotent sync).
            old_idx = existing_df.set_index("session_date")
            new_idx = clean_new.set_index("session_date")
            common = new_idx.index.intersection(old_idx.index)
            unchanged = [
                d for d in common
                if all(
                    _same_value(old_idx.at[d, c], new_idx.at[d, c])
                    for c in _DATA_COLUMNS
                    if c in new_idx.columns and c in old_idx.columns
                )
            ]
            changed_new = clean_new[~clean_new["session_date"].isin(set(unchanged))]
            if changed_new.empty:
                return 0, len(existing_df)
            merged = pd.concat([existing_df, changed_new], ignore_index=True)
            merged = merged.drop_duplicates(subset=["session_date"], keep="last")
            new_rows = len(changed_new)
        else:
            merged = clean_new
            new_rows = len(clean_new)

        merged = merged.sort_values(by="session_date").reset_index(drop=True)

        # Ensure all columns exist and match types
        for col in ["adj_factor"]:
            if col not in merged.columns:
                merged[col] = 1.0
        for col in ["source"]:
            if col not in merged.columns:
                merged[col] = "unknown"
        for col in ["spread"]:
            if col not in merged.columns:
                merged[col] = 0

        # Floor timestamps to seconds to match Arrow timestamp[s, tz=UTC] schema
        merged["ts_utc"] = pd.to_datetime(merged["ts_utc"], utc=True).dt.floor("s")
        merged["ingested_at"] = pd.to_datetime(merged["ingested_at"], utc=True).dt.floor("s")
        merged["session_date"] = pd.to_datetime(merged["session_date"]).dt.date

        # Build PyArrow Table with custom metadata
        table = pa.Table.from_pandas(merged[ARROW_D1_SCHEMA.names], schema=ARROW_D1_SCHEMA, preserve_index=False)
        metadata = {
            b"schema_version": b"1",
            b"ticker": ticker.upper().encode("utf-8"),
            b"timeframe": b"D1",
            b"tz_server": b"America/Sao_Paulo",
        }
        table = table.replace_schema_metadata({**table.schema.metadata, **metadata} if table.schema.metadata else metadata)

        # Atomic write via temp file
        tmp_file = self.data_dir / f"{ticker.upper()}.parquet.tmp"
        try:
            pq.write_table(table, tmp_file, compression="zstd")
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise DataError(f"Failed to write temp Parquet for {ticker}: {e}") from e

        # Atomic replace with retry for Windows file locks
        replaced = False
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                os.replace(tmp_file, target_file)
                replaced = True
                break
            except PermissionError as pe:
                last_err = pe
                time.sleep(0.1 * (2**attempt))
            except Exception as e:
                last_err = e
                time.sleep(0.1)

        if not replaced:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass
            raise DataError(f"Failed to atomically write Parquet file for {ticker}: {last_err}")

        return new_rows, len(merged)


_DATA_COLUMNS = ("open", "high", "low", "close", "real_volume", "tick_volume", "spread")


def _same_value(a: Any, b: Any) -> bool:
    try:
        if pd.isna(a) and pd.isna(b):
            return True
        return bool(abs(float(a) - float(b)) <= 1e-9)
    except (TypeError, ValueError):
        return bool(a == b)
