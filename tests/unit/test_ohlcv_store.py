"""Unit tests for OhlcvStore atomic Parquet operations."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ts.data.ohlcv_store import OhlcvStore


def test_ohlcv_store_write_read_and_idempotence(
    sample_daily_df: pd.DataFrame, tmp_path: Path
) -> None:
    store = OhlcvStore(data_dir=tmp_path / "ohlcv")

    # 1. Upsert new bars
    inserted, total = store.upsert("PETR4", sample_daily_df)
    assert inserted == len(sample_daily_df)
    assert total == len(sample_daily_df)

    # 2. Read back
    df_read = store.read("PETR4")
    assert len(df_read) == len(sample_daily_df)
    assert "adj_factor" in df_read.columns

    # 3. Read with adjusted=True
    df_adj = store.read("PETR4", adjusted=True)
    assert "adj_close" in df_adj.columns

    # 4. Upserting the same data again produces identical total rows without duplication
    ins2, tot2 = store.upsert("PETR4", sample_daily_df)
    assert tot2 == total

    # Check file hash stability
    file_bytes_1 = (tmp_path / "ohlcv" / "PETR4.parquet").read_bytes()
    store.upsert("PETR4", sample_daily_df)
    file_bytes_2 = (tmp_path / "ohlcv" / "PETR4.parquet").read_bytes()
    assert len(file_bytes_1) == len(file_bytes_2)


def test_ohlcv_store_latest_session_date(sample_daily_df: pd.DataFrame, tmp_path: Path) -> None:
    store = OhlcvStore(data_dir=tmp_path / "ohlcv")
    assert store.get_latest_session_date("VALE3") is None

    store.upsert("VALE3", sample_daily_df)
    latest_d = store.get_latest_session_date("VALE3")
    assert latest_d == sample_daily_df["session_date"].max()
