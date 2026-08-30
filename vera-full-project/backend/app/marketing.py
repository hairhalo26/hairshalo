"""Marketing list and campaigns.

Non-negotiable rule this module exists to enforce:

    Nobody receives marketing they did not ask for.

Concretely, and each of these is a decision that could have gone the other way:

* **Double opt-in.** Typing an address into a form creates a `pending`
  subscriber and sends one confirmation email. Only clicking the signed link in
  that email makes the address `confirmed`. Anyone can type someone else's
  address into a form; only the mailbox owner can click the link.
* **Buying is not subscribing.** Checkout creates a Customer, never a
  subscriber. There is deliberately no endpoint anywhere that mails "all
  customers" — a campaign can only resolve recipients from confirmed
  subscribers.
* **Campaigns go through the notification outbox**, so suppression, hard
  bounces, unsubscribes and idempotency all apply without this module
  reimplementing any of them.
* **Every message carries an unsubscribe link**, and unsubscribing is honoured
  in one place (the suppression list from Phase 6), so it applies to future
  campaigns even if this table is edited by hand.
"""
import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app import models, notifications as notify
from app.config import settings

logger = logging.getLogger("vera.marketing")


class MarketingError(Exception):
    """Rejected subscription or send. The message is shown to the caller."""


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def find(db: Session, email: str) -> Optional[models.MarketingSubscriber]:
    address = normalise(email)
    if not address:
        return None
    return db.query(models.MarketingSubscriber).filter(
        models.MarketingSubscriber.email == address
    ).first()


def confirm_url(email: str) -> str:
    """Signed confirmation link. Reuses the notification token scheme, so a
    token cannot be forged for an address the requester does not control."""
    token = notify.unsubscribe_token(normalise(email))
    return f"{settings.STOREFRONT_URL.rstrip('/')}/confirm-subscription?token={token}"


def subscribe(db: Session, email: str, *, name: str = None, source: str = None,
              consent_ip: str = None) -> Tuple[models.MarketingSubscriber, bool]:
    """Record a subscription request and queue the confirmation email.

    Returns (subscriber, confirmation_queued). Re-subscribing an address that is
    already confirmed is a no-op rather than an error — repeating a request that
    is already satisfied should not look like a failure, and telling the caller
    "that address is already subscribed" would leak who is on the list.
    """
    address = normalise(email)
    if not address or "@" not in address:
        raise MarketingError("That does not look like an email address.")

    # A hard bounce or complaint outranks a new sign-up: re-adding an address
    # that a mailbox provider has already rejected is how a sending domain dies.
    suppression = notify.suppression_for(db, address)
    if suppression and suppression.scope == models.SuppressionScope.all:
        raise MarketingError(
            "We cannot add that address. Please contact us if this is unexpected."
        )

    subscriber = find(db, address)
    if subscriber and subscriber.status == models.SubscriberStatus.confirmed:
        return (subscriber, False)

    if not subscriber:
        subscriber = models.MarketingSubscriber(
            email=address, name=(name or "").strip() or None,
            status=models.SubscriberStatus.pending, source=source,
            consent_ip=consent_ip,
        )
        db.add(subscriber)
        db.flush()
    else:
        # Someone re-requesting after unsubscribing starts the consent trail
        # again from scratch, rather than being silently reactivated.
        subscriber.status = models.SubscriberStatus.pending
        subscriber.requested_at = datetime.utcnow()
        subscriber.unsubscribed_at = None
        subscriber.consent_ip = consent_ip or subscriber.consent_ip
        if name:
            subscriber.name = name.strip()

    # The confirmation itself is transactional: it is the direct answer to an
    # action this person just took, and it is the only thing sent before consent
    # exists. Keyed on the request time so a second request re-sends.
    notify.enqueue(
        db, "marketing.confirm", address,
        {
            "customer_name": subscriber.name or "there",
            "confirm_url": confirm_url(address),
            "email": address,
        },
        event_key=f"marketing.confirm:{address}:{subscriber.requested_at.isoformat()}",
        category=models.NotificationCategory.transactional,
        recipient_name=subscriber.name,
        reference_type="subscriber", reference_id=subscriber.id,
    )
    return (subscriber, True)


