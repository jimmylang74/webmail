"""Email Client - Main Flask Application.

A web-based email client with POP3/IMAP support, automatic email classification,
sender grouping, and auto-forwarding rules.
"""

import argparse
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from modules.config import Config
from modules import init_db, get_user_db
from modules.i18n import set_language, T, get_locale_json
from modules.auth import (
    authenticate,
    change_password,
    create_user,
    delete_user,
    get_importance_groups,
    get_user,
    init_default_admin,
    list_users,
)
from modules.email_classify import (
    _extract_domain,
    auto_classify_senders,
    classify_unclassified_emails,
)
from modules.email_fetch import fetch_all_for_user, fetch_emails, get_all_fetch_progress, _update_progress, test_server_connection, check_imap_capabilities, download_all_emails, delete_from_server
from modules.email_send import save_draft, send_email
from modules.forward import (
    create_forward_rule,
    delete_forward_rule,
    get_forward_rules,
    process_forward_rules,
    update_forward_rule,
)
from modules.scheduler import scheduler

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder="web/templates",
    static_folder="web/static",
)
app.config.from_object(Config)
app.config["SECRET_KEY"] = Config.SECRET_KEY
# SESSION_COOKIE_NAME is set in main() after CLI port is resolved

set_language(Config.LANGUAGE)


@app.context_processor
def inject_i18n():
    return dict(lang=Config.LANGUAGE, T=T, locale_json=get_locale_json(Config.LANGUAGE))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def login_required(f):
    """Decorator: require valid user session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: require admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        if session.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


def json_body(f):
    """Decorator: parse JSON request body."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error": "JSON body required"}), 400
        return f(*args, **kwargs)
    return decorated


def _folder_stats(user_id: int):
    """Return counts for each folder."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    stats = {}
    for folder in ("inbox", "outbox", "drafts", "deleted"):
        cursor.execute(
            "SELECT COUNT(*) FROM emails WHERE user_id = ? AND folder = ?",
            (user_id, folder),
        )
        stats[folder] = cursor.fetchone()[0]
    # Unread inbox count
    cursor.execute(
        "SELECT COUNT(*) FROM emails WHERE user_id = ? AND folder = 'inbox' AND is_read = 0",
        (user_id,),
    )
    stats["unread"] = cursor.fetchone()[0]
    # Custom folder counts
    cursor.execute(
        "SELECT id, name FROM folders WHERE user_id=? AND is_system=0 ORDER BY sort_order, name",
        (user_id,),
    )
    stats["custom_folders"] = [dict(r) for r in cursor.fetchall()]
    for cf in stats["custom_folders"]:
        cursor.execute(
            "SELECT COUNT(*) FROM emails WHERE user_id=? AND folder_id=?",
            (user_id, cf["id"]),
        )
        cf["count"] = cursor.fetchone()[0]
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("mail_page"))
    return redirect(url_for("login_page"))


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html", port=Config.LOGIN_PORT)


@app.route("/admin", methods=["GET"])
@login_required
@admin_required
def admin_page():
    return render_template("admin.html")


@app.route("/mail", methods=["GET"])
@login_required
def mail_page():
    return render_template("mail.html")


@app.route("/config", methods=["GET"])
@login_required
def config_page():
    return render_template("config.html")


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
@json_body
def api_login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    existing_id = session.get("user_id")
    if existing_id is not None:
        user = authenticate(username, password)
        if user and user["id"] != existing_id:
            return jsonify({
                "success": False,
                "error": "请先退出当前账号再登录其他账号（同一浏览器无法同时登录不同账号）",
            }), 409
        if user:
            return jsonify({"success": True, "role": user["role"]})
        return jsonify({"success": False, "error": "Invalid credentials"}), 401

    user = authenticate(username, password)
    if user:
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        return jsonify({"success": True, "role": user["role"]})
    return jsonify({"success": False, "error": "Invalid credentials"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/session", methods=["GET"])
@login_required
def api_session():
    return jsonify({
        "user_id": session["user_id"],
        "username": session["username"],
        "role": session["role"],
    })


# ---------------------------------------------------------------------------
# Admin API - user management
# ---------------------------------------------------------------------------

@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def api_admin_list_users():
    users = list_users()
    return jsonify({"users": users})


@app.route("/api/admin/users", methods=["POST"])
@login_required
@admin_required
@json_body
def api_admin_create_user():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 3:
        return jsonify({"error": "Password must be at least 3 characters"}), 400

    ok = create_user(username, password)
    if ok:
        return jsonify({"success": True})
    return jsonify({"error": "Username already exists"}), 409


@app.route("/api/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def api_admin_delete_user(user_id):
    if user_id == session["user_id"]:
        return jsonify({"error": "Cannot delete yourself"}), 400
    ok = delete_user(user_id)
    if ok:
        return jsonify({"success": True})
    return jsonify({"error": "Cannot delete admin user or user not found"}), 400


@app.route("/api/admin/change-password", methods=["POST"])
@login_required
@json_body
def api_change_password():
    data = request.get_json()
    new_password = data.get("new_password", "")
    if len(new_password) < 3:
        return jsonify({"error": "Password must be at least 3 characters"}), 400
    ok = change_password(session["user_id"], new_password)
    return jsonify({"success": ok})


# ---------------------------------------------------------------------------
# Email servers API
# ---------------------------------------------------------------------------

@app.route("/api/servers", methods=["GET"])
@login_required
def api_get_servers():
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM email_servers WHERE user_id = ? ORDER BY id",
        (session["user_id"],),
    )
    servers = [dict(r) for r in cursor.fetchall()]
    conn.close()
    for srv in servers:
        srv.setdefault("imap_idle_supported", 0)
        srv.setdefault("use_imap_idle", 0)
    return jsonify({"servers": servers})


@app.route("/api/servers", methods=["POST"])
@login_required
@json_body
def api_add_server():
    data = request.get_json()
    required = ["server_name", "incoming_server", "username", "password", "incoming_protocol"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"'{field}' is required"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    use_imap_idle = 1 if data.get("use_imap_idle") else 0
    imap_idle_supported = 1 if use_imap_idle and data.get("incoming_protocol", "").upper() == "IMAP" else 0
    cursor.execute(
        """INSERT INTO email_servers
           (user_id, server_name, incoming_server, incoming_port, incoming_protocol,
            outgoing_server, outgoing_port, username, password,
            delete_after_download, use_ssl, fetch_interval_minutes, use_imap_idle,
            imap_idle_supported, max_emails_per_fetch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session["user_id"],
            data["server_name"],
            data["incoming_server"],
            data.get("incoming_port"),
            data["incoming_protocol"],
            data.get("outgoing_server", ""),
            data.get("outgoing_port"),
            data["username"],
            data["password"],
            1 if data.get("delete_after_download") else 0,
            1 if data.get("use_ssl", True) else 0,
            data.get("fetch_interval_minutes", 0),
            use_imap_idle,
            imap_idle_supported,
            data.get("max_emails_per_fetch", 50),
        ),
    )
    conn.commit()
    server_id = cursor.lastrowid
    conn.close()
    return jsonify({"success": True, "id": server_id})


