"""Email fetching module - POP3 and IMAP protocols."""

import email
import email.utils
import imaplib
import poplib
import re
import smtplib
import threading
import time
from datetime import datetime
from email.header import decode_header
from modules import get_user_db
from modules.email_classify import (
    get_or_create_sender_group,
    classify_email,
    classify_unclassified_emails,
)

# ---------------------------------------------------------------------------
# Fetch progress tracking (shared across threads)
# ---------------------------------------------------------------------------

_FETCH_PROGRESS: dict[int, dict] = {}
_FETCH_PROGRESS_LOCK = threading.Lock()

# Per-server fetch-in-progress flag to prevent concurrent fetches
_FETCH_RUNNING: dict[int, bool] = {}
_FETCH_RUNNING_LOCK = threading.Lock()


def _update_progress(
    server_id: int,
    total: int,
    current: int,
    status: str,
    server_name: str = "",
) -> None:
    """Update the in-memory fetch progress for a server.

    Status values: 'fetching', 'done', 'error'.
    """
    with _FETCH_PROGRESS_LOCK:
        if status == "idle":
            _FETCH_PROGRESS.pop(server_id, None)
            return
        _FETCH_PROGRESS[server_id] = {
            "total": total,
            "current": current,
            "status": status,
            "server_name": server_name,
        }


def get_all_fetch_progress() -> dict:
    """Return a snapshot of all current fetch progress states."""
    with _FETCH_PROGRESS_LOCK:
        return dict(_FETCH_PROGRESS)


def _try_acquire_fetch_lock(server_id: int) -> bool:
    """Try to acquire the per-server fetch lock.

    Returns False if a fetch is already in progress for this server.
    """
    with _FETCH_RUNNING_LOCK:
        if _FETCH_RUNNING.get(server_id, False):
            return False
        _FETCH_RUNNING[server_id] = True
        return True


def _release_fetch_lock(server_id: int) -> None:
    """Release the per-server fetch lock."""
    with _FETCH_RUNNING_LOCK:
        _FETCH_RUNNING.pop(server_id, None)


def _decode_mime_header(header_value: str) -> str:
    """Decode MIME encoded header values."""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def _parse_email_date(date_str: str) -> datetime:
    """Parse email date string to datetime."""
    if not date_str:
        return datetime.utcnow()
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return datetime.utcnow()


def _get_email_body(msg) -> tuple:
    """Extract text and HTML body from email message."""
    body_text = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            try:
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain" and not body_text:
                body_text = decoded
            elif content_type == "text/html" and not body_html:
                body_html = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded = payload.decode("utf-8", errors="replace")

            if msg.get_content_type() == "text/html":
                body_html = decoded
            else:
                body_text = decoded

    return body_text, body_html


def _extract_email_address(addr_str: str) -> tuple:
    """Extract name and email from address string."""
    if not addr_str:
        return ("", "")
    name, addr = email.utils.parseaddr(addr_str)
    return (name, addr)


def _parse_addresses(header_value: str) -> str:
    """Parse email addresses from a header."""
    if not header_value:
        return ""
    addresses = email.utils.getaddresses([header_value])
    return ", ".join(f"{addr}" for _, addr in addresses if addr)


