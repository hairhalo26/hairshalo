from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_optional_admin
from app import models, schemas
from app.routers.products import slugify

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _to_out(cat: models.Category, count: int = 0) -> schemas.CategoryOut:
    # tagline and image_url used to be left out here, so a collection image
    # uploaded in the Admin Panel was saved but never reached the storefront.
    return schemas.CategoryOut(
        id=cat.id, name=cat.name, slug=cat.slug, description=cat.description or "",
        tagline=cat.tagline, image_url=cat.image_url, parent_id=cat.parent_id,
        sort_order=cat.sort_order or 0, is_active=bool(cat.is_active), product_count=count,
    )


def _check_parent(db: Session, parent_id, category_id=None):
    """A parent must exist, must not be the category itself, and must be top
    level — one level of nesting keeps the shop's filters readable and makes a
    cycle impossible."""
    if not parent_id:
        return None
    if parent_id == category_id:
        raise HTTPException(status_code=400, detail="A category cannot be its own parent.")
    parent = db.query(models.Category).filter(models.Category.id == parent_id).first()
    if not parent:
        raise HTTPException(status_code=400, detail="Parent category not found.")
    if parent.parent_id:
        raise HTTPException(status_code=400,
                            detail=f"'{parent.name}' is itself a subcategory; choose a top-level category.")
    if category_id and db.query(models.Category).filter(
            models.Category.parent_id == category_id).first():
        raise HTTPException(status_code=400,
                            detail="This category has subcategories, so it cannot become one itself.")
    return parent_id


@router.get("", response_model=List[schemas.CategoryOut])
def list_categories(include_inactive: bool = False, db: Session = Depends(get_db),
                    staff=Depends(get_optional_admin)):
    counts = dict(
        db.query(models.Product.category_id, func.count(models.Product.id))
        .filter(models.Product.status == models.ProductStatus.published)
        .group_by(models.Product.category_id).all()
    )
    q = db.query(models.Category)
    # Hidden categories are a staff view; the flag is ignored for the public.
    if not include_inactive or staff is None:
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
    _check_parent(db, payload.parent_id)
    values = payload.model_dump(exclude={"slug"})
    values["parent_id"] = values.get("parent_id") or None
    cat = models.Category(**values, slug=slug)
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
    values = payload.model_dump(exclude_unset=True)
    if "parent_id" in values:
        values["parent_id"] = _check_parent(db, values["parent_id"], cat.id)
    for field, value in values.items():
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
    # Subcategories move up to top level rather than disappearing with it.
    for child in db.query(models.Category).filter(models.Category.parent_id == category_id).all():
        child.parent_id = None
    db.delete(cat)
    db.commit()
    return None
