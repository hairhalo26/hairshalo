from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
from pydantic import BaseModel, EmailStr, ConfigDict, Field


# ---------- Auth ----------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    full_name: str
    role: str


# ---------- Categories ----------
class CategoryBase(BaseModel):
    name: str
    description: str = ""
    # Presentation for the storefront's collection cards, so that section is
    # database-driven instead of hardcoded markup.
    tagline: Optional[str] = None
    image_url: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True
    # Set to make this a subcategory of another category; None is top level.
    parent_id: Optional[str] = None


class CategoryCreate(CategoryBase):
    slug: Optional[str] = None


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    # Changing a slug changes public filter URLs, so it only happens when asked
    # for explicitly; renaming a category keeps its slug stable.
    slug: Optional[str] = None
    description: Optional[str] = None
    tagline: Optional[str] = None
    image_url: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None
    parent_id: Optional[str] = None


class CategoryOut(CategoryBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    # Published products in this category — for a top-level category, including
    # those filed under its subcategories (its Hair Types).
    product_count: int = 0


class ReorderRequest(BaseModel):
    """Ids in their new display order; sort_order becomes each one's position."""
    ids: List[str] = Field(..., min_length=1, max_length=500)


# ---------- Colours (hair catalogue) ----------
class ColourBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    hex: Optional[str] = None                # "#RRGGBB" swatch, optional
    sort_order: int = 0
    is_active: bool = True


class ColourCreate(ColourBase):
    slug: Optional[str] = None


class ColourUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    slug: Optional[str] = None
    hex: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class ColourOut(ColourBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    # Published products with an available variant in this colour (within the
    # category/type asked about, when the request names one).
    product_count: int = 0
    # Variants of ANY status using it — why a colour can be archived but not deleted.
    variant_count: int = 0


# ---------- Pricing (shared input block) ----------
class PricingIn(BaseModel):
    """What an admin submits. The backend derives price/compare_at_price.

    Note there is no `selling_price` input — a client cannot state the price
    it wants to charge; it can only state the original price and a discount.
    """
    original_price: Optional[Decimal] = Field(None, ge=0)
    discount_type: str = "none"              # none | percentage | fixed_amount
    discount_value: Decimal = Field(0, ge=0)


class PricingOut(BaseModel):
    """Fully resolved pricing, all values computed server-side."""
    original_price: Optional[Decimal] = None   # = compare_at_price, or price if undiscounted
    price: Optional[Decimal] = None            # actual selling price
    compare_at_price: Optional[Decimal] = None # NULL when not discounted
    discount_type: str = "none"
    discount_value: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    discount_percent: int = 0
    on_sale: bool = False


# ---------- Product media ----------
class ProductMediaBase(BaseModel):
    url: str
    media_type: str = "image"      # image | video
    alt_text: str = ""
    sort_order: int = 0
    is_primary: bool = False


class ProductMediaCreate(ProductMediaBase):
    # Optional: attach this file to one variant (a colour). The router checks
    # the variant belongs to the same product before accepting it.
    variant_id: Optional[str] = None


class ProductMediaUpdate(BaseModel):
    """Re-file an existing image: which variant it shows, and its alt text.

    Every field is optional and unset means "leave it alone" — `variant_id`
    needs to distinguish "not mentioned" from "set to product-level", so
    clearing it is done by sending the empty string.
    """
    variant_id: Optional[str] = None
    alt_text: Optional[str] = None


class ProductMediaReorder(BaseModel):
    """[{id, sort_order}] plus an optional new primary."""
    order: List[dict] = []
    primary_id: Optional[str] = None


class ProductMediaOut(ProductMediaBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    content_type: Optional[str] = None
    file_size: Optional[int] = None
    # Which variant this file shows, or None for product-level media. Output
    # only: assigning media to a variant has to check that the variant actually
    # belongs to the product, so the write path is a deliberate separate change
    # rather than a field the client can simply set.
    variant_id: Optional[str] = None


# ---------- Product variants ----------
class ProductVariantBase(BaseModel):
    sku: str
    length: Optional[str] = None
    density: Optional[str] = None
    color: Optional[str] = None
    # A managed colour (Back Office -> Hair Catalogue -> Colours). When given,
    # the server sets `color` to that colour's name.
    colour_id: Optional[str] = None
    lace_type: Optional[str] = None
    cap_size: Optional[str] = None
    stock: int = Field(0, ge=0)
    is_available: bool = True
    sort_order: int = 0
    # Alert and flag "low stock" at or below this. None = the shop default.
    low_stock_threshold: Optional[int] = Field(None, ge=0, le=10000)


class ProductVariantCreate(ProductVariantBase):
    # Optional variant-level pricing; omitted means "inherit the product price"
    original_price: Optional[Decimal] = Field(None, ge=0)
    discount_type: str = "none"
    discount_value: Decimal = Field(0, ge=0)


class ProductVariantUpdate(BaseModel):
    sku: Optional[str] = None
    length: Optional[str] = None
    density: Optional[str] = None
    color: Optional[str] = None
    colour_id: Optional[str] = None       # "" unlinks the managed colour
    lace_type: Optional[str] = None
    cap_size: Optional[str] = None
    original_price: Optional[Decimal] = Field(None, ge=0)
    discount_type: Optional[str] = None
    discount_value: Optional[Decimal] = Field(None, ge=0)
    clear_price_override: bool = False   # revert to the product-level price
    stock: Optional[int] = Field(None, ge=0)
    is_available: Optional[bool] = None
    sort_order: Optional[int] = None
    low_stock_threshold: Optional[int] = Field(None, ge=0, le=10000)


class ProductVariantOut(ProductVariantBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    label: str
    price: Optional[Decimal] = None
    compare_at_price: Optional[Decimal] = None
    discount_type: str = "none"
    discount_value: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    discount_percent: int = 0
    on_sale: bool = False
    in_stock: bool = True
    # True when this variant has its OWN price; False when it inherits the
    # product's. The editor needs the difference: pre-filling an inherited
    # price and saving it back would freeze a copy of today's price onto the
    # variant, and later product price changes would stop reaching it.
    has_price_override: bool = False
    colour_slug: Optional[str] = None
    colour_hex: Optional[str] = None


# ---------- Product readiness (admin publishing aid) ----------
class ProductReadiness(BaseModel):
    is_ready_to_publish: bool
    issues: List[str] = []
    missing_description: bool = False
    missing_price: bool = False
    missing_image: bool = False
    missing_category: bool = False
    missing_variants: bool = False


# ---------- Product ----------
class ProductBase(BaseModel):
    name: str
    short_description: str = ""
    description: str = ""
    brand: Optional[str] = None
    hair_type: Optional[str] = None
    texture: Optional[str] = None
    construction: Optional[str] = None
    product_type: Optional[str] = None
    weight: Optional[str] = None
    heat_resistance: Optional[str] = None
    features: Optional[str] = None           # one point per line
    benefits: Optional[str] = None           # one point per line
    care_instructions: Optional[str] = None
    badge: Optional[str] = None          # admin-chosen only, never derived
    featured: bool = False
    bestseller: bool = False
    new_arrival: bool = False
    sort_order: int = 0


class ProductCreate(ProductBase):
    slug: Optional[str] = None
    category_id: Optional[str] = None
    # Hair Type: a subcategory of `category_id`. The server checks it belongs
    # to that category and is active, then files the product under it.
    subcategory_id: Optional[str] = None
    status: str = "Draft"
    # Pricing is expressed as original + discount; the server derives the rest.
    original_price: Optional[Decimal] = Field(None, ge=0)
    discount_type: str = "none"
    discount_value: Decimal = Field(0, ge=0)
    media: List[ProductMediaCreate] = []
    variants: List[ProductVariantCreate] = []


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    product_type: Optional[str] = None
    weight: Optional[str] = None
    heat_resistance: Optional[str] = None
    features: Optional[str] = None
    benefits: Optional[str] = None
    care_instructions: Optional[str] = None
    category_id: Optional[str] = None
    subcategory_id: Optional[str] = None     # Hair Type; see ProductCreate
    short_description: Optional[str] = None
    description: Optional[str] = None
    brand: Optional[str] = None
    hair_type: Optional[str] = None
    texture: Optional[str] = None
    construction: Optional[str] = None
    original_price: Optional[Decimal] = Field(None, ge=0)
    discount_type: Optional[str] = None
    discount_value: Optional[Decimal] = Field(None, ge=0)
    badge: Optional[str] = None
    featured: Optional[bool] = None
    bestseller: Optional[bool] = None
    new_arrival: Optional[bool] = None
    sort_order: Optional[int] = None
    status: Optional[str] = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    status: str
    category: Optional[str] = None       # category name, for the existing UI
    category_id: Optional[str] = None
    category_slug: Optional[str] = None
    # The hair hierarchy, derived from category_id: the Hair Category (top
    # level) and, when the product is filed under one, its Hair Type.
    main_category_id: Optional[str] = None
    main_category: Optional[str] = None
    main_category_slug: Optional[str] = None
    subcategory_id: Optional[str] = None
    subcategory: Optional[str] = None
    subcategory_slug: Optional[str] = None
    # Managed colours this product's variants come in, in Back Office order.
    colours: List[ColourOut] = []
    image_url: Optional[str] = None      # primary image, derived from media
    video_url: Optional[str] = None      # first video, if any
    rating: float
    review_count: int
    is_demo: bool = False
    is_purchasable: bool = False
    total_stock: int = 0
    in_stock: bool = False

    # Resolved pricing — every field computed server-side
    price: Optional[Decimal] = None
    compare_at_price: Optional[Decimal] = None
    original_price: Optional[Decimal] = None
    discount_type: str = "none"
    discount_value: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    discount_percent: int = 0
    on_sale: bool = False
    price_min: Optional[Decimal] = None
    price_max: Optional[Decimal] = None

    media: List[ProductMediaOut] = []
    variants: List[ProductVariantOut] = []
    readiness: Optional[ProductReadiness] = None
    published_at: Optional[datetime] = None
    created_at: datetime


class ProductListOut(BaseModel):
    """Paginated envelope for the admin/storefront listing."""
    items: List[ProductOut]
    total: int
    limit: int
    offset: int


class ProductStatusChange(BaseModel):
    """Explicit workflow transition, rather than a free-text status write."""
    action: str          # submit_for_review | publish | unpublish | archive | restore
    force: bool = False  # publish despite readiness warnings (admin override)


# ---------- Product placeholders (separate domain — never sellable) ----------
class ProductPlaceholderBase(BaseModel):
    name: str
    category: str
    short_description: str = ""
    placeholder_image: Optional[str] = None
    placeholder_label: str = "Coming Soon"
    display_price: Optional[str] = None   # display text only, never a real price
    badge: Optional[str] = None
    sort_order: int = 0
    is_visible: bool = True


class ProductPlaceholderCreate(ProductPlaceholderBase):
    pass


class ProductPlaceholderUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    short_description: Optional[str] = None
    placeholder_image: Optional[str] = None
    placeholder_label: Optional[str] = None
    display_price: Optional[str] = None
    badge: Optional[str] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None


class ProductPlaceholderOut(ProductPlaceholderBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Constant discriminator so frontend code can never confuse the two domains.
    is_placeholder: bool = True


class PlaceholderConvertRequest(BaseModel):
    """Fields needed to promote a placeholder into a real, sellable product."""
    price: float
    compare_at_price: Optional[float] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    status: str = "Draft"
    delete_placeholder: bool = False


# ---------- Inventory ----------
class InventoryItemBase(BaseModel):
    product_id: str
    variant_id: Optional[str] = None
    sku: str
    variant: str
    warehouse: str = "Main Warehouse"
    units: int = 0
    low_stock_threshold: int = 15


class InventoryItemCreate(InventoryItemBase):
    pass


class InventoryAdjust(BaseModel):
    units_delta: int  # positive to add stock, negative to remove


# ---------- Inventory (variant-driven, with audit movements) ----------
class InventoryRowOut(BaseModel):
    variant_id: str
    product_id: str
    product_name: str
    product_status: str
    sku: str
    variant_label: str
    warehouse: Optional[str] = None
    stock: int
    is_available: bool
    stock_level: str          # ok | low | crit
    low_stock_threshold: int


class InventoryMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    delta: int
    stock_after: int
    reason: str
    note: Optional[str] = None
    actor: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    created_at: datetime


class InventoryAdjustRequest(BaseModel):
    variant_id: str
    delta: int                # signed: +5 restock, -2 damaged
    reason: str               # restock | adjustment | damaged | correction | cancellation | refund
    note: Optional[str] = None


class InventoryAdjustResult(BaseModel):
    row: InventoryRowOut
    movement: InventoryMovementOut


# ---------- Payments ----------
class PaymentConfigOut(BaseModel):
    provider: str            # none | manual | razorpay
    enabled: bool
    holds_order: bool        # true when the order waits at Pending Payment
    public_key: Optional[str] = None   # publishable key only, never a secret


class PaymentIntentRequest(BaseModel):
    """Only the order id. The amount is read from the database."""
    order_id: str


class PaymentIntentOut(BaseModel):
    payment_id: str
    provider: str
    provider_order_id: str
    amount: Decimal
    currency: str
    public_key: Optional[str] = None
    instructions: Optional[str] = None
    extra: dict = {}


class PaymentConfirmRequest(BaseModel):
    """Ids the GATEWAY issued, plus its signature. No status field exists —
    a client cannot declare a payment successful."""
    payment_id: str
    gateway_response: dict = {}


class ManualSettleRequest(BaseModel):
    reference: Optional[str] = None    # bank reference / receipt number
    note: Optional[str] = None


class RefundRequest(BaseModel):
    amount: Optional[Decimal] = Field(None, ge=0)   # omit for a full refund
    reason: Optional[str] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_id: str
    provider: str
    provider_order_id: Optional[str] = None
    provider_payment_id: Optional[str] = None
    status: str
    amount: Decimal
    currency: str
    amount_refunded: Decimal = Decimal("0.00")
    method: Optional[str] = None
    reference: Optional[str] = None
    note: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime


# ---------- Currency (display only; INR is canonical) ----------
class CurrencyOut(BaseModel):
    code: str
    symbol: str
    name: str
    decimals: int


class RatesOut(BaseModel):
    base: str
    rates: dict
    source: str               # http | static
    age_seconds: int
    is_indicative: bool       # true when falling back to the built-in table
    as_of: Optional[str] = None
    cache_ttl: int


class CurrencyDetectOut(BaseModel):
    country: Optional[str] = None
    currency: Optional[str] = None
    source: str
    fallback: str


class ConvertOut(BaseModel):
    amount_inr: float
    currency: str
    rate: float
    converted: float
    formatted: str
    source: str


# ---------- Coupons (validation against a real basket) ----------
class CouponPreviewRequest(BaseModel):
    code: str
    subtotal: Decimal = Field(0, ge=0)


class CouponPreviewResponse(BaseModel):
    valid: bool
    message: str
    code: Optional[str] = None
    discount_amount: Decimal = Decimal("0.00")
    shipping_discount: Decimal = Decimal("0.00")
    shipping_fee: Decimal = Decimal("0.00")
    new_total: Optional[Decimal] = None
    # Subtotal at which shipping is waived, so the basket can show how far a
    # customer is from free delivery without inventing the threshold itself.
    free_shipping_threshold: Decimal = Decimal("0.00")


class InventoryItemOut(InventoryItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    stock_level: str
    product_name: Optional[str] = None


# ---------- Customer ----------
class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    loyalty_points: int
    created_at: datetime
    order_count: int = 0
    total_spent: float = 0.0


# ---------- Orders ----------
class OrderItemIn(BaseModel):
    """Note: no price field. Prices are always resolved server-side from the
    database, so a client cannot influence what is charged."""
    product_id: str
    variant_id: Optional[str] = None
    quantity: int = 1


class ShippingAddressIn(BaseModel):
    """The delivery address for one order.

    Pydantic only checks SHAPE here (types, lengths). The real validation —
    is this a plausible name, is this phone dialable, is this a valid PIN code
    for India — lives in `app/addresses.py`, because it depends on the country
    and is shared with the saved-address endpoints. Doing it in two places
    would mean two answers.
    """
    full_name: str
    phone: Optional[str] = None
    line1: str
    line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "India"
    # Offer to keep this in the customer's address book. Only honoured for a
    # signed-in customer; a guest has no address book to save into.
    save_to_address_book: bool = False


class ShippingAddressOut(BaseModel):
    """The address an order was actually sent to, as stored on the order."""
    full_name: Optional[str] = None
    phone: Optional[str] = None
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    # What the postal field is called where this order is going, so the admin
    # panel can label it "PIN Code" for India and "ZIP Code" for the US
    # without shipping a copy of the country table to the browser.
    postal_label: str = "Postal Code"
    # False for orders placed before the structured snapshot existed; those
    # carry only `shipping_address` text.
    structured: bool = False


class CountryOut(BaseModel):
    """A shipping destination, and what its postal field is called."""
    name: str
    code: Optional[str] = None
    postal_label: str
    postal_example: str = ""
    postal_required: bool = True
    requires_state: bool = False


class OrderCreate(BaseModel):
    customer_name: str
    customer_email: EmailStr
    # Exactly one of these supplies the delivery address:
    #   `shipping`            - typed at checkout
    #   `shipping_address_id` - chosen from the signed-in customer's address book
    # Ownership of a saved address is checked against the TOKEN, never against
    # anything in this payload.
    shipping: Optional[ShippingAddressIn] = None
    shipping_address_id: Optional[str] = None
    # Legacy free-text address. Retained so the field is not silently dropped
    # from old clients, but it is no longer sufficient on its own: an order
    # needs a structured address to be shippable. See the order router.
    shipping_address: str = ""
    items: List[OrderItemIn]
    coupon_code: Optional[str] = None
    # The currency the customer was BROWSING in. Recorded for the receipt only.
    # Any rate or converted total sent by the client is ignored — the server
    # looks the rate up itself, and always charges in INR.
    display_currency: Optional[str] = None
    # How many loyalty points to spend. Note there is no "loyalty_discount"
    # field: the client says how many points, never what they are worth. The
    # value, the balance and the ceiling all come from the server.
    redeem_loyalty_points: Optional[int] = Field(None, ge=0)
    # One per checkout ATTEMPT, generated by the browser. Sending the same key
    # again returns the order it already created — a double-click, a network
    # retry or a refresh cannot place (and reserve stock for) a second order.
    idempotency_key: Optional[str] = Field(None, max_length=80)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    product_name: str
    variant_label: Optional[str] = None
    variant_sku: Optional[str] = None
    quantity: int
    price: Decimal                            # unit price captured at purchase
    compare_at_price: Optional[Decimal] = None  # original price at purchase
    image_url: Optional[str] = None           # live lookup; None once the product is gone


class OrderTimelineStep(BaseModel):
    status: str
    note: Optional[str] = None
    at: Optional[datetime] = None             # None = time not recorded (pre-0013 order)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_number: str
    customer_name: str
    customer_email: EmailStr
    shipping_address: str = ""
    shipping: Optional[ShippingAddressOut] = None
    subtotal: Optional[Decimal] = None
    discount_total: Optional[Decimal] = None
    shipping_fee: Optional[Decimal] = None
    coupon_code: Optional[str] = None
    loyalty_points_redeemed: int = 0
    loyalty_discount: Optional[Decimal] = None
    total: Decimal
    currency: str = "INR"
    payment_status: Optional[str] = None
    is_paid: bool = False
    display_currency: Optional[str] = None
    display_rate: Optional[Decimal] = None
    display_total: Optional[Decimal] = None
    status: str
    created_at: datetime
    items: List[OrderItemOut] = []
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    tracking_url: Optional[str] = None
    timeline: List[OrderTimelineStep] = []


class OrderAdminOut(OrderOut):
    """What staff see. Adds the fields a customer must never receive."""
    customer_id: Optional[str] = None
    internal_notes: Optional[str] = None
    placed_signed_in: bool = False
    customer_phone: Optional[str] = None
    payment_id: Optional[str] = None           # latest attempt, for mark-paid / refund
    payment_provider: Optional[str] = None
    payment_reference: Optional[str] = None


class OrderStatusUpdate(BaseModel):
    status: str
    # Shown to the customer on their order timeline, e.g. "Handed to Delhivery".
    note: Optional[str] = Field(None, max_length=300)
    # Optional on any transition; usually sent with "Shipped".
    tracking_number: Optional[str] = Field(None, max_length=120)
    carrier: Optional[str] = Field(None, max_length=120)
    tracking_url: Optional[str] = Field(None, max_length=500)


class OrderFulfilmentUpdate(BaseModel):
    """Edit tracking details or staff notes without changing the status."""
    tracking_number: Optional[str] = Field(None, max_length=120)
    carrier: Optional[str] = Field(None, max_length=120)
    tracking_url: Optional[str] = Field(None, max_length=500)
    internal_notes: Optional[str] = Field(None, max_length=5000)


# ---------- Appointments ----------
class AppointmentCreate(BaseModel):
    customer_name: str
    customer_email: EmailStr
    customer_phone: Optional[str] = None
    appointment_type: str
    stylist: str = "Any available stylist"
    scheduled_at: datetime
    notes: str = ""


class AppointmentOut(AppointmentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: str
    created_at: datetime


class AppointmentStatusUpdate(BaseModel):
    status: str


# ---------- Coupons ----------
class CouponCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    description: str = ""
    discount_type: str = "percent"            # percent | flat | free_shipping
    discount_value: float = Field(0, ge=0)
    active: bool = True
    min_order_amount: Optional[Decimal] = Field(None, ge=0)
    max_discount_amount: Optional[Decimal] = Field(None, gt=0)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    usage_limit: Optional[int] = Field(None, ge=1)
    per_customer_limit: Optional[int] = Field(None, ge=1)


class CouponUpdate(BaseModel):
    """Partial update. Send a field as null to remove that rule."""
    description: Optional[str] = None
    discount_type: Optional[str] = None
    discount_value: Optional[float] = Field(None, ge=0)
    active: Optional[bool] = None
    min_order_amount: Optional[Decimal] = Field(None, ge=0)
    max_discount_amount: Optional[Decimal] = Field(None, gt=0)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    usage_limit: Optional[int] = Field(None, ge=1)
    per_customer_limit: Optional[int] = Field(None, ge=1)


class CouponOut(CouponCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    usage_count: int
    code: str


class CouponValidateRequest(BaseModel):
    code: str


class CouponValidateResponse(BaseModel):
    valid: bool
    discount_type: Optional[str] = None
    discount_value: Optional[float] = None
    message: str


# ---------- Analytics ----------
class AnalyticsSummary(BaseModel):
    revenue_this_month: float
    orders_this_month: int
    active_customers: int
    # Optional because it is genuinely unknown without traffic tracking. The
    # dashboard renders "—" rather than inventing a plausible-looking rate.
    conversion_rate: Optional[float] = None
    avg_order_value: float


class LocationShare(BaseModel):
    """Where orders ship to — computed from the delivery snapshots, not typed in."""
    city: str
    orders: int
    share: float


class TopProduct(BaseModel):
    name: str
    units_sold: int


# ---------- Notifications ----------
class NotificationOut(BaseModel):
    """List view. Bodies are deliberately omitted — a queue listing should not
    ship every rendered email to the browser."""
    model_config = ConfigDict(from_attributes=True)
    id: str
    event_key: str
    event_type: str
    channel: str
    category: str
    status: str
    recipient: str
    recipient_name: Optional[str] = None
    subject: str
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    attempts: int
    max_attempts: int
    attempts_remaining: int
    next_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    sent_at: Optional[datetime] = None
    # NULL until an admin acknowledges it in the panel. Distinct from `status`,
    # which is about the mail server rather than about a person.
    read_at: Optional[datetime] = None
    is_read: bool = False
    created_at: Optional[datetime] = None


class NotificationDetailOut(NotificationOut):
    """Single message, including exactly what was sent."""
    body_text: str
    body_html: Optional[str] = None


class NotificationConfigOut(BaseModel):
    channel: str
    sends_real_mail: bool
    dispatch_mode: str
    mail_from: str
    mail_from_name: str
    reply_to: Optional[str] = None
    admin_alert_emails: List[str] = []
    max_attempts: int
    batch_size: int
    low_stock_threshold: int
    queued: int
    failed: int
    suppressed_addresses: int
    warnings: List[str] = []


class NotificationDispatchResult(BaseModel):
    channel: str
    attempted: int
    sent: int
    failed: int
    retrying: int
    suppressed: int


class NotificationTestRequest(BaseModel):
    to: EmailStr


class SuppressionCreate(BaseModel):
    email: EmailStr
    scope: str = "marketing"          # marketing | all
    reason: Optional[str] = None
    note: Optional[str] = None


class SuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    scope: str
    reason: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class UnsubscribeResult(BaseModel):
    email: str
    unsubscribed: bool
    message: str


# ---------- Reviews ----------
class ReviewCreate(BaseModel):
    """A review submission.

    `order_number` + `email` are how a purchase is verified. There is no
    `is_verified_purchase` field — a client cannot claim that badge, the
    server derives it from the order.
    """
    product_id: str
    order_number: str
    email: EmailStr
    rating: int = Field(..., ge=1, le=5)
    title: Optional[str] = None
    body: str = ""
    author_name: Optional[str] = None


class ReviewReplyOut(BaseModel):
    body: str
    replied_at: datetime


class ReviewOut(BaseModel):
    """Public shape. Carries a display name, never the reviewer's address."""
    id: str
    product_id: str
    author: str
    rating: int
    title: Optional[str] = None
    body: str = ""
    is_verified_purchase: bool
    created_at: Optional[datetime] = None
    reply: Optional[ReviewReplyOut] = None


class ReviewAdminOut(ReviewOut):
    """Moderation view — adds who wrote it and what happened to it."""
    author_name: str
    author_email: str
    status: str
    order_id: Optional[str] = None
    product_name: Optional[str] = None
    moderated_by: Optional[str] = None
    moderated_at: Optional[datetime] = None
    moderation_note: Optional[str] = None


class ReviewModerate(BaseModel):
    status: str                       # Published | Rejected | Pending
    note: Optional[str] = None


class ReviewReplyIn(BaseModel):
    body: str


class ReviewSummaryOut(BaseModel):
    product_id: str
    average: float
    count: int
    verified_count: int
    breakdown: dict                   # {1: n, 2: n, ... 5: n}


class ReviewableItemOut(BaseModel):
    product_id: str
    product_name: str
    variant_label: Optional[str] = None
    order_item_id: str


# ---------- Loyalty ----------
class LoyaltyTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    customer_id: str
    delta: int
    balance_after: int
    reason: str
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    note: Optional[str] = None
    actor: Optional[str] = None
    created_at: Optional[datetime] = None


class LoyaltyBalanceOut(BaseModel):
    customer_id: str
    email: str
    balance: int
    value: Decimal                    # what the balance is worth in INR
    point_value: Decimal
    earn_per: Decimal
    max_redeem_pct: int


class LoyaltyAdjustRequest(BaseModel):
    delta: int
    note: str


class LoyaltyProgrammeOut(BaseModel):
    """Public terms of the programme. No balances — those need a customer
    login this application does not have yet."""
    earn_per: Decimal
    point_value: Decimal
    max_redeem_pct: int
    example: str


# ---------- Marketing ----------
class SubscribeRequest(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    source: Optional[str] = None


class SubscribeResult(BaseModel):
    status: str
    message: str


class SubscriberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    name: Optional[str] = None
    status: str
    source: Optional[str] = None
    requested_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    unsubscribed_at: Optional[datetime] = None
    last_campaign_at: Optional[datetime] = None


class CampaignCreate(BaseModel):
    name: str
    subject: str
    body: str
    preheader: Optional[str] = None
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    subject: str
    preheader: Optional[str] = None
    body: str
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None
    status: str
    recipient_count: int = 0
    skipped_count: int = 0
    sent_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None


class CampaignSendResult(BaseModel):
    campaign: CampaignOut
    queued: int
    skipped: int
    audience: int



# ---------- Customer accounts ----------
class RegisterRequest(BaseModel):
    """Note there is no `email_verified`, `loyalty_points` or `is_active` field:
    a registration cannot assert anything about itself beyond credentials."""
    email: EmailStr
    password: str
    name: str
    phone: Optional[str] = None


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str


class CustomerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    email_verified: bool
    loyalty_points: int = 0
    preferred_currency: Optional[str] = None
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class CustomerToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    customer: CustomerProfileOut


class AccountMessage(BaseModel):
    message: str


class TokenRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    preferred_currency: Optional[str] = None


class AddressIn(BaseModel):
    label: Optional[str] = None
    full_name: str
    phone: Optional[str] = None
    line1: str
    line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "India"
    is_default: bool = False


class AddressOut(AddressIn):
    model_config = ConfigDict(from_attributes=True)
    id: str


class WishlistAdd(BaseModel):
    product_id: str
    variant_id: Optional[str] = None


class WishlistItemOut(BaseModel):
    id: str
    product_id: str
    variant_id: Optional[str] = None
    product_name: str
    variant_label: Optional[str] = None
    price: Optional[Decimal] = None
    image_url: Optional[str] = None
    in_stock: bool = False
    added_at: Optional[datetime] = None


class LoyaltyEntryOut(BaseModel):
    delta: int
    balance_after: int
    reason: str
    note: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    created_at: Optional[datetime] = None


class MyLoyaltyOut(BaseModel):
    balance: int
    value: Decimal
    point_value: Decimal
    earn_per: Decimal
    max_redeem_pct: int
    earned_total: int
    redeemed_total: int
    history: List[LoyaltyEntryOut] = []

class PaymentStatusOut(BaseModel):
    """Narrow status view for the storefront. No PII, no line items."""
    payment_id: str
    payment_status: str
    order_id: str
    order_number: str
    order_status: Optional[str] = None
    is_paid: bool = False
    amount: Optional[Decimal] = None
    currency: str = "INR"
    error_message: Optional[str] = None


# ---------- Site content (editable storefront copy) ----------
class SiteContentOut(BaseModel):
    """One editable block of storefront copy."""
    key: str
    label: str
    description: str = ""
    payload: dict = Field(default_factory=dict)
    is_active: bool = True
    sort_order: int = 0
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class SiteContentUpdate(BaseModel):
    """A partial update. Fields omitted from `payload` keep their current value.

    There is no `key` field: the key comes from the URL, so a request cannot
    rename a block out from under the storefront that reads it.
    """
    payload: Optional[dict] = None
    is_active: Optional[bool] = None


# ---------- Admin notification centre ----------
class NotificationSummaryOut(BaseModel):
    """Counts behind the admin panel's bell.

    Every number here is a COUNT(*) against the notifications table. Nothing is
    kept in browser state, so a refresh cannot invent or lose an unread badge,
    and two admin sessions see the same figure.
    """
    unread: int = 0
    unread_orders: int = 0
    queued: int = 0
    failed: int = 0
    total: int = 0


class NotificationMarkRead(BaseModel):
    """Which messages to acknowledge. Empty `ids` with `all=true` clears the lot."""
    ids: List[str] = Field(default_factory=list)
    all: bool = False
    # Restrict a bulk mark-read to one event type, so "clear new-order alerts"
    # does not also silence a failed-payment alert nobody has looked at.
    event_type: Optional[str] = None


# ---------- Supplier import (generic, admin-only) ----------
class SupplierIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    code: Optional[str] = Field(None, max_length=12)
    reference: Optional[str] = Field(None, max_length=120)
    currency: str = Field("INR", min_length=3, max_length=3)
    # Hosts image downloads may come from, e.g. ["cdn.supplier.example"].
    allowed_image_hosts: List[str] = []
    notes: Optional[str] = Field(None, max_length=2000)


class SupplierUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    reference: Optional[str] = Field(None, max_length=120)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    allowed_image_hosts: Optional[List[str]] = None
    notes: Optional[str] = Field(None, max_length=2000)


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: str
    reference: Optional[str] = None
    currency: str
    allowed_image_hosts: List[str] = []
    field_mapping: dict = {}
    category_mapping: dict = {}
    pricing: dict = {}
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    product_count: int = 0
    suggested_exchange_rate: Optional[Decimal] = None


class ImportPricingIn(BaseModel):
    exchange_rate: Optional[Decimal] = Field(None, gt=0)
    markup_percent: Decimal = Field(0, ge=0, le=1000)
    markup_fixed: Decimal = Field(0, ge=0)
    rounding: str = "nearest_1"


class ImportPreviewRequest(BaseModel):
    mapping: Dict[str, str]
    category_mapping: Dict[str, Optional[str]] = {}
    pricing: ImportPricingIn = ImportPricingIn()


class ImportSelection(BaseModel):
    key: str
    action: str = "import"                 # import | update | new | skip
    price_override: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[str] = None


class ImportCommitRequest(BaseModel):
    selections: List[ImportSelection]
    # The admin confirms Hairshalo may use the images in THIS file. Without it
    # no URL is ever fetched (uploaded files are the admin's own act).
    images_authorized: bool = False
    # Opening stock for NEW variants from the supplier quantity column.
    stock_from_supplier: bool = False
    # Recalculate the selling price of products being UPDATED.
    update_prices: bool = False


class ImportSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    supplier_id: str
    supplier_name: Optional[str] = None
    file_name: str
    file_format: str
    status: str
    total_rows: int
    valid_rows: int
    imported: int
    updated: int
    skipped: int
    duplicates: int
    errors: int
    failed_images: int
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    committed_at: Optional[datetime] = None


class ImportDetailOut(ImportSummaryOut):
    columns: List[str] = []
    sample_rows: List[dict] = []
    suggested_mapping: Dict[str, str] = {}
    settings: dict = {}
    preview: dict = {}
    results: List[dict] = []
    image_files: List[str] = []
