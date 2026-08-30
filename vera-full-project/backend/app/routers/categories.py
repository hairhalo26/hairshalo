from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas
from app.routers.products import slugify

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _to_out(cat: models.Category, count: int = 0) -> schemas.CategoryOut:
    return schemas.CategoryOut(
        id=cat.id, name=cat.name, slug=cat.slug, description=cat.description or "",
        sort_order=cat.sort_order or 0, is_active=bool(cat.is_active), product_count=count,
    )


@router.get("", response_model=List[schemas.CategoryOut])
def list_categories(include_inactive: bool = False, db: Session = Depends(get_db)):
    counts = dict(
        db.query(models.Product.category_id, func.count(models.Product.id))
        .filter(models.Product.status == models.ProductStatus.published)
        .group_by(models.Product.category_id).all()
    )
    q = db.query(models.Category)
    if not include_inactive:
        q = q.filter(models.Category.is_active == True)  # noqa: E712
    cats = q.order_by(models.Category.sort_order.asc(), models.Category.name.asc()).all()
    return [_to_out(c, counts.get(c.id, 0)) for c in cats]


@router.post("", response_model=schemas.CategoryOut, status_code=201)
def create_category(
    payload: schemas.CategoryCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    slug = payload.slug or slugify(payload.name)
    if db.query(models.Category).filter(
        (models.Category.slug == slug) | (models.Category.name == payload.name)
    ).first():
        raise HTTPException(status_code=400, detail="A category with that name or slug already exists")
    cat = models.Category(**payload.model_dump(exclude={"slug"}), slug=slug)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return _to_out(cat)


@router.put("/{category_id}", response_model=schemas.CategoryOut)
def update_category(
    category_id: str,
    payload: schemas.CategoryUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    cat = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(cat, field, value)
    db.commit()
    db.refresh(cat)
    return _to_out(cat)


@router.delete("/{category_id}", status_code=204)
def delete_category(
    category_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    cat = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if db.query(models.Product).filter(models.Product.category_id == category_id).first():
        raise HTTPException(
            status_code=400,
            detail="Category still has products — reassign them or deactivate the category instead.",
        )
    db.delete(cat)
    db.commit()
    return None
