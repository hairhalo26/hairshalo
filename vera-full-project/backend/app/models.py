import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Text, Enum,
    Index, JSON, Numeric, UniqueConstraint
)
from sqlalchemy.orm import relationship

from app.database import Base, PLACEHOLDER_SCHEMA
from app.pricing import discount_amount_for, discount_percent_for, is_on_sale

# All monetary columns use this type. Float is never used for currency.
Money = Numeric(12, 2)


def gen_uuid():
    return str(uuid.uuid4())


class OrderStatus(str, enum.Enum):
    pending_payment = "Pending Payment"
    paid = "Paid"
    processing = "Processing"
    shipped = "Shipped"
    out_for_delivery = "Out for Delivery"
    delivered = "Delivered"
    cancelled = "Cancelled"
    refunded = "Refunded"


class PaymentStatus(str, enum.Enum):
    """Gateway-side lifecycle, deliberately separate from the order's own."""
    pending = "Pending"        # intent created, customer has not paid yet
    authorized = "Authorized"  # funds held, not captured
    paid = "Paid"              # captured and confirmed by the gateway
    failed = "Failed"
    cancelled = "Cancelled"
    refunded = "Refunded"


class ProductStatus(str, enum.Enum):
    """Publishing workflow: Draft -> Review -> Published -> Archived.

    `out_of_stock` is a published-but-unbuyable state, kept separate from
    `archived` (retired) and `draft` (never released).
    """
    draft = "Draft"
    review = "Review"
    published = "Published"
    out_of_stock = "Out of Stock"
    archived = "Archived"


class ProductBadge(str, enum.Enum):
    """Explicitly chosen by an admin — never derived from pricing."""
    new = "New"
    bestseller = "Bestseller"
    featured = "Featured"
    limited = "Limited"
    sale = "Sale"


class DiscountKind(str, enum.Enum):
    none = "none"
    percentage = "percentage"
    fixed_amount = "fixed_amount"


class MovementReason(str, enum.Enum):
    """Why stock changed. Every change to ProductVariant.stock writes one."""
    initial = "initial"           # variant created with opening stock
    order = "order"               # sold
    cancellation = "cancellation"  # order cancelled, stock returned
    refund = "refund"             # refunded, stock returned
    restock = "restock"           # new supply received
    adjustment = "adjustment"     # manual correction by an admin
    damaged = "damaged"           # written off
    correction = "correction"     # reconciling a counting error


class MediaType(str, enum.Enum):
    image = "image"
    video = "video"


class AppointmentStatus(str, enum.Enum):
    pending = "Pending"
    confirmed = "Confirmed"
    completed = "Completed"
    cancelled = "Cancelled"


class DiscountType(str, enum.Enum):
    percent = "percent"
    flat = "flat"
    free_shipping = "free_shipping"


class NotificationStatus(str, enum.Enum):
    """Lifecycle of one queued message."""
    queued = "Queued"        # written by the business transaction, not yet sent
    sent = "Sent"            # the channel accepted it
    failed = "Failed"        # permanently failed, or out of retries (dead letter)
    suppressed = "Suppressed"  # deliberately not sent (opt-out, bounce, channel off)
    cancelled = "Cancelled"  # abandoned by an admin


class NotificationCategory(str, enum.Enum):
    """What kind of message this is — decides whether opt-out applies.

    Transactional messages (you bought something, it shipped) are a record of
    a transaction the customer entered into, so a marketing unsubscribe does
    not silence them; only a hard bounce does.
    """
    transactional = "transactional"   # to the customer, about their own order
    operational = "operational"       # to staff — new order, payment failed, low stock
    marketing = "marketing"           # promotional; always honours opt-out


class NotificationChannel(str, enum.Enum):
    email = "email"
    sms = "sms"            # reserved: no provider implemented yet


class SuppressionScope(str, enum.Enum):
    marketing = "marketing"   # unsubscribed from promotions only
    all = "all"               # hard bounce / complaint — send nothing at all


class ReviewStatus(str, enum.Enum):
    """Reviews are moderated before they count towards a rating."""
    pending = "Pending"
    published = "Published"
    rejected = "Rejected"


class LoyaltyReason(str, enum.Enum):
    """Why a balance changed. Every change to Customer.loyalty_points writes one."""
    earned = "earned"            # an order was paid for
    redeemed = "redeemed"        # spent at checkout
    reversed = "reversed"        # order cancelled/refunded: earnings clawed back
    returned = "returned"        # order cancelled/refunded: redeemed points given back
    adjustment = "adjustment"    # manual correction by an admin
    expired = "expired"          # aged out


class SubscriberStatus(str, enum.Enum):
    """Double opt-in: nobody is mailed marketing until they confirm."""
    pending = "Pending"          # asked to join, has not confirmed the email yet
    confirmed = "Confirmed"      # confirmed — the only status a campaign may reach
    unsubscribed = "Unsubscribed"


class CampaignStatus(str, enum.Enum):
    draft = "Draft"
    sent = "Sent"
    cancelled = "Cancelled"


class User(Base):
    """Admin / staff account used to log into the admin dashboard."""
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, default="admin")  # admin | staff
    created_at = Column(DateTime, default=datetime.utcnow)
    # Token version (migration 0015). Every staff token carries the version it
    # was issued under; signing out increments it, so tokens issued before are
    # refused at once instead of staying valid until they expire.
    token_version = Column(Integer, default=0, nullable=False)