def fetch_pop3(server_config: dict, max_emails: int = 50) -> list:
    """Fetch emails from a POP3 server.

    Returns list of email dicts.
    """
    emails_fetched = []
    try:
        if server_config["use_ssl"]:
            server = poplib.POP3_SSL(
                server_config["incoming_server"],
                server_config["incoming_port"] or 995,
                timeout=30,
            )
        else:
            server = poplib.POP3(
                server_config["incoming_server"],
                server_config["incoming_port"] or 110,
                timeout=30,
            )

        server.user(server_config["username"])
        server.pass_(server_config["password"])

        num_messages = len(server.list()[1])
        fetch_count = min(num_messages, max_emails)

        for i in range(num_messages, num_messages - fetch_count, -1):
            try:
                # Get POP3 UIDL for dedup
                server_uid = ""
                try:
                    uidl_resp = server.uidl(i)
                    if uidl_resp and len(uidl_resp) >= 2 and uidl_resp[1]:
                        server_uid = uidl_resp[1][0].split()[-1].decode()
                except Exception:
                    pass

                raw_email = b"\n".join(server.retr(i)[1])
                msg = email.message_from_bytes(raw_email)

                subject = _decode_mime_header(msg.get("Subject", ""))
                sender_raw = msg.get("From", "")
                sender_name, sender_addr = _extract_email_address(sender_raw)
                recipients = _parse_addresses(msg.get("To", ""))
                date_str = msg.get("Date", "")
                message_id = msg.get("Message-ID", "")

                received_date = _parse_email_date(date_str)
                body_text, body_html = _get_email_body(msg)

                emails_fetched.append({
                    "message_id": message_id,
                    "server_uid": server_uid,
                    "sender": sender_addr or sender_raw,
                    "sender_name": sender_name or sender_addr,
                    "recipients": recipients,
                    "subject": subject,
                    "body_text": body_text,
                    "body_html": body_html,
                    "received_date": received_date.isoformat(),
                })
            except Exception:
                continue

        if server_config["delete_after_download"]:
            for i in range(num_messages, num_messages - fetch_count, -1):
                try:
                    server.dele(i)
                except Exception:
                    pass

        server.quit()
    except Exception as e:
        raise Exception(f"POP3 fetch failed: {e}")

    return emails_fetched


def _parse_capabilities(caps) -> list:
    """Parse imaplib capability response into a list of strings."""
    if not caps:
        return []
    raw = caps[0] if isinstance(caps[0], bytes) else b""
    return raw.decode("utf-8", errors="replace").split()


def _send_imap_id(server) -> bool:
    """Send IMAP ID command (RFC 2971) for servers that require client identification.

    Checks the server's capabilities first — only sends ID if the server
    advertises support for it (e.g. NetEase 126/Coremail).  Returns True
    when the ID command completed successfully, False otherwise.
    """
    try:
        _, caps = server.capability()
        if not caps or not isinstance(caps[0], bytes):
            return False
        if b"ID" not in caps[0].split():
            return False

        tag = server._new_tag().decode("ascii")
        cmd = f'{tag} ID ("name" "PythonIMAP" "version" "1.0" "vendor" "self")\r\n'
        server.send(cmd.encode("ascii"))
        while True:
            line = server.readline()
            if not line:
                break
            line_str = line.decode("ascii", errors="replace").strip()
            if line_str.startswith(f"{tag} OK"):
                return True
            if line_str.startswith(f"{tag} NO") or line_str.startswith(f"{tag} BAD"):
                return False
    except Exception:
        pass
    return False


