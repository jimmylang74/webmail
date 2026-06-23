"""Application configuration."""

import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "email-client-secret-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'db', 'email_client.db')}"
    )
    # Default admin credentials
    DEFAULT_ADMIN_USER = "admin"
    DEFAULT_ADMIN_PASS = "1234"
    # Language
    LANGUAGE = os.environ.get("LANGUAGE", "zh")
    # Login port
    LOGIN_PORT = int(os.environ.get("LOGIN_PORT", 5566))
    # Upload max size
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
