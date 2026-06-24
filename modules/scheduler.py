"""Scheduled email fetch daemon.

Runs a background thread that periodically checks every user's email_servers
for those with fetch_interval_minutes > 0 and triggers a fetch when the
interval has elapsed since last_fetch_at.
"""

import logging
import threading
from datetime import datetime, timedelta
from modules import get_user_db, get_global_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60  # master loop granularity


def _fetch_for_server(server_id: int, user_id: int) -> None:
    """Run fetch_emails + classification for a single server."""
    try:
        from modules.email_fetch import fetch_emails
        from modules.email_classify import (
            classify_unclassified_emails,
            auto_classify_senders,
        )

        result = fetch_emails(server_id)
        if result.get("success"):
            classify_unclassified_emails(user_id)
            auto_classify_senders(user_id)
    except Exception as e:
        logger.error("Scheduled fetch for server %s failed: %s", server_id, e)


def _check_and_fetch() -> None:
    """Scan all users' servers and trigger fetches for those that are due."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users")
    user_ids = [r["id"] for r in cursor.fetchall()]
    conn.close()

    now = datetime.utcnow()

    for uid in user_ids:
        try:
            uconn = get_user_db(uid)
            uc = uconn.cursor()
            uc.execute(
                "SELECT id, last_fetch_at, fetch_interval_minutes "
                "FROM email_servers WHERE fetch_interval_minutes > 0"
            )
            for row in uc.fetchall():
                interval = row["fetch_interval_minutes"]
                last_fetch = row["last_fetch_at"]
                due = False

                if not last_fetch:
                    due = True
                else:
                    try:
                        last_dt = datetime.fromisoformat(last_fetch)
                        if now - last_dt >= timedelta(minutes=interval):
                            due = True
                    except (ValueError, TypeError):
                        due = True

                if due:
                    _fetch_for_server(row["id"], uid)
            uconn.close()
        except Exception as e:
            logger.error("Scheduler error for user %s: %s", uid, e)


def scheduler_loop(stop_event: threading.Event) -> None:
    """Main scheduler loop – runs until *stop_event* is set."""
    logger.info("Email fetch scheduler started (poll every %ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            _check_and_fetch()
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)
        stop_event.wait(POLL_INTERVAL_SECONDS)
    logger.info("Email fetch scheduler stopped")


class EmailFetchScheduler:
    """Manage the background scheduler thread for automatic email fetching."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduler already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=scheduler_loop,
            args=(self._stop_event,),
            daemon=True,
            name="email-fetch-scheduler",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


# Singleton
scheduler = EmailFetchScheduler()
