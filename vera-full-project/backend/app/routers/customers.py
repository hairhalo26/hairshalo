from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/customers", tags=["customers"])


@router.get("", response_model=List[schemas.CustomerOut])
def list_customers(
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    customers = db.query(models.Customer).options(joinedload(models.Customer.orders)).all()
    results = []
    for c in customers:
        results.append(schemas.CustomerOut(
            id=c.id,
            name=c.name,
            email=c.email,
            phone=c.phone,
            loyalty_points=c.loyalty_points,
            created_at=c.created_at,
            order_count=len(c.orders),
            total_spent=sum(o.total for o in c.orders),
        ))
    return results
