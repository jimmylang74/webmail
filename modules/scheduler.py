"""Scheduled email fetch daemon.

Runs a background thread that periodically checks every user's email_servers
for those with fetch_interval_minutes > 0 and triggers a fetch when the
interval has elapsed since last_fetch_at.

Also manages long-lived IMAP IDLE connections for servers configured to use
IMAP IDLE, refreshing them every 29 minutes to keep the connection alive.
"""

import logging
import select
import socket
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


class ImapIdleConnection:
    """Maintain one long-lived IMAP IDLE connection for a single server."""

    def __init__(self, user_id: int, server_id: int, server_config: dict) -> None:
        self.user_id = user_id
        self.server_id = server_id
        self.server_config = server_config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

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
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._idle_session()
            except Exception as e:
                logger.error("IMAP IDLE session error for server %s: %s", self.server_id, e)
            if not self._stop_event.is_set():
                time.sleep(30)

    def _idle_session(self) -> None:
        import imaplib

        cfg = self.server_config
        if cfg["use_ssl"]:
            server = imaplib.IMAP4_SSL(
                cfg["incoming_server"],
                cfg["incoming_port"] or 993,
                timeout=15,
            )
        else:
            server = imaplib.IMAP4(
                cfg["incoming_server"],
                cfg["incoming_port"] or 143,
                timeout=15,
            )

        try:
            server.login(cfg["username"], cfg["password"])
            server.select("INBOX")

            while not self._stop_event.is_set():
                self._start_idle(server)
                new_mail = self._wait_for_idle_event(server)
                self._stop_idle(server)

                if new_mail:
                    threading.Thread(
                        target=_fetch_for_server,
                        args=(self.server_id, self.user_id),
                        daemon=True,
                    ).start()
        finally:
            try:
                server.close()
            except Exception:
                pass
            try:
                server.logout()
            except Exception:
                pass

    def _start_idle(self, server) -> None:
        tag = server._new_tag().decode()
        server.send(f"{tag} IDLE\r\n".encode())
        deadline = time.time() + 10
        while time.time() < deadline:
            line = server.readline()
            if line.startswith(b"+"):
                return
            if line.startswith(tag.encode() + b" NO") or line.startswith(tag.encode() + b" BAD"):
                raise Exception(f"IMAP IDLE not accepted: {line}")
        raise Exception("IMAP IDLE continuation response timeout")

    def _stop_idle(self, server) -> None:
        server.send(b"DONE\r\n")
        deadline = time.time() + 10
        while time.time() < deadline:
            line = server.readline()
            if b" OK" in line or b" NO" in line or b" BAD" in line:
                return

    def _wait_for_idle_event(self, server) -> bool:
        idle_start = time.time()
        new_mail = False
        while not self._stop_event.is_set():
            elapsed = time.time() - idle_start
            remaining = IMAP_IDLE_REFRESH_SECONDS - elapsed
            if remaining <= 0:
                break

            sock = server.socket()
            sock.settimeout(min(remaining, 5))
            try:
                line = server.readline()
            except socket.timeout:
                continue
            except OSError:
                raise

            text = line.decode("utf-8", errors="replace")
            if " EXISTS" in text or " RECENT" in text:
                new_mail = True
                break
        return new_mail


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
            args=(self._stop_event,),
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
