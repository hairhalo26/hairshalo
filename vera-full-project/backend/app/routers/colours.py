"""Managed colours for the hair catalogue (Back Office -> Hair Catalogue).

One global list, shared by Synthetic Hair and Human Hair. A colour reaches a
product only through a variant that uses it (ProductVariant.colour_id), so the
list is a vocabulary, never a claim about what any product comes in.
"""
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_optional_admin
from app import models, schemas
from app.routers.products import slugify, _category_scope

router = APIRouter(prefix="/api/colours", tags=["colours"])

HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _clean_hex(value: Optional[str]) -> Optional[str]:
    """'#abc', 'ABC' or '#AABBCC' -> '#aabbcc'; blank -> None."""
    if value is None or not str(value).strip():
        return None
    m = HEX.match(str(value).strip())
    if not m:
        raise HTTPException(status_code=400, detail="Swatch must be a hex colour such as #2B1B14.")
    digits = m.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.lower()


def _check_unique(db: Session, name: Optional[str], slug: Optional[str], exclude_id=None):
    q = db.query(models.Colour)
    if exclude_id:
        q = q.filter(models.Colour.id != exclude_id)
    if name is not None and q.filter(func.lower(models.Colour.name) == name.lower()).first():
        raise HTTPException(status_code=400, detail=f"A colour called '{name}' already exists.")
    if slug is not None:
        if not slug:
            raise HTTPException(status_code=400, detail="That name has no letters or numbers to make a slug from.")
        if q.filter(models.Colour.slug == slug).first():
            raise HTTPException(status_code=400, detail=f"A colour with the slug '{slug}' already exists.")


def _out(c: models.Colour, product_count=0, variant_count=0) -> schemas.ColourOut:
    return schemas.ColourOut(id=c.id, name=c.name, slug=c.slug, hex=c.hex,
                             sort_order=c.sort_order or 0, is_active=bool(c.is_active),
                             product_count=product_count, variant_count=variant_count)


def _get_or_404(db: Session, colour_id: str) -> models.Colour:
    c = db.query(models.Colour).filter(models.Colour.id == colour_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="Colour not found")
    return c


@router.get("", response_model=List[schemas.ColourOut])
def list_colours(
    include_inactive: bool = False,
    in_use: bool = False,
    category: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_db),
    staff=Depends(get_optional_admin),
):
    """Colours in Back Office order.

    `product_count` is the number of PUBLISHED products with an available
    variant in that colour — within `category` / `type` when given, which is
    what the shop's colour filter shows. `in_use=true` drops colours with none.
    """
    scope = _category_scope(db, category, type)
    pq = (db.query(models.ProductVariant.colour_id,
                   func.count(func.distinct(models.ProductVariant.product_id)))
          .join(models.Product, models.Product.id == models.ProductVariant.product_id)
          .filter(models.ProductVariant.colour_id.isnot(None),
                  models.ProductVariant.is_available == True,  # noqa: E712
                  models.Product.status == models.ProductStatus.published))
    if scope is not None:
        pq = pq.filter(models.Product.category_id.in_(scope))
    product_counts = dict(pq.group_by(models.ProductVariant.colour_id).all())

    variant_counts = {}
    if staff is not None:
        variant_counts = dict(
            db.query(models.ProductVariant.colour_id, func.count(models.ProductVariant.id))
            .filter(models.ProductVariant.colour_id.isnot(None))
            .group_by(models.ProductVariant.colour_id).all())

    q = db.query(models.Colour)
    if not include_inactive or staff is None:
        q = q.filter(models.Colour.is_active == True)  # noqa: E712
    rows = q.order_by(models.Colour.sort_order.asc(), models.Colour.name.asc()).all()
    out = [_out(c, product_counts.get(c.id, 0), variant_counts.get(c.id, 0)) for c in rows]
    if in_use:
        out = [c for c in out if c.product_count > 0]
    return out


@router.post("", response_model=schemas.ColourOut, status_code=201)
def create_colour(payload: schemas.ColourCreate, db: Session = Depends(get_db),
                  _admin=Depends(get_current_admin)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A colour needs a name.")
    slug = slugify(payload.slug or name)
    _check_unique(db, name, slug)
    colour = models.Colour(name=name, slug=slug, hex=_clean_hex(payload.hex),
                           sort_order=payload.sort_order, is_active=payload.is_active)
    db.add(colour)
    db.commit()
    db.refresh(colour)
    return _out(colour)


@router.post("/reorder", response_model=List[schemas.ColourOut])
def reorder_colours(payload: schemas.ReorderRequest, db: Session = Depends(get_db),
                    _admin=Depends(get_current_admin)):
    rows = {c.id: c for c in db.query(models.Colour)
            .filter(models.Colour.id.in_(payload.ids)).all()}
    if len(rows) != len(set(payload.ids)):
        raise HTTPException(status_code=400, detail="Unknown colour in the new order.")
    for position, cid in enumerate(payload.ids):
        rows[cid].sort_order = position
    db.commit()
    return [_out(rows[cid]) for cid in payload.ids]


@router.put("/{colour_id}", response_model=schemas.ColourOut)
def update_colour(colour_id: str, payload: schemas.ColourUpdate,
                  db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    colour = _get_or_404(db, colour_id)
    values = payload.model_dump(exclude_unset=True)
    name = values.get("name")
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="A colour needs a name.")
    # Renaming keeps the slug — it is what filter links use.
    slug = slugify(values["slug"]) if values.get("slug") else None
    _check_unique(db, name, slug, exclude_id=colour.id)

    if name is not None and name != colour.name:
        # Variants keep a copy of the name in `color` (labels, carts, the
        # importer read it). Past ORDERS keep their own snapshot and are not
        # touched: a rename never rewrites what a customer bought.
        for v in colour.variants:
            v.color = name
        colour.name = name
    if slug:
        colour.slug = slug
    if "hex" in values:
        colour.hex = _clean_hex(values["hex"])
    if values.get("sort_order") is not None:
        colour.sort_order = values["sort_order"]
    if values.get("is_active") is not None:
        colour.is_active = values["is_active"]
    db.commit()
    db.refresh(colour)
    return _out(colour)


@router.delete("/{colour_id}", status_code=204)
def delete_colour(colour_id: str, db: Session = Depends(get_db),
                  _admin=Depends(get_current_admin)):
    """Only an unused colour can be deleted. One that any variant uses — even
    a draft's, even an unavailable one — is archived (deactivated) instead."""
    colour = _get_or_404(db, colour_id)
    used = db.query(models.ProductVariant).filter(
        models.ProductVariant.colour_id == colour_id).count()
    if used:
        raise HTTPException(
            status_code=400,
            detail=f"'{colour.name}' is used by {used} variant{'s' if used != 1 else ''} — "
                   "archive it (deactivate) instead of deleting.")
    db.delete(colour)
    db.commit()
    return None