def check_imap_capabilities(server_config: dict) -> dict:
    """Connect to an IMAP server and return its capability list.

    Tries CAPABILITY before login so the check can run as soon as the user
    enters the server address. If IDLE is not advertised pre-login and
    credentials are supplied, logs in and tries again.

    Returns {"success": bool, "capabilities": list, "idle_supported": bool, "error": str}.
    """
    result = {"success": False, "capabilities": [], "idle_supported": False, "error": ""}
    server = None
    prelogin_capabilities = None
    try:
        if server_config["use_ssl"]:
            server = imaplib.IMAP4_SSL(
                server_config["incoming_server"],
                server_config["incoming_port"] or 993,
                timeout=15,
            )
        else:
            server = imaplib.IMAP4(
                server_config["incoming_server"],
                server_config["incoming_port"] or 143,
                timeout=15,
            )

        _send_imap_id(server)
        status, caps = server.capability()
        capabilities = _parse_capabilities(caps)
        if status == "OK":
            prelogin_capabilities = capabilities
            if "IDLE" in capabilities:
                result["success"] = True
                result["capabilities"] = capabilities
                result["idle_supported"] = True
                try:
                    server.logout()
                except Exception:
                    pass
                return result

        has_credentials = bool(server_config.get("username") and server_config.get("password"))
        if has_credentials:
            try:
                server.login(server_config["username"], server_config["password"])
                status, caps = server.capability()
                capabilities = _parse_capabilities(caps)
                server.logout()
                result["success"] = status == "OK"
                result["capabilities"] = capabilities
                result["idle_supported"] = "IDLE" in capabilities
                return result
            except Exception:
                if prelogin_capabilities is not None:
                    result["success"] = True
                    result["capabilities"] = prelogin_capabilities
                    result["idle_supported"] = "IDLE" in prelogin_capabilities
                    return result
                raise

        result["success"] = status == "OK"
        result["capabilities"] = capabilities
        result["idle_supported"] = "IDLE" in capabilities
    except Exception as e:
        if prelogin_capabilities is not None and result["capabilities"] == []:
            result["success"] = True
            result["capabilities"] = prelogin_capabilities
            result["idle_supported"] = "IDLE" in prelogin_capabilities
        else:
            result["error"] = str(e)
        if server:
            try:
                server.logout()
            except Exception:
                pass
    return result


def fetch_imap(
    server_config: dict,
    max_emails: int = 50,
    known_uids: set[str] | None = None,
    server_id: int | None = None,
    server_name: str = "",
) -> list:
    """Fetch emails from an IMAP server using UID-based incremental fetch.

    Uses UID SEARCH to get all server UIDs, compares with *known_uids*
    to only download new emails. Updates global fetch progress when
    *server_id* is provided.

    Returns list of email dicts with reliable server_uid values.
    """
    emails_fetched = []
    try:
        if server_config["use_ssl"]:
            server = imaplib.IMAP4_SSL(
                server_config["incoming_server"],
                server_config["incoming_port"] or 993,
                timeout=30,
            )
        else:
            server = imaplib.IMAP4(
                server_config["incoming_server"],
                server_config["incoming_port"] or 143,
                timeout=30,
            )

        _send_imap_id(server)
        server.login(server_config["username"], server_config["password"])
        server.select("INBOX")

        # Get all UIDs from the server
        _, data = server.uid("SEARCH", None, "ALL")
        uids = data[0].split() if data[0] else []
        # uids is list of bytes like [b'1', b'2', ...]; filter empty
        uids = [u for u in uids if u.strip()]

        # Filter to only new/unknown UIDs
        if known_uids:
            known_bytes = {u.encode() if isinstance(u, str) else u for u in known_uids}
            new_uids = [u for u in uids if u not in known_bytes]
        else:
            new_uids = list(uids)

        # Cap to max_emails (newest first)
        fetch_uids = new_uids[-max_emails:]
        fetch_count = len(fetch_uids)

        if fetch_count == 0:
            server.close()
            server.logout()
            if server_id is not None:
                _update_progress(server_id, 0, 0, "done", server_name)
            return []

        _update_progress(server_id or 0, fetch_count, 0, "fetching", server_name)

        seen_msg_ids: set[str] = set()
        for idx, uid in enumerate(fetch_uids):
            try:
                _, msg_data = server.uid("FETCH", uid, "(RFC822)")
                # Extract the raw email body from response
                raw_email = None
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                        raw_email = part[1]
                        break
                if raw_email is None:
                    continue

                msg = email.message_from_bytes(raw_email)

                server_uid = uid.decode() if isinstance(uid, bytes) else str(uid)

                subject = _decode_mime_header(msg.get("Subject", ""))
                sender_raw = msg.get("From", "")
                sender_name, sender_addr = _extract_email_address(sender_raw)
                recipients = _parse_addresses(msg.get("To", ""))
                date_str = msg.get("Date", "")
                message_id = (msg.get("Message-ID") or "").strip()

                # Dedup by message_id within this batch
                if message_id and message_id in seen_msg_ids:
                    continue
                if message_id:
                    seen_msg_ids.add(message_id)

                received_date = _parse_email_date(date_str)
                body_text, body_html = _get_email_body(msg)

                emails_fetched.append({
                    "message_id": message_id,
                    "server_uid": server_uid,
                    "sender": sender_addr or sender_raw,
                    "sender_name": sender_name or sender_addr,
                    "recipients": recipients,
                    "subject": subject,
                    "body_text": body_text,
                    "body_html": body_html,
                    "received_date": received_date.isoformat(),
                })
            except Exception as e:
                continue

            _update_progress(server_id or 0, fetch_count, idx + 1, "fetching", server_name)

        if server_config.get("delete_after_download"):
            for uid in fetch_uids:
                try:
                    server.uid("STORE", uid, "+FLAGS", "\\Deleted")
                except Exception:
                    pass
            server.expunge()

        server.close()
        server.logout()
        if server_id is not None:
            _update_progress(server_id, fetch_count, fetch_count, "done", server_name)

    except Exception as e:
        if server_id is not None:
            _update_progress(server_id, 0, 0, "error", server_name)
        raise Exception(f"IMAP fetch failed: {e}")

    return emails_fetched


