"""Market data synchronization module with incremental updates and fault tolerance."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date

from ts.core.calendar import B3Calendar, get_calendar
from ts.core.config import Settings, compute_normalized_yaml_hash, load_settings
from ts.core.errors import DataUnavailableError
from ts.data.mt5_gateway import MT5Gateway
from ts.data.ohlcv_store import OhlcvStore
from ts.data.universe import universe_on
from ts.data.validation import DataValidator
from ts.persistence.db import DatabaseManager

logger = logging.getLogger("ts.data.sync")


@dataclass
class SyncReport:
    run_id: str
    session_date: date
    status: str  # OK, FAILED, SKIPPED_HOLIDAY
    total_tickers: int
    successful_tickers: list[str] = field(default_factory=list)
    failed_tickers: list[tuple[str, str]] = field(default_factory=list)
    blocked_tickers: list[tuple[str, str]] = field(default_factory=list)
    exit_code: int = 0


def sync_universe(
    asof: date,
    tickers: list[str] | None = None,
    full: bool = False,
    gateway: MT5Gateway | None = None,
    store: OhlcvStore | None = None,
    validator: DataValidator | None = None,
    db: DatabaseManager | None = None,
    calendar: B3Calendar | None = None,
    settings: Settings | None = None,
) -> SyncReport:
    """Synchronize D1 bars incrementally for universe and IBOV up to asof."""
    settings = settings or load_settings()
    calendar = calendar or get_calendar()
    store = store or OhlcvStore(data_dir=f"{settings.system.data_dir}/ohlcv/d1")
    validator = validator or DataValidator(calendar=calendar)
    db = db or DatabaseManager(db_path=settings.system.db_path)

    run_id = f"sync_{asof.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"

    # Check holiday
    if not calendar.is_session(asof):
        logger.info(f"{asof} is a holiday or weekend in B3 calendar. Skipping sync.")
        db.record_run(run_id, "sync", asof.isoformat(), "holiday", "0.1.0")
        db.update_run(run_id, "SKIPPED_HOLIDAY")
        return SyncReport(
            run_id=run_id,
            session_date=asof,
            status="SKIPPED_HOLIDAY",
            total_tickers=0,
            exit_code=0,
        )

    # Determine universe
    ibov_symbol = settings.mt5.ibov_symbol.upper()
    if tickers is None or len(tickers) == 0:
        active_universe = universe_on(asof)
    else:
        active_universe = [t.strip().upper() for t in tickers]

    # Always include IBOV
    target_set = set(active_universe)
    target_set.add(ibov_symbol)
    target_tickers = sorted(target_set)

    cfg_hash = compute_normalized_yaml_hash(settings.model_dump())
    db.record_run(run_id, "sync", asof.isoformat(), cfg_hash, "0.1.0")

    success: list[str] = []
    failed: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []

    # Connect gateway if not already connected
    owns_gateway = False
    if gateway is None:
        gateway = MT5Gateway(settings=settings)
        gateway.connect()
        owns_gateway = True

    try:
        for ticker in target_tickers:
            # 1. Determine start date
            if full:
                start_date = date(2019, 1, 1)
            else:
                last_d = store.get_latest_session_date(ticker)
                if last_d is None:
                    start_date = date(2019, 1, 1)
                else:
                    # Overlap 5 sessions to capture retroactive corporate action adjustments
                    curr_d = last_d
                    for _ in range(5):
                        try:
                            curr_d = calendar.previous_session(curr_d)
                        except Exception:
                            break
                    start_date = curr_d

            # 2. Fetch bars from gateway
            try:
                df = gateway.daily_bars(ticker, start_date, asof)
            except DataUnavailableError as due:
                logger.warning(f"Data unavailable for {ticker}: {due}")
                failed.append((ticker, str(due)))
                db.save_quality_issue(asof.isoformat(), ticker, "data_unavailable", "WARN", str(due))
                continue
            except Exception as ex:
                logger.error(f"Error fetching bars for {ticker}: {ex}")
                failed.append((ticker, str(ex)))
                db.save_quality_issue(asof.isoformat(), ticker, "fetch_exception", "WARN", str(ex))
                continue

            # 3. Validation
            issues = validator.validate_bars(ticker, df, asof=asof, check_calendar=True)
            for iss in issues:
                db.save_quality_issue(
                    iss.session_date.isoformat(),
                    iss.ticker,
                    iss.check_name,
                    iss.severity,
                    iss.detail,
                )

            if validator.has_blocking_issues(issues):
                block_details = "; ".join([i.detail for i in issues if i.severity == "BLOCK"])
                logger.error(f"Blocking data quality issues in {ticker}: {block_details}")
                blocked.append((ticker, block_details))
                continue

            # 4. Upsert into Parquet store
            try:
                store.upsert(ticker, df)
                success.append(ticker)
            except Exception as e:
                logger.error(f"Failed to upsert Parquet for {ticker}: {e}")
                failed.append((ticker, str(e)))

    finally:
        if owns_gateway:
            gateway.shutdown()

    # Determine overall status: 95% universe + IBOV
    total = len(target_tickers)
    success_rate = len(success) / total if total > 0 else 0.0
    ibov_ok = ibov_symbol in success

    if success_rate >= 0.95 and ibov_ok:
        status = "OK"
        exit_code = 0
    else:
        status = "FAILED"
        exit_code = 4

    error_msg = None
    if status == "FAILED":
        error_msg = (
            f"Sync finished with success_rate={success_rate:.1%}, ibov_ok={ibov_ok}. "
            f"Failed: {len(failed)}, Blocked: {len(blocked)}"
        )
    db.update_run(run_id, status, error=error_msg)

    return SyncReport(
        run_id=run_id,
        session_date=asof,
        status=status,
        total_tickers=total,
        successful_tickers=success,
        failed_tickers=failed,
        blocked_tickers=blocked,
        exit_code=exit_code,
    )
