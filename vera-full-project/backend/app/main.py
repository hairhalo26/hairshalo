import logging
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import runtime
from app.config import settings
from app.database import SessionLocal
from app.middleware import (
    RateLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware,
)
from app.observability import configure_logging
from app.routers import (
    auth, products, categories, product_placeholders, orders, customers,
    appointments, inventory, coupons, analytics, currency, payments,
    notifications, reviews, loyalty, marketing, account
)

logger = logging.getLogger("vera")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure logging, then refuse to serve an unsafe production config.

    The preflight runs against the database when it is reachable, so checks
    like "the seeded admin password still works" and "migrations are pending"
    are included. If the database is down the config-only checks still run —
    a boot should not be waved through just because Postgres was slow to start.
    """
    configure_logging()
    db = None
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except Exception as exc:       # noqa: BLE001 - checked, then reported
        logger.warning("Database not reachable during preflight: %s", exc)
        if db is not None:
            db.close()
            db = None
    try:
        runtime.run_preflight(db)
    finally:
        if db is not None:
            db.close()

    info = runtime.runtime_info()
    logger.info("Hairshalo API %s starting in %s", info["version"], info["environment"],
                extra=info)
    yield
    logger.info("Hairshalo API shutting down")


app = FastAPI(
    title="Hairshalo API",
    description="Backend for the Hairshalo wig & hairstyling e-commerce platform.",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # Interactive docs are an inventory of the attack surface; keep them off a
    # production host and read them from a staging deployment instead.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Middleware is applied bottom-up: the LAST one added is the outermost.
# Effective order per request:
#   RequestContext -> CORS -> TrustedHost -> HTTPS redirect -> SecurityHeaders
#   -> RateLimit -> routers
# CORS sits outside the rate limiter deliberately: a 429 without CORS headers
# reaches the browser as an opaque network failure instead of a readable error.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

if settings.FORCE_HTTPS:
    app.add_middleware(HTTPSRedirectMiddleware)

if settings.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

app.add_middleware(RequestContextMiddleware)

# Uploaded product media. In production this should be served by the CDN /
# object store instead (see app/storage.py) rather than by the API process.
os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
app.mount(
    settings.MEDIA_URL_PREFIX,
    StaticFiles(directory=settings.MEDIA_ROOT),
    name="media",
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(categories.router)
app.include_router(product_placeholders.router)
app.include_router(orders.router)
app.include_router(customers.router)
app.include_router(appointments.router)
app.include_router(inventory.router)
app.include_router(coupons.router)
app.include_router(analytics.router)
app.include_router(currency.router)
app.include_router(payments.router)
app.include_router(notifications.router)
app.include_router(reviews.router)
app.include_router(loyalty.router)
app.include_router(marketing.router)
app.include_router(account.router)


if settings.cors_is_wildcard and not settings.is_production:
    logger.warning(
        "CORS_ORIGINS is '*' — this is a production gap. Set it to your real "
        "storefront origin(s) before deploying."
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return a JSON 500 instead of letting the exception escape.

    An escaping exception produces a bare response with no CORS headers, so the
    browser reports an opaque network failure and the real cause is invisible
    to the frontend. This keeps errors readable without leaking internals — the
    request id is echoed so a customer's screenshot leads to the log lines.
    """
    request_id = getattr(request.state, "request_id", "-")
    logger.error("Unhandled error on %s %s\n%s", request.method, request.url.path,
                 traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again.",
                 "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


@app.get("/api/health")
def health_check():
    """Liveness: is this process running? Deliberately touches nothing else.

    A liveness probe that checks the database restarts healthy API containers
    during a database blip, turning a small outage into a large one.
    """
    return {"status": "ok"}


@app.get("/api/ready")
def readiness_check():
    """Readiness: should this process receive traffic?

    Checks the database and reports the configuration findings, so a pipeline
    can ask "would this be accepted in production?" before promoting a build.
    Returns 503 when the database is unreachable or migrations are pending.
    """
    db = None
    database_ok, database_error = False, None
    findings = []
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        database_ok = True
    except Exception as exc:       # noqa: BLE001 - reported, not raised
        database_error = str(exc)[:200]
    try:
        findings = runtime.collect_findings(db if database_ok else None)
    finally:
        if db is not None:
            db.close()

    blocking = [f for f in findings if f.code == "migrations_pending"]
    ready = database_ok and not blocking
    body = {
        "ready": ready,
        "database": "ok" if database_ok else "unreachable",
        "environment": settings.env,
        "version": settings.APP_VERSION,
        "findings": [f.as_dict() for f in findings],
    }
    if database_error:
        body["database_error"] = database_error
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/api/version")
def version():
    """What is actually deployed here. No secrets, safe to expose."""
    return runtime.runtime_info()
