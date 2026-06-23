"""Auto-forward module - forward emails based on rules."""

from datetime import datetime
from modules import get_user_db
from modules.email_send import send_email


def get_forward_rules(user_id: int) -> list:
    """Get all forward rules for a user."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT fr.*, ig.name as importance_name, sg.group_name as sender_group_name "
        "FROM forward_rules fr "
        "LEFT JOIN importance_groups ig ON fr.importance_group_id = ig.id "
        "LEFT JOIN sender_groups sg ON fr.sender_group_id = sg.id "
        "WHERE fr.user_id = ? ORDER BY fr.id",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_forward_rule(user_id: int, forward_to: str,
                        importance_group_id: int = None,
                        sender_group_id: int = None) -> dict:
    """Create a new forward rule."""
    if not forward_to:
        return {"success": False, "error": "Forward email address required"}
    if not importance_group_id and not sender_group_id:
        return {"success": False, "error": "Group selection required"}

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO forward_rules (user_id, importance_group_id, sender_group_id, forward_to) "
        "VALUES (?, ?, ?, ?)",
        (user_id, importance_group_id, sender_group_id, forward_to),
    )
    conn.commit()
    rule_id = cursor.lastrowid
    conn.close()
    return {"success": True, "id": rule_id}


def update_forward_rule(rule_id: int, user_id: int, **kwargs) -> dict:
    """Update a forward rule."""
    allowed = ["forward_to", "importance_group_id", "sender_group_id", "enabled"]
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}

    if not updates:
        return {"success": False, "error": "No fields to update"}

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [rule_id, user_id]

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE forward_rules SET {set_clause} WHERE id = ? AND user_id = ?",
        values,
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return {"success": ok}


def delete_forward_rule(rule_id: int, user_id: int) -> dict:
    """Delete a forward rule."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM forward_rules WHERE id = ? AND user_id = ?",
        (rule_id, user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return {"success": ok}


def process_forward_rules(user_id: int, email_id: int) -> list:
    """Process forward rules for a newly received email.

    Returns list of forwarding results.
    """
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT e.*, es.id as es_id FROM emails e "
        "LEFT JOIN email_servers es ON e.server_id = es.id "
        "WHERE e.id = ? AND e.user_id = ?",
        (email_id, user_id),
    )
    email_data = cursor.fetchone()
    if not email_data:
        conn.close()
        return []

    email_data = dict(email_data)

    # Find matching rules
    cursor.execute(
        "SELECT * FROM forward_rules "
        "WHERE user_id = ? AND enabled = 1 "
        "AND (importance_group_id IS NULL OR importance_group_id = ? "
        "OR sender_group_id = ?)",
        (user_id, email_data["importance_group_id"], email_data["sender_group_id"]),
    )
    rules = cursor.fetchall()
    conn.close()

    results = []
    for rule in rules:
        # Check if this rule matches
        match = False
        if rule["sender_group_id"] and email_data["sender_group_id"] == rule["sender_group_id"]:
            match = True
        if rule["importance_group_id"] and email_data["importance_group_id"] == rule["importance_group_id"]:
            match = True

        if not match:
            continue

        # Find first available SMTP-enabled server
        conn2 = get_user_db(user_id)
        c2 = conn2.cursor()
        c2.execute(
            "SELECT id FROM email_servers WHERE user_id = ? AND outgoing_server IS NOT NULL AND outgoing_server != '' LIMIT 1",
            (user_id,),
        )
        srv = c2.fetchone()
        conn2.close()

        if srv:
            result = send_email(
                user_id=user_id,
                server_id=srv["id"],
                to_addr=rule["forward_to"],
                subject=f"Fwd: {email_data['subject']}",
                body_text=f"---------- Forwarded email ----------\n"
                          f"From: {email_data['sender']}\n"
                          f"Subject: {email_data['subject']}\n"
                          f"Date: {email_data['received_date']}\n\n"
                          f"{email_data['body_text']}",
            )
            results.append(result)

    return results
