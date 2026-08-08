import os

from datetime import timedelta

from dotenv import load_dotenv


load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Runtime configuration loaded from environment variables."""

    SECRET_KEY = os.getenv("SECRET_KEY", "local-development-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///inventory.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_PRODUCT_CSV_BYTES = int(os.getenv("MAX_PRODUCT_CSV_BYTES", str(2 * 1024 * 1024)))
    MAX_PRODUCT_CSV_ROWS = int(os.getenv("MAX_PRODUCT_CSV_ROWS", "1000"))

    POS_WEBHOOK_TOKEN = os.getenv("POS_WEBHOOK_TOKEN", "local-pos-token")
    INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "local-job-token")

    # Sprint 3 named-user authentication is enabled by default. Existing
    # deployments can keep STAFF_USERNAME/STAFF_PASSWORD for a one-time,
    # automatic migration of the former shared login into the users table.
    STAFF_AUTH_ENABLED = _as_bool(os.getenv("STAFF_AUTH_ENABLED"), default=True)
    STAFF_USERNAME = os.getenv("STAFF_USERNAME", "")
    STAFF_PASSWORD = os.getenv("STAFF_PASSWORD", "")
    DEFAULT_WORKSPACE_NAME = os.getenv("DEFAULT_WORKSPACE_NAME", "StockPilot Workspace")
    DEFAULT_STAFF_EMAIL = os.getenv("DEFAULT_STAFF_EMAIL", "staff@stockpilot.local")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.getenv("SESSION_COOKIE_SECURE"))
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    FORECAST_LOOKBACK_DAYS = int(os.getenv("FORECAST_LOOKBACK_DAYS", "28"))
    DEFAULT_SUPPLIER_LEAD_TIME_DAYS = int(
        os.getenv("DEFAULT_SUPPLIER_LEAD_TIME_DAYS", "3")
    )
    CRITICAL_STOCKOUT_DAYS = max(
        1, int(os.getenv("CRITICAL_STOCKOUT_DAYS", "3"))
    )
    REPORT_CURRENCY = os.getenv("REPORT_CURRENCY", "INR").strip().upper() or "INR"

    AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
    BEDROCK_ENABLED = _as_bool(os.getenv("BEDROCK_ENABLED"))
    BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")

    SES_ENABLED = _as_bool(os.getenv("SES_ENABLED"))
    SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL", "")
    ALERT_RECIPIENTS = [
        address.strip()
        for address in os.getenv("ALERT_RECIPIENTS", "").split(",")
        if address.strip()
    ]
