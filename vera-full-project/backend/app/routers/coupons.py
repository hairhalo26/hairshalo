from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/coupons", tags=["coupons"])


@router.post("/validate", response_model=schemas.CouponValidateResponse)
def validate_coupon(payload: schemas.CouponValidateRequest, db: Session = Depends(get_db)):
    coupon = db.query(models.Coupon).filter(
        models.Coupon.code == payload.code.upper(),
        models.Coupon.active == True,  # noqa: E712
    ).first()
    if not coupon:
        return schemas.CouponValidateResponse(valid=False, message="Coupon not found or inactive")
    return schemas.CouponValidateResponse(
        valid=True,
        discount_type=coupon.discount_type,
        discount_value=coupon.discount_value,
        message=f"{coupon.code} applied",
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