@app.route("/api/servers/<int:server_id>", methods=["PUT"])
@login_required
@json_body
def api_update_server(server_id):
    data = request.get_json()
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    use_imap_idle = 1 if data.get("use_imap_idle") else 0
    imap_idle_supported = 1 if use_imap_idle and data.get("incoming_protocol", "").upper() == "IMAP" else 0
    cursor.execute(
        """UPDATE email_servers SET
           server_name=?, incoming_server=?, incoming_port=?, incoming_protocol=?,
           outgoing_server=?, outgoing_port=?, username=?, password=?,
           delete_after_download=?, use_ssl=?, fetch_interval_minutes=?, use_imap_idle=?,
           imap_idle_supported=?, max_emails_per_fetch=?
           WHERE id=? AND user_id=?""",
        (
            data.get("server_name", ""),
            data.get("incoming_server", ""),
            data.get("incoming_port"),
            data.get("incoming_protocol", "POP3"),
            data.get("outgoing_server", ""),
            data.get("outgoing_port"),
            data.get("username", ""),
            data.get("password", ""),
            1 if data.get("delete_after_download") else 0,
            1 if data.get("use_ssl", True) else 0,
            data.get("fetch_interval_minutes", 0),
            use_imap_idle,
            imap_idle_supported,
            data.get("max_emails_per_fetch", 50),
            server_id,
            session["user_id"],
        ),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/servers/<int:server_id>", methods=["DELETE"])
@login_required
def api_delete_server(server_id):
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM emails WHERE server_id=? AND user_id=?",
        (server_id, user_id),
    )
    cursor.execute(
        "DELETE FROM sender_groups WHERE user_id=? AND id NOT IN "
        "(SELECT DISTINCT sender_group_id FROM emails WHERE sender_group_id IS NOT NULL)",
        (user_id,),
    )
    cursor.execute(
        "UPDATE contacts SET default_server_id=NULL WHERE default_server_id=? AND user_id=?",
        (server_id, user_id),
    )
    cursor.execute(
        "DELETE FROM email_servers WHERE id=? AND user_id=?",
        (server_id, user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/servers/<int:server_id>/fetch", methods=["POST"])
@login_required
def api_fetch_server(server_id):
    user_id = session["user_id"]

    def _fetch_and_fwd():
        result = fetch_emails(server_id)
        if result.get("success"):
            classify_unclassified_emails(user_id)
            auto_classify_senders(user_id)
        elif not result.get("success"):
            logging.error("Fetch failed for server %d: %s", server_id, result.get("error"))

    thread = threading.Thread(target=_fetch_and_fwd, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Fetch started"})


@app.route("/api/servers/<int:server_id>/download-all", methods=["POST"])
@login_required
def api_download_all_server(server_id):
    user_id = session["user_id"]

    def _download_and_fwd():
        conn = get_user_db(user_id)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM emails WHERE user_id=? AND server_id=? AND folder IN ('inbox', 'deleted')",
            (user_id, server_id),
        )
        conn.commit()
        conn.close()

        result = download_all_emails(server_id, user_id)
        if result.get("success"):
            conn = get_user_db(user_id)
            cursor = conn.cursor()
            cursor.execute("SELECT server_name FROM email_servers WHERE id=?", (server_id,))
            row = cursor.fetchone()
            conn.close()
            server_name = row["server_name"] if row else ""

            _update_progress(server_id, 1, 0, "classifying", server_name)
            try:
                classify_unclassified_emails(user_id)
                auto_classify_senders(user_id)
            finally:
                _update_progress(server_id, 1, 1, "done", server_name)
        elif not result.get("success"):
            logging.error("Download all failed for server %d: %s", server_id, result.get("error"))

    thread = threading.Thread(target=_download_and_fwd, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Download all started"})


@app.route("/api/servers/<int:server_id>/test", methods=["POST"])
@login_required
def api_test_server(server_id):
    """Test a server configuration (incoming POP3/IMAP + optional SMTP)."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM email_servers WHERE id=? AND user_id=?",
        (server_id, session["user_id"]),
    )
    server = cursor.fetchone()
    conn.close()

    if not server:
        return jsonify({"success": False, "error": "Server not found"}), 404

    cfg = dict(server)
    # Temporarily pause the IDLE connection for this server (if any) so the
    # test connection does not exceed the server's per-user connection limit.
    is_idle = (
        cfg.get("incoming_protocol", "").upper() == "IMAP"
        and cfg.get("use_imap_idle")
    )
    if is_idle:
        scheduler._idle_manager.pause_connection(
            session["user_id"], server_id
        )
    try:
        result = test_server_connection(cfg)
    finally:
        if is_idle:
            scheduler._idle_manager.resume_connection(
                session["user_id"], server_id
            )

    return jsonify(result)


@app.route("/api/servers/<int:server_id>/idle-supported", methods=["POST"])
@login_required
@json_body
def api_server_idle_supported(server_id):
    """Check whether a saved IMAP server supports the IDLE capability."""
    data = request.get_json()
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM email_servers WHERE id=? AND user_id=?",
        (server_id, session["user_id"]),
    )
    server = cursor.fetchone()
    conn.close()

    if not server:
        return jsonify({"success": False, "error": "Server not found"}), 404

    cfg = dict(server)
    cfg["incoming_server"] = data.get("incoming_server", cfg["incoming_server"])
    cfg["incoming_port"] = data.get("incoming_port", cfg["incoming_port"])
    cfg["incoming_protocol"] = data.get("incoming_protocol", cfg["incoming_protocol"])
    cfg["use_ssl"] = data.get("use_ssl", cfg["use_ssl"])
    cfg["username"] = data.get("username", cfg["username"])
    cfg["password"] = data.get("password", cfg["password"])

    # Temporarily pause any active IDLE connection so the capability check
    # does not exceed the server's per-user connection limit.
    is_idle = cfg.get("incoming_protocol", "").upper() == "IMAP" and cfg.get("use_imap_idle")
    if is_idle:
        scheduler._idle_manager.pause_connection(session["user_id"], server_id)
    try:
        result = check_imap_capabilities(cfg)
    finally:
        if is_idle:
            scheduler._idle_manager.resume_connection(session["user_id"], server_id)

    if result["success"] and cfg["incoming_protocol"].upper() == "IMAP":
        uconn = get_user_db(session["user_id"])
        uc = uconn.cursor()
        uc.execute(
            "UPDATE email_servers SET imap_idle_supported=? WHERE id=? AND user_id=?",
            (1 if result["idle_supported"] else 0, server_id, session["user_id"]),
        )
        uconn.commit()
        uconn.close()

    return jsonify(result)


@app.route("/api/servers/check-idle", methods=["POST"])
@login_required
@json_body
def api_check_idle_unsaved():
    """Check IMAP IDLE support for an unsaved server configuration."""
    data = request.get_json()
    if not data.get("incoming_server") or data.get("incoming_protocol", "").upper() != "IMAP":
        return jsonify({"success": False, "idle_supported": False, "capabilities": []})

    cfg = {
        "incoming_server": data["incoming_server"],
        "incoming_port": data.get("incoming_port"),
        "incoming_protocol": "IMAP",
        "use_ssl": data.get("use_ssl", True),
        "username": data.get("username", ""),
        "password": data.get("password", ""),
    }
    return jsonify(check_imap_capabilities(cfg))


@app.route("/api/fetch-all", methods=["POST"])
@login_required
def api_fetch_all():
    user_id = session["user_id"]

    now = datetime.utcnow().isoformat()
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE email_servers SET last_fetch_at=? WHERE user_id=?",
        (now, user_id),
    )
    conn.commit()
    conn.close()

    def _fetch_all_and_fwd():
        results = fetch_all_for_user(user_id)
        classify_unclassified_emails(user_id)
        auto_classify_senders(user_id)

    thread = threading.Thread(target=_fetch_all_and_fwd, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Fetch started for all servers"})


# ---------------------------------------------------------------------------
# Mailbox API
# ---------------------------------------------------------------------------

def _build_inbox_children(user_id, cursor, imp_groups, server_id=None):
    """Build inbox-subtree children, optionally scoped to one server.

    When *server_id* is provided email counts and sender-groups are
    filtered to that server only.
    """
    server_where = "AND server_id = ?" if server_id else ""
    server_params = [server_id] if server_id else []
    suffix = f"_s{server_id}" if server_id else ""

    children = []
    for ig in imp_groups:
        cursor.execute(
            f"SELECT COUNT(*) FROM emails WHERE user_id=? AND folder='inbox' AND importance_group_id=? {server_where}",
            [user_id, ig["id"]] + server_params,
        )
        imp_count = cursor.fetchone()[0]
        cursor.execute(
            f"SELECT COUNT(*) FROM emails WHERE user_id=? AND folder='inbox' AND importance_group_id=? AND is_read=0 {server_where}",
            [user_id, ig["id"]] + server_params,
        )
        imp_unread = cursor.fetchone()[0]

        imp_node = {
            "id": f"imp_{ig['id']}{suffix}",
            "name": ig["name"],
            "icon": "flag",
            "count": imp_count,
            "unread": imp_unread,
            "imp_group_id": ig["id"],
            "is_system": ig.get("is_system", 0),
            "children": [],
        }
        if server_id:
            imp_node["server_id"] = server_id

        cursor.execute(
            f"""SELECT sg.id, sg.sender_email, sg.group_name,
                (SELECT COUNT(*) FROM emails WHERE sender_group_id=sg.id AND user_id=? AND folder='inbox' AND importance_group_id=? {server_where}) AS sender_count
                FROM sender_groups sg WHERE sg.user_id=? AND sg.id IN (
                    SELECT sender_group_id FROM emails WHERE user_id=? AND folder='inbox' AND importance_group_id=? {server_where}
                )
                ORDER BY sg.group_name""",
            [user_id, ig["id"]] + server_params + [user_id] + [user_id, ig["id"]] + server_params,
        )
        for sg in cursor.fetchall():
            if sg["sender_count"] > 0:
                cursor.execute(
                    f"SELECT COUNT(*) FROM emails WHERE user_id=? AND folder='inbox' AND sender_group_id=? AND is_read=0 {server_where}",
                    [user_id, sg["id"]] + server_params,
                )
                sg_unread = cursor.fetchone()[0]
                cursor.execute(
                    f"SELECT id, sender, sender_name, subject, received_date, is_read, server_badge "
                    f"FROM emails WHERE user_id=? AND folder='inbox' AND sender_group_id=? AND importance_group_id=? {server_where} "
                    f"ORDER BY received_date DESC LIMIT 50",
                    [user_id, sg["id"], ig["id"]] + server_params,
                )
                email_children = []
                for em in cursor.fetchall():
                    email_children.append({
                        "id": f"email_{em['id']}{suffix}",
                        "name": em["subject"] or "(No Subject)",
                        "email_id": em["id"],
                        "sender": em["sender"],
                        "sender_name": em["sender_name"],
                        "sender_group_name": sg["group_name"],
                        "is_read": em["is_read"],
                        "received_date": em["received_date"],
                        "server_badge": em["server_badge"],
                        "imp_group_id": ig["id"],
                        "sender_group_id": sg["id"],
                        "type": "email",
                    })

                sender_node = {
                    "id": f"sender_{sg['id']}{suffix}",
                    "name": sg["group_name"],
                    "email": sg["sender_email"],
                    "icon": "user",
                    "count": sg["sender_count"],
                    "unread": sg_unread,
                    "sender_group_id": sg["id"],
                    "imp_group_id": ig["id"],
                    "children": email_children,
                }
                if server_id:
                    sender_node["server_id"] = server_id
                imp_node["children"].append(sender_node)

        children.append(imp_node)
    return children


def _folder_email_flat_children(user_id, cursor, folder_name=None, folder_id=None, server_id=None, limit=100):
    """Build flat time-sorted email children for a folder.

    Supports filtering by folder name, folder_id, and/or server_id.
    Returns emails sorted by received_date DESC (newest first).
    """
    where_parts = ["user_id = ?"]
    params = [user_id]
    if folder_name:
        where_parts.append("folder = ?")
        params.append(folder_name)
    if folder_id:
        where_parts.append("folder_id = ?")
        params.append(folder_id)
    if server_id:
        where_parts.append("server_id = ?")
        params.append(server_id)
    where = " AND ".join(where_parts)

    cursor.execute(
        f"SELECT id, sender, sender_name, subject, received_date, is_read, server_badge, "
        f"importance_group_id, sender_group_id "
        f"FROM emails WHERE {where} ORDER BY received_date DESC LIMIT ?",
        params + [limit],
    )
    return [
        {
            "id": f"email_{em['id']}",
            "name": em["subject"] or "(No Subject)",
            "email_id": em["id"],
            "folder": folder_name,
            "folder_id": folder_id,
            "sender": em["sender"],
            "sender_name": em["sender_name"],
            "is_read": em["is_read"],
            "received_date": em["received_date"],
            "server_badge": em["server_badge"],
            "imp_group_id": em["importance_group_id"],
            "sender_group_id": em["sender_group_id"],
            "type": "email",
        }
        for em in cursor.fetchall()
    ]


@app.route("/api/mailbox/tree", methods=["GET"])
@login_required
def api_mailbox_tree():
    """Return the mailbox tree structure for the left menu."""
    user_id = session["user_id"]
    stats = _folder_stats(user_id)

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    imp_groups = get_importance_groups(user_id)

    folders = []
    sort_by_time = session.get("sort_by_time", False)

    cursor.execute(
        "SELECT id, server_name FROM email_servers WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    servers = cursor.fetchall()

    if session.get("group_by_server") and servers:
        for srv in servers:
            cursor.execute(
                "SELECT COUNT(*) FROM emails WHERE user_id=? AND folder='inbox' AND server_id=?",
                (user_id, srv["id"]),
            )
            total = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM emails WHERE user_id=? AND folder='inbox' AND server_id=? AND is_read=0",
                (user_id, srv["id"]),
            )
            unread = cursor.fetchone()[0]

            if sort_by_time:
                srv_children = _folder_email_flat_children(user_id, cursor, folder_name='inbox', server_id=srv["id"])
            else:
                srv_children = _build_inbox_children(user_id, cursor, imp_groups, server_id=srv["id"])

            srv_node = {
                "id": f"server_{srv['id']}",
                "name": srv["server_name"],
                "icon": "inbox",
                "server_id": srv["id"],
                "count": total,
                "unread": unread,
                "children": srv_children,
            }
            folders.append(srv_node)
    else:
        if sort_by_time:
            inbox_children = _folder_email_flat_children(user_id, cursor, folder_name='inbox')
        else:
            inbox_children = _build_inbox_children(user_id, cursor, imp_groups)
        folders.append({
            "id": "inbox",
            "name": "Inbox",
            "icon": "inbox",
            "count": stats["inbox"],
            "unread": stats["unread"],
            "children": inbox_children,
        })

    def _folder_email_children(cursor, folder_name):
        """Build tree email children for non-inbox folders."""
        cursor.execute(
            "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge "
            "FROM emails WHERE user_id=? AND folder=? "
            "ORDER BY received_date DESC LIMIT 50",
            (user_id, folder_name),
        )
        return [
            {
                "id": f"{folder_name}_{em['id']}",
                "name": em["subject"] or "(No Subject)",
                "email_id": em["id"],
                "folder": folder_name,
                "sender": em["sender"],
                "sender_name": em["sender_name"],
                "is_read": em["is_read"],
                "received_date": em["received_date"],
                "server_badge": em["server_badge"],
                "type": "email",
            }
            for em in cursor.fetchall()
        ]

    def _folder_sender_group_children(cursor, folder_name):
        children = []
        cursor.execute(
            "SELECT sg.id, sg.group_name, sg.sender_email, COUNT(*) as cnt "
            "FROM emails e JOIN sender_groups sg ON e.sender_group_id = sg.id "
            "WHERE e.user_id=? AND e.folder=? "
            "GROUP BY sg.id ORDER BY sg.group_name",
            (user_id, folder_name),
        )
        for sg in cursor.fetchall():
            cursor.execute(
                "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge, importance_group_id "
                "FROM emails WHERE user_id=? AND folder=? AND sender_group_id=? "
                "ORDER BY received_date DESC LIMIT 50",
                (user_id, folder_name, sg["id"]),
            )
            email_children = [
                {
                    "id": f"{folder_name}_{em['id']}",
                    "name": em["subject"] or "(No Subject)",
                    "email_id": em["id"],
                    "folder": folder_name,
                    "sender": em["sender"],
                    "sender_name": em["sender_name"],
                    "is_read": em["is_read"],
                    "received_date": em["received_date"],
                    "server_badge": em["server_badge"],
                    "imp_group_id": em["importance_group_id"],
                    "type": "email",
                }
                for em in cursor.fetchall()
            ]
            sg_imp_id = email_children[0]["imp_group_id"] if email_children else None
            children.append({
                "id": f"{folder_name}_sg_{sg['id']}",
                "name": sg["group_name"],
                "email": sg["sender_email"],
                "folder": folder_name,
                "icon": "user",
                "count": sg["cnt"],
                "sender_group_id": sg["id"],
                "imp_group_id": sg_imp_id,
                "children": email_children,
            })
        cursor.execute(
            "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge "
            "FROM emails WHERE user_id=? AND folder=? AND sender_group_id IS NULL "
            "ORDER BY received_date DESC LIMIT 50",
            (user_id, folder_name),
        )
        for em in cursor.fetchall():
            children.append({
                "id": f"{folder_name}_{em['id']}",
                "name": em["subject"] or "(No Subject)",
                "email_id": em["id"],
                "folder": folder_name,
                "sender": em["sender"],
                "sender_name": em["sender_name"],
                "is_read": em["is_read"],
                "received_date": em["received_date"],
                "server_badge": em["server_badge"],
                "type": "email",
            })
        return children

    if sort_by_time:
        flat_outbox = _folder_email_flat_children(user_id, cursor, folder_name='outbox')
        flat_drafts = _folder_email_flat_children(user_id, cursor, folder_name='drafts')
        flat_deleted = _folder_email_flat_children(user_id, cursor, folder_name='deleted')
    else:
        flat_outbox = _folder_email_children(cursor, "outbox")
        flat_drafts = _folder_email_children(cursor, "drafts")
        flat_deleted = _folder_sender_group_children(cursor, "deleted")
    folders.extend([
        {"id": "outbox", "name": "Outbox", "icon": "send", "count": stats["outbox"],
         "children": flat_outbox},
        {"id": "drafts", "name": "Drafts", "icon": "file-text", "count": stats["drafts"],
         "children": flat_drafts},
        {"id": "deleted", "name": "Deleted", "icon": "trash-2", "count": stats["deleted"],
         "children": flat_deleted},
    ])

    # Custom folders
    cursor.execute(
        "SELECT id, name, icon, sort_order FROM folders WHERE user_id=? AND is_system=0 ORDER BY sort_order, name",
        (user_id,),
    )
    for cf in cursor.fetchall():
        cursor.execute(
            "SELECT COUNT(*) FROM emails WHERE user_id=? AND folder_id=?",
            (user_id, cf["id"]),
        )
        cf_count = cursor.fetchone()[0]
        if sort_by_time:
            children = _folder_email_flat_children(user_id, cursor, folder_id=cf["id"])
            # Rebuild each child dict with the custom folder node-id prefix
            children = [
                {
                    "id": f"cf_{cf['id']}_{em['email_id']}",
                    "name": em["name"],
                    "email_id": em["email_id"],
                    "folder": cf["name"],
                    "folder_id": cf["id"],
                    "sender": em["sender"],
                    "sender_name": em["sender_name"],
                    "is_read": em["is_read"],
                    "received_date": em["received_date"],
                    "server_badge": em["server_badge"],
                    "imp_group_id": em["imp_group_id"],
                    "sender_group_id": em["sender_group_id"],
                    "type": "email",
                }
                for em in children
            ]
        else:
            children = []
            if cf_count > 0:
                cursor.execute(
                    "SELECT sg.id, sg.group_name, sg.sender_email, COUNT(*) as cnt "
                    "FROM emails e JOIN sender_groups sg ON e.sender_group_id = sg.id "
                    "WHERE e.user_id=? AND e.folder_id=? "
                    "GROUP BY sg.id ORDER BY sg.group_name",
                    (user_id, cf["id"]),
                )
                for sg in cursor.fetchall():
                    cursor.execute(
                        "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge, importance_group_id "
                        "FROM emails WHERE user_id=? AND folder_id=? AND sender_group_id=? "
                        "ORDER BY received_date DESC LIMIT 50",
                        (user_id, cf["id"], sg["id"]),
                    )
                    email_children = [
                        {
                            "id": f"cf_{cf['id']}_{em['id']}",
                            "name": em["subject"] or "(No Subject)",
                            "email_id": em["id"],
                            "folder": cf["name"],
                            "folder_id": cf["id"],
                            "sender": em["sender"],
                            "sender_name": em["sender_name"],
                            "is_read": em["is_read"],
                            "received_date": em["received_date"],
                            "server_badge": em["server_badge"],
                            "imp_group_id": em["importance_group_id"],
                            "sender_group_id": sg["id"],
                            "type": "email",
                        }
                        for em in cursor.fetchall()
                    ]
                    sg_imp_id = email_children[0]["imp_group_id"] if email_children else None
                    children.append({
                        "id": f"cf_{cf['id']}_sg_{sg['id']}",
                        "name": sg["group_name"],
                        "email": sg["sender_email"],
                        "folder": cf["name"],
                        "folder_id": cf["id"],
                        "icon": "user",
                        "count": sg["cnt"],
                        "sender_group_id": sg["id"],
                        "imp_group_id": sg_imp_id,
                        "children": email_children,
                    })
                cursor.execute(
                    "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge, importance_group_id "
                    "FROM emails WHERE user_id=? AND folder_id=? AND sender_group_id IS NULL "
                    "ORDER BY received_date DESC LIMIT 50",
                    (user_id, cf["id"]),
                )
                for em in cursor.fetchall():
                    children.append({
                        "id": f"cf_{cf['id']}_{em['id']}",
                        "name": em["subject"] or "(No Subject)",
                        "email_id": em["id"],
                        "folder": cf["name"],
                        "folder_id": cf["id"],
                        "sender": em["sender"],
                        "sender_name": em["sender_name"],
                        "is_read": em["is_read"],
                        "received_date": em["received_date"],
                        "server_badge": em["server_badge"],
                        "imp_group_id": em["importance_group_id"],
                        "type": "email",
                    })
        folders.append({
            "id": f"cf_{cf['id']}",
            "name": cf["name"],
            "icon": cf["icon"] or "folder",
            "count": cf_count,
            "folder_id": cf["id"],
            "is_custom_folder": True,
            "children": children,
        })

    conn.close()
    return jsonify({"folders": folders})


@app.route("/api/emails", methods=["GET"])
@login_required
def api_get_emails():
    """Get emails with filtering."""
    user_id = session["user_id"]
    folder = request.args.get("folder", "inbox")
    folder_id = request.args.get("folder_id", type=int)
    imp_group_id = request.args.get("imp_group_id", type=int)
    sender_group_id = request.args.get("sender_group_id", type=int)
    server_id = request.args.get("server_id", type=int)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "").strip()

    params = [user_id]
    where_clauses = ["user_id = ?"]
    if folder_id:
        where_clauses.append("folder_id = ?")
        params.append(folder_id)
    else:
        where_clauses.append("folder = ?")
        params.append(folder)

    if imp_group_id:
        where_clauses.append("importance_group_id = ?")
        params.append(imp_group_id)

    if sender_group_id:
        where_clauses.append("sender_group_id = ?")
        params.append(sender_group_id)

    if server_id:
        where_clauses.append("server_id = ?")
        params.append(server_id)

    if search:
        where_clauses.append("(subject LIKE ? OR sender LIKE ? OR sender_name LIKE ? OR body_text LIKE ?)")
        search_pattern = f"%{search}%"
        params.extend([search_pattern, search_pattern, search_pattern, search_pattern])

    where = " AND ".join(where_clauses)

    # Count total
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM emails WHERE {where}", params)
    total = cursor.fetchone()[0]

    # Fetch page
    offset = (page - 1) * per_page
    cursor.execute(
        f"SELECT id, sender, sender_name, subject, received_date, is_read, "
        f"server_badge, importance_group_id, sender_group_id "
        f"FROM emails WHERE {where} ORDER BY received_date DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )
    emails = [dict(r) for r in cursor.fetchall()]
    conn.close()

    return jsonify({"emails": emails, "total": total, "page": page, "per_page": per_page})


@app.route("/api/emails/<int:email_id>", methods=["GET"])
@login_required
def api_get_email(email_id):
    """Get a single email with full content."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT e.*, es.server_name as server_display_name "
        "FROM emails e LEFT JOIN email_servers es ON e.server_id = es.id "
        "WHERE e.id = ? AND e.user_id = ?",
        (email_id, session["user_id"]),
    )
    email_data = cursor.fetchone()
    if not email_data:
        conn.close()
        return jsonify({"error": "Email not found"}), 404

    email_data = dict(email_data)

    # Mark as read
    if not email_data["is_read"]:
        cursor.execute(
            "UPDATE emails SET is_read = 1 WHERE id = ?",
            (email_id,),
        )
        conn.commit()

    conn.close()
    return jsonify({"email": email_data})


@app.route("/api/emails/<int:email_id>/move", methods=["POST"])
@login_required
@json_body
def api_move_email(email_id):
    """Move email to a different folder."""
    data = request.get_json()
    folder = data.get("folder", "")
    folder_id = data.get("folder_id")
    if folder_id is not None:
        folder_id = int(folder_id)

    if folder_id:
        conn = get_user_db(session["user_id"])
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, is_system FROM folders WHERE id=? AND user_id=?",
            (folder_id, session["user_id"]),
        )
        f = cursor.fetchone()
        if not f:
            conn.close()
            return jsonify({"error": "Folder not found"}), 404
        folder = f["name"]
        cursor.execute(
            "UPDATE emails SET folder_id=?, folder=? WHERE id=? AND user_id=?",
            (folder_id, folder, email_id, session["user_id"]),
        )
    elif folder:
        if folder not in ("inbox", "outbox", "drafts", "deleted"):
            return jsonify({"error": "Invalid folder"}), 400
        conn = get_user_db(session["user_id"])
        cursor = conn.cursor()
        # Resolve folder_id for system folders
        cursor.execute(
            "SELECT id FROM folders WHERE user_id=? AND name=? AND is_system=1",
            (session["user_id"], folder),
        )
        sys_folder = cursor.fetchone()
        sys_folder_id = sys_folder["id"] if sys_folder else None

        if folder == "deleted":
            cursor.execute(
                "UPDATE emails SET original_folder = CASE WHEN original_folder IS NULL THEN folder ELSE original_folder END, "
                "folder_id=?, folder=? WHERE id=? AND user_id=?",
                (sys_folder_id, folder, email_id, session["user_id"]),
            )
        else:
            cursor.execute(
                "UPDATE emails SET folder_id=?, folder=? WHERE id=? AND user_id=?",
                (sys_folder_id, folder, email_id, session["user_id"]),
            )
    else:
        return jsonify({"error": "folder or folder_id required"}), 400

    conn.commit()
    ok = cursor.rowcount > 0

    # If moving to deleted AND user wants server-side deletion
    delete_server_result = None
    if ok and folder == "deleted":
        delete_from_server_flag = data.get("delete_from_server", False)
        if delete_from_server_flag:
            # Look up the email's server info and UID
            cursor.execute(
                "SELECT e.server_id, e.server_uid, es.incoming_protocol, es.incoming_server, "
                "es.incoming_port, es.username, es.password, es.use_ssl "
                "FROM emails e LEFT JOIN email_servers es ON e.server_id = es.id "
                "WHERE e.id=? AND e.user_id=?",
                (email_id, session["user_id"]),
            )
            email_info = cursor.fetchone()
            if email_info and email_info["server_uid"] and email_info["incoming_server"]:
                server_config = {
                    "incoming_protocol": email_info["incoming_protocol"],
                    "incoming_server": email_info["incoming_server"],
                    "incoming_port": email_info["incoming_port"],
                    "username": email_info["username"],
                    "password": email_info["password"],
                    "use_ssl": email_info["use_ssl"],
                }
                delete_server_result = delete_from_server(server_config, email_info["server_uid"])

    conn.close()

    if ok and folder == "deleted":
        # Process forward rules before marking deleted
        process_forward_rules(session["user_id"], email_id)

    response = {"success": ok}
    if delete_server_result is not None:
        response["delete_from_server"] = delete_server_result
    return jsonify(response)


@app.route("/api/emails/<int:email_id>/restore", methods=["POST"])
@login_required
def api_restore_email(email_id):
    """Restore a deleted email back to its original folder."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT folder, original_folder FROM emails WHERE id=? AND user_id=?",
        (email_id, session["user_id"]),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Email not found"}), 404

    if row["folder"] != "deleted":
        conn.close()
        return jsonify({"error": "Email is not in trash"}), 400

    target = row["original_folder"] if row["original_folder"] in ("inbox", "outbox", "drafts") else "inbox"

    cursor.execute(
        "UPDATE emails SET folder=?, original_folder=NULL WHERE id=? AND user_id=?",
        (target, email_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "restored_to": target})


@app.route("/api/emails/mark-read", methods=["POST"])
@login_required
@json_body
def api_mark_read():
    """Mark emails as read or unread for a given scope.

    Scope: 'folder' (inbox), 'imp', 'sender', or 'email'.
    Optional 'read' param (default true) controls read vs unread.
    """
    data = request.get_json()
    user_id = session["user_id"]
    scope = data.get("scope")
    is_read = 1 if data.get("read", True) else 0

    valid_scopes = ("folder", "imp", "sender", "email")
    if scope not in valid_scopes:
        return jsonify({"error": f"Invalid scope, must be one of {valid_scopes}"}), 400

    conn = get_user_db(user_id)
    cursor = conn.cursor()

    if scope == "folder":
        server_id = data.get("server_id")
        if server_id:
            cursor.execute(
                "UPDATE emails SET is_read=? WHERE user_id=? AND folder='inbox' AND server_id=?",
                (is_read, user_id, server_id),
            )
        else:
            cursor.execute(
                "UPDATE emails SET is_read=? WHERE user_id=? AND folder='inbox'",
                (is_read, user_id),
            )
    elif scope == "imp":
        imp_group_id = data.get("imp_group_id")
        if not imp_group_id:
            conn.close()
            return jsonify({"error": "imp_group_id required"}), 400
        cursor.execute(
            "UPDATE emails SET is_read=? WHERE user_id=? AND folder='inbox' AND importance_group_id=?",
            (is_read, user_id, imp_group_id),
        )
    elif scope == "sender":
        sender_group_id = data.get("sender_group_id")
        if not sender_group_id:
            conn.close()
            return jsonify({"error": "sender_group_id required"}), 400
        cursor.execute(
            "UPDATE emails SET is_read=? WHERE user_id=? AND folder='inbox' AND sender_group_id=?",
            (is_read, user_id, sender_group_id),
        )
    elif scope == "email":
        email_id = data.get("email_id")
        if not email_id:
            conn.close()
            return jsonify({"error": "email_id required"}), 400
        cursor.execute(
            "UPDATE emails SET is_read=? WHERE id=? AND user_id=?",
            (is_read, email_id, user_id),
        )

    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return jsonify({"success": True, "affected": affected})


@app.route("/api/emails/<int:email_id>", methods=["DELETE"])
@login_required
def api_delete_email(email_id):
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM emails WHERE id=? AND user_id=?",
        (email_id, session["user_id"]),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/emails/group/<int:sender_group_id>", methods=["DELETE"])
@login_required
def api_delete_group_emails(sender_group_id):
    """Move all emails in a sender group to trash.

    Query param: delete_from_server=1  — also delete from server for each email.
    Query param: imp_group_id=N       — only delete emails in this importance group.
    """
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    imp_group_id = request.args.get("imp_group_id", type=int)

    if imp_group_id:
        cursor.execute(
            "SELECT id, server_id, server_uid, folder FROM emails WHERE sender_group_id=? AND user_id=? AND importance_group_id=?",
            (sender_group_id, session["user_id"], imp_group_id),
        )
    else:
        cursor.execute(
            "SELECT id, server_id, server_uid, folder FROM emails WHERE sender_group_id=? AND user_id=?",
            (sender_group_id, session["user_id"]),
        )
    rows = cursor.fetchall()

    if imp_group_id:
        cursor.execute(
            "UPDATE emails SET folder='deleted' WHERE sender_group_id=? AND user_id=? AND importance_group_id=?",
            (sender_group_id, session["user_id"], imp_group_id),
        )
    else:
        cursor.execute(
            "UPDATE emails SET folder='deleted' WHERE sender_group_id=? AND user_id=?",
            (sender_group_id, session["user_id"]),
        )
    conn.commit()
    deleted = cursor.rowcount

    server_results = []
    delete_from_server_flag = request.args.get("delete_from_server", "0") == "1"
    if delete_from_server_flag:
        for row in rows:
            if row["folder"] == "drafts":
                continue
            if row["server_uid"] and row["server_id"]:
                cursor.execute(
                    "SELECT incoming_protocol, incoming_server, incoming_port, username, password, use_ssl "
                    "FROM email_servers WHERE id=? AND user_id=?",
                    (row["server_id"], session["user_id"]),
                )
                srv = cursor.fetchone()
                if srv and srv["incoming_server"]:
                    sc = {
                        "incoming_protocol": srv["incoming_protocol"],
                        "incoming_server": srv["incoming_server"],
                        "incoming_port": srv["incoming_port"],
                        "username": srv["username"],
                        "password": srv["password"],
                        "use_ssl": srv["use_ssl"],
                    }
                    server_results.append({
                        "email_id": row["id"],
                        "result": delete_from_server(sc, row["server_uid"]),
                    })

    conn.close()
    resp = {"success": True, "deleted": deleted}
    if server_results:
        resp["server_results"] = server_results
    return jsonify(resp)


@app.route("/api/emails/group/<int:sender_group_id>/delete-progress", methods=["POST"])
@login_required
def api_delete_group_emails_with_progress(sender_group_id):
    """Delete all emails in a sender group with progress tracking.

    Query param: delete_from_server=1 — also delete from server for each email.
    Query param: imp_group_id=N       — only delete emails in this importance group.
    Returns a task_id immediately; frontend polls GET /api/delete-progress/<task_id>.
    """
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    imp_group_id = request.args.get("imp_group_id", type=int)

    if imp_group_id:
        cursor.execute(
            "SELECT id, server_id, server_uid, folder "
            "FROM emails WHERE sender_group_id=? AND user_id=? AND importance_group_id=?",
            (sender_group_id, session["user_id"], imp_group_id),
        )
    else:
        cursor.execute(
            "SELECT id, server_id, server_uid, folder "
            "FROM emails WHERE sender_group_id=? AND user_id=?",
            (sender_group_id, session["user_id"]),
        )
    rows = cursor.fetchall()

    delete_from_server_flag = request.args.get("delete_from_server", "0") == "1"

    # Split: emails that can't be server-deleted move to deleted now;
    #        emails that need server deletion go to background thread.
    noop_row_ids = []
    server_rows = []
    for row in rows:
        if delete_from_server_flag and row["folder"] != "drafts" and row["server_uid"] and row["server_id"]:
            server_rows.append(row)
        else:
            noop_row_ids.append(row["id"])

    # Instantly move emails that don't need server deletion
    if noop_row_ids:
        placeholders = ",".join("?" * len(noop_row_ids))
        cursor.execute(
            f"UPDATE emails SET folder='deleted' WHERE id IN ({placeholders}) AND user_id=?",
            (*noop_row_ids, session["user_id"]),
        )

    conn.commit()

    task_id = str(uuid.uuid4())

    if server_rows:
        with _delete_progress_lock:
            _delete_progress[task_id] = {
                "total": len(server_rows),
                "current": 0,
                "status": "running",
                "error": None,
            }

        threading.Thread(
            target=_background_delete_group_emails,
            args=(task_id, session["user_id"], server_rows),
            daemon=True,
        ).start()

    conn.close()
    return jsonify({
        "task_id": task_id if server_rows else None,
        "total": len(rows),
        "server_delete_total": len(server_rows),
    })


@app.route("/api/delete-progress/<task_id>", methods=["GET"])
@login_required
def api_delete_progress(task_id):
    with _delete_progress_lock:
        entry = _delete_progress.get(task_id)

    if entry is None:
        return jsonify({"status": "not_found"})

    resp = {
        "total": entry["total"],
        "current": entry["current"],
        "status": entry["status"],
    }
    if entry.get("error"):
        resp["error"] = entry["error"]

    if entry["status"] in ("done", "partial", "error"):
        with _delete_progress_lock:
            _delete_progress.pop(task_id, None)

    return jsonify(resp)


@app.route("/api/emails/group/<int:sender_group_id>/move", methods=["POST"])
@login_required
@json_body
def api_move_group_to_folder(sender_group_id):
    data = request.get_json()
    folder = data.get("folder", "")
    folder_id = data.get("folder_id")

    if folder_id:
        folder_id = int(folder_id)
        conn = get_user_db(session["user_id"])
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM folders WHERE id=? AND user_id=?",
            (folder_id, session["user_id"]),
        )
        f = cursor.fetchone()
        if not f:
            conn.close()
            return jsonify({"error": "Folder not found"}), 404
        folder = f["name"]
        cursor.execute(
            "UPDATE emails SET folder=?, folder_id=? WHERE sender_group_id=? AND user_id=?",
            (folder, folder_id, sender_group_id, session["user_id"]),
        )
    elif folder:
        if folder not in ("inbox", "outbox", "drafts"):
            return jsonify({"error": "Invalid folder"}), 400
        conn = get_user_db(session["user_id"])
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM folders WHERE user_id=? AND name=? AND is_system=1",
            (session["user_id"], folder),
        )
        sys_folder = cursor.fetchone()
        sys_folder_id = sys_folder["id"] if sys_folder else None
        cursor.execute(
            "UPDATE emails SET folder=?, folder_id=? WHERE sender_group_id=? AND user_id=?",
            (folder, sys_folder_id, sender_group_id, session["user_id"]),
        )
    else:
        return jsonify({"error": "folder or folder_id required"}), 400

    conn.commit()
    moved = cursor.rowcount
    conn.close()
    return jsonify({"success": True, "moved": moved})


@app.route("/api/emails/group/importance/<int:imp_group_id>", methods=["DELETE"])
@login_required
def api_delete_importance_group_emails(imp_group_id):
    """Move all emails in an importance group to trash.

    Query param: delete_from_server=1  — also delete from server for each email.
    """
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, server_id, server_uid FROM emails WHERE importance_group_id=? AND user_id=? AND folder='inbox'",
        (imp_group_id, session["user_id"]),
    )
    rows = cursor.fetchall()

    cursor.execute(
        "UPDATE emails SET folder='deleted' WHERE importance_group_id=? AND user_id=? AND folder='inbox'",
        (imp_group_id, session["user_id"]),
    )
    conn.commit()
    deleted = cursor.rowcount

    server_results = []
    delete_from_server_flag = request.args.get("delete_from_server", "0") == "1"
    if delete_from_server_flag:
        for row in rows:
            if row["server_uid"] and row["server_id"]:
                cursor.execute(
                    "SELECT incoming_protocol, incoming_server, incoming_port, username, password, use_ssl "
                    "FROM email_servers WHERE id=? AND user_id=?",
                    (row["server_id"], session["user_id"]),
                )
                srv = cursor.fetchone()
                if srv and srv["incoming_server"]:
                    sc = {
                        "incoming_protocol": srv["incoming_protocol"],
                        "incoming_server": srv["incoming_server"],
                        "incoming_port": srv["incoming_port"],
                        "username": srv["username"],
                        "password": srv["password"],
                        "use_ssl": srv["use_ssl"],
                    }
                    server_results.append({
                        "email_id": row["id"],
                        "result": delete_from_server(sc, row["server_uid"]),
                    })

    conn.close()
    resp = {"success": True, "deleted": deleted}
    if server_results:
        resp["server_results"] = server_results
    return jsonify(resp)


@app.route("/api/emails/group/importance/<int:imp_group_id>/delete-progress", methods=["POST"])
@login_required
def api_delete_importance_group_emails_with_progress(imp_group_id):
    """Delete all emails in an importance group with progress tracking.

    Query param: delete_from_server=1 — also delete from server for each email.
    """
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, server_id, server_uid, folder "
        "FROM emails WHERE importance_group_id=? AND user_id=? AND folder='inbox'",
        (imp_group_id, session["user_id"]),
    )
    rows = cursor.fetchall()

    cursor.execute(
        "UPDATE emails SET folder='deleted' WHERE importance_group_id=? AND user_id=? AND folder='inbox'",
        (imp_group_id, session["user_id"]),
    )
    conn.commit()

    delete_from_server_flag = request.args.get("delete_from_server", "0") == "1"

    server_delete_total = 0
    server_rows = []
    if delete_from_server_flag:
        for row in rows:
            if row["server_uid"] and row["server_id"]:
                server_delete_total += 1
                server_rows.append(row)

    task_id = str(uuid.uuid4())

    if server_delete_total > 0:
        with _delete_progress_lock:
            _delete_progress[task_id] = {
                "total": server_delete_total,
                "current": 0,
                "status": "running",
                "error": None,
            }

        threading.Thread(
            target=_background_delete_group_emails,
            args=(task_id, session["user_id"], server_rows),
            daemon=True,
        ).start()

    conn.close()
    return jsonify({
        "task_id": task_id if server_delete_total > 0 else None,
        "total": len(rows),
        "server_delete_total": server_delete_total,
    })


@app.route("/api/emails/clear-deleted", methods=["POST"])
@login_required
def api_clear_deleted():
    """Permanently delete ALL emails in the Deleted folder."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM emails WHERE user_id=? AND folder='deleted'",
        (session["user_id"],),
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return jsonify({"success": True, "deleted": deleted})


@app.route("/api/emails/deleted-group/<int:sender_group_id>", methods=["POST"])
@login_required
@json_body
def api_deleted_group_action(sender_group_id):
    """Bulk action on all emails of a sender group inside the Deleted folder.

    JSON body: {"action": "restore"} — restore all to original folder.
               {"action": "clear"}   — permanently delete from DB.
    """
    data = request.get_json()
    action = data.get("action", "")
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    if action == "restore":
        # Restore each email to its original_folder
        cursor.execute(
            "SELECT id, original_folder FROM emails WHERE user_id=? AND sender_group_id=? AND folder='deleted'",
            (user_id, sender_group_id),
        )
        rows = cursor.fetchall()
        for row in rows:
            target = row["original_folder"] if row["original_folder"] in ("inbox", "outbox", "drafts") else "inbox"
            cursor.execute(
                "UPDATE emails SET folder=?, original_folder=NULL WHERE id=? AND user_id=?",
                (target, row["id"], user_id),
            )
        affected = len(rows)
    elif action == "clear":
        cursor.execute(
            "DELETE FROM emails WHERE user_id=? AND sender_group_id=? AND folder='deleted'",
            (user_id, sender_group_id),
        )
        affected = cursor.rowcount
    else:
        conn.close()
        return jsonify({"error": "Invalid action"}), 400

    conn.commit()
    conn.close()
    return jsonify({"success": True, "affected": affected})


@app.route("/api/emails/<int:email_id>/move-importance", methods=["POST"])
@login_required
@json_body
def api_move_email_importance(email_id):
    """Move a single email to a different importance group.

    Creates a sender subgroup in the target importance if missing, and cleans
    up the source sender subgroup if it becomes empty. Unlike moving an entire
    sender group (PUT /api/sender-groups/<id>), this does NOT auto-deduplicate
    when the same sender appears across multiple importance levels — the user
    is explicitly choosing to distribute a sender's emails.
    """
    data = request.get_json()
    target_imp_id = data.get("importance_group_id")
    if not target_imp_id:
        return jsonify({"error": "importance_group_id required"}), 400

    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, sender, sender_name, sender_group_id, importance_group_id, folder, folder_id "
        "FROM emails WHERE id=? AND user_id=?",
        (email_id, user_id),
    )
    email = cursor.fetchone()
    if not email:
        conn.close()
        return jsonify({"error": "Email not found"}), 404

    old_sender_group_id = email["sender_group_id"]
    old_imp_id = email["importance_group_id"]

    if old_imp_id == target_imp_id:
        conn.close()
        return jsonify({"success": True})

    sender_email = email["sender"]
    sender_name = email["sender_name"] or ""

    if old_sender_group_id:
        cursor.execute(
            "SELECT sender_name, group_name FROM sender_groups WHERE id=?",
            (old_sender_group_id,),
        )
        old_sg = cursor.fetchone()
        if old_sg and not sender_name:
            sender_name = old_sg["sender_name"]

    sender_domain = _extract_domain(sender_email)

    cursor.execute(
        "SELECT sg.id FROM sender_groups sg "
        "WHERE sg.user_id=? AND sg.sender_domain=? AND sg.importance_group_id=?",
        (user_id, sender_domain, target_imp_id),
    )
    target_sg = cursor.fetchone()

    if target_sg:
        new_sender_group_id = target_sg["id"]
    else:
        group_name = sender_domain if sender_domain else sender_email
        cursor.execute(
            "INSERT INTO sender_groups (user_id, sender_email, sender_name, sender_domain, group_name, importance_group_id, is_auto_classified) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (user_id, sender_email, sender_name, sender_domain, group_name, target_imp_id),
        )
        new_sender_group_id = cursor.lastrowid

    update_fields = {"importance_group_id": target_imp_id, "sender_group_id": new_sender_group_id}
    if email["folder"] != "inbox":
        update_fields["folder"] = "inbox"
        update_fields["folder_id"] = None

    set_clause = ", ".join(f"{k}=?" for k in update_fields)
    values = list(update_fields.values()) + [email_id, user_id]
    cursor.execute(
        f"UPDATE emails SET {set_clause} WHERE id=? AND user_id=?",
        values,
    )

    if old_sender_group_id and old_sender_group_id != new_sender_group_id:
        cursor.execute(
            "SELECT COUNT(*) FROM emails WHERE sender_group_id=? AND user_id=? AND folder='inbox'",
            (old_sender_group_id, user_id),
        )
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute("DELETE FROM sender_groups WHERE id=?", (old_sender_group_id,))

    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Sender groups API
