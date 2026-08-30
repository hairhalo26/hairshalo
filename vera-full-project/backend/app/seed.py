"""DEVELOPMENT / DEMO SEED DATA — NOT FOR PRODUCTION.

Every row created here is tagged `is_demo=True` where the model supports it, so
demo data is always distinguishable from real production data.

This script refuses to run unless it is explicitly allowed, so it can never be
triggered accidentally against a production database:

    SEED_DEMO_DATA=true python -m app.seed
    python -m app.seed --force

Tables are created by Alembic, not by this script:

    alembic upgrade head
"""
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.database import engine, SessionLocal
from app import loyalty, models, reviews, seed_placeholders
from app.pricing import compute_pricing
from app.security import hash_password


def _priced(target, original, dtype="none", dvalue=0):
    """Apply the canonical pricing engine to a seeded product/variant."""
    price, compare_at, _amt, ty, val = compute_pricing(original, dtype, dvalue)
    target.price = price
    target.compare_at_price = compare_at
    target.discount_type = models.DiscountKind(ty)
    target.discount_value = val
    return target

BANNER = """
============================================================
  HAIRSHALO — DEVELOPMENT / DEMO SEED DATA
  All catalog rows are tagged is_demo=True.
  Do NOT run this against a production database.
============================================================
"""


def _allowed() -> bool:
    return (
        os.getenv("SEED_DEMO_DATA", "").lower() in ("1", "true", "yes")
        or "--force" in sys.argv
    )


