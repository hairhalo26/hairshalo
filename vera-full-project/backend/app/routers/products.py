import re
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import or_, func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas, inventory
from app.pricing import compute_pricing, PricingError
from app.storage import get_storage, validate_and_classify, UploadRejected

router = APIRouter(prefix="/api/products", tags=["products"])


# Readiness issues that `force=true` may NOT waive — without these a published
# product is a dead end the customer can see but never buy.
HARD_PUBLISH_REQUIREMENTS = {"missing_variants", "missing_price", "missing_category"}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _apply_pricing(target, original_price, discount_type, discount_value):
    """Run the canonical pricing engine and write the result onto a model.

    `target` is a Product or ProductVariant. Raises HTTP 400 with the
    validation message on invalid pricing.
    """
    try:
        price, compare_at, _amount, dtype, dvalue = compute_pricing(
            original_price, discount_type, discount_value
        )
    except PricingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    target.price = price
    target.compare_at_price = compare_at
    target.discount_type = models.DiscountKind(dtype)
    target.discount_value = dvalue
    return target


def _readiness(product: models.Product) -> schemas.ProductReadiness:
    issues = product.readiness_issues
    return schemas.ProductReadiness(
        is_ready_to_publish=len(issues) == 0,
        issues=issues,
        missing_description="missing_description" in issues,
        missing_price="missing_price" in issues,
        missing_image="missing_image" in issues,
        missing_category="missing_category" in issues,
        missing_variants="missing_variants" in issues,
    )


def _variant_out(v: models.ProductVariant, product: models.Product) -> schemas.ProductVariantOut:
    # A variant without its own price inherits the product's resolved pricing,
    # so the storefront always has a concrete price for the selected variant.
    price = v.price if v.price is not None else product.price
    compare_at = v.compare_at_price if v.price is not None else product.compare_at_price
    from app.pricing import discount_amount_for, discount_percent_for, is_on_sale
    return schemas.ProductVariantOut(
        id=v.id, sku=v.sku, label=v.label, length=v.length, density=v.density,
        color=v.color, lace_type=v.lace_type, cap_size=v.cap_size,
        price=price, compare_at_price=compare_at,
        discount_type=(v.discount_type.value if v.price is not None and v.discount_type
                       else (product.discount_type.value if product.discount_type else "none")),
        discount_value=(v.discount_value if v.price is not None else product.discount_value) or Decimal("0.00"),
        discount_amount=discount_amount_for(price, compare_at),
        discount_percent=discount_percent_for(price, compare_at),
        on_sale=is_on_sale(price, compare_at),
        stock=v.stock or 0, is_available=bool(v.is_available),
        in_stock=bool(v.is_available) and (v.stock or 0) > 0,
        sort_order=v.sort_order or 0,
    )


def _to_out(product: models.Product) -> schemas.ProductOut:
    pmin, pmax = product.price_range
    video = next((m for m in product.media if m.media_type == models.MediaType.video), None)
    return schemas.ProductOut(
        id=product.id,
        name=product.name,
        slug=product.slug,
        status=product.status.value if product.status else "Draft",
        category=product.category,
        category_id=product.category_id,
        short_description=product.short_description or "",
        description=product.description or "",
        brand=product.brand,
        hair_type=product.hair_type,
        texture=product.texture,
        construction=product.construction,
        badge=product.badge.value if product.badge else None,
        featured=bool(product.featured),
        bestseller=bool(product.bestseller),
        new_arrival=bool(product.new_arrival),
        sort_order=product.sort_order or 0,
        image_url=product.primary_image_url,
        video_url=video.url if video else None,
        rating=product.rating or 0.0,
        review_count=product.review_count or 0,
        is_demo=bool(product.is_demo),
        is_purchasable=product.is_purchasable,
        total_stock=product.total_stock,
        in_stock=product.total_stock > 0,
        # Resolved pricing
        price=product.price,
        compare_at_price=product.compare_at_price,
        original_price=product.compare_at_price if product.compare_at_price is not None else product.price,
        discount_type=product.discount_type.value if product.discount_type else "none",
        discount_value=product.discount_value or Decimal("0.00"),
        discount_amount=product.discount_amount,
        discount_percent=product.discount_percent,
        on_sale=product.on_sale,
        price_min=pmin,
        price_max=pmax,
        media=[schemas.ProductMediaOut(
            id=m.id, url=m.url, media_type=m.media_type.value, alt_text=m.alt_text or "",
            sort_order=m.sort_order or 0, is_primary=bool(m.is_primary),
            content_type=m.content_type, file_size=m.file_size,
        ) for m in product.media],
        variants=[_variant_out(v, product) for v in product.variants],
        readiness=_readiness(product),
        published_at=product.published_at,
        created_at=product.created_at,
    )