# ---------------------------------------------------------------------------

@app.route("/api/sender-groups", methods=["GET"])
@login_required
def api_get_sender_groups():
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sg.*, ig.name as importance_name "
        "FROM sender_groups sg "
        "LEFT JOIN importance_groups ig ON sg.importance_group_id = ig.id "
        "WHERE sg.user_id=? ORDER BY sg.group_name",
        (session["user_id"],),
    )
    groups = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"sender_groups": groups})


def _dedup_sender_groups(cursor, user_id, sender_domain):
    """Consolidate duplicate sender groups to the lowest importance level.

    If a domain has sender groups in multiple importance levels, all emails are
    consolidated into the lowest level (Ad < Normal < Important) and the
    higher-level groups are deleted.
    """
    cursor.execute(
        "SELECT sg.id, sg.importance_group_id, ig.sort_order "
        "FROM sender_groups sg "
        "JOIN importance_groups ig ON sg.importance_group_id = ig.id "
        "WHERE sg.user_id=? AND sg.sender_domain=?",
        (user_id, sender_domain),
    )
    groups = cursor.fetchall()

    if len(groups) > 1:
        lowest = min(groups, key=lambda r: r["sort_order"])
        lowest_id = lowest["id"]
        lowest_imp_id = lowest["importance_group_id"]

        for g in groups:
            if g["id"] != lowest_id:
                cursor.execute(
                    "UPDATE emails SET sender_group_id=?, importance_group_id=? WHERE sender_group_id=? AND user_id=?",
                    (lowest_id, lowest_imp_id, g["id"], user_id),
                )
                cursor.execute("DELETE FROM sender_groups WHERE id=?", (g["id"],))


