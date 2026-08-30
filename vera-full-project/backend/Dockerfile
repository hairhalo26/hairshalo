# Multi-stage: wheels are built with a compiler that the runtime image never
# ships. A smaller runtime is not the main point — not having gcc and the
# libpq headers sitting on a public host is.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production

# libpq5 is the runtime half of libpq-dev; curl is here only for HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Run as a non-root user. A container compromise then starts without the
# ability to rewrite the application it is running.
RUN useradd --create-home --uid 10001 vera \
    && mkdir -p /app/media \
    && chown -R vera:vera /app
USER vera

EXPOSE 8000

# Liveness only — it must not depend on the database, or a database blip would
# restart every healthy API container and turn a small outage into a large one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]

# Gunicorn supervises uvicorn workers: a worker that dies is replaced, and
# WEB_CONCURRENCY controls how many run. With more than one API container,
# also set NOTIFY_DISPATCH=worker and run the notifier service, so mail retries
# have exactly one owner.
CMD ["sh", "-c", "gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers ${WEB_CONCURRENCY:-2} \
    --bind 0.0.0.0:8000 \
    --timeout ${WEB_TIMEOUT:-60} \
    --graceful-timeout 30 \
    --access-logfile -"]
