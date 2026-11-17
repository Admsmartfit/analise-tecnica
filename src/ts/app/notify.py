"""Telegram notification module with retry and fallback to log file."""
from __future__ import annotations

import logging
import time
from datetime import UTC
from pathlib import Path

logger = logging.getLogger("ts.app.notify")

_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 4.0, 10.0)


def _get_telegram_credentials() -> tuple[str, str] | None:
    """Read bot token and chat ID from Windows Credential Manager via keyring."""
    try:
        import keyring  # type: ignore

        token = keyring.get_password("ts-telegram", "bot_token")
        chat_id = keyring.get_password("ts-telegram", "chat_id")
        if token and chat_id:
            return token, chat_id
    except Exception as exc:
        logger.debug("keyring not available: %s", exc)
    return None


def send_telegram(text: str, *, log_fallback_dir: str = "logs") -> bool:
    """Send a message via Telegram Bot API.

    Retries up to 3 times with exponential backoff.
    Falls back to appending the message to a log file if Telegram is unavailable.

    Args:
        text: Message text (supports Markdown V2).
        log_fallback_dir: Directory for fallback log file.

    Returns:
        True if message was sent successfully, False if fallback was used.
    """
    creds = _get_telegram_credentials()
    if creds is None:
        logger.warning("Telegram credentials not configured — logging to fallback file")
        _fallback_log(text, log_fallback_dir)
        return False

    token, chat_id = creds
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            import httpx  # type: ignore

            resp = httpx.post(url, json=payload, timeout=15.0)
            if resp.status_code == 200:
                logger.debug("Telegram: message sent (attempt %d)", attempt + 1)
                return True
            logger.warning("Telegram: HTTP %d on attempt %d", resp.status_code, attempt + 1)
        except Exception as exc:
            logger.warning("Telegram: error on attempt %d: %s", attempt + 1, exc)

        if delay is not None:
            time.sleep(delay)

    # All retries exhausted
    logger.error("Telegram: all %d attempts failed — using fallback log", _MAX_RETRIES)
    _fallback_log(text, log_fallback_dir)
    return False


def _fallback_log(text: str, log_dir: str) -> None:
    """Append message to a plain-text fallback notification log."""
    from datetime import datetime

    fallback_path = Path(log_dir) / "telegram_fallback.log"
    try:
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat()
        with open(fallback_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {text}\n")
    except Exception as exc:
        logger.error("Could not write to fallback log: %s", exc)


class RecordingNotifier:
    """Test double that records all messages without sending anything."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True