class Customer(Base):
    """A shopper. Rows are created two ways, and the difference matters:

    * **By checkout**, keyed on the email typed into the order form. Such a row
      has no password and nobody has proved they own that mailbox.
    * **By registration**, which sets a password and sends a verification link.

    Because the first kind exists, registering with an address is NOT enough to
    see that address's order history — `email_verified` gates it. Otherwise
    anyone could register with a stranger's email and read their orders.
    """
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    loyalty_points = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # --- Account credentials (NULL for checkout-created customers) ---------
    hashed_password = Column(String, nullable=True)
    email_verified = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # Bumped on password change and "log out everywhere". Tokens carry the
    # version they were minted with, so old tokens stop working immediately —
    # without this, a stolen JWT survives a password reset until it expires.
    token_version = Column(Integer, default=0, nullable=False)
    # Single-use, hashed. Storing the raw token would let anyone with read
    # access to the database take over every account waiting on a reset.
    password_reset_hash = Column(String, nullable=True)
    password_reset_expires_at = Column(DateTime, nullable=True)
    password_changed_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, nullable=True)
    # Saved display-currency preference (priority 2 in app/currency.py).
    preferred_currency = Column(String, nullable=True)

    @property
    def has_account(self):
        return bool(self.hashed_password)

    @property
    def can_see_order_history(self):
        """Owning the mailbox is what entitles someone to the history behind it."""
        return self.has_account and self.email_verified and self.is_active

    orders = relationship("Order", back_populates="customer")
    loyalty_transactions = relationship(
        "LoyaltyTransaction", back_populates="customer",
        cascade="all, delete-orphan",
        order_by="LoyaltyTransaction.created_at.desc()",
    )
    addresses = relationship(
        "CustomerAddress", back_populates="customer",
        cascade="all, delete-orphan",
        order_by="CustomerAddress.created_at.desc()",
    )
    wishlist = relationship(
        "WishlistItem", back_populates="customer",
        cascade="all, delete-orphan",
        order_by="WishlistItem.created_at.desc()",
    )


class Category(Base):
    """Database-driven product categories (previously a free-text column)."""
    __tablename__ = "categories"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    description = Column(Text, default="")
    # Presentation, so the storefront's collection cards are database-driven
    # rather than hardcoded markup with external image URLs.
    tagline = Column(String, nullable=True)     # "Best for beginners"
    image_url = Column(String, nullable=True)   # uploaded through /api/site-content/media
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # A subcategory names its parent; NULL is top level (migration 0013).
    # This is also the hair hierarchy: a top-level category is a Hair Category
    # (Synthetic Hair, Human Hair) and its subcategories are its Hair Types.
    # Indexed in migration 0016, since the shop now filters on it.
    parent_id = Column(String, ForeignKey("categories.id", ondelete="SET NULL"),
                       nullable=True, index=True)

    products = relationship("Product", back_populates="category_ref")
    parent = relationship("Category", remote_side=[id])


class Colour(Base):
    """A reusable colour (Black, Honey Blonde, 1B …), managed in the Back Office.

    Colours are global — Synthetic Hair and Human Hair draw from the same list —
    but a colour only appears on a product through a variant that uses it, so
    the list itself never implies what any product is available in.
    Migration 0016.
    """
    __tablename__ = "colours"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    hex = Column(String, nullable=True)          # "#2B1B14" swatch, optional
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    variants = relationship("ProductVariant", back_populates="colour")


