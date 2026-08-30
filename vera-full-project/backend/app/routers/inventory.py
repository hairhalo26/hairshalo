"""Inventory admin API.

Rows are driven by ProductVariant — the authoritative stock — so a product
created through the admin appears here immediately with no manual ledger step.
`inventory_items` still supplies warehouse labels where one exists.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas, inventory as inventory_service

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

LOW_STOCK_THRESHOLD = 15


def _row(variant: models.ProductVariant) -> schemas.InventoryRowOut:
    warehouse = variant.inventory[0].warehouse if variant.inventory else None
    stock = variant.stock or 0
    level = "crit" if stock <= 5 else "low" if stock <= LOW_STOCK_THRESHOLD else "ok"
    return schemas.InventoryRowOut(
        variant_id=variant.id,
        product_id=variant.product_id,
        product_name=variant.product.name if variant.product else "—",
        product_status=variant.product.status.value if variant.product and variant.product.status else "—",
        sku=variant.sku,
        variant_label=variant.label,
        warehouse=warehouse,
        stock=stock,
        is_available=bool(variant.is_available),
        stock_level=level,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
    )


@router.get("", response_model=List[schemas.InventoryRowOut])
def list_inventory(
    stock_status: Optional[str] = Query(None, description="in_stock | low_stock | out_of_stock"),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    variants = (
        db.query(models.ProductVariant)
        .options(joinedload(models.ProductVariant.product),
                 joinedload(models.ProductVariant.inventory))
        .all()
    )
    rows = [_row(v) for v in variants]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in r.product_name.lower() or needle in (r.sku or "").lower()]
    if stock_status == "in_stock":
        rows = [r for r in rows if r.stock > 0]
    elif stock_status == "low_stock":
        rows = [r for r in rows if 0 < r.stock <= LOW_STOCK_THRESHOLD]
    elif stock_status == "out_of_stock":
        rows = [r for r in rows if r.stock == 0]
    rows.sort(key=lambda r: (r.stock, r.product_name))
    return rows


@router.post("/adjust", response_model=schemas.InventoryAdjustResult)
def adjust_stock(
    payload: schemas.InventoryAdjustRequest,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Apply an audited manual adjustment. Never bypasses the movement log."""
    try:
        reason = models.MovementReason(payload.reason)
    except ValueError:
        allowed = ", ".join(r.value for r in models.MovementReason)
        raise HTTPException(status_code=400, detail=f"Reason must be one of: {allowed}")

    if reason in (models.MovementReason.order,):
        raise HTTPException(status_code=400, detail="Order movements are created by checkout, not manually.")
    if payload.delta == 0:
        raise HTTPException(status_code=400, detail="Adjustment quantity cannot be zero.")

    movement = inventory_service.adjust(
        db, payload.variant_id, payload.delta, reason,
        note=payload.note, actor=admin.email,
    )
    db.commit()
    variant = db.query(models.ProductVariant).options(
        joinedload(models.ProductVariant.product),
        joinedload(models.ProductVariant.inventory),
    ).filter(models.ProductVariant.id == payload.variant_id).first()
    return schemas.InventoryAdjustResult(
        row=_row(variant),
        movement=schemas.InventoryMovementOut(
            id=movement.id, delta=movement.delta, stock_after=movement.stock_after,
            reason=movement.reason.value, note=movement.note, actor=movement.actor,
            reference_type=movement.reference_type, reference_id=movement.reference_id,
            created_at=movement.created_at,
        ),
    )


@router.get("/movements/{variant_id}", response_model=List[schemas.InventoryMovementOut])
def list_movements(
    variant_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    rows = (
        db.query(models.InventoryMovement)
        .filter(models.InventoryMovement.variant_id == variant_id)
        .order_by(models.InventoryMovement.created_at.desc())
        .limit(limit).all()
    )
    return [schemas.InventoryMovementOut(
        id=m.id, delta=m.delta, stock_after=m.stock_after, reason=m.reason.value,
        note=m.note, actor=m.actor, reference_type=m.reference_type,
        reference_id=m.reference_id, created_at=m.created_at,
    ) for m in rows]
