"""Startup preflight and runtime facts.

Non-negotiable rule this module exists to enforce:

    A production deployment fails loudly rather than running insecurely.

Every check below describes a way this application can be deployed and still be
wrong: a default signing key, a wildcard CORS policy, the seeded admin password
left in place, demo products in a real catalog. Each of those is silent — the
app starts, serves traffic, and looks healthy. So on `APP_ENV=production` an
`error`-level finding aborts the boot, with the fix printed. Off production the
same findings are logged as warnings, because a laptop is allowed to be
insecure and a developer is not helped by a refusal to start.

The checks are also readable over HTTP at `GET /api/ready`, so a deployment
pipeline can ask "would this configuration be accepted?" before promoting it.
"""
import logging
import os
from typing import List, Optional

from sqlalchemy import text

from app import payments
from app.config import settings

logger = logging.getLogger("vera.runtime")

#: Values that ship with the repository and must never survive to production.
DEFAULT_SECRET_KEYS = {
    "change-this-secret-in-production",
    "replace-with-a-long-random-string",
    "local-dev-secret-not-for-production",
}
SEEDED_ADMIN_EMAIL = "admin@verahair.co"
SEEDED_ADMIN_PASSWORD = "ChangeMe123!"
MIN_SECRET_LENGTH = 32


class PreflightError(RuntimeError):
    """Raised when production configuration is unsafe. Aborts the boot."""


class Finding:
    """One thing that is wrong, and what to do about it.

    `level` is "error" (refuses to boot in production) or "warning" (logged,
    surfaced in /api/ready, but never blocks).
    """

    def __init__(self, level: str, code: str, message: str, fix: str):
        self.level, self.code, self.message, self.fix = level, code, message, fix

    def __str__(self):
        return f"[{self.level}] {self.code}: {self.message} — {self.fix}"

    def as_dict(self):
        return {"level": self.level, "code": self.code,
                "message": self.message, "fix": self.fix}


def _seeded_admin_still_default(db) -> bool:
    """True when the seeded admin can still log in with the published password.

    Checked against the hash rather than a flag, so rotating the password
    clears the finding without anyone having to remember to flip something.
    """
    try:
        from app import models
        from app.security import verify_password
        user = db.query(models.User).filter(models.User.email == SEEDED_ADMIN_EMAIL).first()
        if not user:
            return False
        return verify_password(SEEDED_ADMIN_PASSWORD, user.hashed_password)
    except Exception:              # noqa: BLE001 - a check must never break the boot
        logger.debug("Could not check the seeded admin password", exc_info=True)
        return False


def _demo_data_present(db) -> bool:
    try:
        from app import models
        return db.query(models.Product.id).filter(models.Product.is_demo.is_(True)).first() is not None
    except Exception:              # noqa: BLE001
        return False