def confirm(db: Session, token: str) -> models.MarketingSubscriber:
    """Turn a signed token into consent."""
    email = notify.verify_unsubscribe_token(token)
    if not email:
        raise MarketingError("That confirmation link is not valid or has expired.")

    subscriber = find(db, email)
    if not subscriber:
        raise MarketingError("That confirmation link is not valid or has expired.")
    if subscriber.status == models.SubscriberStatus.confirmed:
        return subscriber

    subscriber.status = models.SubscriberStatus.confirmed
    subscriber.confirmed_at = datetime.utcnow()
    subscriber.unsubscribed_at = None

    # Confirming re-consent after an unsubscribe must also clear the marketing
    # suppression, or the confirmation would be quietly ignored at send time.
    suppression = notify.suppression_for(db, email)
    if suppression and suppression.scope == models.SuppressionScope.marketing:
        db.delete(suppression)
    return subscriber


def unsubscribe(db: Session, email: str, reason: str = "unsubscribe") -> Optional[models.MarketingSubscriber]:
    """Leave the list. Also writes the suppression, which is what send time
    actually checks — so an opt-out survives even if this row is edited."""
    address = normalise(email)
    subscriber = find(db, address)
    if subscriber:
        subscriber.status = models.SubscriberStatus.unsubscribed
        subscriber.unsubscribed_at = datetime.utcnow()
    notify.suppress(db, address, models.SuppressionScope.marketing, reason=reason)
    return subscriber


def confirmed_subscribers(db: Session):
    return db.query(models.MarketingSubscriber).filter(
        models.MarketingSubscriber.status == models.SubscriberStatus.confirmed
    ).order_by(models.MarketingSubscriber.confirmed_at.asc()).all()


def audience_size(db: Session) -> int:
    return db.query(models.MarketingSubscriber).filter(
        models.MarketingSubscriber.status == models.SubscriberStatus.confirmed
    ).count()


def send_campaign(db: Session, campaign: models.Campaign, actor: str = None) -> dict:
    """Queue a campaign to every confirmed subscriber. Caller commits.

    Recipients are resolved here and nowhere else: there is no argument for
    "send to these addresses", so a campaign cannot be pointed at the customer
    table or at a pasted list.
    """
    if campaign.status == models.CampaignStatus.sent:
        raise MarketingError("That campaign has already been sent.")
    if campaign.status == models.CampaignStatus.cancelled:
        raise MarketingError("That campaign was cancelled.")

    queued, skipped = 0, 0
    for subscriber in confirmed_subscribers(db):
        row = notify.enqueue(
            db, "marketing.campaign", subscriber.email,
            {
                "customer_name": subscriber.name or "there",
                "subject": campaign.subject,
                "preheader": campaign.preheader,
                "body": campaign.body,
                "cta_label": campaign.cta_label,
                "cta_url": campaign.cta_url,
                "unsubscribe_url": notify.unsubscribe_url(subscriber.email),
            },
            event_key=f"marketing.campaign:{campaign.id}:{subscriber.email}",
            category=models.NotificationCategory.marketing,
            recipient_name=subscriber.name,
            reference_type="campaign", reference_id=campaign.id,
        )
        if row and row.status == models.NotificationStatus.queued:
            queued += 1
            subscriber.last_campaign_at = datetime.utcnow()
        else:
            # Suppressed, or already queued for this campaign.
            skipped += 1

    campaign.status = models.CampaignStatus.sent
    campaign.sent_at = datetime.utcnow()
    campaign.recipient_count = queued
    campaign.skipped_count = skipped
    logger.info("Campaign %s queued to %s subscribers (%s skipped)",
                campaign.name, queued, skipped, extra={"campaign_id": campaign.id})
    return {"queued": queued, "skipped": skipped}
