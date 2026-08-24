from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, ConfigDict


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


# ---------- Product ----------
class ProductBase(BaseModel):
    name: str
    category: str
    description: str = ""
    price: float
    compare_at_price: Optional[float] = None
    image_url: Optional[str] = None
    status: str = "Active"


class ProductCreate(ProductBase):
    slug: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    compare_at_price: Optional[float] = None
    image_url: Optional[str] = None
    status: Optional[str] = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    rating: float
    review_count: int
    created_at: datetime


# ---------- Inventory ----------
class InventoryItemBase(BaseModel):
    product_id: str
    sku: str
    variant: str
    warehouse: str = "Main Warehouse"
    units: int = 0
    low_stock_threshold: int = 15


class InventoryItemCreate(InventoryItemBase):
    pass


class InventoryAdjust(BaseModel):
    units_delta: int  # positive to add stock, negative to remove


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
    product_id: str
    quantity: int = 1


class OrderCreate(BaseModel):
    customer_name: str
    customer_email: EmailStr
    shipping_address: str = ""
    items: List[OrderItemIn]
    coupon_code: Optional[str] = None


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_name: str
    quantity: int
    price: float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_number: str
    customer_name: str
    customer_email: EmailStr
    total: float
    status: str
    created_at: datetime
    items: List[OrderItemOut] = []


class OrderStatusUpdate(BaseModel):
    status: str


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
    code: str
    description: str = ""
    discount_type: str = "percent"
    discount_value: float = 0
    active: bool = True


class CouponOut(CouponCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    usage_count: int


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
    conversion_rate: float
    avg_order_value: float


class TopProduct(BaseModel):
    name: str
    units_sold: int
