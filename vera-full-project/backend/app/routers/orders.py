import random
import string
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/orders", tags=["orders"])


def generate_order_number() -> str:
    return "VR-" + "".join(random.choices(string.digits, k=4))


@router.post("", response_model=schemas.OrderOut, status_code=201)
def create_order(payload: schemas.OrderCreate, db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    # find or create the customer record
    customer = db.query(models.Customer).filter(
        models.Customer.email == payload.customer_email
    ).first()
    if not customer:
        customer = models.Customer(name=payload.customer_name, email=payload.customer_email)
        db.add(customer)
        db.flush()

    order_items = []
    total = 0.0
    for item in payload.items:
        product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        line_total = product.price * item.quantity
        total += line_total
        order_items.append(models.OrderItem(
            product_id=product.id,
            product_name=product.name,
            quantity=item.quantity,
            price=product.price,
        ))

    # optional coupon
    if payload.coupon_code:
        coupon = db.query(models.Coupon).filter(
            models.Coupon.code == payload.coupon_code.upper(),
            models.Coupon.active == True,  # noqa: E712
        ).first()
        if coupon:
            if coupon.discount_type == models.DiscountType.percent:
                total -= total * (coupon.discount_value / 100)
            elif coupon.discount_type == models.DiscountType.flat:
                total -= coupon.discount_value
            coupon.usage_count += 1
            total = max(total, 0)

    order = models.Order(
        order_number=generate_order_number(),
        customer_id=customer.id,
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        shipping_address=payload.shipping_address,
        total=round(total, 2),
        items=order_items,
    )
    db.add(order)
    customer.loyalty_points += int(total // 100)
    db.commit()
    db.refresh(order)
    return order


@router.get("", response_model=List[schemas.OrderOut])
def list_orders(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    q = db.query(models.Order).options(joinedload(models.Order.items))
    if status:
        q = q.filter(models.Order.status == status)
    return q.order_by(models.Order.created_at.desc()).all()


@router.get("/{order_id}", response_model=schemas.OrderOut)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    order = db.query(models.Order).options(joinedload(models.Order.items)).filter(
        models.Order.id == order_id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.put("/{order_id}/status", response_model=schemas.OrderOut)
def update_order_status(
    order_id: str,
    payload: schemas.OrderStatusUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if payload.status not in [s.value for s in models.OrderStatus]:
        raise HTTPException(status_code=400, detail="Invalid status")
    order.status = payload.status
    db.commit()
    db.refresh(order)
    return order
