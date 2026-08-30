"""Production-readiness tests.

The preflight is the thing worth testing hardest: it is what stands between a
misconfigured deploy and a live shop, and it is only useful if it fires on the
exact settings people actually ship by accident.

Unit tests here mutate `settings` and restore it — the app reads settings
lazily, so this is safe as long as every test cleans up (the `restore_settings`
fixture does).

The HTTP tests need a backend running on port 8010, like the rest of the suite:

    uvicorn app.main:app --port 8010
    python -m pytest tests/test_production.py -q
"""
import json
import logging
import os
import types

import pytest
import requests

from app import runtime
from app.config import settings
from app.middleware import RateLimitMiddleware, client_ip, parse_limit
from app.observability import JsonFormatter, RequestIdFilter, request_id_var

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")

WATCHED = (
    "APP_ENV", "SECRET_KEY", "CORS_ORIGINS", "ALLOWED_HOSTS", "DATABASE_URL",
    "RATE_LIMIT_ENABLED", "TRUST_PROXY", "FORCE_HTTPS", "ADMIN_ALERT_EMAILS",
)


@pytest.fixture(autouse=True)
def restore_settings():
    saved = {name: getattr(settings, name) for name in WATCHED}
    seed_flag = os.environ.get("SEED_DEMO_DATA")
    yield
    for name, value in saved.items():
        setattr(settings, name, value)
    if seed_flag is None:
        os.environ.pop("SEED_DEMO_DATA", None)
    else:
        os.environ["SEED_DEMO_DATA"] = seed_flag


def _safe_production():
    """A configuration that should raise nothing at all."""
    settings.APP_ENV = "production"
    settings.SECRET_KEY = "x" * 64
    settings.CORS_ORIGINS = ["https://verahair.co"]
    settings.ALLOWED_HOSTS = ["api.verahair.co"]
    settings.DATABASE_URL = "postgresql+psycopg2://vera:Str0ngPass@db:5432/vera"
    settings.RATE_LIMIT_ENABLED = "true"
    settings.TRUST_PROXY = True
    settings.ADMIN_ALERT_EMAILS = ["studio@verahair.co"]


def codes(db=None):
    return {f.code for f in runtime.collect_findings(db)}


def errors(db=None):
    return {f.code for f in runtime.collect_findings(db) if f.level == "error"}


# ---------------- the preflight ----------------

def test_a_correct_production_config_raises_no_errors():
    _safe_production()
    assert errors() == set()


def test_the_shipped_secret_key_is_an_error():
    _safe_production()
    for default in runtime.DEFAULT_SECRET_KEYS:
        settings.SECRET_KEY = default
        assert "default_secret_key" in errors(), default


def test_a_short_secret_key_is_an_error():
    _safe_production()
    settings.SECRET_KEY = "short"
    assert "short_secret_key" in errors()


def test_wildcard_cors_is_an_error():
    _safe_production()
    settings.CORS_ORIGINS = ["*"]
    assert "wildcard_cors" in errors()


def test_an_empty_host_allow_list_is_an_error():
    _safe_production()
    settings.ALLOWED_HOSTS = []
    assert "no_allowed_hosts" in errors()


def test_sqlite_and_the_example_database_password_are_errors():
    _safe_production()
    settings.DATABASE_URL = "sqlite:///./vera_hair_co.db"
    assert "sqlite_in_production" in errors()

    _safe_production()
    settings.DATABASE_URL = "postgresql+psycopg2://vera_user:vera_pass@db:5432/vera"
    assert "default_db_password" in errors()


def test_demo_seeding_in_production_is_an_error():
    _safe_production()
    os.environ["SEED_DEMO_DATA"] = "true"
    assert "demo_seed_enabled" in errors()


def test_a_remote_database_without_ssl_is_flagged():
    _safe_production()
    settings.DATABASE_URL = "postgresql+psycopg2://vera:pw@db.example.com:5432/vera"
    assert "db_no_sslmode" in codes()
    settings.DATABASE_URL += "?sslmode=require"
    assert "db_no_sslmode" not in codes()


def test_missing_tls_and_rate_limiting_are_warnings_not_blockers():
    """These degrade a deployment; they do not make it forgeable. Blocking on
    them would train people to set APP_ENV=development to get past the check."""
    _safe_production()
    settings.TRUST_PROXY = False
    settings.FORCE_HTTPS = False
    settings.RATE_LIMIT_ENABLED = "false"
    found = codes()
    assert {"no_tls_signal", "no_rate_limit"} <= found
    assert errors() == set()


def test_production_refuses_to_boot_on_an_error():
    _safe_production()
    settings.SECRET_KEY = "change-this-secret-in-production"
    with pytest.raises(runtime.PreflightError) as exc:
        runtime.run_preflight()
    message = str(exc.value)
    assert "Refusing to start" in message
    assert "default_secret_key" in message
    assert "fix:" in message              # the message must say what to do


def test_the_same_config_only_warns_outside_production():
    """A laptop is allowed to be insecure; it is not helped by a refusal."""
    _safe_production()
    settings.APP_ENV = "development"
    settings.SECRET_KEY = "change-this-secret-in-production"
    findings = runtime.run_preflight()          # must not raise
    assert "default_secret_key" in {f.code for f in findings}


