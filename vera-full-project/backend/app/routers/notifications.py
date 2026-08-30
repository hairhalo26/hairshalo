"""Notification endpoints — the queue, its dead letters, and opt-outs.

Everything that mutates is admin-only, with one deliberate exception: an
unsubscribe carries its own HMAC token instead of a login, because the person
clicking it is a customer reading an email, not a user of the dashboard.

Route order matters here — the literal paths are declared before
`/{notification_id}`, otherwise "config" would be read as an id.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import models, notifications as notify, schemas
from app.config import settings
from app.database import get_db
from app.deps import get_current_admin

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _out(row: models.Notification, detail: bool = False):
    payload = dict(
        id=row.id, event_key=row.event_key, event_type=row.event_type,
        channel=row.channel.value, category=row.category.value,
        status=row.status.value, recipient=row.recipient,
        recipient_name=row.recipient_name, subject=row.subject,
        reference_type=row.reference_type, reference_id=row.reference_id,
        attempts=row.attempts or 0, max_attempts=row.max_attempts or 0,
        attempts_remaining=row.attempts_remaining,
        next_attempt_at=row.next_attempt_at, last_error=row.last_error,
        provider=row.provider, provider_message_id=row.provider_message_id,
        sent_at=row.sent_at, created_at=row.created_at,
    )
    if detail:
        return schemas.NotificationDetailOut(
            **payload, body_text=row.body_text, body_html=row.body_html)
    return schemas.NotificationOut(**payload)


# ---------------------------------------------------------------- config


@router.get("/config", response_model=schemas.NotificationConfigOut)
def notification_config(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """What is configured, and what about it is a production gap."""
    channel = notify.get_channel()
    warnings = []
    if channel.name == "console":
        warnings.append(
            "NOTIFY_CHANNEL=console — messages are queued and logged, never delivered. "
            "Set NOTIFY_CHANNEL=smtp with SMTP_* credentials to send real email."
        )
    if channel.name == "smtp" and not settings.SMTP_HOST:
        warnings.append("NOTIFY_CHANNEL=smtp but SMTP_HOST is empty; sending will fail.")
    if channel.name == "null":
        warnings.append("NOTIFY_CHANNEL=null — every message is recorded and suppressed.")
    if not settings.ADMIN_ALERT_EMAILS:
        warnings.append(
            "ADMIN_ALERT_EMAILS is empty — nobody is alerted about new orders, "
            "failed payments or low stock."
        )
    if settings.NOTIFY_DISPATCH == "worker":
        warnings.append(
            "NOTIFY_DISPATCH=worker — nothing sends until a worker calls "
            "POST /api/notifications/dispatch or runs `python -m app.notify_worker`."
        )

    counts = dict(
        queued=db.query(models.Notification).filter(
            models.Notification.status == models.NotificationStatus.queued).count(),
        failed=db.query(models.Notification).filter(
            models.Notification.status == models.NotificationStatus.failed).count(),
    )
    return schemas.NotificationConfigOut(
        channel=channel.name,
        sends_real_mail=channel.sends_real_mail,
        dispatch_mode=settings.NOTIFY_DISPATCH,
        mail_from=settings.MAIL_FROM,
        mail_from_name=settings.MAIL_FROM_NAME,
        reply_to=settings.MAIL_REPLY_TO or None,
        admin_alert_emails=settings.ADMIN_ALERT_EMAILS,
        max_attempts=settings.NOTIFY_MAX_ATTEMPTS,
        batch_size=settings.NOTIFY_BATCH_SIZE,
        low_stock_threshold=settings.LOW_STOCK_ALERT_THRESHOLD,
        suppressed_addresses=db.query(models.NotificationSuppression).count(),
        warnings=warnings,
        **counts,
    )


# ---------------------------------------------------------------- dispatch


@router.post("/dispatch", response_model=schemas.NotificationDispatchResult)
def dispatch_now(limit: Optional[int] = Query(None, ge=1, le=500),
                 db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Drain the queue now. This is also the endpoint a cron worker calls."""
    return schemas.NotificationDispatchResult(**notify.dispatch_pending(db, limit))


