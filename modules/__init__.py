"""Email Client - Python modules.

Database architecture:
  - Global DB (db/email_client.db): users table only (shared auth).
  - Per-user DB (db/user_{id}.db): email_servers, importance_groups,
    sender_groups, emails, forward_rules (isolated per user).
"""

import os
import sqlite3

DB_DIR = os.path.join(
    os.path.abspath(os.path.dirname(os.path.dirname(__file__))), "db"
)
GLOBAL_DB_PATH = os.path.join(DB_DIR, "email_client.db")


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_global_db():
    """Open connection to the global (users) database."""
    conn = sqlite3.connect(GLOBAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_user_db_path(user_id: int) -> str:
    """Return filesystem path for a user's private database."""
    return os.path.join(DB_DIR, f"user_{user_id}.db")


def get_user_db(user_id: int):
    """Open connection to a user's private database."""
    path = get_user_db_path(user_id)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Backward-compatible alias
get_db = get_global_db


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

GLOBAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

USER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS email_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    server_name TEXT NOT NULL,
    incoming_server TEXT NOT NULL,
    incoming_port INTEGER,
    incoming_protocol TEXT NOT NULL DEFAULT 'POP3',
    outgoing_server TEXT,
    outgoing_port INTEGER,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    delete_after_download INTEGER DEFAULT 0,
    use_ssl INTEGER DEFAULT 1,
    last_fetch_at TIMESTAMP,
    fetch_interval_minutes INTEGER DEFAULT 0,
    imap_idle_supported INTEGER DEFAULT 0,
    use_imap_idle INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS importance_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sender_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sender_email TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    group_name TEXT NOT NULL,
    importance_group_id INTEGER,
    is_auto_classified INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (importance_group_id) REFERENCES importance_groups(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    server_id INTEGER,
    sender_group_id INTEGER,
    importance_group_id INTEGER,
    message_id TEXT,
    sender TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    recipients TEXT,
    subject TEXT DEFAULT '',
    body_text TEXT,
    body_html TEXT,
    received_date TIMESTAMP,
    is_read INTEGER DEFAULT 0,
    folder TEXT DEFAULT 'inbox',
    server_badge TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (server_id) REFERENCES email_servers(id) ON DELETE SET NULL,
    FOREIGN KEY (sender_group_id) REFERENCES sender_groups(id) ON DELETE SET NULL,
    FOREIGN KEY (importance_group_id) REFERENCES importance_groups(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS forward_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    importance_group_id INTEGER,
    sender_group_id INTEGER,
    forward_to TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (importance_group_id) REFERENCES importance_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (sender_group_id) REFERENCES sender_groups(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emails_user_folder ON emails(user_id, folder);
CREATE INDEX IF NOT EXISTS idx_emails_user_sender ON emails(user_id, sender);
CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_sender_groups_user ON sender_groups(user_id);
CREATE INDEX IF NOT EXISTS idx_forward_rules_user ON forward_rules(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imp_groups_user_name ON importance_groups(user_id, name);
"""


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def _init_user_groups(user_id: int):
    """Create default importance groups (Ad / Normal / Important) for a user."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    groups = [("Ad", 0), ("Normal", 1), ("Important", 2)]
    for name, sort_order in groups:
        cursor.execute(
            "INSERT OR IGNORE INTO importance_groups (user_id, name, sort_order) "
            "VALUES (?, ?, ?)",
            (user_id, name, sort_order),
        )
    conn.commit()
    conn.close()


def _deduplicate_importance_groups():
    """Remove duplicate importance groups per user, rewriting FK references.

    Keeps the row with the lowest id per (user_id, name), rewrites FKs in
    sender_groups / emails / forward_rules, then deletes extras.
    """
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users")
    user_ids = [r["id"] for r in cursor.fetchall()]
    conn.close()

    for uid in user_ids:
        db_path = get_user_db_path(uid)
        if not os.path.exists(db_path):
            continue
        conn = get_user_db(uid)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, MIN(id) as keep_id FROM importance_groups "
            "WHERE user_id = ? GROUP BY name HAVING COUNT(*) > 1",
            (uid,),
        )
        for dup in cursor.fetchall():
            name = dup["name"]
            keep_id = dup["keep_id"]
            cursor.execute(
                "SELECT id FROM importance_groups WHERE user_id=? AND name=? AND id!=?",
                (uid, name, keep_id),
            )
            for del_id in [r["id"] for r in cursor.fetchall()]:
                for tbl, col in (
                    ("sender_groups", "importance_group_id"),
                    ("emails", "importance_group_id"),
                    ("forward_rules", "importance_group_id"),
                ):
                    cursor.execute(
                        f"UPDATE {tbl} SET {col}=? WHERE {col}=?",
                        (keep_id, del_id),
                    )
                cursor.execute("DELETE FROM importance_groups WHERE id=?", (del_id,))
        conn.commit()
        conn.close()


def init_user_db(user_id: int):
    """Create the per-user database file and populate schema + defaults."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.executescript(USER_SCHEMA_SQL)
    conn.commit()

    for col_name, col_def in (
        ("imap_idle_supported", "INTEGER DEFAULT 0"),
        ("use_imap_idle", "INTEGER DEFAULT 0"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE email_servers ADD COLUMN {col_name} {col_def}"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()
    _init_user_groups(user_id)


def _migrate_from_old_db():
    """One-time migration from the old single-file DB to per-user databases.

    Reads the legacy per-user tables from db/email_client.db (the file that
    now serves as the global DB) and copies each user's rows into a dedicated
    user_{id}.db file.  Idempotent via INSERT OR IGNORE -- runs at most once
    per user because per-user rows become INSERT OR IGNORE'd and the per-user
    DB file presence acts as the sentinel for each user.
    """
    old_path = os.path.join(DB_DIR, "email_client.db")
    if not os.path.exists(old_path):
        return

    try:
        old_conn = sqlite3.connect(old_path)
        old_conn.row_factory = sqlite3.Row
        oc = old_conn.cursor()
        oc.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        if not oc.fetchone():
            old_conn.close()
            return

        oc.execute("SELECT * FROM users")
        old_users = oc.fetchall()
        if not old_users:
            old_conn.close()
            return

        for old_user in old_users:
            uid = old_user["id"]

            if os.path.exists(get_user_db_path(uid)):
                continue

            init_user_db(uid)

            # Copy per-user tables from old DB
            user_conn = get_user_db(uid)
            uc = user_conn.cursor()
            for table in (
                "email_servers",
                "importance_groups",
                "sender_groups",
                "emails",
                "forward_rules",
            ):
                try:
                    oc.execute(f"SELECT * FROM {table} WHERE user_id = ?", (uid,))
                    for row in oc.fetchall():
                        rd = dict(row)
                        cols = ", ".join(rd.keys())
                        ph = ", ".join(["?" for _ in rd])
                        uc.execute(
                            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({ph})",
                            list(rd.values()),
                        )
                except Exception:
                    continue  # table may not exist in old DB – skip
            user_conn.commit()
            user_conn.close()

        old_conn.close()
    except Exception:
        # Migration is best-effort; don't block startup
        pass


def init_db():
    """Initialize the global database and all per-user databases."""
    os.makedirs(DB_DIR, exist_ok=True)

    conn = get_global_db()
    cursor = conn.cursor()
    cursor.executescript(GLOBAL_SCHEMA_SQL)
    conn.commit()
    conn.close()

    # Ensure at least the default admin user exists BEFORE creating per-user DBs
    from modules.auth import init_default_admin
    init_default_admin()

    # One-time migration from old single-file database
    _migrate_from_old_db()

    # Ensure every user has a per-user DB with full schema (idempotent)
    conn = get_global_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users")
    for row in cursor.fetchall():
        init_user_db(row["id"])
    conn.close()

    _deduplicate_importance_groups()
