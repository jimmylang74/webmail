"""Email classification module - Ad detection and importance grouping."""

import re
from modules import get_user_db

# Keywords that indicate advertising/promotional emails
AD_KEYWORDS = [
    "advertisement", "sponsor", "sponsored", "promotion", "promotional",
    "special offer", "exclusive offer", "limited time", "act now",
    "buy now", "shop now", "order now", "call now", "don't miss out",
    "click here", "subscribe", "unsubscribe", "marketing", "newsletter",
    "sale", "deal", "discount", "save", "free", "trial", "offer",
    "coupon", "voucher", "promo code", "best price", "clearance",
    "limited offer", "new arrival", "shop", "store", "online shop",
    "you've been selected", "congratulations", "winner", "prize",
    "earn money", "work from home", "make money", "investment opportunity",
    "act immediately", "expires", "hurry", "last chance",
    # Chinese ad keywords
    "广告", "推广", "促销", "优惠", "打折", "特价", "限时",
    "免费", "赠品", "会员", "订阅", "营销", "活动",
    "邀请", "注册", "官网", "官方", "正品",
]

# Keywords indicating important emails
IMPORTANT_KEYWORDS = [
    "urgent", "important", "asap", "critical", "deadline",
    "meeting", "appointment", "invoice", "payment", "bill",
    "contract", "agreement", "approved", "confirmed",
    "security", "alert", "verification", "password reset",
    "紧急", "重要", "会议", "合同", "确认", "安全", "验证",
    "审批", "付款", "账单", "发票", "截止",
]

# Sender domains commonly associated with marketing
AD_DOMAINS = [
    "mail", "marketing", "newsletter", "news", "promo", "info",
    "noreply", "no-reply", "notification", "notification",
    "mailer", "send", "email", "ecommerce", "shop", "store",
]


def _extract_domain(email_addr: str) -> str:
    """Extract domain from email address."""
    match = re.search(r"@([\w.-]+)", email_addr.lower())
    return match.group(1) if match else ""


def _email_is_advertisement(sender: str, sender_name: str, subject: str, body_text: str) -> bool:
    """Detect if an email is an advertisement based on content analysis."""
    text_lower = f"{subject} {body_text}".lower() if body_text else subject.lower()
    sender_lower = f"{sender} {sender_name}".lower()
    domain = _extract_domain(sender)

    # Check sender name/email for common patterns
    for ad_domain_kw in AD_DOMAINS:
        if ad_domain_kw in sender_lower or ad_domain_kw in domain:
            return True

    # Check content for ad keywords
    keyword_count = 0
    for kw in AD_KEYWORDS:
        if kw.lower() in text_lower:
            keyword_count += 1

    # If subject has 2+ ad keywords or body has 3+, classify as ad
    subject_lower = subject.lower() if subject else ""
    subject_ad_count = sum(1 for kw in AD_KEYWORDS if kw.lower() in subject_lower)
    if subject_ad_count >= 2:
        return True

    return keyword_count >= 3


def _email_is_important(subject: str, body_text: str) -> bool:
    """Detect if an email is important."""
    text_lower = f"{subject} {body_text}".lower() if body_text else (subject or "").lower()

    keyword_count = 0
    for kw in IMPORTANT_KEYWORDS:
        if kw.lower() in text_lower:
            keyword_count += 1

    return keyword_count >= 1


def classify_email(sender: str, sender_name: str, subject: str, body_text: str) -> str:
    """Classify email importance: 'Ad', 'Normal', or 'Important'.

    Returns the importance group name.
    """
    if _email_is_advertisement(sender, sender_name, subject, body_text):
        return "Ad"
    if _email_is_important(subject, body_text):
        return "Important"
    return "Normal"


def get_or_create_sender_group(user_id: int, sender: str, sender_name: str, conn=None) -> dict:
    """Get or create a sender group for a given sender email.

    Groups are keyed by the sender's email domain (part after @) rather than
    the full email address.  All senders sharing the same domain are placed
    into one sender group.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_user_db(user_id)
    cursor = conn.cursor()

    domain = _extract_domain(sender)

    cursor.execute(
        "SELECT sg.*, ig.name as importance_name FROM sender_groups sg "
        "LEFT JOIN importance_groups ig ON sg.importance_group_id = ig.id "
        "WHERE sg.user_id = ? AND sg.sender_domain = ?",
        (user_id, domain),
    )
    existing = cursor.fetchone()
    if existing:
        if own_conn:
            conn.close()
        return dict(existing)

    importance = classify_email(sender, sender_name, "", "")
    group_name = domain if domain else sender

    cursor.execute(
        "SELECT id FROM importance_groups WHERE user_id = ? AND name = ?",
        (user_id, importance),
    )
    ig = cursor.fetchone()
    imp_id = ig["id"] if ig else None

    cursor.execute(
        "INSERT INTO sender_groups (user_id, sender_email, sender_name, sender_domain, group_name, importance_group_id, is_auto_classified) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user_id, sender, sender_name, domain, group_name, imp_id),
    )
    new_id = cursor.lastrowid

    cursor.execute(
        "SELECT sg.*, ig.name as importance_name FROM sender_groups sg "
        "LEFT JOIN importance_groups ig ON sg.importance_group_id = ig.id "
        "WHERE sg.id = ?",
        (new_id,),
    )
    row = cursor.fetchone()
    if own_conn:
        conn.commit()
        conn.close()
    return dict(row)


def auto_classify_senders(user_id: int):
    """Re-classify all unassigned sender groups."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT sg.id, sg.sender_email, sg.sender_name, "
        "(SELECT subject FROM emails WHERE sender_group_id = sg.id ORDER BY received_date DESC LIMIT 1) as last_subject, "
        "(SELECT body_text FROM emails WHERE sender_group_id = sg.id ORDER BY received_date DESC LIMIT 1) as last_body "
        "FROM sender_groups sg WHERE sg.user_id = ? AND sg.importance_group_id IS NULL",
        (user_id,),
    )
    unclassified = cursor.fetchall()

    for row in unclassified:
        importance = classify_email(
            row["sender_email"], row["sender_name"] or "",
            row["last_subject"] or "", row["last_body"] or "",
        )
        cursor.execute(
            "SELECT id FROM importance_groups WHERE user_id = ? AND name = ?",
            (user_id, importance),
        )
        ig = cursor.fetchone()
        if ig:
            cursor.execute(
                "UPDATE sender_groups SET importance_group_id = ?, is_auto_classified = 1 WHERE id = ?",
                (ig["id"], row["id"]),
            )

    conn.commit()
    conn.close()


def classify_unclassified_emails(user_id: int):
    """Classify emails that don't have importance_group_id set."""
    conn = get_user_db(user_id)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT e.id, e.sender, e.sender_name, e.subject, e.body_text "
        "FROM emails e WHERE e.user_id = ? AND e.importance_group_id IS NULL AND e.folder = 'inbox'",
        (user_id,),
    )
    unclassified = cursor.fetchall()

    for row in unclassified:
        importance = classify_email(
            row["sender"], row["sender_name"] or "",
            row["subject"] or "", row["body_text"] or "",
        )
        cursor.execute(
            "SELECT id FROM importance_groups WHERE user_id = ? AND name = ?",
            (user_id, importance),
        )
        ig = cursor.fetchone()
        if ig:
            cursor.execute(
                "UPDATE emails SET importance_group_id = ? WHERE id = ?",
                (ig["id"], row["id"]),
            )

    conn.commit()
    conn.close()
