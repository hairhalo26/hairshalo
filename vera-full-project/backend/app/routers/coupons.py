from datetime import timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_optional_customer
from app import models, schemas
from app import coupons as coupon_service
from app.pricing import money

router = APIRouter(prefix="/api/coupons", tags=["coupons"])


@router.post("/validate", response_model=schemas.CouponValidateResponse)
def validate_coupon(payload: schemas.CouponValidateRequest, db: Session = Depends(get_db)):
    """Existence check only — kept for backwards compatibility.

    It deliberately does NOT claim a coupon is "applied", because whether it can
    reduce a total depends on the basket. Use /preview for a real answer.
    """
    coupon = coupon_service.find(db, payload.code)
    if not coupon or not coupon.active:
        return schemas.CouponValidateResponse(valid=False, message="Coupon not found or inactive")
    if coupon.is_expired:
        return schemas.CouponValidateResponse(valid=False, message=f"{coupon.code} has expired")
    if coupon.is_exhausted:
        return schemas.CouponValidateResponse(valid=False, message=f"{coupon.code} has reached its usage limit")
    return schemas.CouponValidateResponse(
        valid=True,
        discount_type=coupon.discount_type,
        discount_value=coupon.discount_value,
        message=f"{coupon.code} is a valid code",
    )


@router.post("/preview", response_model=schemas.CouponPreviewResponse)
def preview_coupon(payload: schemas.CouponPreviewRequest, db: Session = Depends(get_db),
                   customer=Depends(get_optional_customer)):
    """Evaluate a coupon against a real basket subtotal.

    This is what the checkout UI calls. A coupon that cannot actually reduce
    the total comes back `valid: false` with the reason — never a false success.
    The figures returned are informational; checkout re-evaluates server-side.
    """
    subtotal = money(payload.subtotal or 0)
    shipping = coupon_service.shipping_fee_for(subtotal)
    try:
        coupon, goods_off, ship_off, message = coupon_service.evaluate(
            db, payload.code, subtotal, shipping,
            customer_id=customer.id if customer else None,
        )
    except coupon_service.CouponError as exc:
        return schemas.CouponPreviewResponse(
            valid=False, message=str(exc), shipping_fee=shipping,
            new_total=money(subtotal + shipping),
            free_shipping_threshold=coupon_service.free_shipping_threshold(),
        )
    return schemas.CouponPreviewResponse(
        valid=True, message=message, code=coupon.code,
        discount_amount=goods_off, shipping_discount=ship_off,
        shipping_fee=money(shipping - ship_off),
        new_total=money(subtotal - goods_off + shipping - ship_off),
        free_shipping_threshold=coupon_service.free_shipping_threshold(),
    )


@router.get("/quote", response_model=schemas.CouponPreviewResponse)
def shipping_quote(subtotal: float = 0, db: Session = Depends(get_db)):
    """Shipping cost for a basket, with no coupon applied."""
    sub = money(subtotal or 0)
    ship = coupon_service.shipping_fee_for(sub)
    return schemas.CouponPreviewResponse(
        valid=True,
        message="Free delivery" if ship == 0 else "Standard delivery",
        shipping_fee=ship, new_total=money(sub + ship),
        free_shipping_threshold=coupon_service.free_shipping_threshold(),
    )


@router.get("", response_model=List[schemas.CouponOut])
def list_coupons(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    return db.query(models.Coupon).order_by(models.Coupon.code.asc()).all()


def _check_rules(values: dict) -> dict:
    """Refuse a coupon that could never work, before it reaches a customer."""
    dtype = values.get("discount_type")
    if dtype is not None:
        try:
            values["discount_type"] = models.DiscountType(dtype)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="discount_type must be one of: percent, flat, free_shipping.")
    kind = values.get("discount_type")
    value = values.get("discount_value")
    if kind == models.DiscountType.percent and value is not None and not (0 < value <= 100):
        raise HTTPException(status_code=400, detail="A percentage discount must be between 0 and 100.")
    if kind == models.DiscountType.flat and value is not None and value <= 0:
        raise HTTPException(status_code=400, detail="A flat discount must be more than zero.")
    starts, ends = values.get("starts_at"), values.get("expires_at")
    if starts and ends and ends <= starts:
        raise HTTPException(status_code=400, detail="The expiry date must be after the start date.")
    for key in ("starts_at", "expires_at"):
        # Stored naive-UTC like every other timestamp in this schema.
        if values.get(key) is not None and values[key].tzinfo is not None:
            values[key] = values[key].astimezone(timezone.utc).replace(tzinfo=None)
    return values


@router.post("", response_model=schemas.CouponOut, status_code=201)
def create_coupon(
    payload: schemas.CouponCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    if db.query(models.Coupon).filter(models.Coupon.code == payload.code.upper()).first():
        raise HTTPException(status_code=400, detail="Coupon code already exists")
    values = _check_rules(payload.model_dump())
    values["code"] = payload.code.strip().upper()
    coupon = models.Coupon(**values)
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


@router.put("/{coupon_id}", response_model=schemas.CouponOut)
def update_coupon(
    coupon_id: str,
    payload: schemas.CouponUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Edit a coupon's rules. The code itself is fixed: orders refer to it."""
    coupon = db.query(models.Coupon).filter(models.Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    values = payload.model_dump(exclude_unset=True)
    merged = {
        "discount_type": values.get("discount_type", coupon.discount_type.value
                                    if coupon.discount_type else None),
        "discount_value": values.get("discount_value", float(coupon.discount_value or 0)),
        "starts_at": values.get("starts_at", coupon.starts_at),
        "expires_at": values.get("expires_at", coupon.expires_at),
    }
    merged = _check_rules(merged)
    for key in ("discount_type", "starts_at", "expires_at"):
        if key in values:
            values[key] = merged[key]
    for field, value in values.items():
        setattr(coupon, field, value)
    db.commit()
    db.refresh(coupon)
    return coupon


@router.delete("/{coupon_id}", status_code=204)
def delete_coupon(
    coupon_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    coupon = db.query(models.Coupon).filter(models.Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    db.delete(coupon)
    db.commit()
    return None
