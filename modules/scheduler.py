"""Scheduled email fetch daemon.

Runs a background thread that periodically checks every user's email_servers
for those with fetch_interval_minutes > 0 and triggers a fetch when the
interval has elapsed since last_fetch_at.

Also manages long-lived IMAP IDLE connections for servers configured to use
IMAP IDLE, refreshing them every 29 minutes to keep the connection alive.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from modules import get_user_db, get_global_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
IMAP_IDLE_REFRESH_SECONDS = 29 * 60
IMAP_IDLE_POLL_SECONDS = 30


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


def _check_and_fetch(
    idle_manager: ImapIdleManager | None = None,
) -> None:
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
                "FROM email_servers WHERE fetch_interval_minutes > 0 AND (use_imap_idle = 0 OR use_imap_idle IS NULL)"
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

            # Safety-net: also fetch IDLE servers if their last fetch was > 5 min ago.
            # IDLE should handle real-time delivery, but SSL drops can leave gaps.
            # Skip the safety-net fetch when an IDLE connection is already active – a
            # second parallel connection to the same server can exceed the server's
            # per-client connection limit.
            uc.execute(
                "SELECT id, last_fetch_at FROM email_servers WHERE use_imap_idle = 1"
            )
            for row in uc.fetchall():
                is_active = (
                    idle_manager is not None
                    and idle_manager.has_active_connection(uid, row["id"])
                )
                if is_active:
                    continue
                last_fetch = row["last_fetch_at"]
                due = False
                if not last_fetch:
                    due = True
                else:
                    try:
                        last_dt = datetime.fromisoformat(last_fetch)
                        if now - last_dt >= timedelta(minutes=5):
                            due = True
                    except (ValueError, TypeError):
                        due = True
                if due:
                    _fetch_for_server(row["id"], uid)
            uconn.close()
        except Exception as e:
            logger.error("Scheduler error for user %s: %s", uid, e)


def scheduler_loop(
    stop_event: threading.Event,
    idle_manager: ImapIdleManager | None = None,
) -> None:
    """Main scheduler loop – runs until *stop_event* is set."""
    logger.info("Email fetch scheduler started (poll every %ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            _check_and_fetch(idle_manager=idle_manager)
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)
        stop_event.wait(POLL_INTERVAL_SECONDS)
    logger.info("Email fetch scheduler stopped")


class ImapIdleConnection:
    """Maintain one long-lived IMAP IDLE connection for a single server."""

    def __init__(self, user_id: int, server_id: int, server_config: dict) -> None:
        self.user_id = user_id
        self.server_id = server_id
        self.server_config = server_config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = 1  # Exponential backoff (seconds) for reconnection

    @property
    def active(self) -> bool:
        """The connection thread is alive and not being shut down."""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"imap-idle-{self.server_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=6)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._idle_session()
                self._backoff = 1  # Reset backoff on successful cycle
            except Exception as e:
                logger.error("IMAP IDLE session error for server %s: %s", self.server_id, e)
            if not self._stop_event.is_set():
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 120)  # Cap at 120s

    def _idle_session(self) -> None:
        """IMAP IDLE session using imapclient (select-based timeout, no makefile corruption)."""
        import imapclient

        cfg = self.server_config
        host = cfg["incoming_server"]
        port = cfg["incoming_port"] or (993 if cfg["use_ssl"] else 143)

        client = imapclient.IMAPClient(
            host,
            port=port,
            ssl=bool(cfg["use_ssl"]),
            timeout=30,
        )
        try:
            client.login(cfg["username"], cfg["password"])
            client.select_folder("INBOX")

            # Fetch any emails that arrived while the connection was down
            threading.Thread(
                target=_fetch_for_server,
                args=(self.server_id, self.user_id),
                daemon=True,
            ).start()

            while not self._stop_event.is_set():
                client.idle()
                idle_start = time.time()
                got_mail = False
                idle_ok = True
                try:
                    while not self._stop_event.is_set():
                        elapsed = time.time() - idle_start
                        remaining = IMAP_IDLE_REFRESH_SECONDS - elapsed
                        if remaining <= 0:
                            break
                        responses = client.idle_check(
                            timeout=min(remaining, 5),
                        )
                        if responses:
                            got_mail = any(
                                len(r) >= 2 and r[1] == b"EXISTS"
                                for r in responses
                            )
                            break
                except Exception as idle_err:
                    logger.error("IMAP idle_check error for server %s: %s", self.server_id, idle_err)
                    idle_ok = False
                finally:
                    try:
                        # idle_done() returns (command_text, pending_responses).
                        # Pending responses are those that arrived between the
                        # last idle_check() and the DONE command — without
                        # checking them we can miss EXISTS notifications.
                        _, pending = client.idle_done()
                        if pending and not got_mail:
                            got_mail = any(
                                len(r) >= 2 and r[1] == b"EXISTS"
                                for r in pending
                            )
                    except Exception as done_err:
                        logger.error("IMAP idle_done error for server %s: %s", self.server_id, done_err)

                if not idle_ok:
                    break  # connection lost, reconnect in _run
                if self._stop_event.is_set():
                    break
                if got_mail:
                    threading.Thread(
                        target=_fetch_for_server,
                        args=(self.server_id, self.user_id),
                        daemon=True,
                    ).start()
        finally:
            try:
                client.logout()
            except Exception:
                pass


class ImapIdleManager:
    """Track and manage IMAP IDLE connections across all users/servers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[tuple[int, int], ImapIdleConnection] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="imap-idle-manager",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            for conn in list(self._connections.values()):
                conn.stop()
            self._connections.clear()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sync_connections()
            except Exception as e:
                logger.error("IMAP IDLE manager sync error: %s", e)
            self._stop_event.wait(IMAP_IDLE_POLL_SECONDS)

    def has_active_connection(self, user_id: int, server_id: int) -> bool:
        """Return True if the IDLE connection thread is actually alive for this server.

        A connection whose thread has crashed or is stuck in backoff is no
        longer active — the safety-net periodic fetch should be allowed to
        pick up any pending emails.
        """
        with self._lock:
            conn = self._connections.get((user_id, server_id))
        return conn is not None and conn.active

    def pause_connection(self, user_id: int, server_id: int) -> None:
        """Temporarily stop the IDLE connection for a specific server.

        Used before testing a connection to avoid exceeding the server's
        per-user connection limit (e.g. Yahoo: 2, Outlook: 5).
        Call ``resume_connection`` afterward to recreate it.
        """
        key = (user_id, server_id)
        with self._lock:
            conn = self._connections.pop(key, None)
        if conn:
            conn.stop()

    def resume_connection(self, user_id: int, server_id: int) -> None:
        """Recreate the IDLE connection for this server after ``pause_connection``."""
        key = (user_id, server_id)
        with self._lock:
            if key in self._connections:
                return  # already active (someone else resumed)
        try:
            uconn = get_user_db(user_id)
            uc = uconn.cursor()
            uc.execute(
                "SELECT * FROM email_servers "
                "WHERE id=? AND incoming_protocol='IMAP' AND use_imap_idle=1",
                (server_id,),
            )
            row = uc.fetchone()
            uconn.close()
            if row:
                cfg = dict(row)
                new_conn = ImapIdleConnection(user_id, server_id, cfg)
                new_conn.start()
                with self._lock:
                    # Only add if still absent (race check)
                    if key not in self._connections:
                        self._connections[key] = new_conn
        except Exception as e:
            logger.error(
                "Failed to resume IDLE connection for server %s: %s", server_id, e
            )

    def _sync_connections(self) -> None:
        conn = get_global_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users")
        user_ids = [r["id"] for r in cursor.fetchall()]
        conn.close()

        desired: set[tuple[int, int]] = set()
        configs: dict[tuple[int, int], dict[str, object]] = {}

        for uid in user_ids:
            try:
                uconn = get_user_db(uid)
                uc = uconn.cursor()
                uc.execute(
                    "SELECT * FROM email_servers "
                    "WHERE incoming_protocol = 'IMAP' AND use_imap_idle = 1"
                )
                for row in uc.fetchall():
                    key = (uid, row["id"])
                    desired.add(key)
                    configs[key] = dict(row)
                uconn.close()
            except Exception as e:
                logger.error("IMAP IDLE manager user %s scan error: %s", uid, e)

        with self._lock:
            current = set(self._connections.keys())
            to_stop = current - desired
            to_start = desired - current

            for key in to_stop:
                old_conn = self._connections.pop(key, None)
                if old_conn:
                    old_conn.stop()

            for key in to_start:
                cfg = configs[key]
                new_conn = ImapIdleConnection(key[0], key[1], cfg)
                new_conn.start()
                self._connections[key] = new_conn


class EmailFetchScheduler:
    """Manage the background scheduler thread for automatic email fetching."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._idle_manager = ImapIdleManager()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduler already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=scheduler_loop,
            args=(self._stop_event, self._idle_manager),
            daemon=True,
            name="email-fetch-scheduler",
        )
        self._thread.start()
        self._idle_manager.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._idle_manager.stop()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


# Singleton
scheduler = EmailFetchScheduler()
