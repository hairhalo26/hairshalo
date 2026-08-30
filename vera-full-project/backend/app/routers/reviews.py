"""Review endpoints.

Public callers can read published reviews and submit one for something they
bought. Everything else — seeing pending reviews, publishing, rejecting,
replying — is admin-only.

Nothing here lets a caller state that a review is verified, or set a rating on
a product directly. Both are derived: see app/reviews.py.
"""
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app import models, notifications as notify, reviews as service, schemas
from app.config import settings
from app.database import get_db
from app.deps import get_current_admin

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


def _public_out(review: models.Review) -> schemas.ReviewOut:
    return schemas.ReviewOut(
        id=review.id, product_id=review.product_id,
        author=review.author_display,          # never the raw name or address
        rating=review.rating, title=review.title, body=review.body or "",
        is_verified_purchase=review.is_verified_purchase,
        created_at=review.created_at,
        reply=(schemas.ReviewReplyOut(body=review.reply_body, replied_at=review.reply_at)
               if review.reply_body and review.reply_at else None),
    )


def _admin_out(review: models.Review) -> schemas.ReviewAdminOut:
    return schemas.ReviewAdminOut(
        **_public_out(review).model_dump(),
        author_name=review.author_name,
        author_email=review.author_email,
        status=review.status.value,
        order_id=review.order_id,
        product_name=review.product.name if review.product else None,
        moderated_by=review.moderated_by,
        moderated_at=review.moderated_at,
        moderation_note=review.moderation_note,
    )


# ---------------------------------------------------------------- public


@router.get("/product/{product_id}", response_model=List[schemas.ReviewOut])
def reviews_for_product(product_id: str, limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """Published reviews only — pending ones are invisible until moderated."""
    rows = (
        db.query(models.Review)
        .filter(models.Review.product_id == product_id,
                models.Review.status == models.ReviewStatus.published)
        .order_by(models.Review.created_at.desc())
        .offset(offset).limit(limit).all()
    )
    return [_public_out(r) for r in rows]


@router.get("/product/{product_id}/summary", response_model=schemas.ReviewSummaryOut)
def review_summary(product_id: str, db: Session = Depends(get_db)):
    """Average, count and the 1-5 histogram, computed from published reviews."""
    return schemas.ReviewSummaryOut(**service.summary_for(db, product_id))


@router.get("/recent", response_model=List[schemas.ReviewOut])
def recent_reviews(limit: int = Query(8, ge=1, le=50),
                   min_rating: int = Query(1, ge=1, le=5),
                   verified_only: bool = False, db: Session = Depends(get_db)):
    """Published reviews across the catalog, newest first.

    This is what the storefront's testimonial strip reads. It used to be four
    hardcoded five-star quotes with invented names; now the section shows real
    reviews or nothing at all.

    `min_rating` exists because a shop may reasonably feature its best reviews —
    but it filters real ones rather than inventing flattering copy, and the
    product's own page always shows every published review.
    """
    rows = (
        db.query(models.Review)
        .options(joinedload(models.Review.product))
        .filter(models.Review.status == models.ReviewStatus.published,
                models.Review.rating >= min_rating)
    )
    if verified_only:
        rows = rows.filter(models.Review.is_verified_purchase.is_(True))
    return [_public_out(r) for r in
            rows.order_by(models.Review.created_at.desc()).limit(limit).all()]


@router.get("/reviewable", response_model=List[schemas.ReviewableItemOut])
def reviewable_items(order_number: str, email: str, db: Session = Depends(get_db)):
    """What this order still has left to review.

    Returns an empty list rather than an error for an unknown order: a distinct
    "no such order" would make this an order-number oracle.
    """
    return [schemas.ReviewableItemOut(**item)
            for item in service.reviewable_items(db, order_number, email)]


@router.post("", response_model=schemas.ReviewOut, status_code=201)
def submit_review(payload: schemas.ReviewCreate, background: BackgroundTasks,
                  db: Session = Depends(get_db)):
    """Submit a review for something you bought.

    The purchase is verified against the order; the review is held for
    moderation unless REVIEWS_REQUIRE_MODERATION is off.
    """
    try:
        review = service.submit(
            db, product_id=payload.product_id, order_number=payload.order_number,
            email=str(payload.email), rating=payload.rating, title=payload.title,
            body=payload.body, author_name=payload.author_name,
        )
    except service.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not settings.REVIEWS_REQUIRE_MODERATION:
        service.moderate(db, review, models.ReviewStatus.published, actor="auto-publish")
    else:
        notify.notify_admins(
            db, "admin.review_pending",
            {"product_name": review.product.name if review.product else "a product",
             "author": review.author_display, "rating": review.rating,
             "title": review.title, "body": review.body},
            key_suffix=review.id, reference_type="review", reference_id=review.id,
        )
    db.commit()
    db.refresh(review)
    notify.schedule_dispatch(background)
    return _public_out(review)


# ---------------------------------------------------------------- admin


@router.get("", response_model=List[schemas.ReviewAdminOut])
def list_reviews(status: Optional[str] = None, product_id: Optional[str] = None,
                 limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    q = db.query(models.Review).options(joinedload(models.Review.product))
    if status:
        valid = [s.value for s in models.ReviewStatus]
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"status must be one of: {', '.join(valid)}")
        q = q.filter(models.Review.status == models.ReviewStatus(status))
    if product_id:
        q = q.filter(models.Review.product_id == product_id)
    rows = q.order_by(models.Review.created_at.desc()).offset(offset).limit(limit).all()
    return [_admin_out(r) for r in rows]


@router.post("/{review_id}/moderate", response_model=schemas.ReviewAdminOut)
def moderate_review(review_id: str, payload: schemas.ReviewModerate,
                    db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Publish or reject a review. The product's rating is recomputed either way."""
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    valid = [s.value for s in models.ReviewStatus]
    if payload.status not in valid:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of: {', '.join(valid)}")

    service.moderate(db, review, models.ReviewStatus(payload.status),
                     actor=admin.email, note=payload.note)
    db.commit()
    db.refresh(review)
    return _admin_out(review)


@router.post("/{review_id}/reply", response_model=schemas.ReviewAdminOut)
def reply_to_review(review_id: str, payload: schemas.ReviewReplyIn,
                    db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Publish the shop's response underneath a review."""
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    service.reply(db, review, payload.body)
    db.commit()
    db.refresh(review)
    return _admin_out(review)


@router.delete("/{review_id}", status_code=204)
def delete_review(review_id: str, db: Session = Depends(get_db),
                  _admin=Depends(get_current_admin)):
    """Remove a review entirely (spam, or a customer's request to be forgotten)."""
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    product_id = review.product_id
    db.delete(review)
    db.flush()
    service.recalculate(db, product_id)
    db.commit()
