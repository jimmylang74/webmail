"""Authentication module - user management, login, session handling."""

import hashlib
import os
import sqlite3
from modules import get_global_db, get_user_db, init_user_db


def _hash_password(password: str) -> str:
    """Hash password with SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def init_default_admin():
    """Create default admin user if no users exist."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    if count == 0:
        cursor.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("admin", _hash_password("1234"), "admin"),
        )
        conn.commit()
        uid = cursor.lastrowid
        conn.close()
        init_user_db(uid)
    else:
        conn.close()
        # Ensure all existing users have per-user databases (idempotent)
        c = get_global_db()
        for row in c.execute("SELECT id FROM users").fetchall():
            init_user_db(row["id"])
        c.close()


def authenticate(username: str, password: str):
    """Verify username and password. Returns user dict or None."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, role FROM users WHERE username = ? AND password = ?",
        (username, _hash_password(password)),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


def get_user(user_id: int):
    """Get user by ID."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


def list_users():
    """List all users."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str = "user"):
    """Create a new user. Returns True on success, False if username exists."""
    conn = get_global_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            (username, _hash_password(password), role),
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        # Initialize per-user database with schema and default groups
        init_user_db(user_id)
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def delete_user(user_id: int):
    """Delete a user by ID. Cannot delete admin."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row["role"] == "admin":
        conn.close()
        return False
    cursor.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()

    # Remove the per-user database file
    if deleted:
        from modules import get_user_db_path
        db_path = get_user_db_path(user_id)
        if os.path.exists(db_path):
            os.remove(db_path)

    return deleted


def change_password(user_id: int, new_password: str) -> bool:
    """Change user password."""
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password = ? WHERE id = ?",
        (_hash_password(new_password), user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return ok


def get_importance_groups(user_id: int):
    """Get importance groups for a user (reads from per-user DB)."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, sort_order FROM importance_groups WHERE user_id = ? ORDER BY sort_order",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]
