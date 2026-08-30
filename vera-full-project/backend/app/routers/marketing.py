"""Marketing endpoints — the list, its consent trail, and campaigns.

The public surface is deliberately small: subscribe, confirm, unsubscribe. All
three answer the same way whether or not an address is already on the list,
because a different answer would let anyone test which addresses are.

There is no endpoint that sends to customers, to an uploaded list, or to
anything other than confirmed subscribers.
"""
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import marketing as service, models, notifications as notify, schemas
from app.database import get_db
from app.deps import get_current_admin
from app.middleware import client_ip

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


def _campaign_out(c: models.Campaign) -> schemas.CampaignOut:
    return schemas.CampaignOut(
        id=c.id, name=c.name, subject=c.subject, preheader=c.preheader,
        body=c.body, cta_label=c.cta_label, cta_url=c.cta_url,
        status=c.status.value, recipient_count=c.recipient_count or 0,
        skipped_count=c.skipped_count or 0, sent_at=c.sent_at,
        created_by=c.created_by, created_at=c.created_at,
    )


def _subscriber_out(s: models.MarketingSubscriber) -> schemas.SubscriberOut:
    return schemas.SubscriberOut(
        id=s.id, email=s.email, name=s.name, status=s.status.value,
        source=s.source, requested_at=s.requested_at, confirmed_at=s.confirmed_at,
        unsubscribed_at=s.unsubscribed_at, last_campaign_at=s.last_campaign_at,
    )


# ---------------------------------------------------------------- public


@router.post("/subscribe", response_model=schemas.SubscribeResult, status_code=202)
def subscribe(payload: schemas.SubscribeRequest, request: Request,
              background: BackgroundTasks, db: Session = Depends(get_db)):
    """Ask to join the list. Sends one confirmation email; nothing more.

    The response is identical whether the address is new, pending or already
    confirmed — otherwise this endpoint would report who is on the list.
    """
    try:
        service.subscribe(
            db, str(payload.email), name=payload.name,
            source=payload.source or "storefront",
            consent_ip=client_ip(request),
        )
    except service.MarketingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    notify.schedule_dispatch(background)
    return schemas.SubscribeResult(
        status="pending",
        message=("Check your inbox — we have sent a confirmation link. "
                 "You will not receive anything else until you confirm."),
    )


@router.post("/confirm", response_model=schemas.SubscribeResult)
def confirm(token: str, db: Session = Depends(get_db)):
    """Turn the signed link into consent. This is the only path to `confirmed`."""
    try:
        subscriber = service.confirm(db, token)
    except service.MarketingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return schemas.SubscribeResult(
        status=subscriber.status.value.lower(),
        message="You are subscribed. Every email we send has an unsubscribe link.",
    )


@router.post("/unsubscribe", response_model=schemas.SubscribeResult)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    """Leave the list. Also writes the suppression that send time checks."""
    email = notify.verify_unsubscribe_token(token)
    if not email:
        raise HTTPException(status_code=400, detail="That link is not valid.")
    service.unsubscribe(db, email)
    db.commit()
    return schemas.SubscribeResult(
        status="unsubscribed",
        message="You will no longer receive offers. Order emails continue.",
    )


# ---------------------------------------------------------------- admin


@router.get("/subscribers", response_model=List[schemas.SubscriberOut])
def list_subscribers(status: Optional[str] = None,
                     limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    q = db.query(models.MarketingSubscriber)
    if status:
        valid = [s.value for s in models.SubscriberStatus]
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"status must be one of: {', '.join(valid)}")
        q = q.filter(models.MarketingSubscriber.status == models.SubscriberStatus(status))
    rows = (q.order_by(models.MarketingSubscriber.requested_at.desc())
            .offset(offset).limit(limit).all())
    return [_subscriber_out(s) for s in rows]


@router.get("/audience")
def audience(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """How many people a campaign would actually reach, and why the rest cannot.

    Stated as a breakdown rather than a single number, because "we have 900
    subscribers" is the sentence that hides 700 unconfirmed addresses.
    """
    counts = {
        status.value: db.query(models.MarketingSubscriber).filter(
            models.MarketingSubscriber.status == status).count()
        for status in models.SubscriberStatus
    }
    return {
        "mailable": counts.get(models.SubscriberStatus.confirmed.value, 0),
        "awaiting_confirmation": counts.get(models.SubscriberStatus.pending.value, 0),
        "unsubscribed": counts.get(models.SubscriberStatus.unsubscribed.value, 0),
        "suppressed_addresses": db.query(models.NotificationSuppression).count(),
        "note": ("Only confirmed subscribers can be mailed. Customers are not "
                 "subscribers: buying something is not consent to marketing."),
    }


@router.get("/campaigns", response_model=List[schemas.CampaignOut])
def list_campaigns(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    rows = db.query(models.Campaign).order_by(models.Campaign.created_at.desc()).all()
    return [_campaign_out(c) for c in rows]


@router.post("/campaigns", response_model=schemas.CampaignOut, status_code=201)
def create_campaign(payload: schemas.CampaignCreate, db: Session = Depends(get_db),
                    admin=Depends(get_current_admin)):
    """Draft a campaign. Creating one sends nothing."""
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="A campaign needs a body.")
    campaign = models.Campaign(
        name=payload.name.strip(), subject=payload.subject.strip(),
        preheader=payload.preheader, body=payload.body.strip(),
        cta_label=payload.cta_label, cta_url=payload.cta_url,
        status=models.CampaignStatus.draft, created_by=admin.email,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_out(campaign)


@router.post("/campaigns/{campaign_id}/send", response_model=schemas.CampaignSendResult)
def send_campaign(campaign_id: str, background: BackgroundTasks,
                  db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Queue the campaign to every confirmed subscriber.

    Recipients are resolved inside the service; this endpoint takes no
    recipient argument, by design.
    """
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        result = service.send_campaign(db, campaign, actor=admin.email)
    except service.MarketingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(campaign)
    notify.schedule_dispatch(background)
    return schemas.CampaignSendResult(
        campaign=_campaign_out(campaign), queued=result["queued"],
        skipped=result["skipped"], audience=service.audience_size(db),
    )


@router.post("/campaigns/{campaign_id}/cancel", response_model=schemas.CampaignOut)
def cancel_campaign(campaign_id: str, db: Session = Depends(get_db),
                    _admin=Depends(get_current_admin)):
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status == models.CampaignStatus.sent:
        raise HTTPException(status_code=400,
                            detail="That campaign has already been sent.")
    campaign.status = models.CampaignStatus.cancelled
    db.commit()
    db.refresh(campaign)
    return _campaign_out(campaign)
