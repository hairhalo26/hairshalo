"""Centralised stock mutation.

`ProductVariant.stock` is the single authoritative quantity. Nothing else in
the codebase may assign to it — every change goes through `apply_movement()`,
which:

  1. takes a `SELECT ... FOR UPDATE` row lock on the variant,
  2. validates the resulting quantity is not negative,
  3. writes the new running total,
  4. appends an `InventoryMovement` row explaining the change.

Because the lock is held until the caller's transaction commits, two
simultaneous checkouts of the same variant serialise, and the movement log can
never disagree with the running total.

`inventory_items` is a per-warehouse breakdown for reporting only; it is never
consulted to decide whether something can be sold.
"""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models


class InsufficientStock(Exception):
    def __init__(self, available: int, requested: int, label: str):
        self.available, self.requested, self.label = available, requested, label
        super().__init__(f"Insufficient stock for '{label}': {available} available, {requested} requested.")


def lock_variant(db: Session, variant_id: str) -> Optional[models.ProductVariant]:
    """Fetch a variant with a row lock held until the transaction ends."""
    return (
        db.query(models.ProductVariant)
        .filter(models.ProductVariant.id == variant_id)
        .with_for_update()
        .first()
    )


def apply_movement(
    db: Session,
    variant: models.ProductVariant,
    delta: int,
    reason: models.MovementReason,
    *,
    reference_type: str = None,
    reference_id: str = None,
    note: str = None,
    actor: str = None,
    allow_negative: bool = False,
) -> models.InventoryMovement:
    """Change stock by `delta` and record why. Caller must already hold the lock.

    Raises InsufficientStock when the result would go below zero.
    """
    if delta == 0:
        raise ValueError("A stock movement must be non-zero")

    current = variant.stock or 0
    new_stock = current + delta
    if new_stock < 0 and not allow_negative:
        raise InsufficientStock(current, abs(delta), variant.label)

    variant.stock = new_stock
    movement = models.InventoryMovement(
        variant_id=variant.id,
        delta=delta,
        stock_after=new_stock,
        reason=reason,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
        actor=actor,
    )
    db.add(movement)
    return movement


def consume_for_order(db: Session, variant: models.ProductVariant, quantity: int,
                      order_id: str = None) -> models.InventoryMovement:
    """Deduct stock for a sale. Variant must already be locked by the caller."""
    return apply_movement(
        db, variant, -abs(quantity), models.MovementReason.order,
        reference_type="order", reference_id=order_id,
    )


def release_for_order(db: Session, variant: models.ProductVariant, quantity: int,
                      order_id: str, reason: models.MovementReason,
                      actor: str = None) -> models.InventoryMovement:
    """Return stock after a cancellation or refund."""
    return apply_movement(
        db, variant, abs(quantity), reason,
        reference_type="order", reference_id=order_id, actor=actor,
    )


def open_stock(db: Session, variant: models.ProductVariant, quantity: int,
               actor: str = None) -> Optional[models.InventoryMovement]:
    """Record the opening balance when a variant is created."""
    if not quantity:
        return None
    # The variant was constructed with `stock` already set, so rebase to zero
    # first and let apply_movement establish the total through the audit trail.
    variant.stock = 0
    return apply_movement(
        db, variant, int(quantity), models.MovementReason.initial,
        note="Opening stock", actor=actor,
    )


def adjust(db: Session, variant_id: str, delta: int, reason: models.MovementReason,
           note: str = None, actor: str = None) -> models.InventoryMovement:
    """Admin-initiated adjustment. Takes its own lock."""
    variant = lock_variant(db, variant_id)
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    try:
        return apply_movement(
            db, variant, delta, reason, note=note, actor=actor,
        )
    except InsufficientStock as exc:
        raise HTTPException(status_code=400, detail=str(exc))
