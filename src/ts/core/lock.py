"""Single-instance process lock (PRD 1.3.1, "Concorrência": o pacote MetaTrader5 não é thread-safe).

Uses an OS-level byte-range lock on an open file. The OS releases it automatically when the process
dies, so a crashed routine never leaves a stale lock behind (unlike a plain "lock file exists" check).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType

from ts.core.errors import TSError


class LockBusyError(TSError):
    """Another routine already holds the system lock."""


class ProcessLock:
    def __init__(self, path: str | Path, timeout_s: float = 0.0, poll_s: float = 0.5) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fh: int | None = None

    def _try_lock(self, fd: int) -> bool:
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + self.timeout_s
        while not self._try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockBusyError(f"Another routine holds {self.path}")
            time.sleep(self.poll_s)
        try:  # informative only (which PID holds the lock)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(os.getpid()).ljust(12).encode("ascii"))
        except OSError:
            pass
        self._fh = fd

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fh, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self._fh, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            os.close(self._fh)
            self._fh = None

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