@app.route("/api/sender-groups/<int:group_id>", methods=["PUT"])
@login_required
@json_body
def api_update_sender_group(group_id):
    """Update a sender group (change importance group assignment or name)."""
    data = request.get_json()
    importance_group_id = data.get("importance_group_id")
    group_name = data.get("group_name")

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    # Build dynamic SET clause for provided fields
    server_id = data.get("server_id")

    updates = []
    params = []

    if importance_group_id is not None and not server_id:
        updates.append("importance_group_id=?")
        params.append(importance_group_id)
        updates.append("is_auto_classified=0")

    if group_name is not None:
        updates.append("group_name=?")
        params.append(group_name)
        updates.append("is_auto_classified=0")

    if updates:
        params.extend([group_id, session["user_id"]])
        cursor.execute(
            f"UPDATE sender_groups SET {', '.join(updates)} WHERE id=? AND user_id=?",
            params,
        )
        ok = cursor.rowcount > 0
    else:
        ok = True

    if importance_group_id is not None:
        if server_id:
            cursor.execute(
                "SELECT sender_domain FROM sender_groups WHERE id=? AND user_id=?",
                (group_id, session["user_id"]),
            )
            sg_info = cursor.fetchone()
            if sg_info:
                sender_domain = sg_info["sender_domain"]

                existing = cursor.execute(
                    "SELECT id, sender_email, sender_name FROM sender_groups WHERE user_id=? AND sender_domain=? AND importance_group_id=?",
                    (session["user_id"], sender_domain, importance_group_id),
                ).fetchone()
                if existing:
                    target_sg_id = existing["id"]
                else:
                    cursor.execute(
                        "SELECT sender, sender_name FROM emails WHERE sender_group_id=? AND user_id=? AND server_id=? LIMIT 1",
                        (group_id, session["user_id"], server_id),
                    )
                    sample = cursor.fetchone()
                    if sample:
                        sender_email = sample["sender"]
                        sender_name = sample["sender_name"] or sender_email.split("@")[0]
                        cursor.execute(
                            "INSERT INTO sender_groups (user_id, sender_email, sender_name, sender_domain, group_name, importance_group_id, is_auto_classified) "
                            "VALUES (?, ?, ?, ?, ?, ?, 0)",
                            (session["user_id"], sender_email, sender_name, sender_domain, sender_name, importance_group_id),
                        )
                        target_sg_id = cursor.lastrowid
                    else:
                        target_sg_id = None

                if target_sg_id:
                    cursor.execute(
                        "UPDATE emails SET sender_group_id=?, importance_group_id=? WHERE sender_group_id=? AND user_id=? AND server_id=?",
                        (target_sg_id, importance_group_id, group_id, session["user_id"], server_id),
                    )

            cursor.execute(
                "SELECT COUNT(*) FROM emails WHERE sender_group_id=?", (group_id,)
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute("DELETE FROM sender_groups WHERE id=?", (group_id,))
        else:
            cursor.execute(
                "UPDATE emails SET importance_group_id=? WHERE sender_group_id=? AND user_id=?",
                (importance_group_id, group_id, session["user_id"]),
            )

            cursor.execute(
                "SELECT sender_domain FROM sender_groups WHERE id=? AND user_id=?",
                (group_id, session["user_id"]),
            )
            sg = cursor.fetchone()
            if sg:
                sender_domain = sg["sender_domain"]
                _dedup_sender_groups(cursor, session["user_id"], sender_domain)

    conn.commit()
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/sender-groups/auto-classify", methods=["POST"])
@login_required
def api_auto_classify():
    """Run auto-classification on all senders."""
    auto_classify_senders(session["user_id"])
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Compose / Draft API
# ---------------------------------------------------------------------------

@app.route("/api/compose", methods=["POST"])
@login_required
@json_body
def api_compose():
    """Send an email."""
    data = request.get_json()
    server_id = data.get("server_id")
    to_addr = data.get("to", "").strip()
    subject = data.get("subject", "")
    body_text = data.get("body_text", "")
    body_html = data.get("body_html", "")

    if not server_id:
        return jsonify({"error": "Server selection required"}), 400
    if not to_addr:
        return jsonify({"error": "Recipient required"}), 400
    if "@" not in to_addr:
        return jsonify({"error": "Invalid email address"}), 400

    result = send_email(
        session["user_id"], server_id, to_addr, subject, body_text, body_html,
    )
    return jsonify(result)


@app.route("/api/drafts", methods=["GET"])
@login_required
def api_get_drafts():
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, sender, recipients, subject, body_text, body_html, created_at "
        "FROM emails WHERE user_id=? AND folder='drafts' ORDER BY created_at DESC",
        (session["user_id"],),
    )
    drafts = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"drafts": drafts})