def _base_query(db: Session):
    return db.query(models.Product).options(
        joinedload(models.Product.media),
        joinedload(models.Product.variants),
        joinedload(models.Product.category_ref),
        joinedload(models.Product.inventory),
    )


def _get_or_404(product_id: str, db: Session) -> models.Product:
    product = _base_query(db).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("", response_model=List[schemas.ProductOut])
def list_products(
    # --- filtering ---
    category: Optional[str] = Query(None, description="Category name"),
    category_id: Optional[str] = None,
    status: Optional[str] = Query(None, description="Admin only; public callers see Published"),
    q: Optional[str] = Query(None, description="Search name/description"),
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    length: Optional[str] = None,
    color: Optional[str] = None,
    density: Optional[str] = None,
    texture: Optional[str] = None,
    availability: Optional[str] = Query(None, description="in_stock | out_of_stock"),
    featured: Optional[bool] = None,
    bestseller: Optional[bool] = None,
    new_arrival: Optional[bool] = None,
    on_sale: Optional[bool] = Query(None, description="Only discounted products"),
    stock_status: Optional[str] = Query(None, description="in_stock | low_stock | out_of_stock"),
    include_demo: bool = True,
    # --- sorting / paging ---
    sort: str = Query("curated", description="curated|newest|price_asc|price_desc|bestselling|featured|name|discount"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    qry = _base_query(db)

    if status and status.lower() == "all":
        pass  # admin view: every status
    elif status:
        qry = qry.filter(models.Product.status == _coerce_status(status))
    else:
        # Public callers only ever see published products. Draft, review,
        # out-of-stock and archived products are never exposed by default.
        qry = qry.filter(models.Product.status == models.ProductStatus.published)

    if category:
        qry = qry.join(models.Category).filter(models.Category.name == category)
    if category_id:
        qry = qry.filter(models.Product.category_id == category_id)
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(models.Product.name.ilike(like), models.Product.description.ilike(like)))
    if min_price is not None:
        qry = qry.filter(models.Product.price >= min_price)
    if max_price is not None:
        qry = qry.filter(models.Product.price <= max_price)
    if texture:
        qry = qry.filter(models.Product.texture == texture)
    if featured is not None:
        qry = qry.filter(models.Product.featured == featured)
    if bestseller is not None:
        qry = qry.filter(models.Product.bestseller == bestseller)
    if new_arrival is not None:
        qry = qry.filter(models.Product.new_arrival == new_arrival)
    if not include_demo:
        qry = qry.filter(models.Product.is_demo == False)  # noqa: E712
    if on_sale is not None:
        # A genuine markdown only — compare_at_price must exceed price.
        cond = (models.Product.compare_at_price.isnot(None)) & (
            models.Product.compare_at_price > models.Product.price
        )
        qry = qry.filter(cond if on_sale else ~cond)

    # Variant-level attribute filters
    variant_filters = []
    if length:
        variant_filters.append(models.ProductVariant.length == length)
    if color:
        variant_filters.append(models.ProductVariant.color == color)
    if density:
        variant_filters.append(models.ProductVariant.density == density)
    if availability == "in_stock":
        variant_filters.append(models.ProductVariant.stock > 0)
        variant_filters.append(models.ProductVariant.is_available == True)  # noqa: E712
    if variant_filters:
        sub = db.query(models.ProductVariant.product_id).filter(*variant_filters).distinct()
        qry = qry.filter(models.Product.id.in_(sub))

    sorts = {
        "newest": models.Product.created_at.desc(),
        "price_asc": models.Product.price.asc(),
        "price_desc": models.Product.price.desc(),
        "bestselling": models.Product.bestseller.desc(),
        "featured": models.Product.featured.desc(),
        "name": models.Product.name.asc(),
        "discount": (models.Product.compare_at_price - models.Product.price).desc(),
        "curated": models.Product.sort_order.asc(),
    }
    qry = qry.order_by(sorts.get(sort, sorts["curated"]), models.Product.created_at.desc())

    total = qry.order_by(None).with_entities(func.count(func.distinct(models.Product.id))).scalar() or 0
    products = qry.offset(offset).limit(limit).all()

    # Stock filters run in Python because total_stock spans variants+inventory.
    if availability == "out_of_stock" or stock_status == "out_of_stock":
        products = [p for p in products if p.total_stock == 0]
    elif stock_status == "in_stock":
        products = [p for p in products if p.total_stock > 0]
    elif stock_status == "low_stock":
        products = [p for p in products if 0 < p.total_stock <= 15]

    return [_to_out(p) for p in products]


@router.get("/paged", response_model=schemas.ProductListOut)
def list_products_paged(
    q: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    sort: str = "curated",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated listing envelope (items + total) for large catalogs."""
    qry = _base_query(db)
    if status and status.lower() == "all":
        pass
    elif status:
        qry = qry.filter(models.Product.status == _coerce_status(status))
    else:
        qry = qry.filter(models.Product.status == models.ProductStatus.published)
    if category:
        qry = qry.join(models.Category).filter(models.Category.name == category)
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(models.Product.name.ilike(like), models.Product.description.ilike(like)))

    total = qry.order_by(None).with_entities(func.count(func.distinct(models.Product.id))).scalar() or 0
    sorts = {
        "newest": models.Product.created_at.desc(),
        "price_asc": models.Product.price.asc(),
        "price_desc": models.Product.price.desc(),
        "name": models.Product.name.asc(),
        "curated": models.Product.sort_order.asc(),
    }
    rows = qry.order_by(sorts.get(sort, sorts["curated"])).offset(offset).limit(limit).all()
    return schemas.ProductListOut(
        items=[_to_out(p) for p in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{product_id}", response_model=schemas.ProductOut)
def get_product(product_id: str, db: Session = Depends(get_db)):
    return _to_out(_get_or_404(product_id, db))


@router.get("/{product_id}/preview", response_model=schemas.ProductOut)
def preview_product(
    product_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Full product payload regardless of status, for pre-publish preview."""
    return _to_out(_get_or_404(product_id, db))


@router.post("", response_model=schemas.ProductOut, status_code=201)
def create_product(
    payload: schemas.ProductCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    slug = payload.slug or slugify(payload.name)
    if db.query(models.Product).filter(models.Product.slug == slug).first():
        slug = f"{slug}-{db.query(models.Product).count() + 1}"

    if payload.category_id and not db.query(models.Category).filter(
        models.Category.id == payload.category_id
    ).first():
        raise HTTPException(status_code=400, detail="Category not found")

    data = payload.model_dump(exclude={
        "slug", "media", "variants", "status", "badge",
        "original_price", "discount_type", "discount_value",
    })
    product = models.Product(**data, slug=slug)
    product.status = _coerce_status(payload.status)
    product.badge = _coerce_badge(payload.badge)
    _apply_pricing(product, payload.original_price, payload.discount_type, payload.discount_value)

    for m in payload.media:
        product.media.append(models.ProductMedia(
            url=m.url, media_type=models.MediaType(m.media_type),
            alt_text=m.alt_text, sort_order=m.sort_order, is_primary=m.is_primary,
        ))
    seen_skus = set()
    for v in payload.variants:
        if v.sku in seen_skus:
            raise HTTPException(status_code=400, detail=f"Duplicate SKU '{v.sku}' in request")
        seen_skus.add(v.sku)
        _assert_sku_free(v.sku, db)
        vdata = v.model_dump(exclude={"original_price", "discount_type", "discount_value"})
        variant = models.ProductVariant(**vdata)
        if v.original_price is not None:
            _apply_pricing(variant, v.original_price, v.discount_type, v.discount_value)
        product.variants.append(variant)

    db.add(product)
    db.flush()
    # Opening stock is recorded as a movement, so a new product's inventory is
    # visible and auditable immediately — no manual ledger step required.
    for variant in product.variants:
        inventory.open_stock(db, variant, variant.stock or 0, actor=_admin.email)
    db.commit()
    db.refresh(product)
    return _to_out(product)


def _coerce_status(value) -> models.ProductStatus:
    if not value:
        return models.ProductStatus.draft
    for s in models.ProductStatus:
        if s.value.lower() == str(value).lower() or s.name == str(value):
            return s
    raise HTTPException(status_code=400, detail=f"Invalid status '{value}'")


def _coerce_badge(value):
    if value in (None, "", "none", "None"):
        return None
    for b in models.ProductBadge:
        if b.value.lower() == str(value).lower() or b.name == str(value):
            return b
    raise HTTPException(status_code=400, detail=f"Invalid badge '{value}'")


def _assert_sku_free(sku: str, db: Session, exclude_id: Optional[str] = None):
    qy = db.query(models.ProductVariant).filter(models.ProductVariant.sku == sku)
    if exclude_id:
        qy = qy.filter(models.ProductVariant.id != exclude_id)
    if qy.first():
        raise HTTPException(status_code=400, detail=f"SKU '{sku}' already exists")


@router.put("/{product_id}", response_model=schemas.ProductOut)
def update_product(
    product_id: str,
    payload: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    product = _get_or_404(product_id, db)
    data = payload.model_dump(exclude_unset=True)

    if "category_id" in data and data["category_id"]:
        if not db.query(models.Category).filter(models.Category.id == data["category_id"]).first():
            raise HTTPException(status_code=400, detail="Category not found")
    if "status" in data:
        data["status"] = _coerce_status(data["status"])
    if "badge" in data:
        data["badge"] = _coerce_badge(data["badge"])

    # Pricing is recomputed as a set, never patched field-by-field, so an
    # inconsistent combination can never be written.
    pricing_keys = {"original_price", "discount_type", "discount_value"}
    if pricing_keys & data.keys():
        current_original = product.compare_at_price if product.compare_at_price is not None else product.price
        _apply_pricing(
            product,
            data.pop("original_price", current_original),
            data.pop("discount_type", product.discount_type.value if product.discount_type else "none"),
            data.pop("discount_value", product.discount_value or 0),
        )

    for field, value in data.items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return _to_out(product)


@router.patch("/{product_id}", response_model=schemas.ProductOut)
def patch_product(
    product_id: str,
    payload: schemas.ProductUpdate,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Alias of PUT — both apply a partial update (exclude_unset)."""
    return update_product(product_id, payload, db, admin)


@router.post("/{product_id}/status", response_model=schemas.ProductOut)
def change_status(
    product_id: str,
    payload: schemas.ProductStatusChange,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Publishing workflow: Draft -> Review -> Published -> Archived."""
    product = _get_or_404(product_id, db)
    action = payload.action

    if action == "submit_for_review":
        product.status = models.ProductStatus.review
    elif action == "publish":
        issues = product.readiness_issues
        # `force` may waive presentation gaps, but never the requirements that
        # make a product actually sellable — otherwise publishing creates a
        # product customers can see and can never buy.
        blocking = [i for i in issues if i in HARD_PUBLISH_REQUIREMENTS]
        waivable = [i for i in issues if i not in HARD_PUBLISH_REQUIREMENTS]
        if blocking or (waivable and not payload.force):
            labels = {
                "missing_description": "Add a description",
                "missing_price": "Set a price",
                "missing_image": "Add a primary image",
                "missing_category": "Choose a category",
                "missing_variants": "Add at least one variant",
            }
            problems = [labels.get(i, i) for i in (blocking if blocking else issues)]
            raise HTTPException(
                status_code=400,
                detail="Cannot publish this product: " + "; ".join(problems) + ".",
            )
        if not any(v.is_available and (v.stock or 0) >= 0 for v in product.variants):
            raise HTTPException(
                status_code=400,
                detail="Cannot publish this product: at least one variant must be available.",
            )
        product.status = models.ProductStatus.published
        product.published_at = product.published_at or datetime.utcnow()
    elif action == "unpublish":
        product.status = models.ProductStatus.draft
    elif action == "archive":
        product.status = models.ProductStatus.archived
    elif action == "restore":
        product.status = models.ProductStatus.draft
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")

    db.commit()
    db.refresh(product)
    return _to_out(product)


@router.post("/{product_id}/publish", response_model=schemas.ProductOut)
def publish_product(
    product_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Convenience alias for POST /{id}/status {"action":"publish"}."""
    return change_status(product_id, schemas.ProductStatusChange(action="publish", force=force), db, admin)


@router.post("/{product_id}/archive", response_model=schemas.ProductOut)
def archive_product(
    product_id: str,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Convenience alias for POST /{id}/status {"action":"archive"}."""
    return change_status(product_id, schemas.ProductStatusChange(action="archive"), db, admin)


@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Hard delete. Refused once the product has order history — archive instead,
    so historical orders keep their referential context."""
    product = _get_or_404(product_id, db)
    if db.query(models.OrderItem).filter(models.OrderItem.product_id == product_id).first():
        raise HTTPException(
            status_code=400,
            detail="This product appears in existing orders — archive it instead of deleting.",
        )
    for m in product.media:
        if m.storage_key:
            get_storage().delete(m.storage_key)
    db.delete(product)
    db.commit()
    return None


# ---------------- Variants ----------------

@router.get("/{product_id}/variants", response_model=List[schemas.ProductVariantOut])
def list_variants(product_id: str, db: Session = Depends(get_db)):
    product = _get_or_404(product_id, db)
    return [schemas.ProductVariantOut(
        id=v.id, sku=v.sku, label=v.label, length=v.length, density=v.density, color=v.color,
        lace_type=v.lace_type, cap_size=v.cap_size, price=v.price, stock=v.stock or 0,
        is_available=bool(v.is_available), sort_order=v.sort_order or 0,
    ) for v in product.variants]


@router.post("/{product_id}/variants", response_model=schemas.ProductVariantOut, status_code=201)
def add_variant(
    product_id: str,
    payload: schemas.ProductVariantCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    product = _get_or_404(product_id, db)
    _assert_sku_free(payload.sku, db)
    vdata = payload.model_dump(exclude={"original_price", "discount_type", "discount_value"})
    opening = int(vdata.get("stock") or 0)
    variant = models.ProductVariant(product_id=product.id, **vdata)
    if payload.original_price is not None:
        _apply_pricing(variant, payload.original_price, payload.discount_type, payload.discount_value)
    db.add(variant)
    db.flush()
    inventory.open_stock(db, variant, opening, actor=_admin.email)
    db.commit()
    db.refresh(variant)
    db.refresh(product)
    return _variant_out(variant, product)


@router.put("/variants/{variant_id}", response_model=schemas.ProductVariantOut)
def update_variant(
    variant_id: str,
    payload: schemas.ProductVariantUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    variant = db.query(models.ProductVariant).filter(models.ProductVariant.id == variant_id).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    data = payload.model_dump(exclude_unset=True)
    if "sku" in data:
        _assert_sku_free(data["sku"], db, exclude_id=variant_id)

    if data.pop("clear_price_override", False):
        variant.price = None
        variant.compare_at_price = None
        variant.discount_type = models.DiscountKind.none
        variant.discount_value = Decimal("0.00")
        data.pop("original_price", None)
        data.pop("discount_type", None)
        data.pop("discount_value", None)
    elif {"original_price", "discount_type", "discount_value"} & data.keys():
        current_original = variant.compare_at_price if variant.compare_at_price is not None else variant.price
        _apply_pricing(
            variant,
            data.pop("original_price", current_original),
            data.pop("discount_type", variant.discount_type.value if variant.discount_type else "none"),
            data.pop("discount_value", variant.discount_value or 0),
        )

    # Stock is never assigned directly — a change becomes an auditable
    # adjustment movement so the ledger always explains the running total.
    new_stock = data.pop("stock", None)

    for field, value in data.items():
        setattr(variant, field, value)

    if new_stock is not None:
        delta = int(new_stock) - int(variant.stock or 0)
        if delta != 0:
            locked = inventory.lock_variant(db, variant_id)
            try:
                inventory.apply_movement(
                    db, locked, delta, models.MovementReason.adjustment,
                    note="Set via product editor", actor=_admin.email,
                )
            except inventory.InsufficientStock as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    db.commit()
    db.refresh(variant)
    product = _get_or_404(variant.product_id, db)
    return _variant_out(variant, product)


@router.delete("/variants/{variant_id}", status_code=204)
def delete_variant(
    variant_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    variant = db.query(models.ProductVariant).filter(models.ProductVariant.id == variant_id).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    if db.query(models.OrderItem).filter(models.OrderItem.variant_id == variant_id).first():
        raise HTTPException(
            status_code=400,
            detail="This variant appears in existing orders — mark it unavailable instead.",
        )
    db.delete(variant)
    db.commit()
    return None


# ---------------- Media ----------------

@router.post("/{product_id}/media", response_model=schemas.ProductMediaOut, status_code=201)
def add_media(
    product_id: str,
    payload: schemas.ProductMediaCreate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    product = _get_or_404(product_id, db)
    if payload.is_primary:
        for m in product.media:
            m.is_primary = False
    media = models.ProductMedia(
        product_id=product.id, url=payload.url,
        media_type=models.MediaType(payload.media_type),
        alt_text=payload.alt_text, sort_order=payload.sort_order,
        is_primary=payload.is_primary,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return schemas.ProductMediaOut(
        id=media.id, url=media.url, media_type=media.media_type.value,
        alt_text=media.alt_text or "", sort_order=media.sort_order or 0,
        is_primary=bool(media.is_primary),
    )


@router.post("/{product_id}/media/upload", response_model=schemas.ProductMediaOut, status_code=201)
async def upload_media(
    product_id: str,
    file: UploadFile = File(...),
    alt_text: str = Form(""),
    is_primary: bool = Form(False),
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Upload a real image or video file.

    The client's filename is discarded — a UUID name is generated — and the
    declared content type must agree with the file's magic bytes.
    """
    product = _get_or_404(product_id, db)

    head = await file.read(32)
    try:
        kind, ext, cap = validate_and_classify(file.content_type, head)
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await file.seek(0)
    try:
        storage_key, size = get_storage().save(file.file, ext, cap)
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    media_type = models.MediaType.image if kind == "image" else models.MediaType.video
    # First image uploaded becomes primary automatically.
    make_primary = is_primary or (
        media_type == models.MediaType.image
        and not any(m.media_type == models.MediaType.image for m in product.media)
    )
    if make_primary:
        for m in product.media:
            m.is_primary = False

    media = models.ProductMedia(
        product_id=product.id,
        url=get_storage().url_for(storage_key),
        media_type=media_type,
        alt_text=alt_text or product.name,
        sort_order=len(product.media),
        is_primary=make_primary and media_type == models.MediaType.image,
        storage_key=storage_key,
        content_type=file.content_type,
        file_size=size,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return schemas.ProductMediaOut(
        id=media.id, url=media.url, media_type=media.media_type.value,
        alt_text=media.alt_text or "", sort_order=media.sort_order or 0,
        is_primary=bool(media.is_primary), content_type=media.content_type,
        file_size=media.file_size,
    )


@router.post("/{product_id}/media/reorder", response_model=List[schemas.ProductMediaOut])
def reorder_media(
    product_id: str,
    payload: schemas.ProductMediaReorder,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    product = _get_or_404(product_id, db)
    by_id = {m.id: m for m in product.media}
    for entry in payload.order:
        m = by_id.get(entry.get("id"))
        if m:
            m.sort_order = int(entry.get("sort_order", m.sort_order or 0))
    if payload.primary_id:
        if payload.primary_id not in by_id:
            raise HTTPException(status_code=400, detail="primary_id does not belong to this product")
        for m in product.media:
            m.is_primary = (m.id == payload.primary_id)
    db.commit()
    db.refresh(product)
    return [schemas.ProductMediaOut(
        id=m.id, url=m.url, media_type=m.media_type.value, alt_text=m.alt_text or "",
        sort_order=m.sort_order or 0, is_primary=bool(m.is_primary),
        content_type=m.content_type, file_size=m.file_size,
    ) for m in product.media]


@router.delete("/media/{media_id}", status_code=204)
def delete_media(
    media_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    media = db.query(models.ProductMedia).filter(models.ProductMedia.id == media_id).first()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    was_primary = media.is_primary
    product_id = media.product_id
    if media.storage_key:
        get_storage().delete(media.storage_key)
    db.delete(media)
    db.flush()
    # Promote another image so a product never loses its primary silently.
    if was_primary:
        nxt = db.query(models.ProductMedia).filter(
            models.ProductMedia.product_id == product_id,
            models.ProductMedia.media_type == models.MediaType.image,
        ).order_by(models.ProductMedia.sort_order.asc()).first()
        if nxt:
            nxt.is_primary = True
    db.commit()
    return None