class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    category_id = Column(String, ForeignKey("categories.id"), nullable=True, index=True)
    short_description = Column(String, default="")   # card / listing blurb
    description = Column(Text, default="")           # full description
    brand = Column(String, nullable=True)

    # Canonical pricing (see app/pricing.py):
    #   price            = actual selling price (what orders charge)
    #   compare_at_price = original price, NULL when not discounted
    #   discount_*       = how the selling price was derived
    price = Column(Money, nullable=True)
    compare_at_price = Column(Money, nullable=True)
    discount_type = Column(Enum(DiscountKind), default=DiscountKind.none, nullable=False)
    discount_value = Column(Money, default=0, nullable=False)

    # Product characteristics
    hair_type = Column(String, nullable=True)     # Human Hair | Synthetic | Blend
    texture = Column(String, nullable=True)       # Straight | Wavy | Curly | Body Wave ...
    construction = Column(String, nullable=True)  # Lace Front | Full Lace | Closure ...
    # Descriptive fields supplier catalogues carry (migration 0014). Customer-
    # facing, admin-editable; features/benefits are one point per line.
    product_type = Column(String, nullable=True)      # Wig | Clip-in Extensions | Ponytail ...
    weight = Column(String, nullable=True)            # "120g", kept as the supplier wrote it
    heat_resistance = Column(String, nullable=True)   # "Up to 180°C"
    features = Column(Text, nullable=True)
    benefits = Column(Text, nullable=True)
    care_instructions = Column(Text, nullable=True)

    # DERIVED, never set by hand: app/reviews.py:recalculate() is the only thing
    # that writes these, and only from published reviews. They are stored rather
    # than computed per request so a 200-product listing stays one query — the
    # same trade-off as ProductVariant.stock, with the same rule attached.
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)

    status = Column(Enum(ProductStatus), default=ProductStatus.draft, index=True)
    badge = Column(Enum(ProductBadge), nullable=True)   # admin-chosen, never derived
    featured = Column(Boolean, default=False, index=True)
    bestseller = Column(Boolean, default=False, index=True)
    new_arrival = Column(Boolean, default=False, index=True)
    sort_order = Column(Integer, default=0, index=True)

    # Marks rows created by the development seed so demo data is always
    # distinguishable from production data.
    is_demo = Column(Boolean, default=False, index=True)

    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category_ref = relationship("Category", back_populates="products")
    media = relationship(
        "ProductMedia", back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductMedia.sort_order",
    )
    variants = relationship(
        "ProductVariant", back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductVariant.sort_order",
    )
    inventory = relationship("InventoryItem", back_populates="product", cascade="all, delete-orphan")
    order_items = relationship("OrderItem", back_populates="product")
    reviews = relationship(
        "Review", back_populates="product", cascade="all, delete-orphan",
        order_by="Review.created_at.desc()",
    )

    # ---- Convenience properties (keep the API shape stable for the frontend) ----
    @property
    def category(self):
        return self.category_ref.name if self.category_ref else None

    # The hair hierarchy, derived from the one category_id: a product filed
    # under a Hair Type (a subcategory) belongs to that type's parent Hair
    # Category. Derived rather than stored, so the two can never disagree.
    @property
    def main_category_ref(self):
        c = self.category_ref
        if c is None:
            return None
        return c.parent if c.parent_id and c.parent is not None else c

    @property
    def subcategory_ref(self):
        c = self.category_ref
        return c if c is not None and c.parent_id else None

    @property
    def primary_image_url(self):
        images = [m for m in self.media if m.media_type == MediaType.image]
        if not images:
            return None
        primary = next((m for m in images if m.is_primary), images[0])
        return primary.url

    @property
    def image_url(self):
        """Back-compat alias — media is now the source of truth."""
        return self.primary_image_url

    @property
    def is_purchasable(self):
        return self.status == ProductStatus.published

    # ---- Derived pricing (never stored, so it can never drift) ----
    @property
    def discount_amount(self):
        return discount_amount_for(self.price, self.compare_at_price)

    @property
    def discount_percent(self):
        return discount_percent_for(self.price, self.compare_at_price)

    @property
    def on_sale(self):
        """True only when stored pricing genuinely supports a sale claim."""
        return is_on_sale(self.price, self.compare_at_price)

    @property
    def price_range(self):
        """(min, max) selling price across available variants, else base price."""
        prices = [
            v.price if v.price is not None else self.price
            for v in self.variants if v.is_available
        ]
        prices = [p for p in prices if p is not None]
        if not prices:
            return (self.price, self.price)
        return (min(prices), max(prices))

    @property
    def total_stock(self):
        if self.variants:
            return sum(v.stock or 0 for v in self.variants if v.is_available)
        return sum(i.units or 0 for i in self.inventory)

    @property
    def readiness_issues(self):
        """Which required fields are still missing before this can be published."""
        issues = []
        if not (self.description or "").strip():
            issues.append("missing_description")
        if self.price is None and not any(v.price is not None for v in self.variants):
            issues.append("missing_price")
        if not self.primary_image_url:
            issues.append("missing_image")
        if not self.category_id:
            issues.append("missing_category")
        if not self.variants:
            issues.append("missing_variants")
        return issues

    @property
    def is_ready_to_publish(self):
        return len(self.readiness_issues) == 0


