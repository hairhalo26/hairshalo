"""
Creates all tables and seeds the database with demo data that matches
the Véra Hair Co. frontend mock data, so the site and admin dashboard
show consistent, realistic content out of the box.

Run with:  python -m app.seed
"""
from datetime import datetime, timedelta

from app.database import Base, engine, SessionLocal
from app import models
from app.security import hash_password


def run():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        if db.query(models.User).count() == 0:
            print("Seeding admin user...")
            db.add(models.User(
                email="admin@verahair.co",
                hashed_password=hash_password("ChangeMe123!"),
                full_name="Priya Sharma",
                role="admin",
            ))

        if db.query(models.Product).count() == 0:
            print("Seeding products...")
            products = [
                models.Product(
                    name="Malaika HD Lace Frontal", slug="malaika-hd-lace-frontal",
                    category="Frontal Wigs", price=18999, compare_at_price=None,
                    description="Full HD lace frontal with a natural, undetectable hairline. 360° styling freedom.",
                    image_url="https://images.unsplash.com/photo-1675034743339-0b0747047727?w=500&q=80&fm=jpg&fit=crop&auto=format",
                    rating=4.9, review_count=312, status=models.ProductStatus.active,
                ),
                models.Product(
                    name="Anaya Glueless Bob", slug="anaya-glueless-bob",
                    category="Glueless Wigs", price=12499, compare_at_price=14999,
                    description="Adjustable-strap glueless bob, ideal for first-time wig wearers.",
                    image_url="https://images.unsplash.com/photo-1634449571010-02389ed0f9b0?w=500&q=80&fm=jpg&fit=crop&auto=format",
                    rating=4.8, review_count=204, status=models.ProductStatus.active,
                ),
                models.Product(
                    name="Ishita Closure Wave", slug="ishita-closure-wave",
                    category="Closure Wigs", price=15999, compare_at_price=None,
                    description="Versatile parting closure wig for easy, everyday wear.",
                    image_url="https://images.unsplash.com/photo-1503830232159-4b417691001e?w=500&q=80&fm=jpg&fit=crop&auto=format",
                    rating=4.7, review_count=98, status=models.ProductStatus.active,
                ),
                models.Product(
                    name="Kavya Silk Topper", slug="kavya-silk-topper",
                    category="Toppers", price=9499, compare_at_price=None,
                    description="Blends seamlessly with your own hair for natural crown coverage.",
                    image_url="https://images.unsplash.com/photo-1560869713-bf165a9cfac1?w=500&q=80&fm=jpg&fit=crop&auto=format",
                    rating=4.9, review_count=156, status=models.ProductStatus.active,
                ),
                models.Product(
                    name="Zara 360 Frontal", slug="zara-360-frontal",
                    category="Frontal Wigs", price=21499, compare_at_price=None,
                    description="360° lace frontal for full styling versatility, including updos.",
                    image_url="https://images.unsplash.com/photo-1675034743339-0b0747047727?w=500&q=80&fm=jpg&fit=crop&auto=format",
                    rating=4.6, review_count=61, status=models.ProductStatus.draft,
                ),
            ]
            db.add_all(products)
            db.flush()

            print("Seeding inventory...")
            inv = [
                models.InventoryItem(product_id=products[0].id, sku="VR-FR-001", variant='Natural Black · 18"', warehouse="Mumbai WH1", units=64),
                models.InventoryItem(product_id=products[1].id, sku="VR-BB-002", variant='Dark Brown · 12"', warehouse="Mumbai WH1", units=11),
                models.InventoryItem(product_id=products[2].id, sku="VR-CW-003", variant='Natural Black · 16"', warehouse="Delhi WH2", units=38),
                models.InventoryItem(product_id=products[3].id, sku="VR-TP-004", variant='Chocolate Brown · 8"', warehouse="Delhi WH2", units=6),
                models.InventoryItem(product_id=products[4].id, sku="VR-FR-005", variant='Natural Black · 20"', warehouse="Mumbai WH1", units=22),
            ]
            db.add_all(inv)

            print("Seeding coupons...")
            db.add_all([
                models.Coupon(code="WELCOME10", description="10% off first order", discount_type=models.DiscountType.percent, discount_value=10, usage_count=412),
                models.Coupon(code="FREESHIP", description="Free shipping over ₹8,000", discount_type=models.DiscountType.free_shipping, discount_value=0, usage_count=288),
                models.Coupon(code="FITKIT", description="Free sizing kit", discount_type=models.DiscountType.flat, discount_value=0, usage_count=176),
            ])

            print("Seeding sample customers, orders, and appointments...")
            customers = [
                models.Customer(name="Priyanka R.", email="priyanka@example.com", loyalty_points=612),
                models.Customer(name="Meera S.", email="meera@example.com", loyalty_points=250),
                models.Customer(name="Ananya K.", email="ananya@example.com", loyalty_points=884),
                models.Customer(name="Divya T.", email="divya@example.com", loyalty_points=95),
            ]
            db.add_all(customers)
            db.flush()

            db.add_all([
                models.Order(
                    order_number="VR-3021", customer_id=customers[0].id,
                    customer_name=customers[0].name, customer_email=customers[0].email,
                    total=18999, status=models.OrderStatus.delivered,
                    created_at=datetime.utcnow() - timedelta(days=1),
                    items=[models.OrderItem(product_id=products[0].id, product_name=products[0].name, quantity=1, price=18999)],
                ),
                models.Order(
                    order_number="VR-3020", customer_id=customers[1].id,
                    customer_name=customers[1].name, customer_email=customers[1].email,
                    total=12499, status=models.OrderStatus.out_for_delivery,
                    created_at=datetime.utcnow() - timedelta(days=1),
                    items=[models.OrderItem(product_id=products[1].id, product_name=products[1].name, quantity=1, price=12499)],
                ),
                models.Order(
                    order_number="VR-3019", customer_id=customers[2].id,
                    customer_name=customers[2].name, customer_email=customers[2].email,
                    total=15999, status=models.OrderStatus.shipped,
                    created_at=datetime.utcnow() - timedelta(days=2),
                    items=[models.OrderItem(product_id=products[2].id, product_name=products[2].name, quantity=1, price=15999)],
                ),
                models.Order(
                    order_number="VR-3018", customer_id=customers[3].id,
                    customer_name=customers[3].name, customer_email=customers[3].email,
                    total=9499, status=models.OrderStatus.processing,
                    created_at=datetime.utcnow() - timedelta(days=2),
                    items=[models.OrderItem(product_id=products[3].id, product_name=products[3].name, quantity=1, price=9499)],
                ),
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
        print("Seed complete.")
        print("Admin login -> email: admin@verahair.co  password: ChangeMe123!")
    finally:
        db.close()


if __name__ == "__main__":
    run()