@router.post("/test", response_model=schemas.NotificationOut, status_code=201)
def send_test(payload: schemas.NotificationTestRequest,
              db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Queue and immediately send a test message — proves the channel works.

    Uniquely keyed per address per admin action, so it can be run repeatedly.
    """
    channel = notify.get_channel()
    row = notify.enqueue(
        db, "notification.test", str(payload.to),
        {"channel": channel.name, "mail_from": settings.MAIL_FROM},
        event_key=f"notification.test:{payload.to}:{datetime.utcnow().isoformat()}",
        category=models.NotificationCategory.operational,
    )
    if not row:
        raise HTTPException(status_code=400, detail="Could not queue the test message.")
    db.commit()
    notify.dispatch_pending(db, limit=5)
    db.refresh(row)
    return _out(row)


# ---------------------------------------------------------------- opt-out


@router.get("/unsubscribe", response_model=schemas.UnsubscribeResult)
def unsubscribe_preview(token: str):
    """Show which address a token belongs to WITHOUT acting on it.

    Deliberately read-only: mail clients and security scanners prefetch links
    in emails, and a GET that unsubscribed people would fire on its own.
    """
    email = notify.verify_unsubscribe_token(token)
    if not email:
        raise HTTPException(status_code=400, detail="This unsubscribe link is not valid.")
    return schemas.UnsubscribeResult(
        email=email, unsubscribed=False,
        message="Confirm to stop receiving offers at this address.",
    )


@router.post("/unsubscribe", response_model=schemas.UnsubscribeResult)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    """Act on a signed opt-out token. No login — the token is the authority.

    Only marketing is silenced. Order receipts and delivery updates continue,
    because they are a record of a transaction the customer entered into.
    """
    email = notify.verify_unsubscribe_token(token)
    if not email:
        raise HTTPException(status_code=400, detail="This unsubscribe link is not valid.")
    notify.suppress(db, email, models.SuppressionScope.marketing, reason="unsubscribe")
    db.commit()
    return schemas.UnsubscribeResult(
        email=email, unsubscribed=True,
        message="You will no longer receive offers. Order and delivery emails continue.",
    )


# ---------------------------------------------------------------- suppressions


@router.get("/suppressions", response_model=List[schemas.SuppressionOut])
def list_suppressions(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    rows = db.query(models.NotificationSuppression).order_by(
        models.NotificationSuppression.created_at.desc()).all()
    return [
        schemas.SuppressionOut(
            id=r.id, email=r.email, scope=r.scope.value, reason=r.reason,
            note=r.note, created_at=r.created_at)
        for r in rows
    ]


@router.post("/suppressions", response_model=schemas.SuppressionOut, status_code=201)
def add_suppression(payload: schemas.SuppressionCreate,
                    db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Record a bounce or complaint. `scope=all` stops transactional mail too."""
    valid = [s.value for s in models.SuppressionScope]
    if payload.scope not in valid:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of: {', '.join(valid)}")
    row = notify.suppress(
        db, str(payload.email), models.SuppressionScope(payload.scope),
        reason=payload.reason or "manual",
        note=payload.note or f"Added by {admin.email}",
    )
    db.commit()
    db.refresh(row)
    return schemas.SuppressionOut(
        id=row.id, email=row.email, scope=row.scope.value, reason=row.reason,
        note=row.note, created_at=row.created_at)


@router.delete("/suppressions/{email}", status_code=204)
def remove_suppression(email: str, db: Session = Depends(get_db),
                       _admin=Depends(get_current_admin)):
    row = notify.suppression_for(db, email)
    if not row:
        raise HTTPException(status_code=404, detail="That address is not suppressed.")
    db.delete(row)
    db.commit()


# ---------------------------------------------------------------- queue


@router.get("", response_model=List[schemas.NotificationOut])
def list_notifications(
    status: Optional[str] = None,
    event_type: Optional[str] = None,
    category: Optional[str] = None,
    recipient: Optional[str] = None,
    reference_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    q = db.query(models.Notification)
    if status:
        valid = [s.value for s in models.NotificationStatus]
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"status must be one of: {', '.join(valid)}")
        q = q.filter(models.Notification.status == models.NotificationStatus(status))
    if category:
        valid = [c.value for c in models.NotificationCategory]
        if category not in valid:
            raise HTTPException(status_code=400,
                                detail=f"category must be one of: {', '.join(valid)}")
        q = q.filter(models.Notification.category == models.NotificationCategory(category))
    if event_type:
        q = q.filter(models.Notification.event_type == event_type)
    if recipient:
        q = q.filter(models.Notification.recipient == recipient)
    if reference_id:
        q = q.filter(models.Notification.reference_id == reference_id)
    rows = (q.order_by(models.Notification.created_at.desc())
            .offset(offset).limit(limit).all())
    return [_out(r) for r in rows]


@router.get("/{notification_id}", response_model=schemas.NotificationDetailOut)
def get_notification(notification_id: str, db: Session = Depends(get_db),
                     _admin=Depends(get_current_admin)):
    """The full message, exactly as it was sent — support's answer to
    "what did the customer actually receive?"."""
    row = db.query(models.Notification).filter(
        models.Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _out(row, detail=True)


@router.post("/{notification_id}/retry", response_model=schemas.NotificationOut)
def retry_notification(notification_id: str, background: BackgroundTasks,
                       db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Put a dead letter back on the queue.

    The stored body is reused rather than re-rendered: the customer must
    receive the message that was composed for the event, not one composed from
    today's data.
    """
    row = db.query(models.Notification).filter(
        models.Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.status == models.NotificationStatus.sent:
        raise HTTPException(status_code=400,
                            detail="That message was already sent; retrying would duplicate it.")
    if row.status == models.NotificationStatus.suppressed and \
            notify.is_suppressed(db, row.recipient, row.category):
        raise HTTPException(
            status_code=400,
            detail="That recipient is suppressed. Remove the suppression first.")

    row.status = models.NotificationStatus.queued
    row.attempts = 0
    row.next_attempt_at = datetime.utcnow()
    db.commit()
    notify.schedule_dispatch(background)
    db.refresh(row)
    return _out(row)


@router.post("/{notification_id}/cancel", response_model=schemas.NotificationOut)
def cancel_notification(notification_id: str, db: Session = Depends(get_db),
                        _admin=Depends(get_current_admin)):
    row = db.query(models.Notification).filter(
        models.Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.status == models.NotificationStatus.sent:
        raise HTTPException(status_code=400, detail="That message has already been sent.")
    row.status = models.NotificationStatus.cancelled
    db.commit()
    db.refresh(row)
    return _out(row)
