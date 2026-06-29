"""Email Client - Main Flask Application.

A web-based email client with POP3/IMAP support, automatic email classification,
sender grouping, and auto-forwarding rules.
"""

import argparse
import json
import logging
import os
import threading
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
from modules.email_fetch import fetch_all_for_user, fetch_emails, get_all_fetch_progress, test_server_connection, check_imap_capabilities, download_all_emails
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
        result = download_all_emails(server_id, user_id)
        if result.get("success"):
            classify_unclassified_emails(user_id)
            auto_classify_senders(user_id)
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


@app.route("/api/mailbox/tree", methods=["GET"])
@login_required
def api_mailbox_tree():
    """Return the mailbox tree structure for the left menu."""
    user_id = session["user_id"]
    stats = _folder_stats(user_id)

    conn = get_user_db(user_id)
    cursor = conn.cursor()
    imp_groups = get_importance_groups(user_id)

    cursor.execute(
        "SELECT id, server_name FROM email_servers WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    servers = cursor.fetchall()

    folders = []

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

            srv_node = {
                "id": f"server_{srv['id']}",
                "name": srv["server_name"],
                "icon": "inbox",
                "server_id": srv["id"],
                "count": total,
                "unread": unread,
                "children": _build_inbox_children(user_id, cursor, imp_groups, server_id=srv["id"]),
            }
            folders.append(srv_node)
    else:
        folders.append({
            "id": "inbox",
            "name": "Inbox",
            "icon": "inbox",
            "count": stats["inbox"],
            "unread": stats["unread"],
            "children": _build_inbox_children(user_id, cursor, imp_groups),
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
                "SELECT id, sender, sender_name, subject, received_date, is_read, server_badge "
                "FROM emails WHERE user_id=? AND folder=? AND sender_group_id=? "
                "ORDER BY received_date DESC LIMIT 50",
                (user_id, folder_name, sg["id"]),
            )
            email_children = [
                {
                    "id": f"{folder_name}_{em['id']}",
                    "name": em["subject"] or "(No Subject)",
                    "email_id": em["id"],
                    "sender": em["sender"],
                    "sender_name": em["sender_name"],
                    "is_read": em["is_read"],
                    "received_date": em["received_date"],
                    "server_badge": em["server_badge"],
                    "type": "email",
                }
                for em in cursor.fetchall()
            ]
            children.append({
                "id": f"{folder_name}_sg_{sg['id']}",
                "name": sg["group_name"],
                "email": sg["sender_email"],
                "icon": "user",
                "count": sg["cnt"],
                "sender_group_id": sg["id"],
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
                "sender": em["sender"],
                "sender_name": em["sender_name"],
                "is_read": em["is_read"],
                "received_date": em["received_date"],
                "server_badge": em["server_badge"],
                "type": "email",
            })
        return children

    folders.extend([
        {"id": "outbox", "name": "Outbox", "icon": "send", "count": stats["outbox"],
         "children": _folder_email_children(cursor, "outbox")},
        {"id": "drafts", "name": "Drafts", "icon": "file-text", "count": stats["drafts"],
         "children": _folder_email_children(cursor, "drafts")},
        {"id": "deleted", "name": "Deleted", "icon": "trash-2", "count": stats["deleted"],
         "children": _folder_sender_group_children(cursor, "deleted")},
    ])

    conn.close()
    return jsonify({"folders": folders})


@app.route("/api/emails", methods=["GET"])
@login_required
def api_get_emails():
    """Get emails with filtering."""
    user_id = session["user_id"]
    folder = request.args.get("folder", "inbox")
    imp_group_id = request.args.get("imp_group_id", type=int)
    sender_group_id = request.args.get("sender_group_id", type=int)
    server_id = request.args.get("server_id", type=int)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "").strip()

    params = [user_id, folder]
    where_clauses = ["user_id = ?", "folder = ?"]

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
    if folder not in ("inbox", "outbox", "drafts", "deleted"):
        return jsonify({"error": "Invalid folder"}), 400

    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE emails SET folder=? WHERE id=? AND user_id=?",
        (folder, email_id, session["user_id"]),
    )
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()

    if ok and folder == "deleted":
        # Process forward rules before marking deleted
        process_forward_rules(session["user_id"], email_id)

    return jsonify({"success": ok})


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
    """Move all emails in a sender group to trash."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE emails SET folder='deleted' WHERE sender_group_id=? AND user_id=?",
        (sender_group_id, session["user_id"]),
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return jsonify({"success": True, "deleted": deleted})


@app.route("/api/emails/group/importance/<int:imp_group_id>", methods=["DELETE"])
@login_required
def api_delete_importance_group_emails(imp_group_id):
    """Move all emails in an importance group to trash."""
    conn = get_user_db(session["user_id"])
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE emails SET folder='deleted' WHERE importance_group_id=? AND user_id=? AND folder='inbox'",
        (imp_group_id, session["user_id"]),
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return jsonify({"success": True, "deleted": deleted})


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
        "SELECT id, sender, sender_name, sender_group_id, importance_group_id "
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

    cursor.execute(
        "UPDATE emails SET importance_group_id=?, sender_group_id=? WHERE id=? AND user_id=?",
        (target_imp_id, new_sender_group_id, email_id, user_id),
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
    updates = []
    params = []

    if importance_group_id is not None:
        updates.append("importance_group_id=?")
        params.append(importance_group_id)
        updates.append("is_auto_classified=0")

    if group_name is not None:
        updates.append("group_name=?")
        params.append(group_name)
        updates.append("is_auto_classified=0")

    if not updates:
        conn.close()
        return jsonify({"success": False, "error": "No fields to update"}), 400

    params.extend([group_id, session["user_id"]])
    cursor.execute(
        f"UPDATE sender_groups SET {', '.join(updates)} WHERE id=? AND user_id=?",
        params,
    )
    ok = cursor.rowcount > 0

    if ok and importance_group_id is not None:
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


# ---------------------------------------------------------------------------
# Initialize DB on startup
# ---------------------------------------------------------------------------

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
