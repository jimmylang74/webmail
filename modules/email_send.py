"""Email sending module - SMTP protocol."""

import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from modules import get_user_db


def send_email(
    user_id: int,
    server_id: int,
    to_addr: str,
    subject: str,
    body_text: str,
    body_html: str = "",
) -> dict:
    """Send an email via SMTP.

    Args:
        user_id: User ID
        server_id: Email server configuration ID
        to_addr: Recipient email address
        subject: Email subject
        body_text: Plain text body
        body_html: HTML body (optional)

    Returns:
        dict with success/error info
    """
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM email_servers WHERE id = ? AND user_id = ?",
        (server_id, user_id),
    )
    server = cursor.fetchone()
    conn.close()

    if not server:
        return {"success": False, "error": "Server configuration not found"}
    if not server["outgoing_server"]:
        return {"success": False, "error": "No SMTP server configured"}

    server = dict(server)

    # Build message (no network I/O, done once regardless of retries)
    msg = MIMEMultipart("alternative") if body_html else MIMEText(body_text, "plain", "utf-8")
    msg["From"] = server["username"]
    msg["To"] = to_addr
    msg["Subject"] = subject

    if body_html:
        part1 = MIMEText(body_text, "plain", "utf-8")
        part2 = MIMEText(body_html, "html", "utf-8")
        msg.attach(part1)
        msg.attach(part2)

    smtp_port = server["outgoing_port"] or (465 if server["use_ssl"] else 587)

    # Retry loop for transient SSL / network errors
    last_error = None
    for attempt in range(3):
        smtp = None
        try:
            if server["use_ssl"]:
                smtp = smtplib.SMTP_SSL(
                    server["outgoing_server"],
                    smtp_port,
                    timeout=30,
                )
            else:
                smtp = smtplib.SMTP(
                    server["outgoing_server"],
                    smtp_port,
                    timeout=30,
                )
                smtp.starttls()

            smtp.login(server["username"], server["password"])
            smtp.sendmail(server["username"], [to_addr], msg.as_string())
            smtp.quit()
            smtp = None
            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(2)
        finally:
            if smtp:
                try:
                    smtp.quit()
                except Exception:
                    pass

    if last_error:
        return {"success": False, "error": str(last_error)}

    # Store in outbox
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO emails (user_id, server_id, sender, sender_name, recipients, "
        "subject, body_text, body_html, folder, server_badge) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'outbox', ?)",
        (
            user_id,
            server_id,
            server["username"],
            server["username"],
            to_addr,
            subject,
            body_text,
            body_html,
            server["server_name"],
        ),
    )
    conn.commit()
    conn.close()

    return {"success": True}


def save_draft(
    user_id: int,
    server_id: int,
    to_addr: str,
    subject: str,
    body_text: str,
    body_html: str = "",
    draft_id: int = None,
) -> dict:
    """Save or update a draft email."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    if draft_id:
        cursor.execute(
            "UPDATE emails SET sender=?, recipients=?, subject=?, body_text=?, "
            "body_html=?, server_id=? WHERE id=? AND user_id=? AND folder='drafts'",
            ("", to_addr, subject, body_text, body_html, server_id, draft_id, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO emails (user_id, server_id, sender, recipients, subject, "
            "body_text, body_html, folder) VALUES (?, ?, ?, ?, ?, ?, ?, 'drafts')",
            (user_id, server_id, "", to_addr, subject, body_text, body_html),
        )
        draft_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return {"success": True, "draft_id": draft_id}
