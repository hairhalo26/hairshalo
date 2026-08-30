"""Loyalty endpoints.

Deliberately asymmetric: the programme's *terms* are public, but nobody's
*balance* is. This application has admin accounts and no customer accounts, so
a public "how many points does bhargavi@example.com have?" endpoint would be an
address-enumeration oracle that also leaks how much someone has spent. Balances
are therefore admin-only, and a customer spends points by asking for them at
checkout, where the server already knows who they are from the order.
"""
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import loyalty as service, models, schemas
from app.config import settings
from app.database import get_db
from app.deps import get_current_admin

router = APIRouter(prefix="/api/loyalty", tags=["loyalty"])


@router.get("/programme", response_model=schemas.LoyaltyProgrammeOut)
def programme():
    """The terms, for the storefront to explain. No balances."""
    earn_per = Decimal(str(settings.LOYALTY_EARN_PER))
    value = Decimal(str(settings.LOYALTY_POINT_VALUE))
    return schemas.LoyaltyProgrammeOut(
        earn_per=earn_per,
        point_value=value,
        max_redeem_pct=settings.LOYALTY_MAX_REDEEM_PCT,
        example=(
            f"Earn 1 point for every ₹{earn_per:,.0f} spent, once your order is paid. "
            f"Each point is worth ₹{value:,.0f} and points can cover up to "
            f"{settings.LOYALTY_MAX_REDEEM_PCT}% of an order."
        ),
    )


@router.get("/customers/{customer_id}", response_model=schemas.LoyaltyBalanceOut)
def customer_balance(customer_id: str, db: Session = Depends(get_db),
                     _admin=Depends(get_current_admin)):
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return schemas.LoyaltyBalanceOut(**service.balance_report(db, customer))


@router.get("/customers/{customer_id}/history",
            response_model=List[schemas.LoyaltyTransactionOut])
def customer_history(customer_id: str, limit: int = Query(100, ge=1, le=500),
                     db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """The ledger behind a balance — every point in and out, and why."""
    rows = (
        db.query(models.LoyaltyTransaction)
        .filter(models.LoyaltyTransaction.customer_id == customer_id)
        .order_by(models.LoyaltyTransaction.created_at.desc())
        .limit(limit).all()
    )
    return [
        schemas.LoyaltyTransactionOut(
            id=r.id, customer_id=r.customer_id, delta=r.delta,
            balance_after=r.balance_after, reason=r.reason.value,
            reference_type=r.reference_type, reference_id=r.reference_id,
            note=r.note, actor=r.actor, created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/customers/{customer_id}/adjust",
             response_model=schemas.LoyaltyTransactionOut, status_code=201)
def adjust_balance(customer_id: str, payload: schemas.LoyaltyAdjustRequest,
                   db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Manual correction — goodwill, or clawing back after a chargeback.

    A note is required: an unexplained balance change is indistinguishable from
    a bug when someone looks at the ledger months later.
    """
    if not (payload.note or "").strip():
        raise HTTPException(status_code=400,
                            detail="A note is required so the ledger explains itself.")
    if payload.delta == 0:
        raise HTTPException(status_code=400, detail="An adjustment must be non-zero.")
    try:
        entry = service.adjust(db, customer_id, payload.delta,
                               payload.note.strip(), actor=admin.email)
    except service.LoyaltyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(entry)
    return schemas.LoyaltyTransactionOut(
        id=entry.id, customer_id=entry.customer_id, delta=entry.delta,
        balance_after=entry.balance_after, reason=entry.reason.value,
        reference_type=entry.reference_type, reference_id=entry.reference_id,
        note=entry.note, actor=entry.actor, created_at=entry.created_at,
    )