def _store_emails(emails: list, owner_id: int, server_id: int, server_config: dict) -> int:
    """Store fetched email dicts into the user's database.

    Handles dedup by server_uid, creates sender groups, and classifies
    importance. Returns the number of newly stored emails.
    """
    stored_count = 0
    conn = get_user_db(owner_id)
    cursor = conn.cursor()

    for email_data in emails:
        email_uid = email_data.get("server_uid") or ""
        email_msg_id = email_data.get("message_id") or ""

        # Dedup safety (fetch_imap already filters by UID, but handle edge cases)
        if email_uid:
            cursor.execute(
                "SELECT 1 FROM emails WHERE user_id = ? AND server_id = ? AND server_uid = ?",
                (owner_id, server_id, email_uid),
            )
            if cursor.fetchone():
                continue

        # Get or create sender group
        sg = get_or_create_sender_group(
            owner_id,
            email_data["sender"],
            email_data["sender_name"],
            conn=conn,
        )

        # Get/re-classify importance based on actual content
        importance = classify_email(
            email_data["sender"],
            email_data["sender_name"],
            email_data["subject"],
            email_data["body_text"],
        )

        cursor.execute(
            "SELECT id FROM importance_groups WHERE user_id = ? AND name = ?",
            (owner_id, importance),
        )
        ig = cursor.fetchone()
        imp_id = ig["id"] if ig else None

        # Also update sender group importance if auto-classified
        if sg["importance_group_id"] is None and imp_id:
            cursor.execute(
                "UPDATE sender_groups SET importance_group_id = ?, is_auto_classified = 1 WHERE id = ?",
                (imp_id, sg["id"]),
            )

        cursor.execute(
            "INSERT INTO emails (user_id, server_id, sender_group_id, importance_group_id, "
            "message_id, server_uid, sender, sender_name, recipients, subject, body_text, "
            "body_html, received_date, folder, server_badge) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox', ?)",
            (
                owner_id,
                server_id,
                sg["id"],
                imp_id,
                email_data["message_id"],
                email_data.get("server_uid", ""),
                email_data["sender"],
                email_data["sender_name"],
                email_data["recipients"],
                email_data["subject"],
                email_data["body_text"],
                email_data["body_html"],
                email_data["received_date"],
                server_config["server_name"],
            ),
        )
        stored_count += 1

    conn.commit()
    conn.close()
    return stored_count