@app.route("/api/drafts", methods=["POST"])
@login_required
@json_body
def api_save_draft():
    data = request.get_json()
    result = save_draft(
        user_id=session["user_id"],
        server_id=data.get("server_id"),
        to_addr=data.get("to", ""),
        subject=data.get("subject", ""),
        body_text=data.get("body_text", ""),
        body_html=data.get("body_html", ""),
        draft_id=data.get("draft_id"),
    )
    return jsonify(result)


# ---------------------------------------------------------------------------
# Forward rules API
# ---------------------------------------------------------------------------

@app.route("/api/forward-rules", methods=["GET"])
@login_required
def api_get_forward_rules():
    rules = get_forward_rules(session["user_id"])
    return jsonify({"rules": rules})


@app.route("/api/forward-rules", methods=["POST"])
@login_required
@json_body
def api_create_forward_rule():
    data = request.get_json()
    result = create_forward_rule(
        user_id=session["user_id"],
        forward_to=data.get("forward_to", ""),
        importance_group_id=data.get("importance_group_id"),
        sender_group_id=data.get("sender_group_id"),
    )
    return jsonify(result)


@app.route("/api/forward-rules/<int:rule_id>", methods=["PUT"])
@login_required
@json_body
def api_update_forward_rule(rule_id):
    data = request.get_json()
    result = update_forward_rule(
        rule_id, session["user_id"],
        forward_to=data.get("forward_to"),
        importance_group_id=data.get("importance_group_id"),
        sender_group_id=data.get("sender_group_id"),
        enabled=data.get("enabled"),
    )
    return jsonify(result)


