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

    APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
    ASSET_VERSION = os.getenv("ASSET_VERSION", "20260821.1").strip() or "20260821.1"
    AUTO_CREATE_SCHEMA = _as_bool(
        os.getenv("AUTO_CREATE_SCHEMA"), default=APP_ENV != "production"
    )
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
    ALLOW_WEB_SIGNUP = _as_bool(
        os.getenv("ALLOW_WEB_SIGNUP"), default=APP_ENV != "production"
    )
    ALLOW_ACTOR_HEADER = _as_bool(os.getenv("ALLOW_ACTOR_HEADER"), default=False)
    STAFF_USERNAME = os.getenv("STAFF_USERNAME", "")
    STAFF_PASSWORD = os.getenv("STAFF_PASSWORD", "")
    DEFAULT_WORKSPACE_NAME = os.getenv("DEFAULT_WORKSPACE_NAME", "StockPilot Workspace")
    DEFAULT_BUSINESS_USERNAME = os.getenv("DEFAULT_BUSINESS_USERNAME", "stockpilot")
    DEFAULT_STAFF_EMAIL = os.getenv("DEFAULT_STAFF_EMAIL", "staff@stockpilot.local")
    REQUIRE_EMAIL_VERIFICATION = _as_bool(
        os.getenv("REQUIRE_EMAIL_VERIFICATION"), default=APP_ENV == "production"
    )
    EMAIL_VERIFICATION_HOURS = max(1, int(os.getenv("EMAIL_VERIFICATION_HOURS", "24")))
    PASSWORD_RESET_MINUTES = max(5, int(os.getenv("PASSWORD_RESET_MINUTES", "30")))
    INVITATION_EXPIRY_HOURS = max(1, int(os.getenv("INVITATION_EXPIRY_HOURS", "72")))
    MFA_ISSUER = os.getenv("MFA_ISSUER", "StockPilot")
    MFA_ENCRYPTION_KEY = os.getenv("MFA_ENCRYPTION_KEY", "")
    AUTH_EMAIL_ENABLED = _as_bool(os.getenv("AUTH_EMAIL_ENABLED"), default=False)
    OIDC_ENABLED = _as_bool(os.getenv("OIDC_ENABLED"), default=True)
    DEFAULT_PAGE_SIZE = max(10, min(100, int(os.getenv("DEFAULT_PAGE_SIZE", "25"))))
    MAX_PAGE_SIZE = max(25, min(250, int(os.getenv("MAX_PAGE_SIZE", "100"))))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.getenv("SESSION_COOKIE_SECURE"))
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Flask applies this limit before multipart parsing. Keep a small allowance
    # above the validated CSV payload size for multipart headers and boundaries.
    MAX_CONTENT_LENGTH = MAX_PRODUCT_CSV_BYTES + (256 * 1024)
    TRUSTED_HOSTS = [
        host.strip()
        for host in os.getenv("TRUSTED_HOSTS", "").split(",")
        if host.strip()
    ] or None
    TRUST_PROXY_HEADERS = _as_bool(
        os.getenv("TRUST_PROXY_HEADERS"), default=APP_ENV == "production"
    )
    PREFERRED_URL_SCHEME = "https" if APP_ENV == "production" else "http"
    LOGIN_MAX_ATTEMPTS = max(1, int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")))
    LOGIN_WINDOW_SECONDS = max(30, int(os.getenv("LOGIN_WINDOW_SECONDS", "300")))
    SIGNUP_MAX_ATTEMPTS = max(1, int(os.getenv("SIGNUP_MAX_ATTEMPTS", "5")))
    SIGNUP_WINDOW_SECONDS = max(60, int(os.getenv("SIGNUP_WINDOW_SECONDS", "3600")))
    AUTH_LINK_MAX_ATTEMPTS = max(1, int(os.getenv("AUTH_LINK_MAX_ATTEMPTS", "3")))
    AUTH_LINK_WINDOW_SECONDS = max(
        60, int(os.getenv("AUTH_LINK_WINDOW_SECONDS", "900"))
    )

    FORECAST_LOOKBACK_DAYS = int(os.getenv("FORECAST_LOOKBACK_DAYS", "28"))
    DEFAULT_SUPPLIER_LEAD_TIME_DAYS = int(
        os.getenv("DEFAULT_SUPPLIER_LEAD_TIME_DAYS", "3")
    )
    CRITICAL_STOCKOUT_DAYS = max(
        1, int(os.getenv("CRITICAL_STOCKOUT_DAYS", "3"))
    )
    NEAR_EXPIRY_DAYS = max(1, int(os.getenv("NEAR_EXPIRY_DAYS", "30")))
    DEAD_STOCK_DAYS = max(1, int(os.getenv("DEAD_STOCK_DAYS", "90")))
    FORECAST_ACCURACY_HORIZON_DAYS = max(
        1, int(os.getenv("FORECAST_ACCURACY_HORIZON_DAYS", "7"))
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


def validate_runtime_config(config: dict) -> None:
    """Fail closed when a production process still has development defaults."""

    app_env = str(config.get("APP_ENV", "")).strip().lower()
    if app_env not in {"development", "testing", "production"}:
        raise RuntimeError(
            "APP_ENV must be one of development, testing, or production"
        )
    if config.get("TESTING") or app_env != "production":
        return

    errors: list[str] = []
    secret_values = {
        "SECRET_KEY": ("", "local-development-only-change-me", "replace-with-a-long-random-secret"),
        "POS_WEBHOOK_TOKEN": ("", "local-pos-token", "replace-with-a-long-random-pos-token"),
        "INTERNAL_API_TOKEN": ("", "local-job-token", "replace-with-a-long-random-job-token"),
    }
    for name, rejected in secret_values.items():
        value = str(config.get(name) or "")
        looks_like_placeholder = value.strip().lower().startswith(
            ("replace-", "change-", "local-", "your-")
        )
        if value in rejected or looks_like_placeholder or len(value) < 32:
            errors.append(f"{name} must be a production secret of at least 32 characters")
    if not config.get("STAFF_AUTH_ENABLED"):
        errors.append("STAFF_AUTH_ENABLED must remain enabled in production")
    if not config.get("SESSION_COOKIE_SECURE"):
        errors.append("SESSION_COOKIE_SECURE must be enabled behind HTTPS")
    if config.get("ALLOW_WEB_SIGNUP") and not config.get("REQUIRE_EMAIL_VERIFICATION"):
        errors.append("public signup requires REQUIRE_EMAIL_VERIFICATION in production")
    if config.get("REQUIRE_EMAIL_VERIFICATION") and not (
        config.get("AUTH_EMAIL_ENABLED") or config.get("SES_ENABLED")
    ):
        errors.append("email verification requires AUTH_EMAIL_ENABLED or SES_ENABLED")
    if config.get("REQUIRE_EMAIL_VERIFICATION") and not str(
        config.get("SES_FROM_EMAIL") or ""
    ).strip():
        errors.append("email verification requires SES_FROM_EMAIL")
    mfa_encryption_key = str(config.get("MFA_ENCRYPTION_KEY") or "").strip()
    if not mfa_encryption_key:
        errors.append("MFA_ENCRYPTION_KEY must be set separately from SECRET_KEY")
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(mfa_encryption_key.encode("ascii"))
        except (ValueError, TypeError):
            errors.append("MFA_ENCRYPTION_KEY must be a valid Fernet key")
    if not config.get("TRUSTED_HOSTS"):
        errors.append("TRUSTED_HOSTS must list the production application hostname")
    if str(config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite:"):
        errors.append("DATABASE_URL must use the production database, not SQLite")
    if config.get("AUTO_CREATE_SCHEMA"):
        errors.append("AUTO_CREATE_SCHEMA must be disabled in production")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