def test_findings_are_ordered_worst_first_and_explain_the_fix():
    _safe_production()
    settings.SECRET_KEY = "change-this-secret-in-production"
    settings.TRUST_PROXY = False
    findings = runtime.collect_findings()
    levels = [f.level for f in findings]
    assert levels == sorted(levels, key=lambda l: 0 if l == "error" else 1)
    assert all(f.fix for f in findings)


def test_runtime_info_exposes_no_secrets():
    _safe_production()
    info = json.dumps(runtime.runtime_info())
    assert settings.SECRET_KEY not in info
    assert "password" not in info.lower()


# ---------------- rate limiting ----------------

def test_limit_specs_parse_and_bad_ones_fall_back():
    assert parse_limit("12/60") == (12, 60)
    assert parse_limit("8/300") == (8, 300)
    assert parse_limit("nonsense", (5, 5)) == (5, 5)
    assert parse_limit(None, (5, 5)) == (5, 5)


def _request(path="/api/products", method="GET", host="1.2.3.4", headers=None):
    return types.SimpleNamespace(
        url=types.SimpleNamespace(path=path),
        method=method,
        headers=headers or {},
        client=types.SimpleNamespace(host=host),
    )


def test_buckets_are_chosen_by_route_and_method():
    limiter = RateLimitMiddleware(app=None)
    assert limiter.bucket_for(_request("/api/auth/login", "POST"))[0] == "login"
    assert limiter.bucket_for(_request("/api/orders", "POST"))[0] == "write"
    assert limiter.bucket_for(_request("/api/products", "GET"))[0] == "public"


def test_webhooks_and_probes_are_never_rate_limited():
    """A gateway retrying a webhook is not abuse — throttling it loses money.
    A probe being throttled makes an orchestrator kill a healthy container."""
    limiter = RateLimitMiddleware(app=None)
    assert limiter.bucket_for(_request("/api/payments/webhook/razorpay", "POST")) is None
    assert limiter.bucket_for(_request("/api/health")) is None
    assert limiter.bucket_for(_request("/api/ready")) is None


def test_a_forwarded_header_is_only_believed_behind_a_trusted_proxy():
    """Otherwise anyone rotates X-Forwarded-For and the limiter is decorative."""
    request = _request(headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, host="10.0.0.1")
    settings.TRUST_PROXY = False
    assert client_ip(request) == "10.0.0.1"
    settings.TRUST_PROXY = True
    assert client_ip(request) == "9.9.9.9"


# ---------------- logging ----------------

def test_json_logs_carry_the_request_id_and_extra_context():
    record = logging.LogRecord("vera.http", logging.INFO, __file__, 1,
                               "GET /api/products 200", (), None)
    record.path = "/api/products"
    record.status = 200
    token = request_id_var.set("req-abc-123")
    try:
        RequestIdFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-abc-123"
    assert payload["path"] == "/api/products" and payload["status"] == 200
    assert payload["level"] == "INFO"


def test_a_log_record_outside_a_request_still_formats():
    record = logging.LogRecord("vera", logging.INFO, __file__, 1, "worker tick", (), None)
    RequestIdFilter().filter(record)
    assert json.loads(JsonFormatter().format(record))["request_id"] == "-"


# ---------------- HTTP ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")


@live
def test_liveness_stays_cheap_and_says_nothing_about_the_database():
    body = requests.get(f"{API}/health", timeout=5).json()
    assert body == {"status": "ok"}


@live
def test_readiness_reports_the_database_and_the_findings():
    response = requests.get(f"{API}/ready", timeout=10)
    body = response.json()
    assert response.status_code in (200, 503)
    assert body["database"] in ("ok", "unreachable")
    assert isinstance(body["findings"], list)
    for finding in body["findings"]:
        assert finding["level"] in ("error", "warning")
        assert finding["fix"]


@live
def test_version_is_public_but_carries_no_secrets():
    body = requests.get(f"{API}/version", timeout=5).json()
    assert body["environment"] in ("development", "staging", "production")
    assert "secret" not in json.dumps(body).lower()


@live
def test_every_response_carries_the_security_headers():
    headers = requests.get(f"{API}/health", timeout=5).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


@live
def test_a_request_id_is_returned_and_a_supplied_one_is_kept():
    generated = requests.get(f"{API}/health", timeout=5).headers["X-Request-ID"]
    assert generated and len(generated) <= 64
    echoed = requests.get(f"{API}/health", timeout=5,
                          headers={"X-Request-ID": "trace-from-a-proxy"})
    assert echoed.headers["X-Request-ID"] == "trace-from-a-proxy"


@live
def test_an_error_response_hands_back_the_id_to_quote():
    """Support's first question is "what does the error say?" — the id makes
    that answer point at the exact log lines."""
    response = requests.get(f"{API}/orders/does-not-exist", timeout=5,
                            headers={"X-Request-ID": "trace-404"})
    assert response.status_code in (401, 404)
    assert response.headers["X-Request-ID"] == "trace-404"
