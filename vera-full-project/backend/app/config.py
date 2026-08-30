import os
from dotenv import load_dotenv

load_dotenv()


def _csv(name: str, default: str) -> list:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://vera_user:vera_pass@localhost:5432/vera_hair_co"
    )
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-this-secret-in-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

    # Explicit origins by default. "*" is still accepted but is a production
    # gap — set CORS_ORIGINS to your real storefront origin(s) when deploying.
    CORS_ORIGINS: list = _csv(
        "CORS_ORIGINS",
        "http://localhost:5501,http://127.0.0.1:5501,http://localhost:5500,http://127.0.0.1:5500",
    )

    # Currency. INR is canonical in the database; the rest is display only.
    BASE_CURRENCY: str = os.getenv("BASE_CURRENCY", "INR")
    EXCHANGE_RATE_PROVIDER: str = os.getenv("EXCHANGE_RATE_PROVIDER", "static")
    EXCHANGE_RATE_API_KEY: str = os.getenv("EXCHANGE_RATE_API_KEY", "")
    EXCHANGE_RATE_URL: str = os.getenv(
        "EXCHANGE_RATE_URL",
        "https://v6.exchangerate-api.com/v6/{key}/latest/{base}",
    )
    EXCHANGE_RATE_CACHE_TTL: int = int(os.getenv("EXCHANGE_RATE_CACHE_TTL", "3600"))

    # Payments. "none" keeps the pre-gateway behaviour (orders go straight to
    # Processing). "manual" holds orders at Pending Payment for admin
    # confirmation. "razorpay" requires the keys below.
    PAYMENT_PROVIDER: str = os.getenv("PAYMENT_PROVIDER", "none")
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # Shipping (server-side only; a client can never influence what is charged)
    SHIPPING_FLAT_FEE: str = os.getenv("SHIPPING_FLAT_FEE", "499")
    FREE_SHIPPING_THRESHOLD: str = os.getenv("FREE_SHIPPING_THRESHOLD", "8000")

    # --- Loyalty (see app/loyalty.py) ------------------------------------
    # Points are earned per LOYALTY_EARN_PER rupees spent, and each point is
    # worth LOYALTY_POINT_VALUE rupees at checkout. The defaults are 1 point
    # per ₹100 spent, worth ₹1 — a 1% programme.
    LOYALTY_EARN_PER: str = os.getenv("LOYALTY_EARN_PER", "100")
    LOYALTY_POINT_VALUE: float = float(os.getenv("LOYALTY_POINT_VALUE", "1"))
    # Points may cover at most this share of the goods total, so a big balance
    # discounts an order instead of replacing payment for it.
    LOYALTY_MAX_REDEEM_PCT: int = int(os.getenv("LOYALTY_MAX_REDEEM_PCT", "20"))

    # --- Reviews (see app/reviews.py) ------------------------------------
    # Reviews are held for moderation before they count towards a rating.
    # Setting this false publishes verified reviews immediately, which is a
    # policy choice, not a shortcut: they are still tied to a delivered order.
    REVIEWS_REQUIRE_MODERATION: bool = os.getenv(
        "REVIEWS_REQUIRE_MODERATION", "true").lower() == "true"

    # --- Environment & deployment (see app/runtime.py) -------------------
    # development | staging | production. Production is strict: the startup
    # preflight REFUSES to boot on an insecure default rather than running and
    # hoping nobody notices.
    APP_ENV: str = os.getenv("APP_ENV", "development")
    APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
    GIT_SHA: str = os.getenv("GIT_SHA", "")

    # Host header allow-list. Empty means "any", which is only tolerable off
    # production; a wildcard host in production invites cache poisoning and
    # password-reset link forgery.
    ALLOWED_HOSTS: list = _csv("ALLOWED_HOSTS", "")

    # Set when a reverse proxy terminates TLS, so redirects and logged client
    # IPs use the ORIGINAL scheme/address rather than the proxy's.
    TRUST_PROXY: bool = os.getenv("TRUST_PROXY", "false").lower() == "true"
    FORCE_HTTPS: bool = os.getenv("FORCE_HTTPS", "false").lower() == "true"
    HSTS_MAX_AGE: int = int(os.getenv("HSTS_MAX_AGE", "31536000"))

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    # "json" for log aggregation, "text" for a human reading a terminal.
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "")

    # --- Rate limiting (see app/middleware.py) ---------------------------
    # Off outside production by default: local test suites hammer these
    # endpoints deliberately, and a limiter that trips during development
    # teaches people to disable it.
    RATE_LIMIT_ENABLED: str = os.getenv("RATE_LIMIT_ENABLED", "")
    RATE_LIMIT_PUBLIC: str = os.getenv("RATE_LIMIT_PUBLIC", "60/60")     # requests/seconds
    RATE_LIMIT_WRITE: str = os.getenv("RATE_LIMIT_WRITE", "12/60")       # orders, bookings
    RATE_LIMIT_LOGIN: str = os.getenv("RATE_LIMIT_LOGIN", "8/300")       # credential stuffing

    # --- Database pool ---------------------------------------------------
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "5"))
    DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    DB_POOL_RECYCLE: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    DB_POOL_TIMEOUT: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))

    # --- Notifications (see app/notifications.py) ------------------------
    # Channel that actually delivers email:
    #   console = render and log only, nothing leaves the machine (default)
    #   smtp    = real delivery through the SMTP server configured below
    #   null    = record the notification and mark it suppressed; send nothing
    NOTIFY_CHANNEL: str = os.getenv("NOTIFY_CHANNEL", "console")
    # When the queue is drained:
    #   background = after the HTTP response, in the same process (default)
    #   worker     = never automatically; a cron/worker calls the dispatch
    #                endpoint or runs `python -m app.notify_worker`
    #   inline     = during the request (test/debug only — slows checkout)
    NOTIFY_DISPATCH: str = os.getenv("NOTIFY_DISPATCH", "background")
    NOTIFY_MAX_ATTEMPTS: int = int(os.getenv("NOTIFY_MAX_ATTEMPTS", "5"))
    NOTIFY_RETRY_BASE_SECONDS: int = int(os.getenv("NOTIFY_RETRY_BASE_SECONDS", "60"))
    NOTIFY_BATCH_SIZE: int = int(os.getenv("NOTIFY_BATCH_SIZE", "25"))

    MAIL_FROM: str = os.getenv("MAIL_FROM", "orders@hairshalo.com")
    MAIL_FROM_NAME: str = os.getenv("MAIL_FROM_NAME", "Hairshalo")
    MAIL_REPLY_TO: str = os.getenv("MAIL_REPLY_TO", "")
    # Who receives operational alerts (new order, payment failed, low stock).
    ADMIN_ALERT_EMAILS: list = _csv("ADMIN_ALERT_EMAILS", "")

    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_SECURITY: str = os.getenv("SMTP_SECURITY", "starttls")  # starttls | ssl | none
    SMTP_TIMEOUT: int = int(os.getenv("SMTP_TIMEOUT", "20"))

    # Public storefront URL, used to build links inside emails.
    STOREFRONT_URL: str = os.getenv("STOREFRONT_URL", "http://localhost:5500")
    # Stock at or below this triggers a low-stock alert to ADMIN_ALERT_EMAILS.
    LOW_STOCK_ALERT_THRESHOLD: int = int(os.getenv("LOW_STOCK_ALERT_THRESHOLD", "5"))

    # Media storage (see app/storage.py — swap for S3/Cloudinary in production)
    MEDIA_ROOT: str = os.getenv(
        "MEDIA_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media"),
    )
    MEDIA_URL_PREFIX: str = os.getenv("MEDIA_URL_PREFIX", "/media")

    @property
    def cors_is_wildcard(self) -> bool:
        return "*" in self.CORS_ORIGINS

    @property
    def env(self) -> str:
        return (self.APP_ENV or "development").strip().lower()

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def log_format(self) -> str:
        """Structured logs in production, readable ones on a laptop."""
        if self.LOG_FORMAT:
            return self.LOG_FORMAT.lower()
        return "json" if self.is_production else "text"

    @property
    def rate_limit_enabled(self) -> bool:
        if self.RATE_LIMIT_ENABLED:
            return self.RATE_LIMIT_ENABLED.lower() == "true"
        return self.is_production


settings = Settings()
