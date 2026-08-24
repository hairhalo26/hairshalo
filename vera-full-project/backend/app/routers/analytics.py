from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary", response_model=schemas.AnalyticsSummary)
def get_summary(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_orders = db.query(models.Order).filter(models.Order.created_at >= month_start).all()
    revenue = sum(o.total for o in month_orders)
    order_count = len(month_orders)
    avg_order_value = round(revenue / order_count, 2) if order_count else 0.0

    active_customers = db.query(func.count(models.Customer.id)).scalar() or 0

    return schemas.AnalyticsSummary(
        revenue_this_month=round(revenue, 2),
        orders_this_month=order_count,
        active_customers=active_customers,
        conversion_rate=3.8,  # placeholder — wire up to real site-traffic tracking in production
        avg_order_value=avg_order_value,
    )


@router.get("/top-products", response_model=List[schemas.TopProduct])
def get_top_products(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    rows = (
        db.query(
            models.OrderItem.product_name,
            func.sum(models.OrderItem.quantity).label("units_sold"),
        )
        .group_by(models.OrderItem.product_name)
        .order_by(func.sum(models.OrderItem.quantity).desc())
        .limit(5)
        .all()
    )
    return [schemas.TopProduct(name=r.product_name, units_sold=int(r.units_sold)) for r in rows]


@router.get("/revenue-trend")
def get_revenue_trend(days: int = 30, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Returns daily revenue totals for the last `days` days."""
    start = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.date(models.Order.created_at).label("day"),
            func.sum(models.Order.total).label("total"),
        )
        .filter(models.Order.created_at >= start)
        .group_by(func.date(models.Order.created_at))
        .order_by(func.date(models.Order.created_at))
        .all()
    )
    return [{"date": str(r.day), "total": float(r.total)} for r in rows]
