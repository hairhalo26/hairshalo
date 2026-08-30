"""Outbound notifications — a transactional outbox.

Non-negotiable rule this module exists to enforce:

    A notification is a CONSEQUENCE of a committed fact, never a cause of one —
    and it is never sent twice.

Which means, concretely:

* **Queue inside the transaction, send outside it.** `enqueue()` only ever does
  `db.add()` in the caller's session. If the checkout rolls back, the email
  goes with it — we can never tell a customer about an order that does not
  exist. Conversely nothing here performs I/O during the request, so an
  unreachable mail server cannot fail a checkout. Delivery happens afterwards,
  in `dispatch_pending()`, against its own session.

* **`event_key` is unique.** It is derived from the event ("order.shipped:<id>"),
  not from the moment it happened, so a retried request, a replayed webhook and
  a double-clicked admin button all collapse onto one row — the same idempotency
  argument as `provider_payment_id` in app/payments.py.

* **Enqueueing never raises.** A template bug must not take a checkout down
  with it. Failures here are logged and the business transaction continues.

Channels
--------
`console` — render and log; nothing leaves the machine. The default, so a fresh
            clone never mails a real customer by accident.
`smtp`    — real delivery via stdlib smtplib. No SDK, no third-party client.
`null`    — record the notification and mark it Suppressed. For environments
            that must generate no mail at all (load tests, staging clones).
"""
import base64
import hashlib
import hmac
import logging
import smtplib
import ssl
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from app import email_templates, models
from app.config import settings

logger = logging.getLogger("vera.notifications")


class NotificationError(Exception):
    """A delivery failure.

    `permanent=True` means retrying is pointless (the address is malformed, the
    server rejected the recipient). Those go straight to Failed instead of
    burning five attempts against a mailbox that will never exist.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# ---------------------------------------------------------------- channels


class Channel(ABC):
    name = "base"

    @abstractmethod
    def send(self, notification: models.Notification) -> str:
        """Deliver, or raise NotificationError. Returns a provider message id."""

    @property
    def sends_real_mail(self) -> bool:
        return False


def _console_safe(text: str) -> str:
    """Make text writable to whatever encoding the console actually uses.

    Bodies contain ₹ and typographic dashes. A Windows console is usually
    cp1252, where writing those raises UnicodeEncodeError — which would surface
    as a delivery failure for an email that rendered perfectly. Degrade the
    characters, not the outcome.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


class ConsoleChannel(Channel):
    """Renders to the log. Nothing leaves the machine."""
    name = "console"

    def send(self, notification: models.Notification) -> str:
        logger.info(
            "[console-mail] to=%s subject=%s\n%s\n---",
            notification.recipient,
            _console_safe(notification.subject),
            _console_safe(notification.body_text),
        )
        return make_msgid(domain="verahair.local")


class NullChannel(Channel):
    """Generates no mail at all — used to silence an environment deliberately."""
    name = "null"

    def send(self, notification: models.Notification) -> str:
        raise NotificationError("Notifications are disabled (NOTIFY_CHANNEL=null).",
                                permanent=True)