@app.route("/api/forward-rules/<int:rule_id>", methods=["DELETE"])
@login_required
def api_delete_forward_rule(rule_id):
    result = delete_forward_rule(rule_id, session["user_id"])
    return jsonify(result)


# ---------------------------------------------------------------------------
# Stats API
# ---------------------------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
@login_required
def api_stats():
    stats = _folder_stats(session["user_id"])
    return jsonify(stats)


@app.route("/api/fetch-progress", methods=["GET"])
@login_required
def api_fetch_progress():
    """Return current fetch progress for all servers."""
    progress = get_all_fetch_progress()
    return jsonify({"servers": progress})


@app.route("/api/next-fetch", methods=["GET"])
@login_required
def api_next_fetch():
    """Return the next scheduled automatic fetch time for the current user."""
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, server_name, last_fetch_at, fetch_interval_minutes, use_imap_idle "
        "FROM email_servers WHERE user_id=?",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    now = datetime.utcnow()
    next_fetch = None
    server_name = None
    interval_minutes = None
    servers = []

    for row in rows:
        use_idle = bool(row["use_imap_idle"])
        interval = row["fetch_interval_minutes"] or 0
        last_fetch = row["last_fetch_at"]
        seconds_until = None
        next_fetch_at = None

        if use_idle:
            mode = "imap_idle"
        elif interval > 0:
            mode = "auto"
            try:
                last_dt = datetime.fromisoformat(last_fetch) if last_fetch else now
            except (ValueError, TypeError):
                last_dt = now

            due_at = last_dt + timedelta(minutes=interval)
            while due_at <= now:
                due_at += timedelta(minutes=interval)
            next_fetch_at = due_at.isoformat()
            seconds_until = max(0, int((due_at - now).total_seconds()))

            if next_fetch is None or due_at < next_fetch:
                next_fetch = due_at
                server_name = row["server_name"]
                interval_minutes = interval
        else:
            mode = "manual"

        servers.append({
            "id": row["id"],
            "server_name": row["server_name"],
            "mode": mode,
            "interval_minutes": interval,
            "next_fetch_at": next_fetch_at,
            "seconds_until": seconds_until,
        })

    if next_fetch is None:
        return jsonify({
            "next_fetch_at": None,
            "seconds_until": None,
            "server_name": None,
            "interval_minutes": None,
            "servers": servers,
        })

    seconds_until = max(1, int((next_fetch - now).total_seconds()))
    return jsonify({
        "next_fetch_at": next_fetch.isoformat(),
        "seconds_until": seconds_until,
        "server_name": server_name,
        "interval_minutes": interval_minutes,
        "servers": servers,
    })