def collect_findings(db=None) -> List[Finding]:
    """Everything wrong with this configuration, worst first.

    `db` is optional: the config-only checks run without a database so the
    preflight still says something useful when Postgres is unreachable.
    """
    findings: List[Finding] = []
    add = findings.append
    production = settings.is_production

    # ---- secrets -------------------------------------------------------
    if settings.SECRET_KEY in DEFAULT_SECRET_KEYS:
        add(Finding("error", "default_secret_key",
                    "SECRET_KEY is still the value shipped with the repository, so "
                    "anyone with the source can mint valid admin tokens.",
                    "Set SECRET_KEY to a long random value, e.g. "
                    "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."))
    elif len(settings.SECRET_KEY) < MIN_SECRET_LENGTH:
        add(Finding("error", "short_secret_key",
                    f"SECRET_KEY is only {len(settings.SECRET_KEY)} characters, which is "
                    "brute-forceable offline.",
                    f"Use at least {MIN_SECRET_LENGTH} characters of random data."))

    # ---- exposure ------------------------------------------------------
    if settings.cors_is_wildcard:
        add(Finding("error", "wildcard_cors",
                    "CORS_ORIGINS is '*', so any website can call this API with a "
                    "logged-in browser's credentials.",
                    "Set CORS_ORIGINS to your storefront and dashboard origins."))
    if not settings.ALLOWED_HOSTS:
        add(Finding("error", "no_allowed_hosts",
                    "ALLOWED_HOSTS is empty, so the API answers to any Host header — "
                    "the basis of cache poisoning and forged absolute links.",
                    "Set ALLOWED_HOSTS to your real hostnames, e.g. "
                    "api.verahair.co,verahair.co."))
    if production and not (settings.FORCE_HTTPS or settings.TRUST_PROXY):
        add(Finding("warning", "no_tls_signal",
                    "Neither FORCE_HTTPS nor TRUST_PROXY is set, so nothing here "
                    "guarantees requests arrive over TLS.",
                    "Terminate TLS at a proxy and set TRUST_PROXY=true, or set "
                    "FORCE_HTTPS=true to redirect plain HTTP."))
    if not settings.rate_limit_enabled:
        add(Finding("warning", "no_rate_limit",
                    "Rate limiting is disabled; checkout, booking and login are "
                    "open to scripted abuse.",
                    "Set RATE_LIMIT_ENABLED=true (it is on by default in production)."))

    # ---- database ------------------------------------------------------
    url = settings.DATABASE_URL or ""
    if url.startswith("sqlite"):
        add(Finding("error", "sqlite_in_production",
                    "DATABASE_URL points at SQLite, which cannot honour the row locks "
                    "the checkout relies on to avoid overselling.",
                    "Point DATABASE_URL at PostgreSQL."))
    if "vera_pass" in url:
        add(Finding("error", "default_db_password",
                    "The database password is the example one from the repository.",
                    "Rotate the Postgres password and update DATABASE_URL."))
    if production and url.startswith("postgres") and "sslmode" not in url \
            and not any(h in url for h in ("@db:", "@localhost", "@127.0.0.1")):
        add(Finding("warning", "db_no_sslmode",
                    "DATABASE_URL reaches a remote host without sslmode, so credentials "
                    "and order data may cross the network in the clear.",
                    "Append ?sslmode=require (or verify-full) to DATABASE_URL."))

    # ---- data ----------------------------------------------------------
    if os.getenv("SEED_DEMO_DATA", "").lower() == "true":
        add(Finding("error", "demo_seed_enabled",
                    "SEED_DEMO_DATA=true inserts demo products into this database on "
                    "every boot.",
                    "Remove SEED_DEMO_DATA, or set it to false."))

    # ---- payments & notifications -------------------------------------
    if not payments.known_provider(settings.PAYMENT_PROVIDER):
        add(Finding("error", "unknown_payment_provider",
                    f"PAYMENT_PROVIDER={settings.PAYMENT_PROVIDER!r} is not a provider "
                    "this build knows, so payments are silently disabled — orders "
                    "would be accepted without ever being charged for.",
                    "Set PAYMENT_PROVIDER to one of: none, manual, razorpay."))

    if production:
        from app import notifications
        if payments.get_provider().name == "none":
            add(Finding("warning", "payments_disabled",
                        "PAYMENT_PROVIDER=none: orders are accepted without being paid for.",
                        "Set PAYMENT_PROVIDER to manual or razorpay."))
        channel = notifications.get_channel()
        if not channel.sends_real_mail:
            add(Finding("warning", "notifications_not_delivered",
                        f"NOTIFY_CHANNEL={channel.name}: customers receive nothing; "
                        "messages are only recorded.",
                        "Set NOTIFY_CHANNEL=smtp and configure SMTP_*."))
        if not settings.ADMIN_ALERT_EMAILS:
            add(Finding("warning", "no_admin_alerts",
                        "ADMIN_ALERT_EMAILS is empty: nobody is told about new orders, "
                        "failed payments or low stock.",
                        "Set ADMIN_ALERT_EMAILS to the addresses that should be alerted."))

    # ---- gateway, mail, rates and media credentials ---------------------
    provider_name = (settings.PAYMENT_PROVIDER or "").strip().lower()
    if provider_name == "razorpay":
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            add(Finding("error", "razorpay_keys_missing",
                        "PAYMENT_PROVIDER=razorpay but RAZORPAY_KEY_ID / "
                        "RAZORPAY_KEY_SECRET are not set, so no payment can be "
                        "created and checkout fails at the last step.",
                        "Set both from your Razorpay dashboard."))
        elif settings.RAZORPAY_KEY_ID.startswith("rzp_test_") and production:
            add(Finding("error", "razorpay_test_keys_in_production",
                        "This is a production deployment using Razorpay TEST keys, "
                        "so no real money can ever be collected.",
                        "Swap in the live keys (rzp_live_...)."))
        if not settings.RAZORPAY_WEBHOOK_SECRET:
            add(Finding("warning", "razorpay_webhook_secret_missing",
                        "RAZORPAY_WEBHOOK_SECRET is not set. Payments confirmed by "
                        "webhook — the ones where the customer closed the tab — "
                        "cannot be verified and will be rejected.",
                        "Set it, and point a webhook at POST /api/payments/webhook/razorpay."))

    if (settings.NOTIFY_CHANNEL or "").lower() == "smtp":
        remote_host = settings.SMTP_HOST and settings.SMTP_HOST not in (
            "localhost", "127.0.0.1", "::1")
        if remote_host and not (settings.SMTP_USERNAME and settings.SMTP_PASSWORD):
            add(Finding("warning", "smtp_credentials_missing",
                        "SMTP is configured against a remote host without a username "
                        "and password. Most providers will refuse to relay.",
                        "Set SMTP_USERNAME and SMTP_PASSWORD (an app password, not "
                        "an account password)."))

    if (settings.EXCHANGE_RATE_PROVIDER or "static").lower() != "static"             and not settings.EXCHANGE_RATE_API_KEY:
        add(Finding("warning", "exchange_rate_key_missing",
                    "A live exchange-rate provider is selected but no API key is set, "
                    "so prices fall back to the built-in indicative table.",
                    "Set EXCHANGE_RATE_API_KEY, or set EXCHANGE_RATE_PROVIDER=static "
                    "and accept indicative rates."))

    if production:
        add_media = True
        try:
            from app import storage
            add_media = storage.get_storage().__class__.__name__ == "LocalDiskStorage"
        except Exception:          # noqa: BLE001
            pass
        if add_media:
            add(Finding("warning", "media_on_local_disk",
                        "Uploaded media is stored on the API container's local disk, "
                        "so it is lost when the container is replaced and cannot be "
                        "shared between replicas.",
                        "Point MEDIA storage at S3 or Cloudinary (see app/storage.py)."))

    # ---- checks that need the database --------------------------------
    if db is not None:
        if _seeded_admin_still_default(db):
            add(Finding("error", "seeded_admin_password",
                        f"{SEEDED_ADMIN_EMAIL} still accepts the published seed password.",
                        "Change it before exposing the dashboard."))
        if production and _demo_data_present(db):
            add(Finding("warning", "demo_products_present",
                        "Demo products (is_demo=true) exist in this database.",
                        "Remove them, or keep them unpublished; they are excluded with "
                        "?include_demo=false."))
        pending = pending_migrations(db)
        if pending:
            add(Finding("error", "migrations_pending",
                        f"The database is at {pending[0] or 'no revision'} but the code "
                        f"expects {pending[1]}.",
                        "Run `alembic upgrade head` before serving traffic."))

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: order.get(f.level, 2))
    return findings