def fetch_emails(server_id: int) -> dict:
    """Fetch emails from a server configuration.

    Uses UID-based incremental fetch for IMAP, avoids re-downloading
    already-known emails. Prevents concurrent fetches for the same server.

    Returns result dict with fetched count and any error.
    """
    from modules import get_user_db_path
    import os

    user_db_dir = os.path.dirname(get_user_db_path(0))
    server_config = None
    owner_id = None

    if os.path.isdir(user_db_dir):
        for fname in os.listdir(user_db_dir):
            if fname.startswith("user_") and fname.endswith(".db"):
                uid = int(fname[5:-3])
                try:
                    conn = get_user_db(uid)
                    c = conn.cursor()
                    c.execute("SELECT * FROM email_servers WHERE id = ?", (server_id,))
                    row = c.fetchone()
                    conn.close()
                    if row:
                        server_config = dict(row)
                        owner_id = uid
                        break
                except Exception:
                    continue

    if not server_config:
        return {"success": False, "error": "Server not found"}

    # Prevent concurrent fetches for the same server
    if not _try_acquire_fetch_lock(server_id):
        return {"success": True, "fetched": 0, "status": "skipped", "message": "Fetch already in progress"}

    try:
        # Get known UIDs from DB to pass to fetch_imap
        conn = get_user_db(owner_id)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT server_uid FROM emails WHERE user_id = ? AND server_id = ? AND server_uid != ''",
            (owner_id, server_id),
        )
        known_uids = {r["server_uid"] for r in cursor.fetchall()}
        conn.close()

        max_emails = server_config.get("max_emails_per_fetch", 50)
        # -1 means "all" — use a large sentinel value
        if max_emails is None or max_emails == -1:
            max_emails = 999999

        if server_config["incoming_protocol"].upper() == "POP3":
            emails = fetch_pop3(server_config, max_emails=max_emails)
        elif server_config["incoming_protocol"].upper() == "IMAP":
            server_name = server_config.get("server_name", "")
            emails = fetch_imap(
                server_config,
                max_emails=max_emails,
                known_uids=known_uids,
                server_id=server_id,
                server_name=server_name,
            )
        else:
            return {"success": False, "error": f"Unsupported protocol: {server_config['incoming_protocol']}"}

        stored_count = _store_emails(emails, owner_id, server_id, server_config)

        # Update last fetch time
        conn = get_user_db(owner_id)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE email_servers SET last_fetch_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), server_id),
        )
        conn.commit()
        conn.close()

        return {"success": True, "fetched": stored_count}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        _release_fetch_lock(server_id)