class ProductMedia(Base):
    """Multiple images/videos per product, replacing the single image_url."""
    __tablename__ = "product_media"

    id = Column(String, primary_key=True, default=gen_uuid)
    product_id = Column(String, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    # Optional: media that belongs to ONE variant (a colour, typically), so a
    # customer choosing 1B sees 1B's photos rather than the product's whole
    # gallery. NULL means product-level media, which is the existing behaviour
    # and stays the default — nothing that worked before needs this set.
    #
    # SET NULL, not CASCADE: retiring a colour must not silently delete the
    # photographs of it. The media falls back to the product gallery instead.
    variant_id = Column(String, ForeignKey("product_variants.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    url = Column(String, nullable=False)
    media_type = Column(Enum(MediaType), default=MediaType.image, nullable=False)
    alt_text = Column(String, default="")
    sort_order = Column(Integer, default=0)
    is_primary = Column(Boolean, default=False)
    # Set for uploaded files; NULL for externally hosted URLs. Deleting the row
    # deletes the stored object only when a storage_key is present.
    storage_key = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="media")
    variant = relationship("ProductVariant", back_populates="media")


class ProductVariant(Base):
    """A concrete, sellable configuration of a product."""
    __tablename__ = "product_variants"

    id = Column(String, primary_key=True, default=gen_uuid)
    product_id = Column(String, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    sku = Column(String, unique=True, nullable=False, index=True)
    length = Column(String, nullable=True)      # e.g. 18"
    density = Column(String, nullable=True)     # e.g. 150%
    color = Column(String, nullable=True)       # e.g. Natural Black
    # The managed colour this variant is in (migration 0016). When set, `color`
    # above is kept equal to the colour's name, so labels, order snapshots and
    # anything else that reads the text keep working. NULL for variants whose
    # colour is still free text (every variant that predates 0016).
    colour_id = Column(String, ForeignKey("colours.id", ondelete="RESTRICT"),
                       nullable=True, index=True)
    lace_type = Column(String, nullable=True)   # e.g. HD Lace
    cap_size = Column(String, nullable=True)    # e.g. Medium
    # Variant pricing overrides the product's when `price` is set. Same
    # canonical relationship as Product: price = selling, compare_at = original.
    price = Column(Money, nullable=True)
    compare_at_price = Column(Money, nullable=True)
    discount_type = Column(Enum(DiscountKind), default=DiscountKind.none, nullable=False)
    discount_value = Column(Money, default=0, nullable=False)
    stock = Column(Integer, default=0, nullable=False)
    is_available = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    # NULL = use the shop default (LOW_STOCK_ALERT_THRESHOLD). Migration 0013.
    low_stock_threshold = Column(Integer, nullable=True)

    @property
    def effective_low_stock_threshold(self) -> int:
        from app.config import settings      # local: models must not import config at load
        if self.low_stock_threshold is not None:
            return int(self.low_stock_threshold)
        return int(settings.LOW_STOCK_ALERT_THRESHOLD)

    product = relationship("Product", back_populates="variants")
    colour = relationship("Colour", back_populates="variants")
    # NB: InventoryItem.variant is the legacy free-text label column, so the
    # reverse side is the `variant_ref` relationship, not `variant`.
    inventory = relationship("InventoryItem", back_populates="variant_ref")
    # Photographs of THIS variant. No cascade delete: see ProductMedia.variant_id.
    media = relationship("ProductMedia", back_populates="variant")
    movements = relationship(
        "InventoryMovement", back_populates="variant",
        cascade="all, delete-orphan",
        order_by="InventoryMovement.created_at.desc()",
    )

    @property
    def label(self):
        parts = [p for p in [self.color, self.length, self.density, self.lace_type, self.cap_size] if p]
        return " · ".join(parts) if parts else self.sku

    def effective_price(self, product):
        """Authoritative unit price for this variant. Orders use only this."""
        return self.price if self.price is not None else product.price

    def effective_compare_at(self, product):
        if self.price is not None:
            return self.compare_at_price
        return product.compare_at_price

    @property
    def discount_amount(self):
        return discount_amount_for(self.price, self.compare_at_price)

    @property
    def discount_percent(self):
        return discount_percent_for(self.price, self.compare_at_price)

    @property
    def on_sale(self):
        return is_on_sale(self.price, self.compare_at_price)


Index("ix_product_variants_product_available", ProductVariant.product_id, ProductVariant.is_available)


class InventoryMovement(Base):
    """Append-only audit trail for stock.

    ProductVariant.stock is the single authoritative current quantity; this
    table explains how it got there. Every mutation goes through
    app.inventory.apply_movement(), which holds a row lock on the variant, so
    the running total and the movement log can never disagree.
    """
    __tablename__ = "inventory_movements"

    id = Column(String, primary_key=True, default=gen_uuid)
    variant_id = Column(String, ForeignKey("product_variants.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    delta = Column(Integer, nullable=False)          # signed: -2 sold, +5 restocked
    stock_after = Column(Integer, nullable=False)    # running total after this movement
    reason = Column(Enum(MovementReason), nullable=False, index=True)
    reference_type = Column(String, nullable=True)   # e.g. "order"
    reference_id = Column(String, nullable=True)     # e.g. the order id
    note = Column(String, nullable=True)
    actor = Column(String, nullable=True)            # admin email, or NULL for system
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    variant = relationship("ProductVariant", back_populates="movements")


class InventoryItem(Base):
    """Per-warehouse breakdown. NOT the source of truth for sellable stock —
    ProductVariant.stock is. Kept for warehouse/location reporting."""
    __tablename__ = "inventory_items"

    id = Column(String, primary_key=True, default=gen_uuid)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    variant_id = Column(String, ForeignKey("product_variants.id"), nullable=True, index=True)
    sku = Column(String, unique=True, nullable=False)
    variant = Column(String, nullable=False)  # e.g. "Natural Black · 18\""
    warehouse = Column(String, default="Main Warehouse")
    units = Column(Integer, default=0)
    low_stock_threshold = Column(Integer, default=15)

    product = relationship("Product", back_populates="inventory")
    variant_ref = relationship("ProductVariant", back_populates="inventory")

    @property
    def stock_level(self):
        if self.units <= 5:
            return "crit"
        if self.units <= self.low_stock_threshold:
            return "low"
        return "ok"


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=gen_uuid)
    order_number = Column(String, unique=True, nullable=False)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    # --- Shipping snapshot ---------------------------------------------
    # `shipping_address` is the original free-text column and is still
    # written, so historical orders and anything reading it keep working.
    # The structured columns below are what a label is printed from.
    #
    # These are a COPY of the address used at checkout, never a reference to
    # `customer_addresses`. A customer who later edits or deletes that saved
    # address must not change where a past order was sent — which is exactly
    # what a foreign key here would do.
    shipping_address = Column(Text, default="")
    shipping_name = Column(String, nullable=True)
    shipping_phone = Column(String, nullable=True)
    shipping_line1 = Column(String, nullable=True)
    shipping_line2 = Column(String, nullable=True)
    shipping_city = Column(String, nullable=True)
    shipping_state = Column(String, nullable=True)
    shipping_postal_code = Column(String, nullable=True)
    shipping_country = Column(String, nullable=True)
    subtotal = Column(Money, nullable=True)        # goods, before coupon
    discount_total = Column(Money, nullable=True)  # coupon discount applied
    shipping_fee = Column(Money, nullable=True)    # charged shipping (0 when waived)
    coupon_code = Column(String, nullable=True)    # snapshot of what was redeemed
    # Loyalty points spent on this order, and what they were worth. Kept apart
    # from `discount_total` so a coupon discount and a points discount are never
    # confused for one another in reporting — or refunded twice.
    loyalty_points_redeemed = Column(Integer, default=0, nullable=False)
    loyalty_discount = Column(Money, default=0, nullable=False)
    total = Column(Money, nullable=False)          # subtotal - discounts + shipping

    # --- Currency snapshot ---------------------------------------------
    # The order is charged in `currency` (always INR today). If the customer
    # was BROWSING in another currency we record which one and the rate used,
    # so a receipt can be reproduced exactly. Historical orders are never
    # re-converted at today's rate.
    currency = Column(String, default="INR", nullable=False)   # settlement currency
    display_currency = Column(String, nullable=True)           # what the customer saw
    display_rate = Column(Numeric(18, 8), nullable=True)       # units per 1 INR at purchase
    display_total = Column(Numeric(18, 4), nullable=True)      # total in display currency
    status = Column(Enum(OrderStatus), default=OrderStatus.processing)
    created_at = Column(DateTime, default=datetime.utcnow)

    # --- Fulfilment (migration 0013) ----------------------------------
    tracking_number = Column(String, nullable=True)
    carrier = Column(String, nullable=True)
    tracking_url = Column(String, nullable=True)
    # Staff-only. Deliberately absent from OrderOut, the customer schema.
    internal_notes = Column(Text, nullable=True)
    # One checkout attempt's own id. A retried or double-clicked submit
    # carrying the same key gets the existing order back, not a second one.
    idempotency_key = Column(String, nullable=True, unique=True, index=True)
    # Placed by a signed-in account. An account whose email is not yet
    # verified may still see the orders it placed itself.
    placed_signed_in = Column(Boolean, default=False, nullable=False)

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    events = relationship(
        "OrderEvent", back_populates="order", cascade="all, delete-orphan",
        order_by="OrderEvent.created_at.asc()",
    )
    payments = relationship(
        "Payment", back_populates="order", cascade="all, delete-orphan",
        order_by="Payment.created_at.desc()",
    )

    @property
    def shipping(self):
        """The delivery snapshot, shaped for the API.

        `postal_label` travels with it so the admin panel can print "PIN Code"
        for an Indian order and "ZIP Code" for an American one without shipping
        a copy of the country table to the browser — and without guessing from
        the customer's current profile, which may have changed since.
        """
        from app import addresses            # local: avoids a circular import
        return {
            "full_name": self.shipping_name,
            "phone": self.shipping_phone,
            "line1": self.shipping_line1,
            "line2": self.shipping_line2,
            "city": self.shipping_city,
            "state": self.shipping_state,
            "postal_code": self.shipping_postal_code,
            "country": self.shipping_country,
            "postal_label": addresses.postal_label_for(self.shipping_country),
            "structured": self.has_structured_shipping,
        }

    @property
    def has_structured_shipping(self):
        """True when this order carries the per-field snapshot.

        Orders placed before migration 0012 have only the text blob. They are
        not back-filled by parsing it: a mis-parsed address is worse than an
        honestly unstructured one, because it looks authoritative.
        """
        return bool(self.shipping_line1 and self.shipping_city)

    @property
    def payment(self):
        """Most recent payment attempt, or None."""
        return self.payments[0] if self.payments else None

    @property
    def is_paid(self):
        return any(p.status == PaymentStatus.paid for p in self.payments)

    @property
    def payment_status(self):
        """Gateway status of the latest attempt, for display alongside the order."""
        p = self.payment
        return p.status.value if p and p.status else None

    @property
    def timeline(self):
        """Status history, oldest first.

        Orders placed before `order_events` existed have no rows. For those a
        minimal honest timeline is built — placed, then where it is now — with
        the second step undated, rather than inventing when it happened.
        """
        if self.events:
            return [{"status": e.status, "note": e.note, "at": e.created_at}
                    for e in self.events]
        steps = [{"status": "Placed", "note": None, "at": self.created_at}]
        current = self.status.value if self.status else None
        if current:
            steps.append({"status": current, "note": None, "at": None})
        return steps


class OrderEvent(Base):
    """One step in an order's life: placed, paid, shipped, cancelled..."""
    __tablename__ = "order_events"

    id = Column(String, primary_key=True, default=gen_uuid)
    order_id = Column(String, ForeignKey("orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    status = Column(String, nullable=False)
    note = Column(String, nullable=True)       # customer-visible, e.g. "Shipped via Delhivery"
    actor = Column(String, nullable=True)      # staff email, or "customer" / "payment-gateway"
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order", back_populates="events")


class Payment(Base):
    """Gateway transaction record, kept separate from order business data.

    NO card data is ever stored — only the gateway's own identifiers and the
    status it reported. `provider_payment_id` is unique, which is what makes
    webhook handling idempotent: a replayed event finds the existing row.
    """
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=gen_uuid)
    order_id = Column(String, ForeignKey("orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    provider = Column(String, nullable=False)                 # razorpay | manual
    provider_order_id = Column(String, nullable=True, index=True)   # gateway's order/intent id
    provider_payment_id = Column(String, unique=True, nullable=True, index=True)
    provider_refund_id = Column(String, nullable=True)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.pending,
                    nullable=False, index=True)
    amount = Column(Money, nullable=False)          # charged amount, in `currency`
    currency = Column(String, default="INR", nullable=False)
    amount_refunded = Column(Money, default=0)
    error_code = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    method = Column(String, nullable=True)          # card | upi | netbanking (label only)
    reference = Column(String, nullable=True)       # human reference, e.g. a bank ref
    note = Column(String, nullable=True)            # who confirmed it, and why
    # Last webhook/event id we acted on — second line of idempotency defence.
    last_event_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = relationship("Order", back_populates="payments")


class OrderItem(Base):
    """Line item. Descriptive fields are SNAPSHOTS taken at purchase time so
    later edits to a product (price, name, variant) never rewrite history."""
    __tablename__ = "order_items"

    id = Column(String, primary_key=True, default=gen_uuid)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    product_id = Column(String, ForeignKey("products.id"), nullable=True)
    variant_id = Column(String, ForeignKey("product_variants.id"), nullable=True)
    product_name = Column(String, nullable=False)   # snapshot
    variant_label = Column(String, nullable=True)   # snapshot
    variant_sku = Column(String, nullable=True)     # snapshot
    quantity = Column(Integer, default=1)
    price = Column(Money, nullable=False)           # snapshot of unit price paid
    compare_at_price = Column(Money, nullable=True) # snapshot of original price

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")

    @property
    def image_url(self):
        """A picture of what was bought, for order history.

        The variant's own photograph when it has one (the colour the customer
        chose), else the product's primary image. Looked up live rather than
        snapshotted: a replaced photo should update, and a deleted product
        simply yields None — the order line itself is a snapshot and survives.
        """
        product = self.product
        if product is None:
            return None
        if self.variant_id:
            own = [m for m in product.media
                   if m.variant_id == self.variant_id and m.media_type == MediaType.image]
            if own:
                return sorted(own, key=lambda m: (not m.is_primary, m.sort_order or 0))[0].url
        return product.primary_image_url


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    customer_phone = Column(String, nullable=True)
    appointment_type = Column(String, nullable=False)
    stylist = Column(String, default="Any available stylist")
    scheduled_at = Column(DateTime, nullable=False)
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.pending)
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(String, primary_key=True, default=gen_uuid)
    code = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, default="")
    discount_type = Column(Enum(DiscountType), default=DiscountType.percent)
    discount_value = Column(Money, default=0)
    usage_count = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    # Redemption rules — all enforced server-side in app/coupons.py
    min_order_amount = Column(Money, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    usage_limit = Column(Integer, nullable=True)     # NULL = unlimited
    # Migration 0013. NULL means "no such rule" for each.
    max_discount_amount = Column(Money, nullable=True)   # cap on a % discount
    starts_at = Column(DateTime, nullable=True)
    per_customer_limit = Column(Integer, nullable=True)

    @property
    def not_started(self):
        return self.starts_at is not None and self.starts_at > datetime.utcnow()

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at < datetime.utcnow()

    @property
    def is_exhausted(self):
        return self.usage_limit is not None and (self.usage_count or 0) >= self.usage_limit


class ProductPlaceholder(Base):
    """A non-sellable catalog placeholder, shown while real products are prepared.

    This is deliberately NOT related to Product, ProductVariant, InventoryItem,
    Order or OrderItem — no foreign keys point at it and none point out of it.
    It cannot be purchased, cannot hold stock, and never contributes to revenue.

    `display_price` is a free-text string ("From ₹12,000", "Coming soon") rather
    than a numeric column, precisely so it can never be summed into revenue or
    treated as a real price by accident.
    """
    __tablename__ = "product_placeholders"
    __table_args__ = ({"schema": PLACEHOLDER_SCHEMA} if PLACEHOLDER_SCHEMA else {})

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    short_description = Column(Text, default="")
    placeholder_image = Column(String, nullable=True)
    placeholder_label = Column(String, default="Coming Soon")
    display_price = Column(String, nullable=True)
    badge = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    is_visible = Column(Boolean, default=True)
    is_demo = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Notification(Base):
    """A queued outbound message — the transactional outbox.

    Two rules give this table its shape:

    1. A notification is a CONSEQUENCE of a committed fact, never a cause of
       one. The row is written inside the same transaction as the business
       change (the order, the payment), so it cannot describe something that
       was rolled back — and sending happens only afterwards, outside that
       transaction, so a mail server being down can never fail a checkout.

    2. A notification is never sent twice. `event_key` is UNIQUE and derived
       from the event itself ("order.shipped:<order id>"), so a retried
       request, a replayed webhook or a double-clicked admin button all
       collapse onto the same row.

    The rendered subject/body are stored, not re-rendered at send time: the
    email a customer received must stay reproducible even after the product
    was renamed or the template changed.
    """
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=gen_uuid)
    event_key = Column(String, unique=True, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)   # e.g. "order.shipped"
    channel = Column(Enum(NotificationChannel), default=NotificationChannel.email,
                     nullable=False)
    category = Column(Enum(NotificationCategory),
                      default=NotificationCategory.transactional, nullable=False, index=True)
    status = Column(Enum(NotificationStatus), default=NotificationStatus.queued,
                    nullable=False, index=True)

    recipient = Column(String, nullable=False, index=True)
    recipient_name = Column(String, nullable=True)
    subject = Column(String, nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)

    # What this message is about. Deliberately a loose reference rather than a
    # foreign key: notifications outlive the rows they describe (an order can
    # be deleted; the record that we emailed the customer should remain).
    reference_type = Column(String, nullable=True)      # order | appointment | product
    reference_id = Column(String, nullable=True, index=True)

    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_error = Column(String, nullable=True)
    provider = Column(String, nullable=True)            # console | smtp | null
    provider_message_id = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    # When a human in the admin panel acknowledged this. NULL means unread.
    # Deliberately separate from `status`: "the mail server accepted it" and
    # "someone has seen it" are different facts, and overloading one column
    # with both would lose whichever was written second.
    read_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_read(self):
        return self.read_at is not None

    @property
    def is_terminal(self):
        return self.status in (
            NotificationStatus.sent, NotificationStatus.failed,
            NotificationStatus.suppressed, NotificationStatus.cancelled,
        )

    @property
    def attempts_remaining(self):
        return max(0, (self.max_attempts or 0) - (self.attempts or 0))


Index("ix_notifications_due", Notification.status, Notification.next_attempt_at)


class NotificationSuppression(Base):
    """Addresses we must not mail, and how far the ban goes.

    `marketing` comes from someone clicking unsubscribe. `all` comes from a
    hard bounce or a spam complaint — continuing to mail those addresses is
    what destroys a sending domain's reputation, so it outranks even
    transactional mail.
    """
    __tablename__ = "notification_suppressions"

    id = Column(String, primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    scope = Column(Enum(SuppressionScope), default=SuppressionScope.marketing,
                   nullable=False)
    reason = Column(String, nullable=True)      # unsubscribe | hard_bounce | complaint
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Review(Base):
    """A customer review of something they actually bought.

    Two rules give this table its shape:

    1. **Verified by default.** A review is tied to the order line that bought
       the product, so "verified purchase" is a fact about the data rather than
       a badge someone can set. A review with no order behind it can only be
       created by an admin, and is labelled unverified.
    2. **Moderated before it counts.** Only `published` reviews contribute to a
       product rating, so a rating can never be inflated by something nobody
       has read.

    One review per order line — enforced by a unique constraint, not by a check
    that a double-submit can race past.
    """
    __tablename__ = "reviews"

    id = Column(String, primary_key=True, default=gen_uuid)
    product_id = Column(String, ForeignKey("products.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    order_id = Column(String, ForeignKey("orders.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    order_item_id = Column(String, ForeignKey("order_items.id", ondelete="SET NULL"),
                           nullable=True, unique=True)
    customer_id = Column(String, ForeignKey("customers.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    author_name = Column(String, nullable=False)     # display name only
    author_email = Column(String, nullable=False, index=True)

    rating = Column(Integer, nullable=False)         # 1..5, validated in app/reviews.py
    title = Column(String, nullable=True)
    body = Column(Text, default="")

    status = Column(Enum(ReviewStatus), default=ReviewStatus.pending,
                    nullable=False, index=True)
    #: True only when this review is attached to a real, delivered order line.
    is_verified_purchase = Column(Boolean, default=False, nullable=False)
    is_demo = Column(Boolean, default=False, index=True)

    moderated_by = Column(String, nullable=True)     # admin email
    moderated_at = Column(DateTime, nullable=True)
    moderation_note = Column(String, nullable=True)  # why it was rejected
    reply_body = Column(Text, nullable=True)         # the shop's public response
    reply_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="reviews")
    order = relationship("Order")
    customer = relationship("Customer")

    @property
    def author_display(self):
        """First name plus an initial — a review is public, an address is not."""
        parts = (self.author_name or "").strip().split()
        if not parts:
            return "Verified customer"
        if len(parts) == 1:
            return parts[0]
        return f"{parts[0]} {parts[-1][0]}."


Index("ix_reviews_product_status", Review.product_id, Review.status)


class LoyaltyTransaction(Base):
    """Append-only ledger for loyalty points.

    `Customer.loyalty_points` is the authoritative current balance; this table
    explains how it got there. Same contract as InventoryMovement: nothing
    outside app/loyalty.py may assign to the balance, every change is written
    with the customer row locked, so the running total and the ledger cannot
    disagree.

    Points are earned when an order is PAID, not when it is placed — an
    abandoned checkout must not mint currency.
    """
    __tablename__ = "loyalty_transactions"

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_id = Column(String, ForeignKey("customers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    delta = Column(Integer, nullable=False)          # signed: +120 earned, -500 spent
    balance_after = Column(Integer, nullable=False)
    reason = Column(Enum(LoyaltyReason), nullable=False, index=True)
    reference_type = Column(String, nullable=True)   # e.g. "order"
    reference_id = Column(String, nullable=True, index=True)
    note = Column(String, nullable=True)
    actor = Column(String, nullable=True)            # admin email, or NULL for system
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    customer = relationship("Customer", back_populates="loyalty_transactions")


class MarketingSubscriber(Base):
    """A marketing list built on double opt-in.

    `pending` means someone typed an address into a form. `confirmed` means the
    owner of that mailbox clicked the link in the confirmation email — and only
    `confirmed` addresses can be reached by a campaign, which is why there is no
    "email all customers" endpoint anywhere in this application.

    Buying something is not consent to marketing: customers created by checkout
    never appear here unless they opt in.
    """
    __tablename__ = "marketing_subscribers"

    id = Column(String, primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)
    status = Column(Enum(SubscriberStatus), default=SubscriberStatus.pending,
                    nullable=False, index=True)
    source = Column(String, nullable=True)           # storefront_footer | checkout | import
    # The evidence trail that consent existed — what a complaint or an audit
    # actually asks for.
    requested_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    unsubscribed_at = Column(DateTime, nullable=True)
    consent_ip = Column(String, nullable=True)
    last_campaign_at = Column(DateTime, nullable=True)
    is_demo = Column(Boolean, default=False, index=True)

    @property
    def is_mailable(self):
        return self.status == SubscriberStatus.confirmed


class Campaign(Base):
    """One marketing send.

    Recipients are resolved at send time from the confirmed subscriber list,
    and the messages go through the notification outbox, so suppression,
    unsubscribes and idempotency all apply without this table knowing about
    any of them.
    """
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    preheader = Column(String, nullable=True)
    body = Column(Text, nullable=False)              # plain text, rendered into the template
    cta_label = Column(String, nullable=True)
    cta_url = Column(String, nullable=True)
    status = Column(Enum(CampaignStatus), default=CampaignStatus.draft,
                    nullable=False, index=True)
    recipient_count = Column(Integer, default=0)     # how many were queued
    skipped_count = Column(Integer, default=0)       # suppressed, or already queued
    sent_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CustomerAddress(Base):
    """A saved delivery address.

    Addresses are copied onto an order as text at checkout (Order.shipping_address
    is a snapshot), so editing or deleting one here never rewrites where a past
    order was sent.
    """
    __tablename__ = "customer_addresses"

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_id = Column(String, ForeignKey("customers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    label = Column(String, nullable=True)            # Home, Work, Mum's place
    full_name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    line1 = Column(String, nullable=False)
    line2 = Column(String, nullable=True)
    city = Column(String, nullable=False)
    state = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    country = Column(String, default="India", nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="addresses")

    @property
    def as_text(self):
        parts = [self.full_name, self.line1, self.line2,
                 ", ".join(p for p in [self.city, self.state, self.postal_code] if p),
                 self.country]
        return "\n".join(p for p in parts if p)


class WishlistItem(Base):
    """Saved for later. A wishlist is not a cart: it holds no price and no
    reservation, so nothing here can affect stock or what an order charges."""
    __tablename__ = "wishlist_items"
    __table_args__ = (
        UniqueConstraint("customer_id", "product_id", "variant_id",
                         name="uq_wishlist_customer_product_variant"),
    )

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_id = Column(String, ForeignKey("customers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    product_id = Column(String, ForeignKey("products.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    variant_id = Column(String, ForeignKey("product_variants.id", ondelete="CASCADE"),
                        nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="wishlist")
    product = relationship("Product")
    variant = relationship("ProductVariant")


class SiteContent(Base):
    """Editable storefront copy — the blocks a shop owner rewrites, not a developer.

    One row per block, keyed by a stable string the storefront asks for
    ("announcement", "faq", "footer"). The body is JSON rather than a column
    per field, because these blocks have genuinely different shapes: the
    announcement is one line, the FAQ is a list of question/answer pairs, the
    footer is several link columns. A column per field would mean a migration
    every time the marketing copy grows a bullet point.

    What is deliberately NOT here: anything a customer could be misled by if it
    drifted from reality. Prices, stock, ratings and review counts are owned by
    their own services and are never editable as "content".
    """
    __tablename__ = "site_content"

    id = Column(String, primary_key=True, default=gen_uuid)
    key = Column(String, unique=True, nullable=False, index=True)
    label = Column(String, nullable=False)          # shown in the admin list
    description = Column(Text, nullable=True)       # what this block controls
    payload = Column(JSON, nullable=False, default=dict)
    is_active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    updated_by = Column(String, nullable=True)      # admin email, for the audit line
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------- suppliers
# Generic, supplier-agnostic catalogue import (migration 0014). Nothing here is
# customer-facing: supplier identifiers, prices and availability are internal,
# and a supplier price is never the Hairshalo selling price.


class Supplier(Base):
    """An authorised supplier, and what its last import used."""
    __tablename__ = "suppliers"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)
    code = Column(String, nullable=False, unique=True)   # short, used in generated SKUs
    reference = Column(String, nullable=True)            # account / agreement number
    currency = Column(String, nullable=False, default="INR")
    # Image downloads are only ever made from these hosts, and only when an
    # admin confirms the rights for that import.
    allowed_image_hosts = Column(JSON, nullable=True)
    field_mapping = Column(JSON, nullable=True)          # remembered between imports
    category_mapping = Column(JSON, nullable=True)
    pricing = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    imports = relationship("SupplierImport", back_populates="supplier",
                           cascade="all, delete-orphan")


class SupplierImport(Base):
    """One uploaded supplier file: the import log entry."""
    __tablename__ = "supplier_imports"

    id = Column(String, primary_key=True, default=gen_uuid)
    supplier_id = Column(String, ForeignKey("suppliers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    file_name = Column(String, nullable=False)
    file_format = Column(String, nullable=False)       # csv | json | xlsx
    status = Column(String, nullable=False, default="uploaded")  # uploaded|previewed|committed|failed
    columns = Column(JSON, nullable=True)
    raw_rows = Column(JSON, nullable=True)
    settings = Column(JSON, nullable=True)             # mapping, pricing, options used
    preview = Column(JSON, nullable=True)
    results = Column(JSON, nullable=True)
    image_files = Column(JSON, nullable=True)          # {filename: storage_key}
    total_rows = Column(Integer, default=0, nullable=False)
    valid_rows = Column(Integer, default=0, nullable=False)
    imported = Column(Integer, default=0, nullable=False)
    updated = Column(Integer, default=0, nullable=False)
    skipped = Column(Integer, default=0, nullable=False)
    duplicates = Column(Integer, default=0, nullable=False)
    errors = Column(Integer, default=0, nullable=False)
    failed_images = Column(Integer, default=0, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    committed_at = Column(DateTime, nullable=True)

    supplier = relationship("Supplier", back_populates="imports")


class SupplierProduct(Base):
    """A supplier's item, linked to the Hairshalo product it became."""
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "source_product_id",
                                       name="uq_supplier_products_source"),)

    id = Column(String, primary_key=True, default=gen_uuid)
    supplier_id = Column(String, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String, ForeignKey("products.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    source_product_id = Column(String, nullable=False)
    source_sku = Column(String, nullable=True)
    source_url = Column(String, nullable=True)
    source_name = Column(String, nullable=True)
    source_price = Column(Money, nullable=True)
    source_currency = Column(String, nullable=True)
    source_availability = Column(String, nullable=True)
    source_file = Column(String, nullable=True)
    source_data = Column(JSON, nullable=True)
    last_import_id = Column(String, nullable=True)
    first_imported_at = Column(DateTime, default=datetime.utcnow)
    last_synced_at = Column(DateTime, default=datetime.utcnow)

    supplier = relationship("Supplier")
    product = relationship("Product")
    variants = relationship("SupplierVariant", back_populates="supplier_product",
                            cascade="all, delete-orphan")


class SupplierVariant(Base):
    """A supplier's variant, linked to the Hairshalo variant it became."""
    __tablename__ = "supplier_variants"

    id = Column(String, primary_key=True, default=gen_uuid)
    supplier_product_id = Column(String, ForeignKey("supplier_products.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    variant_id = Column(String, ForeignKey("product_variants.id", ondelete="SET NULL"),
                        nullable=True)
    source_variant_id = Column(String, nullable=True)
    source_sku = Column(String, nullable=True, index=True)
    source_price = Column(Money, nullable=True)
    source_availability = Column(String, nullable=True)
    source_quantity = Column(Integer, nullable=True)
    last_synced_at = Column(DateTime, default=datetime.utcnow)

    supplier_product = relationship("SupplierProduct", back_populates="variants")
    variant = relationship("ProductVariant")