def run():
    if not _allowed():
        print(
            "Refusing to seed: demo data is development-only.\n"
            "Set SEED_DEMO_DATA=true (or pass --force) if this is a dev database."
        )
        sys.exit(1)

    print(BANNER)

    # Schema is owned by Alembic. Fail loudly rather than silently creating it.
    if not inspect(engine).has_table("products"):
        print("Tables are missing. Run migrations first:\n\n    alembic upgrade head\n")
        sys.exit(1)

    db = SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            print("Seeding admin user...")
            db.add(models.User(
                email="admin@hairshalo.com",
                hashed_password=hash_password("ChangeMe123!"),
                full_name="Priya Sharma",
                role="admin",
            ))

        # ---- Categories (database-driven) ----
        category_specs = [
            ("Frontal Wigs", "frontal-wigs", 10),
            ("Closure Wigs", "closure-wigs", 20),
            ("Glueless Wigs", "glueless-wigs", 30),
            ("Toppers", "toppers", 40),
            ("Extensions", "extensions", 50),
        ]
        categories = {}
        for name, slug, order in category_specs:
            cat = db.query(models.Category).filter(models.Category.name == name).first()
            if not cat:
                cat = models.Category(name=name, slug=slug, sort_order=order, is_active=True)
                db.add(cat)
                db.flush()
            categories[name] = cat

        if db.query(models.Product).count() == 0:
            print("Seeding products, media and variants...")
            IMG_FRONTAL = "https://images.unsplash.com/photo-1675034743339-0b0747047727?w=700&q=80&fm=jpg&fit=crop&auto=format"
            IMG_BOB = "https://images.unsplash.com/photo-1634449571010-02389ed0f9b0?w=700&q=80&fm=jpg&fit=crop&auto=format"
            IMG_WAVE = "https://images.unsplash.com/photo-1503830232159-4b417691001e?w=700&q=80&fm=jpg&fit=crop&auto=format"
            IMG_TOPPER = "https://images.unsplash.com/photo-1560869713-bf165a9cfac1?w=700&q=80&fm=jpg&fit=crop&auto=format"

            specs = [
                # Discounted, multi-variant, MULTIPLE IMAGES + variant-level pricing
                dict(
                    name="Malaika HD Lace Frontal", slug="malaika-hd-lace-frontal",
                    category="Frontal Wigs", original=22999, dtype="fixed_amount", dvalue=4000,
                    short="HD lace frontal with an undetectable hairline.",
                    description="Full HD lace frontal with a natural, undetectable hairline. 360° styling freedom.",
                    brand="Hairshalo Signature", hair_type="Human Hair", construction="Lace Frontal",
                    images=[IMG_FRONTAL, IMG_WAVE, IMG_TOPPER],
                    status=models.ProductStatus.published,
                    badge=models.ProductBadge.bestseller, texture="Straight",
                    featured=True, bestseller=True, new_arrival=False, sort_order=10,
                    variants=[
                        dict(sku="VR-FR-001-18-NB", length='18"', density="150%", color="Natural Black", lace_type="HD Lace", cap_size="Medium", stock=40),
                        # Variant-level pricing: longer length costs more, own discount
                        dict(sku="VR-FR-001-20-NB", length='20"', density="180%", color="Natural Black", lace_type="HD Lace", cap_size="Medium", stock=24,
                             original=26999, dtype="fixed_amount", dvalue=4000),
                    ],
                ),
                # Fixed-amount discount
                dict(
                    name="Anaya Glueless Bob", slug="anaya-glueless-bob",
                    category="Glueless Wigs", original=14999, dtype="fixed_amount", dvalue=2500,
                    short="Adjustable-strap glueless bob for first-time wearers.",
                    description="Adjustable-strap glueless bob, ideal for first-time wig wearers.",
                    brand="Hairshalo Everyday", hair_type="Human Hair", construction="Glueless Cap",
                    images=[IMG_BOB],
                    status=models.ProductStatus.published,
                    badge=models.ProductBadge.sale, texture="Straight",
                    featured=False, bestseller=True, new_arrival=False, sort_order=20,
                    variants=[
                        dict(sku="VR-BB-002-12-DB", length='12"', density="150%", color="Dark Brown", lace_type="Transparent Lace", cap_size="Small", stock=11),
                    ],
                ),
                # No discount
                dict(
                    name="Ishita Closure Wave", slug="ishita-closure-wave",
                    category="Closure Wigs", original=15999, dtype="none", dvalue=0,
                    short="Versatile parting closure wig for everyday wear.",
                    description="Versatile parting closure wig for easy, everyday wear.",
                    brand="Hairshalo Everyday", hair_type="Human Hair", construction="Closure",
                    images=[IMG_WAVE],
                    status=models.ProductStatus.published,
                    badge=models.ProductBadge.new, texture="Body Wave",
                    featured=True, bestseller=False, new_arrival=True, sort_order=30,
                    variants=[
                        dict(sku="VR-CW-003-16-NB", length='16"', density="150%", color="Natural Black", lace_type="Swiss Lace", cap_size="Medium", stock=38),
                    ],
                ),
                # No discount, no badge, low stock
                dict(
                    name="Kavya Silk Topper", slug="kavya-silk-topper",
                    category="Toppers", original=9499, dtype="none", dvalue=0,
                    short="Natural crown coverage that blends with your own hair.",
                    description="Blends seamlessly with your own hair for natural crown coverage.",
                    brand="Hairshalo Everyday", hair_type="Human Hair", construction="Silk Base Topper",
                    images=[IMG_TOPPER],
                    status=models.ProductStatus.published,
                    badge=None, texture="Straight",
                    featured=False, bestseller=False, new_arrival=False, sort_order=40,
                    variants=[
                        dict(sku="VR-TP-004-08-CB", length='8"', density="120%", color="Chocolate Brown", lace_type="Silk Base", cap_size="One Size", stock=6),
                    ],
                ),
                # Draft — must never be visible to customers
                dict(
                    name="Zara 360 Frontal", slug="zara-360-frontal",
                    category="Frontal Wigs", original=21499, dtype="none", dvalue=0,
                    short="360° lace frontal for full styling versatility.",
                    description="360° lace frontal for full styling versatility, including updos.",
                    brand="Hairshalo Signature", hair_type="Human Hair", construction="360 Lace",
                    images=[IMG_FRONTAL],
                    status=models.ProductStatus.draft,
                    badge=None, texture="Straight",
                    featured=False, bestseller=False, new_arrival=False, sort_order=50,
                    variants=[
                        dict(sku="VR-FR-005-20-NB", length='20"', density="180%", color="Natural Black", lace_type="HD Lace", cap_size="Large", stock=22),
                    ],
                ),
            ]

            products = []
            for s in specs:
                p = models.Product(
                    name=s["name"], slug=s["slug"], category_id=categories[s["category"]].id,
                    short_description=s["short"], description=s["description"],
                    brand=s["brand"], hair_type=s["hair_type"], construction=s["construction"],
                    texture=s["texture"],
                    # No rating here: it is derived from reviews by
                    # app/reviews.py:recalculate(), never asserted.
                    status=s["status"], badge=s["badge"], featured=s["featured"],
                    bestseller=s["bestseller"], new_arrival=s["new_arrival"],
                    sort_order=s["sort_order"], is_demo=True,
                    published_at=datetime.utcnow() if s["status"] == models.ProductStatus.published else None,
                )
                _priced(p, s["original"], s["dtype"], s["dvalue"])
                for idx, img in enumerate(s["images"]):
                    p.media.append(models.ProductMedia(
                        url=img, media_type=models.MediaType.image,
                        alt_text=s["name"], sort_order=idx, is_primary=(idx == 0),
                    ))
                for i, v in enumerate(s["variants"]):
                    v = dict(v)
                    v_original = v.pop("original", None)
                    v_dtype = v.pop("dtype", "none")
                    v_dvalue = v.pop("dvalue", 0)
                    variant = models.ProductVariant(sort_order=i * 10, **v)
                    if v_original is not None:
                        _priced(variant, v_original, v_dtype, v_dvalue)
                    p.variants.append(variant)
                products.append(p)

            db.add_all(products)
            db.flush()

            print("Seeding inventory...")
            db.add_all([
                models.InventoryItem(product_id=p.id, variant_id=p.variants[0].id,
                                     sku=p.variants[0].sku + "-WH",
                                     variant=p.variants[0].label,
                                     warehouse="Mumbai WH1", units=p.variants[0].stock)
                for p in products
            ])

            print("Seeding coupons...")
            db.add_all([
                models.Coupon(code="WELCOME10", description="10% off first order", discount_type=models.DiscountType.percent, discount_value=10, usage_count=412),
                models.Coupon(code="FREESHIP", description="Free shipping over ₹8,000", discount_type=models.DiscountType.free_shipping, discount_value=0, usage_count=288),
                models.Coupon(code="FITKIT", description="Free sizing kit", discount_type=models.DiscountType.flat, discount_value=0, usage_count=176),
            ])

            print("Seeding sample customers, orders, and appointments...")
            # Balances start at zero and are granted below through the ledger:
            # a balance with no transactions behind it would break the invariant
            # the loyalty code relies on from the very first row.
            customers = [
                models.Customer(name="Priyanka R.", email="priyanka@example.com"),
                models.Customer(name="Meera S.", email="meera@example.com"),
                models.Customer(name="Ananya K.", email="ananya@example.com"),
                models.Customer(name="Divya T.", email="divya@example.com"),
            ]
            db.add_all(customers)
            db.flush()

            order_specs = [
                ("VR-3021", 0, 0, 18999, models.OrderStatus.delivered, 1),
                ("VR-3020", 1, 1, 12499, models.OrderStatus.out_for_delivery, 1),
                ("VR-3019", 2, 2, 15999, models.OrderStatus.shipped, 2),
                ("VR-3018", 3, 3, 9499, models.OrderStatus.processing, 2),
            ]
            for number, ci, pi, amount, status, days_ago in order_specs:
                prod = products[pi]
                var = prod.variants[0]
                db.add(models.Order(
                    order_number=number, customer_id=customers[ci].id,
                    customer_name=customers[ci].name, customer_email=customers[ci].email,
                    total=amount, status=status,
                    created_at=datetime.utcnow() - timedelta(days=days_ago),
                    items=[models.OrderItem(
                        product_id=prod.id, variant_id=var.id, product_name=prod.name,
                        variant_label=var.label, variant_sku=var.sku,
                        quantity=1, price=amount,
                    )],
                ))

            db.flush()

            print("Seeding demo reviews...")
            # Reviews are seeded as rows, and the rating is then RECOMPUTED from
            # them — so even the demo catalog's stars trace to something. The
            # ones attached to the delivered order are genuinely verified; the
            # rest are marked unverified rather than pretending otherwise.
            delivered = db.query(models.Order).filter(
                models.Order.order_number == "VR-3021").first()
            review_specs = [
                (0, "Priyanka R.", "priyanka@example.com", 5, "Worth every rupee",
                 "The HD lace really does disappear. I have worn it to two weddings "
                 "and nobody guessed."),
                (0, "Meera S.", "meera@example.com", 4, "Beautiful, slightly tight cap",
                 "Gorgeous density and the parting looks real. The medium cap runs "
                 "small if you have a lot of hair underneath."),
                (1, "Ananya K.", "ananya@example.com", 5, "My everyday unit",
                 "Glueless and genuinely secure. Takes me four minutes in the morning."),
                (2, "Divya T.", "divya@example.com", 4, "Lovely wave pattern",
                 "Holds the wave after washing. I use a sulphate-free shampoo as advised."),
            ]
            for idx, name, email, rating, title, body in review_specs:
                if idx >= len(products):
                    continue
                product = products[idx]
                line = None
                if delivered and idx == 0 and name == "Priyanka R.":
                    line = next((i for i in delivered.items
                                 if i.product_id == product.id), None)
                db.add(models.Review(
                    product_id=product.id,
                    order_id=delivered.id if line else None,
                    order_item_id=line.id if line else None,
                    author_name=name, author_email=email,
                    rating=rating, title=title, body=body,
                    status=models.ReviewStatus.published,
                    is_verified_purchase=bool(line),
                    is_demo=True,
                    created_at=datetime.utcnow() - timedelta(days=idx + 2),
                ))
            db.flush()
            for product in products:
                reviews.recalculate(db, product.id)

            print("Seeding loyalty balances (through the ledger)...")
            for customer, points, note in (
                (customers[0], 612, "Demo balance carried over from earlier orders"),
                (customers[1], 250, "Demo balance carried over from earlier orders"),
                (customers[2], 884, "Demo balance carried over from earlier orders"),
                (customers[3], 95, "Demo balance carried over from earlier orders"),
            ):
                loyalty.apply(db, customer, points, models.LoyaltyReason.adjustment,
                              note=note, actor="seed")

            print("Seeding marketing subscribers...")
            # Confirmed, because they are demo rows standing in for people who
            # completed double opt-in. Nothing here fabricates consent for a
            # real address: every one of these is @example.com.
            db.add_all([
                models.MarketingSubscriber(
                    email="priyanka@example.com", name="Priyanka R.",
                    status=models.SubscriberStatus.confirmed, source="storefront_footer",
                    confirmed_at=datetime.utcnow() - timedelta(days=20), is_demo=True),
                models.MarketingSubscriber(
                    email="ananya@example.com", name="Ananya K.",
                    status=models.SubscriberStatus.confirmed, source="storefront_footer",
                    confirmed_at=datetime.utcnow() - timedelta(days=9), is_demo=True),
                models.MarketingSubscriber(
                    email="sanya@example.com", name="Sanya D.",
                    status=models.SubscriberStatus.pending, source="storefront_footer",
                    is_demo=True),
            ])

            db.add_all([
                models.Appointment(
                    customer_name="Kavita N.", customer_email="kavita@example.com",
                    appointment_type="Wig Fitting", stylist="Rhea (Stylist)",
                    scheduled_at=datetime.utcnow() + timedelta(days=1, hours=2),
                    status=models.AppointmentStatus.confirmed,
                ),
                models.Appointment(
                    customer_name="Sanya D.", customer_email="sanya@example.com",
                    appointment_type="Hair Consultation", stylist="Rhea (Stylist)",
                    scheduled_at=datetime.utcnow() + timedelta(days=1, hours=5),
                    status=models.AppointmentStatus.confirmed,
                ),
            ])

        db.commit()
        print("Seed complete (demo data).")
        print("Admin login -> email: admin@hairshalo.com  password: ChangeMe123!")
    finally:
        db.close()

    seed_placeholders.run()


if __name__ == "__main__":
    run()