def download_all_emails(server_id: int, user_id: int) -> dict:
    """Download ALL emails from a server, batching by max_emails_per_fetch.

    For IMAP: loops calling fetch_imap with updated known_uids until all
    new UIDs are downloaded. For POP3: gets all UIDs via UIDL, filters
    to new ones, downloads them all.

    Uses the same progress tracking as regular fetch so the frontend
    can poll /api/fetch-progress.
    """
    from modules import get_user_db_path
    import os

    user_db_dir = os.path.dirname(get_user_db_path(0))
    server_config = None
    owner_id = None

    if os.path.isdir(user_db_dir):
        for fname in os.listdir(user_db_dir):
            if fname.startswith("user_") and fname.endswith(".db"):
                uid = int(fname[5:-3])
                try:
                    conn = get_user_db(uid)
                    c = conn.cursor()
                    c.execute("SELECT * FROM email_servers WHERE id = ?", (server_id,))
                    row = c.fetchone()
                    conn.close()
                    if row:
                        server_config = dict(row)
                        owner_id = uid
                        break
                except Exception:
                    continue

    if not server_config:
        return {"success": False, "error": "Server not found"}

    if not _try_acquire_fetch_lock(server_id):
        return {"success": True, "fetched": 0, "status": "skipped", "message": "Download already in progress"}

    try:
        batch_size = server_config.get("max_emails_per_fetch", 50)
        if batch_size is None or batch_size == -1:
            batch_size = 999999

        conn = get_user_db(owner_id)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT server_uid FROM emails WHERE user_id = ? AND server_id = ? AND server_uid != ''",
            (owner_id, server_id),
        )
        known_uids = {r["server_uid"] for r in cursor.fetchall()}
        conn.close()

        protocol = server_config["incoming_protocol"].upper()
        server_name = server_config.get("server_name", "")
        total_fetched = 0

        if protocol == "POP3":
            result_emails = _download_all_pop3(server_config, batch_size, known_uids, server_id, server_name)
            total_fetched = _store_emails(result_emails, owner_id, server_id, server_config)
            _update_progress(server_id, total_fetched, total_fetched, "done", server_name)

        elif protocol == "IMAP":
            # Get all UIDs from the server to know the total
            server_context = _connect_imap(server_config)
            if server_context is None:
                return {"success": False, "error": "IMAP connection failed"}
            server, _login_ok = server_context
            try:
                server.select("INBOX")
                _, data = server.uid("SEARCH", None, "ALL")
                all_uids = data[0].split() if data[0] else []
                all_uids = [u for u in all_uids if u.strip()]
                # Filter known UIDs
                if known_uids:
                    known_bytes = {u.encode() if isinstance(u, str) else u for u in known_uids}
                    new_uids = [u for u in all_uids if u not in known_bytes]
                else:
                    new_uids = list(all_uids)
                total_all = len(new_uids)

                if total_all == 0:
                    _update_progress(server_id, 0, 0, "done", server_name)
                    return {"success": True, "fetched": 0}

                _update_progress(server_id, total_all, 0, "downloading", server_name)
                local_known = set(known_uids)

                for i in range(0, total_all, batch_size):
                    batch_uids = new_uids[i:i + batch_size]
                    batch_emails = _fetch_imap_uids(server, batch_uids, server_config)
                    batch_stored = _store_emails(batch_emails, owner_id, server_id, server_config)
                    total_fetched += batch_stored
                    for em in batch_emails:
                        if em.get("server_uid"):
                            local_known.add(em["server_uid"])
                    for j in range(len(batch_uids)):
                        _update_progress(server_id, total_all, i + j + 1, "downloading", server_name)

                # Handle delete_after_download for IMAP
                if server_config.get("delete_after_download"):
                    for uid in new_uids:
                        try:
                            server.uid("STORE", uid, "+FLAGS", "\\Deleted")
                        except Exception:
                            pass
                    server.expunge()
            finally:
                try:
                    server.close()
                    server.logout()
                except Exception:
                    pass

            _update_progress(server_id, total_all, total_all, "done", server_name)
        else:
            return {"success": False, "error": f"Unsupported protocol: {protocol}"}

        # Update last fetch time
        conn = get_user_db(owner_id)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE email_servers SET last_fetch_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), server_id),
        )
        conn.commit()
        conn.close()

        return {"success": True, "fetched": total_fetched}
    except Exception as e:
        _update_progress(server_id, 0, 0, "error", server_config.get("server_name", ""))
        return {"success": False, "error": str(e)}
    finally:
        _release_fetch_lock(server_id)


def _connect_imap(server_config: dict) -> tuple | None:
    """Connect and login to an IMAP server. Returns (server, True) or None."""
    try:
        if server_config["use_ssl"]:
            server = imaplib.IMAP4_SSL(
                server_config["incoming_server"],
                server_config["incoming_port"] or 993,
                timeout=30,
            )
        else:
            server = imaplib.IMAP4(
                server_config["incoming_server"],
                server_config["incoming_port"] or 143,
                timeout=30,
            )
        _send_imap_id(server)
        server.login(server_config["username"], server_config["password"])
        return (server, True)
    except Exception as e:
        return None


