"""Email fetching module - POP3 and IMAP protocols."""

import email
import email.utils
import imaplib
import poplib
import re
import smtplib
import time
from datetime import datetime
from email.header import decode_header
from modules import get_user_db
from modules.email_classify import (
    get_or_create_sender_group,
    classify_email,
    classify_unclassified_emails,
)


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


def check_imap_capabilities(server_config: dict) -> dict:
    """Connect to an IMAP server and return its capability list.

    Tries CAPABILITY before login so the check can run as soon as the user
    enters the server address. If IDLE is not advertised pre-login and
    credentials are supplied, logs in and tries again.

    Returns {"success": bool, "capabilities": list, "idle_supported": bool, "error": str}.
    """
    result = {"success": False, "capabilities": [], "idle_supported": False, "error": ""}
    server = None
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

        status, caps = server.capability()
        capabilities = _parse_capabilities(caps)
        if status == "OK" and "IDLE" in capabilities:
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
            server.login(server_config["username"], server_config["password"])
            status, caps = server.capability()
            capabilities = _parse_capabilities(caps)
            server.logout()

        result["success"] = status == "OK"
        result["capabilities"] = capabilities
        result["idle_supported"] = "IDLE" in capabilities
    except Exception as e:
        result["error"] = str(e)
        if server:
            try:
                server.logout()
            except Exception:
                pass
    return result


def fetch_imap(server_config: dict, max_emails: int = 50) -> list:
    """Fetch emails from an IMAP server.

    Returns list of email dicts.
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

        server.login(server_config["username"], server_config["password"])
        server.select("INBOX")

        _, data = server.search(None, "ALL")
        if not data[0]:
            server.logout()
            return []

        msg_ids = data[0].split()
        fetch_count = min(len(msg_ids), max_emails)

        for msg_id in msg_ids[-fetch_count:]:
            try:
                _, msg_data = server.fetch(msg_id, "(RFC822)")
                raw_email = msg_data[0][1]
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
            for msg_id in msg_ids[-fetch_count:]:
                try:
                    server.store(msg_id, "+FLAGS", "\\Deleted")
                except Exception:
                    pass
            server.expunge()

        server.close()
        server.logout()
    except Exception as e:
        raise Exception(f"IMAP fetch failed: {e}")

    return emails_fetched


def fetch_emails(server_id: int) -> dict:
    """Fetch emails from a server configuration.

    Returns result dict with fetched count and any error.
    """
    # First get the server config (needs global DB for users, but email_servers
    # is per-user - read from user DB after we find the owner).
    # Scan all user DBs to find the server.  In practice callers pass a
    # server_id that belongs to the current user, so we only need a single
    # lookup.  We optimistically start with user_1, but the real approach
    # is to accept user_id – however the API only passes server_id, so we
    # find the owner at runtime.
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

    try:
        if server_config["incoming_protocol"].upper() == "POP3":
            emails = fetch_pop3(server_config)
        elif server_config["incoming_protocol"].upper() == "IMAP":
            emails = fetch_imap(server_config)
        else:
            return {"success": False, "error": f"Unsupported protocol: {server_config['incoming_protocol']}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Store fetched emails in the owner's per-user DB
    stored_count = 0
    conn = get_user_db(owner_id)
    cursor = conn.cursor()

    # Get existing message IDs to avoid duplicates
    cursor.execute(
        "SELECT message_id FROM emails WHERE user_id = ? AND server_id = ? AND message_id != ''",
        (owner_id, server_id),
    )
    existing = set(r["message_id"] for r in cursor.fetchall())

    for email_data in emails:
        # Skip if message_id exists (dedup)
        if email_data["message_id"] and email_data["message_id"] in existing:
            continue
        if email_data["message_id"]:
            existing.add(email_data["message_id"])

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
            "message_id, sender, sender_name, recipients, subject, body_text, body_html, "
            "received_date, folder, server_badge) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox', ?)",
            (
                owner_id,
                server_id,
                sg["id"],
                imp_id,
                email_data["message_id"],
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

    # Update last fetch time (in user DB)
    conn = get_user_db(owner_id)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE email_servers SET last_fetch_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), server_id),
    )
    conn.commit()
    conn.close()

    return {"success": True, "fetched": stored_count}


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
                server.login(server_config["username"], server_config["password"])
                server.select("INBOX")
                server.close()
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