# ---------------------------------------------------------------------------
# Preferences API
# ---------------------------------------------------------------------------

@app.route("/api/preferences/group-by-server", methods=["GET"])
@login_required
def api_get_group_by_server():
    return jsonify({"group_by_server": session.get("group_by_server", False)})


@app.route("/api/preferences/group-by-server", methods=["POST"])
@login_required
def api_toggle_group_by_server():
    session["group_by_server"] = not session.get("group_by_server", False)
    return jsonify({"group_by_server": session["group_by_server"]})


@app.route("/api/preferences/sort-by-time", methods=["GET"])
@login_required
def api_get_sort_by_time():
    return jsonify({"sort_by_time": session.get("sort_by_time", False)})


@app.route("/api/preferences/sort-by-time", methods=["POST"])
@login_required
def api_toggle_sort_by_time():
    session["sort_by_time"] = not session.get("sort_by_time", False)
    return jsonify({"sort_by_time": session["sort_by_time"]})


# ---------------------------------------------------------------------------
# Contacts API
# ---------------------------------------------------------------------------

@app.route("/api/contacts", methods=["GET"])
@login_required
def api_get_contacts():
    """List contacts with their group info (many-to-many)."""
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT c.*, cg.name as group_name "
        "FROM contacts c "
        "LEFT JOIN contact_groups cg ON c.contact_group_id = cg.id "
        "WHERE c.user_id=? ORDER BY c.is_favorite DESC, c.name ASC",
        (user_id,),
    )
    contacts = [dict(r) for r in cursor.fetchall()]

    cursor.execute(
        "SELECT cgm.contact_id, cgm.contact_group_id FROM contact_group_members cgm "
        "JOIN contacts c ON cgm.contact_id = c.id WHERE c.user_id=?",
        (user_id,),
    )
    group_members = {}
    for row in cursor.fetchall():
        cid = row["contact_id"]
        gid = row["contact_group_id"]
        group_members.setdefault(cid, []).append(gid)

    for c in contacts:
        c["group_ids"] = group_members.get(c["id"], [])

    conn.close()
    return jsonify({"contacts": contacts})


@app.route("/api/contacts", methods=["POST"])
@login_required
@json_body
def api_add_contact():
    """Add a new contact (supports many-to-many groups via group_ids)."""
    data = request.get_json()
    user_id = session["user_id"]
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    if not name or not email:
        return jsonify({"error": "Name and email are required"}), 400

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO contacts (user_id, name, email, phone, contact_group_id, is_favorite, default_server_id, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            name,
            email,
            data.get("phone", ""),
            data.get("contact_group_id"),
            1 if data.get("is_favorite") else 0,
            data.get("default_server_id"),
            data.get("notes", ""),
        ),
    )
    contact_id = cursor.lastrowid

    # Insert many-to-many group memberships
    group_ids = data.get("group_ids") or []
    if data.get("contact_group_id") and data["contact_group_id"] not in group_ids:
        group_ids.append(data["contact_group_id"])
    for gid in group_ids:
        cursor.execute(
            "INSERT OR IGNORE INTO contact_group_members (contact_id, contact_group_id) VALUES (?, ?)",
            (contact_id, gid),
        )

    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": contact_id})


@app.route("/api/contacts/add-from-email", methods=["POST"])
@login_required
@json_body
def api_add_contact_from_email():
    """Add a sender as a new contact from email address."""
    data = request.get_json()
    user_id = session["user_id"]
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "Email address is required"}), 400
    if not name:
        name = email

    conn = get_user_db(user_id)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM contacts WHERE user_id=? AND email=?",
        (user_id, email),
    )
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return jsonify({"success": True, "id": existing["id"], "already_exists": True})

    cursor.execute(
        "INSERT INTO contacts (user_id, name, email) VALUES (?, ?, ?)",
        (user_id, name, email),
    )
    conn.commit()
    contact_id = cursor.lastrowid
    conn.close()
    return jsonify({"success": True, "id": contact_id})


@app.route("/api/contacts/<int:contact_id>", methods=["PUT"])
@login_required
@json_body
def api_update_contact(contact_id):
    """Update a contact (supports many-to-many groups via group_ids)."""
    data = request.get_json()
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    updates = []
    params = []
    for field in ("name", "email", "phone", "contact_group_id", "is_favorite", "default_server_id", "notes"):
        if field in data:
            updates.append(f"{field}=?")
            params.append(data[field])
    if not updates:
        conn.close()
        return jsonify({"success": False, "error": "No fields to update"}), 400

    params.append(contact_id)
    params.append(user_id)
    cursor.execute(
        f"UPDATE contacts SET {', '.join(updates)} WHERE id=? AND user_id=?",
        params,
    )
    ok = cursor.rowcount > 0

    # Update many-to-many group memberships if provided
    if "group_ids" in data:
        cursor.execute(
            "DELETE FROM contact_group_members WHERE contact_id=?",
            (contact_id,),
        )
        group_ids = data["group_ids"]
        if not group_ids and data.get("contact_group_id"):
            group_ids = [data["contact_group_id"]]
        for gid in group_ids:
            cursor.execute(
                "INSERT OR IGNORE INTO contact_group_members (contact_id, contact_group_id) VALUES (?, ?)",
                (contact_id, gid),
            )

    conn.commit()
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
@login_required
def api_delete_contact(contact_id):
    """Delete a contact."""
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM contact_group_members WHERE contact_id=?",
        (contact_id,),
    )
    cursor.execute(
        "DELETE FROM contacts WHERE id=? AND user_id=?",
        (contact_id, user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/contact-groups", methods=["GET"])
@login_required
def api_get_contact_groups():
    """List contact groups (count from many-to-many junction table)."""
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cg.*, (SELECT COUNT(*) FROM contact_group_members cgm WHERE cgm.contact_group_id=cg.id) as contact_count "
        "FROM contact_groups cg WHERE cg.user_id=? ORDER BY cg.sort_order ASC, cg.name ASC",
        (user_id,),
    )
    groups = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"contact_groups": groups})


@app.route("/api/contact-groups", methods=["POST"])
@login_required
@json_body
def api_add_contact_group():
    """Add a contact group."""
    data = request.get_json()
    user_id = session["user_id"]
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO contact_groups (user_id, name) VALUES (?, ?)",
        (user_id, name),
    )
    conn.commit()
    group_id = cursor.lastrowid
    conn.close()
    return jsonify({"success": True, "id": group_id})


