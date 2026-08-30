from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
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
def preview_coupon(payload: schemas.CouponPreviewRequest, db: Session = Depends(get_db)):
    """Evaluate a coupon against a real basket subtotal.

    This is what the checkout UI calls. A coupon that cannot actually reduce
    the total comes back `valid: false` with the reason — never a false success.
    The figures returned are informational; checkout re-evaluates server-side.
    """
    subtotal = money(payload.subtotal or 0)
    shipping = coupon_service.shipping_fee_for(subtotal)
    try:
        coupon, goods_off, ship_off, message = coupon_service.evaluate(
            db, payload.code, subtotal, shipping
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
    return db.query(models.Coupon).all()


@router.post("", response_model=schemas.CouponOut, status_code=201)
def create_coupon(
    payload: schemas.CouponCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    if db.query(models.Coupon).filter(models.Coupon.code == payload.code.upper()).first():
        raise HTTPException(status_code=400, detail="Coupon code already exists")
    coupon = models.Coupon(**{**payload.model_dump(), "code": payload.code.upper()})
    db.add(coupon)
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