def _fetch_imap_uids(server, uids: list, server_config: dict) -> list:
    """Fetch specific UIDs from an already-connected IMAP server.

    Returns list of email dicts.
    """
    emails_fetched = []
    seen_msg_ids = set()
    for uid in uids:
        try:
            _, msg_data = server.uid("FETCH", uid, "(RFC822)")
            raw_email = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                    raw_email = part[1]
                    break
            if raw_email is None:
                continue

            msg = email.message_from_bytes(raw_email)
            server_uid = uid.decode() if isinstance(uid, bytes) else str(uid)
            subject = _decode_mime_header(msg.get("Subject", ""))
            sender_raw = msg.get("From", "")
            sender_name, sender_addr = _extract_email_address(sender_raw)
            recipients = _parse_addresses(msg.get("To", ""))
            date_str = msg.get("Date", "")
            message_id = (msg.get("Message-ID") or "").strip()

            if message_id and message_id in seen_msg_ids:
                continue
            if message_id:
                seen_msg_ids.add(message_id)

            received_date = _parse_email_date(date_str)
            body_text, body_html = _get_email_body(msg)

            emails_fetched.append({
                "message_id": message_id,
                "server_uid": server_uid,
                "sender": sender_addr or sender_raw,
                "sender_name": sender_name or sender_addr,
                "recipients": recipients,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "received_date": received_date.isoformat(),
            })
        except Exception:
            continue
    return emails_fetched


def _download_all_pop3(server_config: dict, batch_size: int, known_uids: set, server_id: int, server_name: str) -> list:
    """Download all new emails from a POP3 server, returning list of email dicts."""
    all_emails = []
    try:
        if server_config["use_ssl"]:
            server = poplib.POP3_SSL(
                server_config["incoming_server"],
                server_config["incoming_port"] or 995,
                timeout=30,
            )
        else:
            server = poplib.POP3(
                server_config["incoming_server"],
                server_config["incoming_port"] or 110,
                timeout=30,
            )

        server.user(server_config["username"])
        server.pass_(server_config["password"])

        # Get UIDL mapping
        num_messages = len(server.list()[1])
        uidl_resp = server.uidl()
        # uidl_resp[1] is list of b'1 uid123 ...' lines
        seq_to_uid = {}
        for line in uidl_resp[1]:
            parts = line.decode().split()
            if len(parts) >= 2:
                seq_to_uid[int(parts[0])] = parts[1]

        # Find new messages (from newest to oldest)
        new_seqs = []
        for seq in range(num_messages, 0, -1):
            uid = seq_to_uid.get(seq, "")
            if uid and uid not in known_uids:
                new_seqs.append(seq)

        total_new = len(new_seqs)
        if total_new == 0:
            server.quit()
            return []

        _update_progress(server_id, total_new, 0, "downloading", server_name)

        # Process in batches
        for i in range(0, total_new, batch_size):
            batch_seqs = new_seqs[i:i + batch_size]
            for j, seq in enumerate(batch_seqs):
                try:
                    uid = seq_to_uid.get(seq, "")
                    raw_email = b"\n".join(server.retr(seq)[1])
                    msg = email.message_from_bytes(raw_email)

                    subject = _decode_mime_header(msg.get("Subject", ""))
                    sender_raw = msg.get("From", "")
                    sender_name, sender_addr = _extract_email_address(sender_raw)
                    recipients = _parse_addresses(msg.get("To", ""))
                    date_str = msg.get("Date", "")
                    message_id = msg.get("Message-ID", "")
                    received_date = _parse_email_date(date_str)
                    body_text, body_html = _get_email_body(msg)

                    all_emails.append({
                        "message_id": message_id,
                        "server_uid": uid,
                        "sender": sender_addr or sender_raw,
                        "sender_name": sender_name or sender_addr,
                        "recipients": recipients,
                        "subject": subject,
                        "body_text": body_text,
                        "body_html": body_html,
                        "received_date": received_date.isoformat(),
                    })
                except Exception:
                    continue

                _update_progress(server_id, total_new, i + j + 1, "downloading", server_name)

        if server_config.get("delete_after_download"):
            for seq in new_seqs:
                try:
                    server.dele(seq)
                except Exception:
                    pass

        server.quit()
    except Exception as e:
        raise Exception(f"POP3 download all failed: {e}")

    return all_emails


