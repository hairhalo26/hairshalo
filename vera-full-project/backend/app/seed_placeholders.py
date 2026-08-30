"""Seed data for PRODUCT PLACEHOLDERS only.

Deliberately kept in its own module and its own entry point so placeholder data
is never generated as a side effect of seeding the real catalog. These records
are not purchasable, hold no stock, and never appear in revenue or analytics.

Run with:  python -m app.seed_placeholders
"""
import sys

from sqlalchemy import inspect

from app.database import engine, SessionLocal
from app import models


PLACEHOLDERS = [
    dict(
        name="Signature Lace Wig", category="Frontal Wigs",
        short_description="Our signature hand-tied lace unit. Full details and pricing coming soon.",
        placeholder_label="Coming Soon", display_price="Price on request",
        badge="Coming Soon", sort_order=10,
    ),
    dict(
        name="Luxury Glueless Wig", category="Glueless Wigs",
        short_description="Adjustable, install-free luxury unit. Launching with the next collection.",
        placeholder_label="Coming Soon", display_price="From ₹14,000",
        badge="Coming Soon", sort_order=20,
    ),
    dict(
        name="Premium Human Hair", category="Extensions",
        short_description="Ethically sourced premium human hair. Lengths and shades being finalised.",
        placeholder_label="Coming Soon", display_price="Price on request",
        badge=None, sort_order=30,
    ),
    dict(
        name="HD Lace Collection", category="Frontal Wigs",
        short_description="An HD lace range built for an undetectable hairline. In production.",
        placeholder_label="Product Image Coming Soon", display_price="From ₹18,000",
        badge="New Collection", sort_order=40,
    ),
    dict(
        name="Natural Wave Collection", category="Closure Wigs",
        short_description="Soft natural wave textures, closure-ready. Photography in progress.",
        placeholder_label="Product Image Coming Soon", display_price="Price on request",
        badge=None, sort_order=50,
    ),
    dict(
        name="Luxury Hair Extensions", category="Extensions",
        short_description="Seamless clip-in and tape-in extensions. Shade matching coming soon.",
        placeholder_label="Coming Soon", display_price="From ₹6,500",
        badge=None, sort_order=60,
    ),
]


def run():
    # Schema is owned by Alembic (`alembic upgrade head`), not by this script.
    from app.database import PLACEHOLDER_SCHEMA
    if not inspect(engine).has_table("product_placeholders", schema=PLACEHOLDER_SCHEMA):
        print("Placeholder table missing. Run migrations first:\n\n    alembic upgrade head\n")
        sys.exit(1)

    db = SessionLocal()
    try:
        if db.query(models.ProductPlaceholder).count() == 0:
            print("Seeding product placeholders (demo data)...")
            db.add_all([models.ProductPlaceholder(is_demo=True, **p) for p in PLACEHOLDERS])
            db.commit()
            print(f"Placeholder seed complete — {len(PLACEHOLDERS)} placeholders created.")
        else:
            print("Product placeholders already present — skipping.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