class SmtpChannel(Channel):
    """Real delivery over SMTP, using only the standard library.

    Connection failures are treated as temporary (retry later); a server that
    rejects the recipient outright is permanent.
    """
    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str,
                 security: str = "starttls", timeout: int = 20):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.security = (security or "none").lower()
        self.timeout = timeout

    @property
    def sends_real_mail(self) -> bool:
        return True

    def _build(self, notification: models.Notification) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = notification.subject
        msg["From"] = formataddr((settings.MAIL_FROM_NAME, settings.MAIL_FROM))
        msg["To"] = (formataddr((notification.recipient_name, notification.recipient))
                     if notification.recipient_name else notification.recipient)
        if settings.MAIL_REPLY_TO:
            msg["Reply-To"] = settings.MAIL_REPLY_TO
        msg["Message-ID"] = make_msgid()
        # Lets a mail client thread order emails together, and gives support a
        # handle back to the row in `notifications`.
        msg["X-Vera-Event"] = notification.event_type
        msg["X-Vera-Notification-Id"] = notification.id
        if notification.category == models.NotificationCategory.marketing:
            msg["List-Unsubscribe"] = f"<{unsubscribe_url(notification.recipient)}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(notification.body_text)
        if notification.body_html:
            msg.add_alternative(notification.body_html, subtype="html")
        return msg

    def _connect(self):
        if not self.host:
            raise NotificationError(
                "NOTIFY_CHANNEL=smtp but SMTP_HOST is not set.", permanent=True)
        try:
            if self.security == "ssl":
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                          context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.security == "starttls":
                    client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password)
            return client
        except smtplib.SMTPAuthenticationError as exc:
            # Bad credentials will not fix themselves on retry.
            raise NotificationError(f"SMTP authentication failed: {exc}", permanent=True)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotificationError(f"Could not reach the SMTP server: {exc}")

    def send(self, notification: models.Notification) -> str:
        msg = self._build(notification)
        client = self._connect()
        try:
            client.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise NotificationError(f"Recipient refused: {exc}", permanent=True)
        except smtplib.SMTPSenderRefused as exc:
            raise NotificationError(f"Sender refused: {exc}", permanent=True)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotificationError(f"SMTP delivery failed: {exc}")
        finally:
            try:
                client.quit()
            except Exception:      # noqa: BLE001 - closing must never mask the result
                pass
        return msg["Message-ID"]


_channel: Optional[Channel] = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        name = (settings.NOTIFY_CHANNEL or "console").lower()
        if name == "smtp":
            _channel = SmtpChannel(
                settings.SMTP_HOST, settings.SMTP_PORT, settings.SMTP_USERNAME,
                settings.SMTP_PASSWORD, settings.SMTP_SECURITY, settings.SMTP_TIMEOUT,
            )
        elif name == "null":
            _channel = NullChannel()
        else:
            _channel = ConsoleChannel()
    return _channel


def reset_channel() -> None:
    """Forget the cached channel — used by tests after changing settings."""
    global _channel
    _channel = None


# ---------------------------------------------------------------- suppression


