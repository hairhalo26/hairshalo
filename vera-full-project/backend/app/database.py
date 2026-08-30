from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

# pool_pre_ping: a connection idle across a database restart or a proxy's idle
# timeout looks fine until it is used; pre-ping turns that into a transparent
# reconnect instead of a 500 for whichever customer arrived first.
# pool_recycle does the same proactively for connections older than the
# database's own timeout.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    # Identifies this application in pg_stat_activity, so a stuck query can be
    # traced back to the API rather than guessed at.
    connect_args={"application_name": f"vera-api-{settings.env}"}
    if settings.DATABASE_URL.startswith("postgresql") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Product placeholders are a completely separate data domain from the real,
# sellable catalog. They live in their own PostgreSQL schema so they can never
# be accidentally joined to, or mistaken for, rows in the public `products`
# table. A separate *physical* database was considered and rejected: orders,
# inventory and analytics all rely on foreign keys into `products`, and a
# second database would mean either cross-database queries or a second engine
# and session for no isolation benefit that a schema does not already provide.
#
# Non-PostgreSQL engines (which have no CREATE SCHEMA) fall back to keeping the
# table in the default schema — still a separate table, never mixed with products.
PLACEHOLDER_SCHEMA = "vera_product_placeholders" if engine.dialect.name == "postgresql" else None


def ensure_schemas() -> None:
    """Create the placeholder schema if it does not exist yet.

    Must run before Base.metadata.create_all(), because PostgreSQL will not
    create a table in a schema that does not exist.
    """
    if PLACEHOLDER_SCHEMA:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{PLACEHOLDER_SCHEMA}"'))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
