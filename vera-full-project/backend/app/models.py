import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Text, Enum
)
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class OrderStatus(str, enum.Enum):
    processing = "Processing"
    shipped = "Shipped"
    out_for_delivery = "Out for Delivery"
    delivered = "Delivered"
    cancelled = "Cancelled"


class ProductStatus(str, enum.Enum):
    active = "Active"
    draft = "Draft"
    archived = "Archived"


class AppointmentStatus(str, enum.Enum):
    pending = "Pending"
    confirmed = "Confirmed"
    completed = "Completed"
    cancelled = "Cancelled"


class DiscountType(str, enum.Enum):
    percent = "percent"
    flat = "flat"
    free_shipping = "free_shipping"


class User(Base):
    """Admin / staff account used to log into the admin dashboard."""
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, default="admin")  # admin | staff
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    loyalty_points = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    orders = relationship("Order", back_populates="customer")


class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    category = Column(String, nullable=False)  # Frontal Wigs | Closure Wigs | Glueless Wigs | Toppers | Extensions
    description = Column(Text, default="")
    price = Column(Float, nullable=False)
    compare_at_price = Column(Float, nullable=True)
    image_url = Column(String, nullable=True)
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)
    status = Column(Enum(ProductStatus), default=ProductStatus.active)
    created_at = Column(DateTime, default=datetime.utcnow)

    inventory = relationship("InventoryItem", back_populates="product", cascade="all, delete-orphan")
    order_items = relationship("OrderItem", back_populates="product")


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id = Column(String, primary_key=True, default=gen_uuid)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    sku = Column(String, unique=True, nullable=False)
    variant = Column(String, nullable=False)  # e.g. "Natural Black · 18\""
    warehouse = Column(String, default="Main Warehouse")
    units = Column(Integer, default=0)
    low_stock_threshold = Column(Integer, default=15)

    product = relationship("Product", back_populates="inventory")

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
    shipping_address = Column(Text, default="")
    total = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.processing)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(String, primary_key=True, default=gen_uuid)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    product_id = Column(String, ForeignKey("products.id"), nullable=True)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    price = Column(Float, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(String, primary_key=True, default=gen_uuid)
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    customer_phone = Column(String, nullable=True)
    appointment_type = Column(String, nullable=False)  # Wig Fitting | Hair Consultation | Styling Session
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
    discount_value = Column(Float, default=0)
    usage_count = Column(Integer, default=0)
    active = Column(Boolean, default=True)
