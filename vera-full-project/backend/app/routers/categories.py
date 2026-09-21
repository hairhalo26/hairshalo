from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_optional_admin
from app import models, schemas
from app.routers.products import slugify

router = APIRouter(prefix="/api/categories", tags=["categories"])

# The category tree is also the hair hierarchy: a top-level category is a Hair
# Category (Synthetic Hair, Human Hair …) and its subcategories are its Hair
# Types. Nothing here names a particular category — they are all rows.


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


def _check_unique(db: Session, name: Optional[str], slug: Optional[str], exclude_id=None):
    """Names and slugs are unique across the whole tree, ignoring case, so
    "human hair" cannot sit beside "Human Hair" and every slug is one URL."""
    q = db.query(models.Category)
    if exclude_id:
        q = q.filter(models.Category.id != exclude_id)
    if name is not None:
        if not name.strip():
            raise HTTPException(status_code=400, detail="A category needs a name.")
        if q.filter(func.lower(models.Category.name) == name.strip().lower()).first():
            raise HTTPException(status_code=400, detail="A category with that name or slug already exists")
    if slug is not None:
        if not slug:
            raise HTTPException(status_code=400, detail="That slug is empty once cleaned up; use letters or numbers.")
        if q.filter(models.Category.slug == slug).first():
            raise HTTPException(status_code=400, detail="A category with that name or slug already exists")


@router.get("", response_model=List[schemas.CategoryOut])
def list_categories(include_inactive: bool = False, db: Session = Depends(get_db),
                    staff=Depends(get_optional_admin)):
    counts = dict(
        db.query(models.Product.category_id, func.count(models.Product.id))
        .filter(models.Product.status == models.ProductStatus.published)
        .group_by(models.Product.category_id).all()
    )
    everything = db.query(models.Category).all()
    # A top-level category's count includes its subcategories' products — the
    # Synthetic Hair card counts every Synthetic Hair type.
    rolled = dict(counts)
    for c in everything:
        if c.parent_id:
            rolled[c.parent_id] = rolled.get(c.parent_id, 0) + counts.get(c.id, 0)
    q = db.query(models.Category)
    # Hidden categories are a staff view; the flag is ignored for the public.
    if not include_inactive or staff is None:
        q = q.filter(models.Category.is_active == True)  # noqa: E712
    cats = q.order_by(models.Category.sort_order.asc(), models.Category.name.asc()).all()
    return [_to_out(c, rolled.get(c.id, 0)) for c in cats]


@router.post("", response_model=schemas.CategoryOut, status_code=201)
def create_category(
    payload: schemas.CategoryCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    payload.name = payload.name.strip()
    slug = slugify(payload.slug or payload.name)
    _check_unique(db, payload.name, slug)
    _check_parent(db, payload.parent_id)
    values = payload.model_dump(exclude={"slug"})
    values["parent_id"] = values.get("parent_id") or None
    cat = models.Category(**values, slug=slug)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return _to_out(cat)


@router.post("/reorder", response_model=List[schemas.CategoryOut])
def reorder_categories(
    payload: schemas.ReorderRequest,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Set the display order of sibling categories — the hair categories, or
    the hair types of ONE category — to the order of `ids`."""
    rows = db.query(models.Category).filter(models.Category.id.in_(payload.ids)).all()
    by_id = {c.id: c for c in rows}
    if len(by_id) != len(set(payload.ids)):
        raise HTTPException(status_code=400, detail="Unknown category in the new order.")
    if len({c.parent_id for c in rows}) > 1:
        raise HTTPException(status_code=400,
                            detail="Only categories at the same level, under the same parent, can be reordered together.")
    for position, cid in enumerate(payload.ids):
        by_id[cid].sort_order = position
    db.commit()
    return [_to_out(by_id[cid]) for cid in payload.ids]


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
    # A rename keeps the slug, so links and filters that use it keep working.
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
    if values.get("slug") is not None:
        values["slug"] = slugify(values["slug"])
    elif "slug" in values:
        values.pop("slug")
    _check_unique(db, values.get("name") if values.get("name") != cat.name else None,
                  values.get("slug") if values.get("slug") != cat.slug else None,
                  exclude_id=cat.id)
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
    # Deleting a hair category used to promote its hair types to the top level,
    # which silently turned "Bob Synthetic Wig" into a category of its own.
    children = db.query(models.Category).filter(models.Category.parent_id == category_id).count()
    if children:
        raise HTTPException(
            status_code=400,
            detail=f"'{cat.name}' still has {children} subcategor{'y' if children == 1 else 'ies'} "
                   "(hair types) — delete or move them first, or deactivate it instead.",
        )
    db.delete(cat)
    db.commit()
    return None