@app.route("/api/contact-groups/<int:group_id>", methods=["PUT"])
@login_required
@json_body
def api_update_contact_group(group_id):
    """Update a contact group."""
    data = request.get_json()
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    name = data.get("name", "").strip()
    if not name:
        conn.close()
        return jsonify({"error": "Group name is required"}), 400
    cursor.execute(
        "UPDATE contact_groups SET name=? WHERE id=? AND user_id=?",
        (name, group_id, user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


@app.route("/api/contact-groups/<int:group_id>", methods=["DELETE"])
@login_required
def api_delete_contact_group(group_id):
    """Delete a contact group (removes group from all contacts)."""
    user_id = session["user_id"]
    conn = get_user_db(user_id)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM contact_group_members WHERE contact_group_id=?",
        (group_id,),
    )
    cursor.execute(
        "UPDATE contacts SET contact_group_id=NULL WHERE contact_group_id=? AND user_id=?",
        (group_id, user_id),
    )
    cursor.execute(
        "DELETE FROM contact_groups WHERE id=? AND user_id=?",
        (group_id, user_id),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return jsonify({"success": ok})


# ---------------------------------------------------------------------------
# Importance / Sender group info (for settings)
# ---------------------------------------------------------------------------

@app.route("/api/groups/importance", methods=["GET"])
@login_required
def api_importance_groups():
    groups = get_importance_groups(session["user_id"])
    return jsonify({"groups": groups})


@app.route("/api/groups/importance", methods=["POST"])
@login_required
@json_body
def api_create_importance_group():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM importance_groups WHERE user_id=? AND name=?",
        (session["user_id"], name),
    )
    if cursor.fetchone():
        conn.close()
        return jsonify({"error": "Group name already exists"}), 409

    cursor.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM importance_groups WHERE user_id=?",
        (session["user_id"],),
    )
    next_order = cursor.fetchone()[0]

    cursor.execute(
        "INSERT INTO importance_groups (user_id, name, sort_order, is_system) VALUES (?, ?, ?, 0)",
        (session["user_id"], name, next_order),
    )
    group_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"success": True, "group_id": group_id}), 201


@app.route("/api/groups/importance/<int:group_id>", methods=["PUT"])
@login_required
@json_body
def api_rename_importance_group(group_id):
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_system FROM importance_groups WHERE id=? AND user_id=?",
        (group_id, session["user_id"]),
    )
    g = cursor.fetchone()
    if not g:
        conn.close()
        return jsonify({"error": "Group not found"}), 404
    if g["is_system"]:
        conn.close()
        return jsonify({"error": "Cannot rename system group"}), 403

    cursor.execute(
        "SELECT id FROM importance_groups WHERE user_id=? AND name=? AND id!=?",
        (session["user_id"], name, group_id),
    )
    if cursor.fetchone():
        conn.close()
        return jsonify({"error": "Group name already exists"}), 409

    cursor.execute(
        "UPDATE importance_groups SET name=? WHERE id=? AND user_id=?",
        (name, group_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/groups/importance/<int:group_id>", methods=["DELETE"])
@login_required
def api_delete_importance_group(group_id):
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_system FROM importance_groups WHERE id=? AND user_id=?",
        (group_id, session["user_id"]),
    )
    g = cursor.fetchone()
    if not g:
        conn.close()
        return jsonify({"error": "Group not found"}), 404
    if g["is_system"]:
        conn.close()
        return jsonify({"error": "Cannot delete system group"}), 403

    cursor.execute(
        "SELECT COUNT(*) FROM emails WHERE importance_group_id=? AND user_id=?",
        (group_id, session["user_id"]),
    )
    count = cursor.fetchone()[0]
    if count > 0:
        conn.close()
        return jsonify({"error": "分组内有邮件，必须先移出所有邮件后才能删除"}), 409

    cursor.execute(
        "DELETE FROM importance_groups WHERE id=? AND user_id=?",
        (group_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Initialize DB on startup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Folders API
# ---------------------------------------------------------------------------

@app.route("/api/folders", methods=["GET"])
@login_required
def api_get_folders():
    """List all folders for the current user."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, icon, sort_order, is_system FROM folders WHERE user_id=? ORDER BY sort_order, name",
        (session["user_id"],),
    )
    folders = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"folders": folders})


@app.route("/api/folders", methods=["POST"])
@login_required
@json_body
def api_create_folder():
    """Create a custom folder."""
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Folder name required"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM folders WHERE user_id=? AND name=?",
        (session["user_id"], name),
    )
    if cursor.fetchone():
        conn.close()
        return jsonify({"error": "Folder name already exists"}), 409

    cursor.execute(
        "INSERT INTO folders (user_id, name, icon, is_system) VALUES (?, ?, 'folder', 0)",
        (session["user_id"], name),
    )
    folder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"success": True, "folder_id": folder_id}), 201


@app.route("/api/folders/<int:folder_id>", methods=["PUT"])
@login_required
@json_body
def api_rename_folder(folder_id):
    """Rename a custom folder."""
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Folder name required"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT is_system FROM folders WHERE id=? AND user_id=?",
        (folder_id, session["user_id"]),
    )
    f = cursor.fetchone()
    if not f:
        conn.close()
        return jsonify({"error": "Folder not found"}), 404
    if f["is_system"]:
        conn.close()
        return jsonify({"error": "Cannot rename system folder"}), 403

    cursor.execute(
        "SELECT id FROM folders WHERE user_id=? AND name=? AND id!=?",
        (session["user_id"], name, folder_id),
    )
    if cursor.fetchone():
        conn.close()
        return jsonify({"error": "Folder name already exists"}), 409

    cursor.execute(
        "UPDATE folders SET name=? WHERE id=? AND user_id=?",
        (name, folder_id, session["user_id"]),
    )
    # Also update the denormalized folder TEXT column for existing emails
    cursor.execute(
        "UPDATE emails SET folder=? WHERE folder_id=? AND user_id=?",
        (name, folder_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/folders/<int:folder_id>", methods=["DELETE"])
@login_required
def api_delete_folder(folder_id):
    """Delete a custom folder. Fails if folder still has emails."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()

    cursor.execute(
        "SELECT is_system FROM folders WHERE id=? AND user_id=?",
        (folder_id, session["user_id"]),
    )
    f = cursor.fetchone()
    if not f:
        conn.close()
        return jsonify({"error": "Folder not found"}), 404
    if f["is_system"]:
        conn.close()
        return jsonify({"error": "Cannot delete system folder"}), 403

    cursor.execute(
        "SELECT COUNT(*) FROM emails WHERE folder_id=? AND user_id=?",
        (folder_id, session["user_id"]),
    )
    count = cursor.fetchone()[0]
    if count > 0:
        conn.close()
        return jsonify({
            "error": f"Cannot delete folder: {count} email(s) still in this folder. Move or delete them first.",
            "email_count": count,
        }), 409

    cursor.execute(
        "DELETE FROM folders WHERE id=? AND user_id=?",
        (folder_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


_delete_progress: dict = {}
_delete_progress_lock = threading.Lock()


def _background_delete_group_emails(
    task_id: str,
    user_id: int,
    server_rows: list,
):
    try:
        conn = get_user_db(user_id)
        cursor = conn.cursor()

        if server_rows:
            for row in server_rows:
                if row["folder"] == "drafts":
                    continue
                if row["server_uid"] and row["server_id"]:
                    cursor.execute(
                        "SELECT incoming_protocol, incoming_server, incoming_port, "
                        "username, password, use_ssl "
                        "FROM email_servers WHERE id=? AND user_id=?",
                        (row["server_id"], user_id),
                    )
                    srv = cursor.fetchone()
                    if srv and srv["incoming_server"]:
                        sc = {
                            "incoming_protocol": srv["incoming_protocol"],
                            "incoming_server": srv["incoming_server"],
                            "incoming_port": srv["incoming_port"],
                            "username": srv["username"],
                            "password": srv["password"],
                            "use_ssl": srv["use_ssl"],
                        }
                        last_error = None
                        for attempt in range(3):
                            result = delete_from_server(sc, row["server_uid"])
                            if result.get("success"):
                                last_error = None
                                break
                            last_error = result.get("error", "Unknown error")
                            if attempt < 2:
                                time.sleep(2)

                        if last_error:
                            with _delete_progress_lock:
                                entry = _delete_progress.get(task_id)
                                if entry:
                                    entry.setdefault("failed", 0)
                                    entry["failed"] += 1
                                    entry.setdefault("errors", [])
                                    entry["errors"].append(
                                        f"Email {row['id']} (UID {row['server_uid']}): {last_error}"
                                    )
                        else:
                            cursor.execute(
                                "UPDATE emails SET folder='deleted' WHERE id=? AND user_id=?",
                                (row["id"], user_id),
                            )
                            conn.commit()

                with _delete_progress_lock:
                    entry = _delete_progress.get(task_id)
                    if entry:
	                        entry["current"] += 1

        with _delete_progress_lock:
            entry = _delete_progress.get(task_id)
            if entry:
                failed = entry.get("failed", 0)
                entry["status"] = "done" if failed == 0 else "partial"
                if failed > 0:
                    entry["error"] = f"{failed} email(s) failed to delete from server"

        conn.close()
    except Exception as e:
        with _delete_progress_lock:
            entry = _delete_progress.get(task_id)
            if entry:
                entry["status"] = "error"
                entry["error"] = str(e)


_db_dir = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_db_dir, exist_ok=True)
with app.app_context():
    init_db()
    init_default_admin()

# Start background scheduler for automatic email fetching
scheduler.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Email Client - Web Interface")
    parser.add_argument("--port", "-p", type=int, default=Config.LOGIN_PORT,
                        help="Server port (default: %(default)s)")
    args = parser.parse_args()

    port = args.port
    app.config["SESSION_COOKIE_NAME"] = f"session_{port}"
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║       Email Client - Web Interface            ║")
    print(f"║──────────────────────────────────────────────║")
    print(f"║  URL:  http://0.0.0.0:{port:<5}                  ║")
    print(f"║  Admin: admin / 1234                         ║")
    print(f"╚══════════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
