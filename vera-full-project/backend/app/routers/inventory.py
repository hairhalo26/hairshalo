from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def _to_out(item: models.InventoryItem) -> schemas.InventoryItemOut:
    return schemas.InventoryItemOut(
        id=item.id,
        product_id=item.product_id,
        sku=item.sku,
        variant=item.variant,
        warehouse=item.warehouse,
        units=item.units,
        low_stock_threshold=item.low_stock_threshold,
        stock_level=item.stock_level,
        product_name=item.product.name if item.product else None,
    )


@router.get("", response_model=List[schemas.InventoryItemOut])
def list_inventory(
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    items = db.query(models.InventoryItem).options(joinedload(models.InventoryItem.product)).all()
    return [_to_out(i) for i in items]


@router.post("", response_model=schemas.InventoryItemOut, status_code=201)
def create_inventory_item(
    payload: schemas.InventoryItemCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    item = models.InventoryItem(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.put("/{item_id}/adjust", response_model=schemas.InventoryItemOut)
def adjust_stock(
    item_id: str,
    payload: schemas.InventoryAdjust,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    item.units = max(0, item.units + payload.units_delta)
    db.commit()
    db.refresh(item)
    return _to_out(item)