def pending_migrations(db) -> Optional[tuple]:
    """(current_revision, expected_head) when they differ, else None.

    Serving traffic against a schema the code does not expect is how a deploy
    turns into data loss, so this is an error-level finding.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import inspect

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = Config(os.path.join(here, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(here, "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()

        # An entirely un-migrated database has no alembic_version table. That is
        # the worst version of this problem, not the absence of one, so it must
        # be reported rather than swallowed as "cannot tell".
        if not inspect(db.get_bind()).has_table("alembic_version"):
            return (None, head)
        current = db.execute(text("select version_num from alembic_version")).scalar()
        return None if current == head else (current, head)
    except Exception:              # noqa: BLE001 - never fail the boot on a check
        logger.debug("Could not compare migration revisions", exc_info=True)
        return None


def run_preflight(db=None) -> List[Finding]:
    """Log every finding; abort the boot if production has an error-level one."""
    findings = collect_findings(db)
    errors = [f for f in findings if f.level == "error"]

    for finding in findings:
        (logger.error if finding.level == "error" else logger.warning)("%s", finding)

    if errors and settings.is_production:
        detail = "\n".join(f"  - {f.code}: {f.message}\n    fix: {f.fix}" for f in errors)
        raise PreflightError(
            f"Refusing to start: {len(errors)} unsafe setting(s) for APP_ENV=production.\n"
            f"{detail}\n"
            "Set APP_ENV=development to run anyway (never do this on a public host)."
        )
    if errors:
        logger.warning(
            "%s setting(s) here would REFUSE to boot with APP_ENV=production.", len(errors))
    return findings


def runtime_info() -> dict:
    """Non-secret facts about this process, for /api/version and logs."""
    return {
        "version": settings.APP_VERSION,
        "commit": settings.GIT_SHA or None,
        "environment": settings.env,
        "payment_provider": settings.PAYMENT_PROVIDER,
        "notify_channel": settings.NOTIFY_CHANNEL,
        "notify_dispatch": settings.NOTIFY_DISPATCH,
        "rate_limiting": settings.rate_limit_enabled,
    }
