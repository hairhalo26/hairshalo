"""Product placeholders — a separate, non-sellable data domain.

These endpoints deliberately never touch the `products`, `inventory_items`,
`orders` or `order_items` tables, except in `convert_to_product`, which creates
a brand new Product row rather than reclassifying the placeholder.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas
from app.pricing import compute_pricing, PricingError
from app.routers.products import slugify, _coerce_status, _to_out as product_to_out

router = APIRouter(prefix="/api/product-placeholders", tags=["product-placeholders"])


def _get_or_404(placeholder_id: str, db: Session) -> models.ProductPlaceholder:
    placeholder = db.query(models.ProductPlaceholder).filter(
        models.ProductPlaceholder.id == placeholder_id
    ).first()
    if not placeholder:
        raise HTTPException(status_code=404, detail="Product placeholder not found")
    return placeholder


@router.get("", response_model=List[schemas.ProductPlaceholderOut])
def list_placeholders(
    category: Optional[str] = None,
    include_hidden: bool = Query(False, description="Admin only — also return hidden placeholders"),
    limit: Optional[int] = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(models.ProductPlaceholder)
    if category:
        q = q.filter(models.ProductPlaceholder.category == category)
    if not include_hidden:
        q = q.filter(models.ProductPlaceholder.is_visible == True)  # noqa: E712
    q = q.order_by(
        models.ProductPlaceholder.sort_order.asc(),
        models.ProductPlaceholder.created_at.asc(),
    )
    if limit:
        q = q.limit(limit)
    return q.all()


@router.get("/{placeholder_id}", response_model=schemas.ProductPlaceholderOut)
def get_placeholder(placeholder_id: str, db: Session = Depends(get_db)):
    return _get_or_404(placeholder_id, db)


@router.post("", response_model=schemas.ProductPlaceholderOut, status_code=201)
def create_placeholder(
    payload: schemas.ProductPlaceholderCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    placeholder = models.ProductPlaceholder(**payload.model_dump())
    db.add(placeholder)
    db.commit()
    db.refresh(placeholder)
    return placeholder


@router.put("/{placeholder_id}", response_model=schemas.ProductPlaceholderOut)
def update_placeholder(
    placeholder_id: str,
    payload: schemas.ProductPlaceholderUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    placeholder = _get_or_404(placeholder_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(placeholder, field, value)
    db.commit()
    db.refresh(placeholder)
    return placeholder


@router.delete("/{placeholder_id}", status_code=204)
def delete_placeholder(
    placeholder_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    placeholder = _get_or_404(placeholder_id, db)
    db.delete(placeholder)
    db.commit()
    return None


@router.post("/{placeholder_id}/convert-to-product", response_model=schemas.ProductOut, status_code=201)
def convert_to_product(
    placeholder_id: str,
    payload: schemas.PlaceholderConvertRequest,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Create a NEW real product from a placeholder's descriptive fields.

    This never flips a flag on the placeholder — a real Product row is created
    with its own id, and the placeholder is left untouched (or deleted if the
    admin asked for that). A real price is required, since placeholders only
    carry display text.
    """
    placeholder = _get_or_404(placeholder_id, db)

    slug = slugify(placeholder.name)
    if db.query(models.Product).filter(models.Product.slug == slug).first():
        slug = f"{slug}-{db.query(models.Product).count() + 1}"

    status = _coerce_status(payload.status)

    # The placeholder stores a category NAME (it has no FK into the real
    # catalog by design), so resolve it to a real Category, creating one if
    # this category does not exist yet.
    category = db.query(models.Category).filter(
        models.Category.name == placeholder.category
    ).first()
    if not category:
        category = models.Category(
            name=placeholder.category,
            slug=slugify(placeholder.category),
            sort_order=(db.query(models.Category).count() + 1) * 10,
        )
        db.add(category)
        db.flush()

    product = models.Product(
        name=placeholder.name,
        slug=slug,
        category_id=category.id,
        short_description=placeholder.short_description or "",
        description=payload.description if payload.description is not None else (placeholder.short_description or ""),
        status=status,
    )
    # Pricing goes through the canonical engine, exactly like a normal product.
    try:
        price, compare_at, _amt, dtype, dvalue = compute_pricing(
            payload.compare_at_price if payload.compare_at_price is not None else payload.price,
            "fixed_amount" if payload.compare_at_price is not None else "none",
            (payload.compare_at_price - payload.price) if payload.compare_at_price is not None else 0,
        )
    except PricingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    product.price = price
    product.compare_at_price = compare_at
    product.discount_type = models.DiscountKind(dtype)
    product.discount_value = dvalue

    # image_url is derived from media now, so seed a media row instead.
    image = payload.image_url or placeholder.placeholder_image
    if image:
        product.media.append(models.ProductMedia(
            url=image, media_type=models.MediaType.image,
            alt_text=placeholder.name, sort_order=0, is_primary=True,
        ))

    db.add(product)

    if payload.delete_placeholder:
        db.delete(placeholder)

    db.commit()
    db.refresh(product)
    return product_to_out(product)