def unsubscribe_token(email: str) -> str:
    """Signed, self-contained opt-out token — no database row to look up.

    HMAC over the address with the app secret, so a token cannot be forged for
    someone else's address and we never expose an enumerable id.
    """
    payload = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")
    sig = hmac.new(settings.SECRET_KEY.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_unsubscribe_token(token: str) -> Optional[str]:
    """Return the address the token is valid for, else None."""
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(settings.SECRET_KEY.encode(), payload.encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    padding = "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload + padding).decode()
    except Exception:              # noqa: BLE001 - a malformed token is just invalid
        return None


def unsubscribe_url(email: str) -> str:
    return f"{settings.STOREFRONT_URL.rstrip('/')}/unsubscribe?token={unsubscribe_token(email)}"


def suppression_for(db, email: str) -> Optional[models.NotificationSuppression]:
    if not email:
        return None
    return db.query(models.NotificationSuppression).filter(
        models.NotificationSuppression.email == email.lower()
    ).first()


def is_suppressed(db, email: str, category: models.NotificationCategory) -> bool:
    """Marketing honours every opt-out; transactional mail only stops on a hard
    bounce or complaint (scope=all), because it is a record of a transaction the
    customer entered into, not something they subscribed to."""
    row = suppression_for(db, email)
    if not row:
        return False
    if row.scope == models.SuppressionScope.all:
        return True
    return category == models.NotificationCategory.marketing


def suppress(db, email: str, scope: models.SuppressionScope,
             reason: str = None, note: str = None) -> models.NotificationSuppression:
    row = suppression_for(db, email)
    if row:
        # Never narrow an existing ban: a hard bounce outranks an unsubscribe.
        if scope == models.SuppressionScope.all:
            row.scope = scope
            row.reason = reason or row.reason
        return row
    row = models.NotificationSuppression(
        email=email.lower(), scope=scope, reason=reason, note=note)
    db.add(row)
    return row


# ---------------------------------------------------------------- enqueueing


def _recipients_for_admins() -> List[str]:
    return [e for e in settings.ADMIN_ALERT_EMAILS if e]


def _already_queued(db, event_key: str) -> bool:
    """Cheap pre-check for a duplicate.

    Only an optimisation — it cannot see a row another transaction has written
    but not committed. The UNIQUE index on `event_key` is the real guarantee,
    and `enqueue` handles losing that race.
    """
    return db.query(models.Notification.id).filter(
        models.Notification.event_key == event_key
    ).first() is not None


def enqueue(db, event_type: str, recipient: str, context: dict, *,
            event_key: str = None,
            category: models.NotificationCategory = models.NotificationCategory.transactional,
            recipient_name: str = None,
            reference_type: str = None,
            reference_id: str = None) -> Optional[models.Notification]:
    """Render and queue one message in the CALLER's transaction.

    Returns the row, or None when nothing was queued (duplicate event, no
    recipient, or a rendering failure). Never raises — a notification problem
    must not roll back the business change that caused it.
    """
    if not recipient:
        return None

    key = event_key or f"{event_type}:{reference_id or recipient}"

    if _already_queued(db, key):
        logger.debug("Notification %s already queued, skipping", key)
        return None

    try:
        rendered = email_templates.render(event_type, context)
    except Exception:              # noqa: BLE001 - a template bug is not a checkout bug
        logger.exception("Could not render notification '%s' for %s", event_type, recipient)
        return None

    status = models.NotificationStatus.queued
    last_error = None
    if is_suppressed(db, recipient, category):
        status = models.NotificationStatus.suppressed
        last_error = "Recipient is on the suppression list."

    row = models.Notification(
        event_key=key,
        event_type=event_type,
        channel=models.NotificationChannel.email,
        category=category,
        status=status,
        recipient=recipient,
        recipient_name=recipient_name,
        subject=rendered.subject,
        body_text=rendered.text,
        body_html=rendered.html,
        reference_type=reference_type,
        reference_id=reference_id,
        max_attempts=settings.NOTIFY_MAX_ATTEMPTS,
        next_attempt_at=datetime.utcnow(),
        last_error=last_error,
    )
    try:
        # A SAVEPOINT, not a plain add: if another request wins the race on the
        # unique event_key, only this insert is rolled back. Without it the
        # IntegrityError would poison the caller's transaction and take the
        # order down with it.
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        logger.debug("Notification %s raced with another writer, skipping", key)
        return None
    return row


# ---------------------------------------------------------------- contexts

#: Which order statuses generate customer mail, and under which template.
#: Statuses absent from here (Paid, Processing) are covered elsewhere or are
#: internal steps the customer does not need an email about.
ORDER_STATUS_EVENTS = {
    "Shipped": "order.shipped",
    "Out for Delivery": "order.out_for_delivery",
    "Delivered": "order.delivered",
    "Cancelled": "order.cancelled",
    "Refunded": "order.refunded",
}


def _status_value(status) -> Optional[str]:
    """Read a status whether it is still an enum or already a plain string.

    The routers assign the incoming string straight to the column
    (`order.status = "Shipped"`), and SQLAlchemy only coerces it to the enum on
    flush — so at the moment a notification is queued the attribute can be
    either. Reaching for `.value` unconditionally raised AttributeError here,
    which turned a status update into a 500.
    """
    if status is None:
        return None
    return getattr(status, "value", status)


def order_context(order, **extra) -> dict:
    ctx = {
        "order_number": order.order_number,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "shipping_address": order.shipping_address,
        "status": _status_value(order.status),
        "subtotal": order.subtotal,
        "discount_total": order.discount_total,
        "shipping_fee": order.shipping_fee,
        "coupon_code": order.coupon_code,
        "total": order.total,
        "currency": order.currency,
        "display_currency": order.display_currency,
        "display_total": order.display_total,
        "created_at": order.created_at,
        "items": [
            {
                "name": line.product_name,
                "variant": line.variant_label,
                "quantity": line.quantity,
                "line_total": (line.price or 0) * (line.quantity or 0),
            }
            for line in order.items
        ],
    }
    ctx.update(extra)
    return ctx


def appointment_context(appt, **extra) -> dict:
    ctx = {
        "customer_name": appt.customer_name,
        "customer_email": appt.customer_email,
        "appointment_type": appt.appointment_type,
        "stylist": appt.stylist,
        "scheduled_at": appt.scheduled_at,
        "status": _status_value(appt.status),
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------- event hooks


def notify_order_event(db, order, event_type: str, **extra) -> Optional[models.Notification]:
    """Queue the customer-facing email for an order event."""
    return enqueue(
        db, event_type, order.customer_email, order_context(order, **extra),
        event_key=f"{event_type}:{order.id}",
        recipient_name=order.customer_name,
        reference_type="order", reference_id=order.id,
    )


def notify_admins(db, event_type: str, context: dict, *, key_suffix: str,
                  reference_type: str = None, reference_id: str = None) -> int:
    """Queue an operational alert to every configured staff address."""
    queued = 0
    for address in _recipients_for_admins():
        row = enqueue(
            db, event_type, address, context,
            event_key=f"{event_type}:{key_suffix}:{address}",
            category=models.NotificationCategory.operational,
            reference_type=reference_type, reference_id=reference_id,
        )
        if row:
            queued += 1
    return queued


def notify_order_placed(db, order, payment_instructions: str = None) -> None:
    """Customer receipt + staff alert for a new order."""
    awaiting = order.status == models.OrderStatus.pending_payment
    notify_order_event(db, order, "order.placed",
                       awaiting_payment=awaiting,
                       payment_instructions=payment_instructions)
    notify_admins(db, "admin.order_placed", order_context(order),
                  key_suffix=order.id, reference_type="order", reference_id=order.id)


def notify_order_status_change(db, order, new_status: str) -> None:
    event_type = ORDER_STATUS_EVENTS.get(new_status)
    if event_type:
        notify_order_event(db, order, event_type)


def notify_payment_paid(db, payment) -> None:
    notify_order_event(
        db, payment.order, "order.paid",
        amount_paid=payment.amount,
        method=payment.method,
        payment_reference=payment.reference or payment.provider_payment_id,
    )


def notify_payment_failed(db, payment) -> None:
    ctx = order_context(
        payment.order,
        error_code=payment.error_code,
        error_message=payment.error_message,
    )
    notify_admins(db, "admin.payment_failed", ctx,
                  key_suffix=payment.id, reference_type="order",
                  reference_id=payment.order_id)


def notify_payment_refunded(db, payment) -> None:
    notify_order_event(db, payment.order, "order.refunded",
                       amount_refunded=payment.amount_refunded or payment.amount)


def notify_appointment(db, appt, event_type: str) -> Optional[models.Notification]:
    return enqueue(
        db, event_type, appt.customer_email, appointment_context(appt),
        event_key=f"{event_type}:{appt.id}",
        recipient_name=appt.customer_name,
        reference_type="appointment", reference_id=appt.id,
    )


def check_low_stock(db, variant, product) -> None:
    """Alert staff when a sale takes a variant to (or below) the threshold.

    Keyed on the stock level it fell to, so crossing the line once produces one
    email — not one per subsequent sale at the same level, and not a fresh
    alert after a restock returns it above the line.
    """
    threshold = settings.LOW_STOCK_ALERT_THRESHOLD
    stock = variant.stock or 0
    if threshold <= 0 or stock > threshold:
        return
    notify_admins(
        db, "admin.low_stock",
        {
            "product_name": product.name,
            "variant_label": variant.label,
            "sku": variant.sku,
            "stock": stock,
            "threshold": threshold,
        },
        key_suffix=f"{variant.id}:{stock}",
        reference_type="product", reference_id=product.id,
    )


# ---------------------------------------------------------------- dispatch


def _backoff(attempts: int) -> timedelta:
    """Exponential backoff, capped at an hour: 1m, 2m, 4m, 8m, 16m…"""
    base = max(1, settings.NOTIFY_RETRY_BASE_SECONDS)
    return timedelta(seconds=min(base * (2 ** max(0, attempts - 1)), 3600))


def _claim_due(db, limit: int) -> List[models.Notification]:
    """Take the next due messages, locked so a second worker skips them."""
    query = (
        db.query(models.Notification)
        .filter(models.Notification.status == models.NotificationStatus.queued)
        .filter(models.Notification.next_attempt_at <= datetime.utcnow())
        .order_by(models.Notification.created_at.asc())
        .limit(limit)
    )
    try:
        return query.with_for_update(skip_locked=True).all()
    except Exception:              # noqa: BLE001 - engines without SKIP LOCKED
        return query.all()


def dispatch_pending(db, limit: int = None) -> dict:
    """Send what is due. Safe to run concurrently and safe to interrupt.

    Each message is committed on its own, so a crash mid-batch loses at most
    the one in flight — and that one is still Queued, so it is retried rather
    than lost.
    """
    limit = limit or settings.NOTIFY_BATCH_SIZE
    channel = get_channel()
    stats = {"channel": channel.name, "attempted": 0, "sent": 0,
             "failed": 0, "retrying": 0, "suppressed": 0}

    for row in _claim_due(db, limit):
        stats["attempted"] += 1

        # A suppression added after the message was queued still counts.
        if is_suppressed(db, row.recipient, row.category):
            row.status = models.NotificationStatus.suppressed
            row.last_error = "Recipient is on the suppression list."
            stats["suppressed"] += 1
            db.commit()
            continue

        row.attempts = (row.attempts or 0) + 1
        row.provider = channel.name
        try:
            message_id = channel.send(row)
        except NotificationError as exc:
            row.last_error = str(exc)[:500]
            out_of_attempts = row.attempts >= (row.max_attempts or 1)
            if exc.permanent or out_of_attempts:
                row.status = models.NotificationStatus.failed
                stats["failed"] += 1
                logger.error("Notification %s failed permanently: %s", row.id, exc)
            else:
                row.next_attempt_at = datetime.utcnow() + _backoff(row.attempts)
                stats["retrying"] += 1
                logger.warning("Notification %s attempt %s failed, retrying at %s: %s",
                               row.id, row.attempts, row.next_attempt_at, exc)
        except Exception as exc:   # noqa: BLE001 - an unexpected bug must not stop the queue
            row.last_error = f"Unexpected error: {exc}"[:500]
            row.status = models.NotificationStatus.failed
            stats["failed"] += 1
            logger.exception("Notification %s raised unexpectedly", row.id)
        else:
            row.status = models.NotificationStatus.sent
            row.provider_message_id = message_id
            row.sent_at = datetime.utcnow()
            row.last_error = None
            stats["sent"] += 1
        db.commit()

    return stats


def dispatch_in_new_session(limit: int = None) -> dict:
    """Drain the queue on a fresh session — for BackgroundTasks and the worker.

    A new session matters: this runs after the request's transaction has
    committed, and must not reuse a session that may already be closed.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        return dispatch_pending(db, limit)
    except Exception:              # noqa: BLE001 - background work must not crash the app
        logger.exception("Notification dispatch failed")
        return {"channel": get_channel().name, "attempted": 0, "sent": 0,
                "failed": 0, "retrying": 0, "suppressed": 0, "error": True}
    finally:
        db.close()


def schedule_dispatch(background_tasks) -> None:
    """Hand delivery to whatever NOTIFY_DISPATCH says owns it.

    `background` runs it after the response is returned, so the customer is
    never kept waiting on a mail server. `worker` leaves the queue alone for an
    external process. `inline` sends during the request — useful in tests,
    wrong in production.
    """
    mode = (settings.NOTIFY_DISPATCH or "background").lower()
    if mode == "worker":
        return
    if mode == "inline":
        dispatch_in_new_session()
        return
    if background_tasks is not None:
        background_tasks.add_task(dispatch_in_new_session)
