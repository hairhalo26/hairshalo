"""HTTP middleware: request identity, security headers, rate limiting.

Ordering matters and is set in app/main.py. Request identity is outermost, so
every log line — including one written by a middleware that rejects the request
— carries the id.
"""
import logging
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Tuple

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.observability import request_id_var

logger = logging.getLogger("vera.http")

#: Endpoints that must never be rate limited or logged with their query string.
#: A payment gateway retries webhooks by design; throttling those loses money.
RATE_LIMIT_EXEMPT_PREFIXES = ("/api/payments/webhook", "/api/health", "/api/ready")


def client_ip(request) -> str:
    """The caller's address, honouring a proxy only when we trust one.

    X-Forwarded-For is caller-supplied text. Reading it unconditionally lets
    anyone rotate a header to defeat rate limiting, so it is consulted only
    when TRUST_PROXY says a proxy is really in front of us — and then only its
    first entry, which is the client the proxy saw.
    """
    if settings.TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Give every request an id, time it, and log one access line."""

    async def dispatch(self, request, call_next):
        incoming = request.headers.get("x-request-id", "")
        # Accept a proxy's id so traces join up, but never echo unbounded
        # caller-controlled text into logs and headers.
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        # The reset lives in `finally`, so everything logged below — including
        # the access line itself — still sees the id. Resetting before logging
        # would silently strip the id from the very line that needs it.
        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            response.headers["X-Request-ID"] = request_id
            # Path only, never the query string: coupon codes, unsubscribe
            # tokens and search terms live there.
            logger.info(
                "%s %s %s %sms", request.method, request.url.path,
                response.status_code, duration_ms,
                extra={"method": request.method, "path": request.url.path,
                       "status": response.status_code, "duration_ms": duration_ms,
                       "client_ip": client_ip(request)},
            )
            return response
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.exception(
                "%s %s failed after %sms", request.method, request.url.path, duration_ms,
                extra={"method": request.method, "path": request.url.path,
                       "duration_ms": duration_ms, "client_ip": client_ip(request)},
            )
            raise
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that cost nothing and close whole classes of attack.

    This is an API plus a `/media` mount, so the CSP is the restrictive kind:
    nothing is allowed to load or frame anything. The dashboard and storefront
    are served separately and carry their own policy.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "img-src 'self' data:",
        )
        headers.setdefault("Permissions-Policy",
                           "geolocation=(), microphone=(), camera=(), payment=()")
        # HSTS only when TLS is actually in play — sending it over plain HTTP
        # can lock a development host out of itself.
        if settings.FORCE_HTTPS or (settings.TRUST_PROXY and
                                    request.headers.get("x-forwarded-proto") == "https"):
            headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.HSTS_MAX_AGE}; includeSubDomains")
        return response


def parse_limit(spec: str, fallback: Tuple[int, int] = (60, 60)) -> Tuple[int, int]:
    """"12/60" -> (12 requests, per 60 seconds)."""
    try:
        count, window = spec.split("/")
        return max(1, int(count)), max(1, int(window))
    except (AttributeError, ValueError):
        return fallback


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A sliding-window limiter, per client IP and per bucket.

    Deliberate limitation, stated rather than hidden: the window lives in this
    process's memory, so N API containers allow N times the configured rate,
    and a restart forgets everything. That is an acceptable speed bump against
    scripted abuse and a poor defence against a distributed attack. The real
    answer is a shared store (Redis) or limiting at the edge proxy — see the
    deployment chapter in the README.

    Payment webhooks are exempt: a gateway retrying a delivery is not abuse,
    and dropping those loses money.
    """

    def __init__(self, app):
        super().__init__(app)
        self._hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._last_sweep = time.monotonic()
        self.public = parse_limit(settings.RATE_LIMIT_PUBLIC, (60, 60))
        self.write = parse_limit(settings.RATE_LIMIT_WRITE, (12, 60))
        self.login = parse_limit(settings.RATE_LIMIT_LOGIN, (8, 300))

    def bucket_for(self, request):
        """Which limit applies, or None when the request is not limited."""
        path, method = request.url.path, request.method
        if path.startswith(RATE_LIMIT_EXEMPT_PREFIXES):
            return None
        if path == "/api/auth/login":
            return "login", self.login
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return "write", self.write
        if path.startswith("/api/"):
            return "public", self.public
        return None

    def _sweep(self, now: float) -> None:
        """Drop idle keys so the map cannot grow forever under a scan."""
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > 3600]:
            self._hits.pop(key, None)

    async def dispatch(self, request, call_next):
        if not settings.rate_limit_enabled:
            return await call_next(request)

        bucket = self.bucket_for(request)
        if not bucket:
            return await call_next(request)

        name, (limit, window) = bucket
        key = (client_ip(request), name)
        now = time.monotonic()

        with self._lock:
            self._sweep(now)
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(window - (now - hits[0])))
                remaining = 0
            else:
                hits.append(now)
                retry_after, remaining = 0, limit - len(hits)

        if retry_after:
            logger.warning("Rate limit hit on %s by %s", request.url.path, key[0],
                           extra={"path": request.url.path, "client_ip": key[0],
                                  "bucket": name})
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down and try again."},
                headers={"Retry-After": str(retry_after),
                         "X-RateLimit-Limit": str(limit),
                         "X-RateLimit-Remaining": "0"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
