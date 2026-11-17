"""SQLite database connection, migration runner and persistence helpers."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("ts.persistence.db")


def get_connection(
    db_path: str | Path = "data/db/trading.db", readonly: bool = False
) -> sqlite3.Connection:
    """Open SQLite connection configured with robust WAL pragmas."""
    db_p = Path(db_path)
    if not readonly and str(db_path) != ":memory:":
        db_p.parent.mkdir(parents=True, exist_ok=True)

    uri = f"file:{db_p.resolve()}?mode=ro" if readonly and str(db_path) != ":memory:" else str(db_p)
    conn = sqlite3.connect(uri, uri=readonly and str(db_path) != ":memory:", timeout=5.0)
    conn.row_factory = sqlite3.Row

    # Apply mandatory PRAGMAs
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: str | Path | None = None) -> None:
    """Execute pending SQL migration files in numerical order."""
    if migrations_dir is None:
        migrations_dir = Path(__file__).parent / "migrations"
    else:
        migrations_dir = Path(migrations_dir)

    # Ensure schema_migrations table exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    conn.commit()

    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_migrations;")
    applied_versions = {row[0] for row in cur.fetchall()}

    migration_files = sorted(migrations_dir.glob("*.sql"))
    for f in migration_files:
        version_num = int(f.name.split("_")[0])
        if version_num not in applied_versions:
            logger.info(f"Applying migration {f.name}...")
            with open(f, encoding="utf-8") as sql_file:
                sql_script = sql_file.read()
            conn.executescript(sql_script)
            now_iso = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                (version_num, now_iso),
            )
            conn.commit()
            logger.info(f"Migration {f.name} applied successfully.")


def backup_database(conn: sqlite3.Connection, backup_path: str | Path) -> None:
    """Perform atomic online backup of SQLite database using sqlite3 backup API."""
    dest_path = Path(backup_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_conn = sqlite3.connect(dest_path)
    try:
        with dest_conn:
            conn.backup(dest_conn, pages=100)
        logger.info(f"Database successfully backed up to {dest_path}")
    finally:
        dest_conn.close()


class DatabaseManager:
    """Higher level persistence manager for system runs, signals, and audits."""

    def __init__(self, db_path: str | Path = "data/db/trading.db") -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_connection(self.db_path)
            apply_migrations(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def record_run(
        self,
        run_id: str,
        routine: str,
        session_date: str,
        config_hash: str,
        code_version: str = "0.1.0",
    ) -> None:
        conn = self.get_conn()
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO runs (run_id, routine, session_date, started_at, status, config_hash, code_version)
            VALUES (?, ?, ?, ?, 'RUNNING', ?, ?);
            """,
            (run_id, routine, session_date, now_iso, config_hash, code_version),
        )
        conn.commit()

    def update_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        conn = self.get_conn()
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = ?, error = ?
            WHERE run_id = ?;
            """,
            (status, now_iso, error, run_id),
        )
        conn.commit()

    def save_quality_issue(
        self,
        session_date: str,
        ticker: str,
        check_name: str,
        severity: str,
        detail: str,
    ) -> None:
        conn = self.get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO data_quality (session_date, ticker, check_name, severity, detail)
            VALUES (?, ?, ?, ?, ?);
            """,
            (session_date, ticker, check_name, severity, detail),
        )
        conn.commit()