def test_server_connection(server_config: dict) -> dict:
    """Test connection to an email server (incoming + outgoing).

    Tests POP3/IMAP incoming connection (connect + login + logout),
    and SMTP outgoing connection if configured.

    Returns dict with per-protocol results.
    """
    results = {}

    try:
        proto = server_config["incoming_protocol"].upper()
        if proto == "POP3":
            if server_config["use_ssl"]:
                server = poplib.POP3_SSL(
                    server_config["incoming_server"],
                    server_config["incoming_port"] or 995,
                    timeout=15,
                )
            else:
                server = poplib.POP3(
                    server_config["incoming_server"],
                    server_config["incoming_port"] or 110,
                    timeout=15,
                )
            try:
                server.user(server_config["username"])
                server.pass_(server_config["password"])
                server.quit()
                results["incoming"] = {
                    "success": True,
                    "message": f"POP3 {server_config['incoming_server']}: connection and login OK",
                }
            except Exception:
                try:
                    server.quit()
                except Exception:
                    pass
                raise
        elif proto == "IMAP":
            if server_config["use_ssl"]:
                server = imaplib.IMAP4_SSL(
                    server_config["incoming_server"],
                    server_config["incoming_port"] or 993,
                    timeout=15,
                )
            else:
                server = imaplib.IMAP4(
                    server_config["incoming_server"],
                    server_config["incoming_port"] or 143,
                    timeout=15,
                )
            try:
                _send_imap_id(server)
                server.login(server_config["username"], server_config["password"])
                select_status, select_data = server.select("INBOX")
                if select_status != "OK":
                    err_msg = (
                        select_data[0].decode("utf-8", errors="replace")
                        if select_data and isinstance(select_data[0], bytes)
                        else "Unknown error"
                    )
                    try:
                        server.logout()
                    except Exception:
                        pass
                    raise Exception(f"INBOX select failed: {err_msg}")
                try:
                    server.close()
                except Exception:
                    pass
                server.logout()
                results["incoming"] = {
                    "success": True,
                    "message": f"IMAP {server_config['incoming_server']}: connection, login and INBOX select OK",
                }
            except Exception:
                # Ensure the connection is cleaned up on error
                try:
                    server.logout()
                except Exception:
                    pass
                raise
        else:
            results["incoming"] = {
                "success": False,
                "message": f"Unsupported protocol: {proto}",
            }
    except Exception as e:
        results["incoming"] = {
            "success": False,
            "message": f"{server_config.get('incoming_protocol', '?')} connection failed: {e}",
        }

    outgoing = (server_config.get("outgoing_server") or "").strip()
    if outgoing:
        try:
            smtp_port = server_config["outgoing_port"] or (465 if server_config["use_ssl"] else 587)

            if server_config["use_ssl"]:
                smtp = smtplib.SMTP_SSL(
                    server_config["outgoing_server"],
                    smtp_port,
                    timeout=15,
                )
            else:
                smtp = smtplib.SMTP(
                    server_config["outgoing_server"],
                    smtp_port,
                    timeout=15,
                )
                smtp.starttls()

            smtp.login(server_config["username"], server_config["password"])
            smtp.quit()
            results["outgoing"] = {
                "success": True,
                "message": f"SMTP {server_config['outgoing_server']}:{smtp_port} connection and login OK",
            }
        except Exception as e:
            results["outgoing"] = {
                "success": False,
                "message": f"SMTP connection failed: {e}",
            }
    else:
        results["outgoing"] = {
            "success": False,
            "message": "SMTP server not configured",
        }

    # Overall success = incoming success (outgoing is optional)
    results["success"] = results["incoming"]["success"]
    return results


def fetch_all_for_user(user_id: int) -> list:
    """Fetch emails from all configured servers for a user."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM email_servers WHERE user_id = ?",
        (user_id,),
    )
    servers = cursor.fetchall()
    conn.close()

    results = []
    for server in servers:
        result = fetch_emails(server["id"])
        result["server_id"] = server["id"]
        results.append(result)

    return results
