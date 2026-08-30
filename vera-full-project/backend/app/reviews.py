"""Product reviews, and the ratings derived from them.

Non-negotiable rule this module exists to enforce:

    A rating is never manufactured. Every star traces to a review a real
    customer wrote about something they actually bought.

Which is why:

* `Product.rating` and `Product.review_count` are written **only** by
  `recalculate()`, from `published` reviews. Nothing else in the codebase
  assigns to them — the same contract `app/inventory.py` has with
  `ProductVariant.stock`. Before this module existed those columns were seeded
  with invented numbers (4.9 from 312 reviews that did not exist).
* A review may only be submitted by someone whose order actually contains the
  product, and only once that order has been delivered. The `verified purchase`
  label is therefore a fact about the data, not a badge a form can set.
* Reviews are moderated before they count, so nothing reaches a rating that
  nobody has read.
"""
import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger("vera.reviews")

MIN_RATING, MAX_RATING = 1, 5
MAX_BODY = 4000
MAX_TITLE = 120

#: Reviews may only be written once the order has actually arrived. Reviewing a
#: product that has not been delivered is not a verified purchase, it is an
#: opinion about a photograph.
REVIEWABLE_ORDER_STATUSES = {models.OrderStatus.delivered}


class ReviewError(Exception):
    """Rejected review. The message is shown to the customer."""


def recalculate(db: Session, product_id: str) -> Tuple[float, int]:
    """Recompute a product's rating from its PUBLISHED reviews.

    The single writer of `Product.rating` / `Product.review_count`. Called after
    every moderation decision, so the cached aggregate can never drift from the
    rows behind it. A product with no published reviews gets 0.0/0 — which the
    storefront renders as "no reviews yet", not as a zero-star rating.
    """
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        return (0.0, 0)

    average, count = db.query(
        func.avg(models.Review.rating), func.count(models.Review.id)
    ).filter(
        models.Review.product_id == product_id,
        models.Review.status == models.ReviewStatus.published,
    ).one()

    product.review_count = int(count or 0)
    # One decimal place: the underlying data is integers 1-5, and rendering
    # 4.6666666 implies a precision the sample size does not support.
    product.rating = round(float(average), 1) if count else 0.0
    return (product.rating, product.review_count)


def _validate(rating: int, body: str, title: str) -> None:
    if rating is None or not isinstance(rating, int):
        raise ReviewError("A star rating is required.")
    if rating < MIN_RATING or rating > MAX_RATING:
        raise ReviewError(f"A rating must be between {MIN_RATING} and {MAX_RATING} stars.")
    if title and len(title) > MAX_TITLE:
        raise ReviewError(f"Keep the title under {MAX_TITLE} characters.")
    if body and len(body) > MAX_BODY:
        raise ReviewError(f"Keep the review under {MAX_BODY} characters.")


def find_purchase(db: Session, order_number: str, email: str,
                  product_id: str) -> Tuple[models.Order, models.OrderItem]:
    """Locate the order line that entitles this person to review this product.

    The order number and the email must agree, and the order must contain the
    product. Every failure returns the SAME message: a distinct "no such order"
    would turn this endpoint into an order-number oracle.
    """
    generic = ReviewError(
        "We could not match that order number and email to a delivered order "
        "containing this product."
    )
    if not order_number or not email:
        raise generic

    order = db.query(models.Order).filter(
        models.Order.order_number == order_number.strip().upper()
    ).first()
    if not order:
        raise generic
    if (order.customer_email or "").lower() != email.strip().lower():
        raise generic
    if order.status not in REVIEWABLE_ORDER_STATUSES:
        raise ReviewError(
            "You can leave a review once your order has been delivered."
        )

    line = next((i for i in order.items if i.product_id == product_id), None)
    if not line:
        raise generic
    return order, line


def submit(db: Session, *, product_id: str, order_number: str, email: str,
           rating: int, title: str = None, body: str = "",
           author_name: str = None) -> models.Review:
    """Create a pending, verified review. Caller commits."""
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise ReviewError("That product could not be found.")
    _validate(rating, body, title)

    order, line = find_purchase(db, order_number, email, product_id)

    # One review per order line. Someone who bought the same wig twice may
    # review it twice; someone who bought it once may not review it twice.
    existing = db.query(models.Review).filter(
        models.Review.order_item_id == line.id
    ).first()
    if existing:
        raise ReviewError("You have already reviewed this item from this order.")

    review = models.Review(
        product_id=product_id,
        order_id=order.id,
        order_item_id=line.id,
        customer_id=order.customer_id,
        author_name=(author_name or order.customer_name or "").strip() or "Customer",
        author_email=order.customer_email,
        rating=rating,
        title=(title or "").strip() or None,
        body=(body or "").strip(),
        status=models.ReviewStatus.pending,
        is_verified_purchase=True,
    )
    db.add(review)
    db.flush()
    return review


def moderate(db: Session, review: models.Review, status: models.ReviewStatus,
             actor: str, note: str = None) -> models.Review:
    """Publish or reject a review, then refresh the product's rating."""
    review.status = status
    review.moderated_by = actor
    review.moderated_at = datetime.utcnow()
    review.moderation_note = note
    db.flush()
    recalculate(db, review.product_id)
    return review


def reply(db: Session, review: models.Review, body: str) -> models.Review:
    review.reply_body = (body or "").strip() or None
    review.reply_at = datetime.utcnow() if review.reply_body else None
    return review


def summary_for(db: Session, product_id: str) -> dict:
    """Rating breakdown for a product page: average, count, stars 1-5.

    Computed from published reviews at read time — a histogram is small, and a
    second cached aggregate would be a second thing that can drift.
    """
    rows = db.query(models.Review.rating, func.count(models.Review.id)).filter(
        models.Review.product_id == product_id,
        models.Review.status == models.ReviewStatus.published,
    ).group_by(models.Review.rating).all()

    breakdown = {star: 0 for star in range(MIN_RATING, MAX_RATING + 1)}
    total, weighted = 0, 0
    for stars, count in rows:
        breakdown[int(stars)] = int(count)
        total += int(count)
        weighted += int(stars) * int(count)
    return {
        "product_id": product_id,
        "average": round(weighted / total, 1) if total else 0.0,
        "count": total,
        "breakdown": breakdown,
        "verified_count": db.query(func.count(models.Review.id)).filter(
            models.Review.product_id == product_id,
            models.Review.status == models.ReviewStatus.published,
            models.Review.is_verified_purchase.is_(True),
        ).scalar() or 0,
    }


def reviewable_items(db: Session, order_number: str, email: str) -> list:
    """What a customer may review, given their order — for a "review your
    purchase" link in the delivered-order email."""
    order = db.query(models.Order).filter(
        models.Order.order_number == (order_number or "").strip().upper()
    ).first()
    if not order or (order.customer_email or "").lower() != (email or "").strip().lower():
        return []
    if order.status not in REVIEWABLE_ORDER_STATUSES:
        return []
    reviewed = {
        r.order_item_id for r in db.query(models.Review).filter(
            models.Review.order_id == order.id).all()
    }
    return [
        {"product_id": line.product_id, "product_name": line.product_name,
         "variant_label": line.variant_label, "order_item_id": line.id}
        for line in order.items
        if line.product_id and line.id not in reviewed
    ]
