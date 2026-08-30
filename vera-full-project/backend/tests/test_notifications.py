"""Notification tests.

Three layers:

* **Pure unit** — rendering, escaping, tokens, backoff, channel selection.
  No database, no server.
* **Database** — the outbox guarantees that only a real transaction can prove:
  a duplicate event key is rejected without poisoning the caller's transaction,
  and a queued message is written by the same transaction as its cause.
  Skipped when DATABASE_URL is unreachable.
* **API** — the lifecycle end to end. Skipped unless the backend is running:

    uvicorn app.main:app --port 8010
    python -m pytest tests/test_notifications.py -q
"""
import os
import smtplib
import uuid
from datetime import datetime, timedelta

import pytest
import requests

from app import email_templates as tpl
from app import models
from app import notifications as notify
from app.config import settings

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}


def _order_ctx(**extra):
    ctx = {
        "order_number": "VR-0001",
        "customer_name": "Test Customer",
        "customer_email": "customer@example.com",
        "shipping_address": "12 MG Road, Bengaluru",
        "status": "Processing",
        "subtotal": 12000, "discount_total": 0, "shipping_fee": 0, "total": 12000,
        "created_at": datetime(2026, 8, 26, 10, 30),
        "items": [{"name": "Lace Front Wig", "variant": "18in Natural Black",
                   "quantity": 1, "line_total": 12000}],
    }
    ctx.update(extra)
    return ctx


# ---------------- unit: rendering ----------------

def test_money_uses_indian_digit_grouping():
    assert tpl.money(499) == "₹499.00"
    assert tpl.money(120499) == "₹1,20,499.00"
    assert tpl.money("12345678.5") == "₹1,23,45,678.50"
    assert tpl.money(None) == "—"


def test_every_registered_template_renders_all_three_parts():
    ctx = _order_ctx(
        scheduled_at=datetime(2026, 9, 1, 15, 0), appointment_type="Install",
        stylist="Any available stylist", product_name="Lace Front Wig",
        variant_label="18in", sku="SKU-1", stock=2, threshold=5,
        channel="console", mail_from="orders@verahair.co",
    )
    for event_type in tpl.TEMPLATES:
        rendered = tpl.render(event_type, ctx)
        assert rendered.subject.strip(), event_type
        assert rendered.text.strip(), event_type
        assert "<html" in rendered.html, event_type


def test_an_unregistered_event_fails_loudly():
    """A typo in an event name must not produce an email with a blank body."""
    with pytest.raises(KeyError):
        tpl.render("order.teleported", _order_ctx())


def test_customer_supplied_text_is_escaped_into_the_html():
    rendered = tpl.render("order.placed", _order_ctx(
        customer_name='<script>alert("xss")</script>',
        shipping_address="<img src=x onerror=alert(1)>",
    ))
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html
    assert "onerror=alert(1)>" not in rendered.html
    # The plain-text part is not markup, so it keeps the raw characters.
    assert "<script>" in rendered.text


def test_order_placed_wording_depends_on_whether_payment_is_owed():
    awaiting = tpl.render("order.placed", _order_ctx(
        awaiting_payment=True, payment_instructions="Transfer to the account on your invoice."))
    assert "payment pending" in awaiting.subject
    assert "Transfer to the account" in awaiting.text

    confirmed = tpl.render("order.placed", _order_ctx(awaiting_payment=False))
    assert "Order confirmed" in confirmed.subject
    assert "Transfer to the account" not in confirmed.text


def test_totals_include_the_coupon_and_the_display_currency():
    rendered = tpl.render("order.placed", _order_ctx(
        discount_total=1000, coupon_code="VERA10", shipping_fee=499,
        display_currency="USD", display_total="144.20"))
    assert "VERA10" in rendered.text
    assert "USD" in rendered.text
    assert "₹499.00" in rendered.text


# ---------------- unit: channels ----------------

@pytest.fixture(autouse=True)
def _restore_channel():
    """Every test that changes NOTIFY_CHANNEL must not leak into the next."""
    original = settings.NOTIFY_CHANNEL
    yield
    settings.NOTIFY_CHANNEL = original
    notify.reset_channel()


def _select(channel_name):
    settings.NOTIFY_CHANNEL = channel_name
    notify.reset_channel()
    return notify.get_channel()


def test_the_default_channel_never_sends_real_mail():
    """A fresh clone must not be able to email a real customer by accident."""
    assert _select("console").name == "console"
    assert _select("console").sends_real_mail is False


def test_channel_selection_follows_configuration():
    assert _select("smtp").name == "smtp"
    assert _select("smtp").sends_real_mail is True
    assert _select("null").name == "null"
    assert _select("nonsense").name == "console"     # unknown degrades to safe


def test_null_channel_fails_permanently_so_it_is_not_retried():
    with pytest.raises(notify.NotificationError) as exc:
        notify.NullChannel().send(object())
    assert exc.value.permanent is True


def test_smtp_without_a_host_fails_permanently():
    channel = notify.SmtpChannel("", 587, "", "", "starttls")
    with pytest.raises(notify.NotificationError) as exc:
        channel._connect()
    assert exc.value.permanent is True
    assert "SMTP_HOST" in str(exc.value)


def test_smtp_connection_failure_is_temporary_and_will_be_retried():
    """A mail server that is down is not a reason to drop a receipt."""
    channel = notify.SmtpChannel("127.0.0.1", 1, "", "", "none", timeout=1)
    with pytest.raises(notify.NotificationError) as exc:
        channel._connect()
    assert exc.value.permanent is False


def _fake_notification(category=models.NotificationCategory.transactional):
    return models.Notification(
        id="n1", event_key="k", event_type="order.placed",
        channel=models.NotificationChannel.email, category=category,
        recipient="customer@example.com", recipient_name="Test Customer",
        subject="Order confirmed", body_text="text body",
        body_html="<html><body>html body</body></html>",
    )


def test_smtp_message_carries_both_parts_and_tracing_headers():
    channel = notify.SmtpChannel("localhost", 25, "", "", "none")
    msg = channel._build(_fake_notification())
    assert msg["To"] == "Test Customer <customer@example.com>"
    assert msg["X-Vera-Event"] == "order.placed"
    assert msg["X-Vera-Notification-Id"] == "n1"
    parts = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in parts and "text/html" in parts


def test_only_marketing_mail_advertises_an_unsubscribe_header():
    channel = notify.SmtpChannel("localhost", 25, "", "", "none")
    transactional = channel._build(_fake_notification())
    marketing = channel._build(_fake_notification(models.NotificationCategory.marketing))
    assert transactional["List-Unsubscribe"] is None
    assert marketing["List-Unsubscribe"] is not None


def test_a_refused_recipient_is_permanent(monkeypatch):
    class Refusing:
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"No such user")})

        def quit(self):
            pass

    channel = notify.SmtpChannel("localhost", 25, "", "", "none")
    monkeypatch.setattr(channel, "_connect", lambda: Refusing())
    with pytest.raises(notify.NotificationError) as exc:
        channel.send(_fake_notification())
    assert exc.value.permanent is True


# ---------------- unit: opt-out tokens ----------------

def test_unsubscribe_token_round_trips():
    token = notify.unsubscribe_token("Person@Example.com")
    assert notify.verify_unsubscribe_token(token) == "person@example.com"


def test_a_forged_or_tampered_token_is_refused():
    token = notify.unsubscribe_token("person@example.com")
    payload, signature = token.split(".", 1)
    assert notify.verify_unsubscribe_token(f"{payload}.{'0' * len(signature)}") is None
    # Swap the address but keep the signature — this is the attack that matters.
    other = notify.unsubscribe_token("victim@example.com").split(".", 1)[0]
    assert notify.verify_unsubscribe_token(f"{other}.{signature}") is None
    assert notify.verify_unsubscribe_token("not-a-token") is None


def test_suppression_rules_differ_by_category(monkeypatch):
    """Marketing honours every opt-out; transactional stops only on a hard bounce."""
    def suppressed_with(scope):
        row = models.NotificationSuppression(email="x@y.z", scope=scope)
        monkeypatch.setattr(notify, "suppression_for", lambda db, email: row)

    transactional = models.NotificationCategory.transactional
    marketing = models.NotificationCategory.marketing

    suppressed_with(models.SuppressionScope.marketing)
    assert notify.is_suppressed(None, "x@y.z", marketing) is True
    assert notify.is_suppressed(None, "x@y.z", transactional) is False

    suppressed_with(models.SuppressionScope.all)
    assert notify.is_suppressed(None, "x@y.z", marketing) is True
    assert notify.is_suppressed(None, "x@y.z", transactional) is True

    monkeypatch.setattr(notify, "suppression_for", lambda db, email: None)
    assert notify.is_suppressed(None, "x@y.z", transactional) is False


# ---------------- unit: retry policy ----------------

def test_backoff_grows_exponentially_and_is_capped():
    delays = [notify._backoff(n).total_seconds() for n in range(1, 8)]
    assert delays[0] == settings.NOTIFY_RETRY_BASE_SECONDS
    assert delays == sorted(delays)                  # never gets shorter
    assert max(delays) <= 3600                       # and never runs away


def test_status_events_cover_delivery_but_not_internal_steps():
    assert notify.ORDER_STATUS_EVENTS["Shipped"] == "order.shipped"
    assert notify.ORDER_STATUS_EVENTS["Delivered"] == "order.delivered"
    # "Paid" is announced by the payment flow, "Processing" is internal.
    assert "Paid" not in notify.ORDER_STATUS_EVENTS
    assert "Processing" not in notify.ORDER_STATUS_EVENTS
    for event_type in notify.ORDER_STATUS_EVENTS.values():
        assert event_type in tpl.TEMPLATES


# ---------------- database: the outbox guarantees ----------------

#: Every row these tests create carries this marker in its event key, so the
#: teardown can remove exactly what the tests made and nothing else.
TEST_MARKER = ":pytest-"


@pytest.fixture
def db():
    """A session that cleans up after itself.

    Rolling back is not enough: `dispatch_pending` commits each message as it
    goes (deliberately — a crash mid-batch must not lose progress), so anything
    a dispatch test touches is already durable. The teardown therefore deletes
    the marked rows explicitly.
    """
    try:
        from app.database import SessionLocal
        session = SessionLocal()
        session.execute(__import__("sqlalchemy").text("select 1"))
    except Exception:
        pytest.skip("database not reachable")
    try:
        yield session
    finally:
        session.rollback()
        session.query(models.Notification).filter(
            models.Notification.event_key.like(f"%{TEST_MARKER}%")
        ).delete(synchronize_session=False)
        session.query(models.NotificationSuppression).filter(
            models.NotificationSuppression.email.like("bounced-%@example.com")
        ).delete(synchronize_session=False)
        session.commit()
        session.close()


def _enqueue(db, key, recipient="customer@example.com"):
    return notify.enqueue(db, "order.placed", recipient, _order_ctx(),
                          event_key=key, reference_type="order", reference_id="test")


def test_the_same_event_is_only_ever_queued_once(db):
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    assert _enqueue(db, key) is not None
    assert _enqueue(db, key) is None
    assert db.query(models.Notification).filter(
        models.Notification.event_key == key).count() == 1


def test_losing_the_unique_race_does_not_poison_the_transaction(db, monkeypatch):
    """The headline requirement: a duplicate notification cannot roll back the
    order that caused it.

    The pre-check is disabled here to force the insert onto the UNIQUE index,
    which is what a genuine race between two checkouts would do.
    """
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    assert _enqueue(db, key) is not None

    monkeypatch.setattr(notify, "_already_queued", lambda *a, **kw: False)
    assert _enqueue(db, key) is None

    # The session must still be usable — this is the whole point of the savepoint.
    assert db.query(models.Notification).filter(
        models.Notification.event_key == key).count() == 1
    later = _enqueue(db, f"order.placed{TEST_MARKER}{uuid.uuid4()}")
    assert later is not None


def test_a_rendering_failure_never_breaks_the_caller(db):
    """A template bug must not take a checkout down with it."""
    row = notify.enqueue(db, "order.placed", "customer@example.com",
                         {"no": "required fields"},
                         event_key=f"broken{TEST_MARKER}{uuid.uuid4()}")
    assert row is None
    assert db.query(models.Notification).count() >= 0     # session still healthy


def test_a_message_with_no_recipient_is_not_queued(db):
    assert notify.enqueue(db, "order.placed", "", _order_ctx()) is None


def test_a_suppressed_recipient_is_recorded_but_not_queued_for_sending(db):
    address = f"bounced-{uuid.uuid4().hex[:8]}@example.com"
    notify.suppress(db, address, models.SuppressionScope.all, reason="hard_bounce")
    db.flush()
    row = _enqueue(db, f"order.placed{TEST_MARKER}{uuid.uuid4()}", recipient=address)
    assert row is not None
    # Recorded for audit, but never delivered.
    assert row.status == models.NotificationStatus.suppressed


def test_the_stored_body_is_what_gets_sent_not_a_re_render(db):
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    row = _enqueue(db, key)
    assert "VR-0001" in row.body_text
    assert "VR-0001" in row.body_html
    assert row.subject and row.max_attempts == settings.NOTIFY_MAX_ATTEMPTS


def test_dispatch_sends_queued_messages_and_marks_them(db):
    _select("console")
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    row = _enqueue(db, key)
    db.flush()
    stats = notify.dispatch_pending(db, limit=50)
    assert stats["channel"] == "console"
    db.refresh(row)
    assert row.status == models.NotificationStatus.sent
    assert row.provider == "console" and row.provider_message_id
    assert row.sent_at is not None


def test_a_temporary_failure_is_rescheduled_rather_than_dropped(db, monkeypatch):
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    row = _enqueue(db, key)
    db.flush()

    channel = _select("console")
    monkeypatch.setattr(channel, "send", lambda n: (_ for _ in ()).throw(
        notify.NotificationError("mail server unreachable")))
    before = datetime.utcnow()
    stats = notify.dispatch_pending(db, limit=50)

    assert stats["retrying"] >= 1
    db.refresh(row)
    assert row.status == models.NotificationStatus.queued     # still owed
    assert row.attempts == 1
    assert row.next_attempt_at > before                       # but not right away
    assert "unreachable" in row.last_error


def test_a_permanent_failure_is_not_retried(db, monkeypatch):
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    row = _enqueue(db, key)
    db.flush()

    channel = _select("console")
    monkeypatch.setattr(channel, "send", lambda n: (_ for _ in ()).throw(
        notify.NotificationError("no such mailbox", permanent=True)))
    notify.dispatch_pending(db, limit=50)

    db.refresh(row)
    assert row.status == models.NotificationStatus.failed
    assert row.attempts == 1                                  # gave up immediately


def test_a_message_that_is_not_due_yet_is_left_alone(db):
    _select("console")
    key = f"order.placed{TEST_MARKER}{uuid.uuid4()}"
    row = _enqueue(db, key)
    row.next_attempt_at = datetime.utcnow() + timedelta(hours=1)
    db.flush()
    notify.dispatch_pending(db, limit=50)
    db.refresh(row)
    assert row.status == models.NotificationStatus.queued


# ---------------- API ----------------

def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _alive(), reason="backend not reachable")


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip("backend not reachable")
    token = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sellable(products, minimum=2):
    """(product, variant) for the first variant that actually has stock.

    Scans every variant: earlier runs sell out whichever one is listed first.
    """
    for product in products:
        for variant in product.get("variants") or []:
            if (variant.get("stock") or 0) > minimum and variant.get("is_available", True):
                return product, variant
    pytest.skip(f"no variant in the catalog has more than {minimum} units in stock")


@pytest.fixture
def placed_order(auth):
    product, variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
    email = f"notify{uuid.uuid4().hex[:8]}@example.com"
    order = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Notify Test", "customer_email": email,
        "shipping_address": "12 MG Road",
        "items": [{"product_id": product["id"], "variant_id": variant["id"], "quantity": 1}],
    }).json()
    return order


def _notifications_for(auth, order_id, **params):
    return requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": order_id, **params}).json()


@live
def test_the_queue_is_admin_only():
    assert requests.get(f"{API}/notifications", timeout=10).status_code == 401
    assert requests.get(f"{API}/notifications/config", timeout=10).status_code == 401
    assert requests.post(f"{API}/notifications/dispatch", timeout=10).status_code == 401


@live
def test_placing_an_order_queues_the_customer_receipt(auth, placed_order):
    rows = _notifications_for(auth, placed_order["id"], event_type="order.placed")
    assert len(rows) == 1
    row = rows[0]
    assert row["recipient"] == placed_order["customer_email"]
    assert row["category"] == "transactional"
    assert placed_order["order_number"] in row["subject"]


@live
def test_the_stored_message_is_readable_for_support(auth, placed_order):
    rows = _notifications_for(auth, placed_order["id"], event_type="order.placed")
    detail = requests.get(f"{API}/notifications/{rows[0]['id']}",
                          headers=auth, timeout=10).json()
    assert placed_order["order_number"] in detail["body_text"]
    assert placed_order["order_number"] in detail["body_html"]
    assert "Notify Test" in detail["body_text"]


@live
def test_each_delivery_step_produces_exactly_one_message(auth, placed_order):
    order_id = placed_order["id"]
    start = placed_order["status"]
    steps = ["Shipped", "Out for Delivery", "Delivered"]
    if start == "Pending Payment":
        pytest.skip("payments enabled; this order cannot be shipped without paying")

    for status in steps:
        r = requests.put(f"{API}/orders/{order_id}/status", headers=auth, timeout=15,
                         json={"status": status})
        assert r.status_code == 200, r.text
        # Repeating the same transition must not produce a second email.
        requests.put(f"{API}/orders/{order_id}/status", headers=auth, timeout=15,
                     json={"status": status})

    rows = _notifications_for(auth, order_id)
    by_type = {}
    for row in rows:
        by_type[row["event_type"]] = by_type.get(row["event_type"], 0) + 1
    for event_type in ("order.shipped", "order.out_for_delivery", "order.delivered"):
        assert by_type.get(event_type) == 1, f"{event_type}: {by_type}"


@live
def test_dispatch_sends_what_is_queued(auth, placed_order):
    """With NOTIFY_DISPATCH=background the queue is usually already drained by
    the time this runs, so assert the outcome, not that there was work left."""
    stats = requests.post(f"{API}/notifications/dispatch", headers=auth, timeout=30).json()
    assert stats["failed"] == 0
    rows = _notifications_for(auth, placed_order["id"], event_type="order.placed")
    assert rows[0]["status"] == "Sent"
    assert rows[0]["sent_at"]
    assert rows[0]["provider"]


@live
def test_a_sent_message_cannot_be_retried_into_a_duplicate(auth, placed_order):
    requests.post(f"{API}/notifications/dispatch", headers=auth, timeout=30)
    rows = _notifications_for(auth, placed_order["id"], event_type="order.placed")
    assert rows[0]["status"] == "Sent"
    r = requests.post(f"{API}/notifications/{rows[0]['id']}/retry",
                      headers=auth, timeout=15)
    assert r.status_code == 400
    assert "already sent" in r.json()["detail"].lower()


@live
def test_config_reports_the_channel_and_its_gaps(auth):
    config = requests.get(f"{API}/notifications/config", headers=auth, timeout=10).json()
    assert config["channel"] in ("console", "smtp", "null")
    assert isinstance(config["warnings"], list)
    if config["channel"] == "console":
        assert config["sends_real_mail"] is False
        assert any("console" in w for w in config["warnings"])


@live
def test_unsubscribe_needs_a_valid_token_and_a_deliberate_post(auth):
    address = f"optout{uuid.uuid4().hex[:8]}@example.com"
    token = notify.unsubscribe_token(address)

    # A prefetched GET must not unsubscribe anyone.
    preview = requests.get(f"{API}/notifications/unsubscribe",
                           params={"token": token}, timeout=10).json()
    assert preview["email"] == address and preview["unsubscribed"] is False
    listed = requests.get(f"{API}/notifications/suppressions", headers=auth, timeout=10).json()
    assert address not in [s["email"] for s in listed]

    assert requests.post(f"{API}/notifications/unsubscribe",
                         params={"token": "forged.0000"}, timeout=10).status_code == 400

    done = requests.post(f"{API}/notifications/unsubscribe",
                         params={"token": token}, timeout=10).json()
    assert done["unsubscribed"] is True

    listed = requests.get(f"{API}/notifications/suppressions", headers=auth, timeout=10).json()
    entry = next(s for s in listed if s["email"] == address)
    assert entry["scope"] == "marketing"          # receipts still get through

    requests.delete(f"{API}/notifications/suppressions/{address}", headers=auth, timeout=10)


@live
def test_a_hard_bounce_stops_even_transactional_mail(auth):
    address = f"bounce{uuid.uuid4().hex[:8]}@example.com"
    requests.post(f"{API}/notifications/suppressions", headers=auth, timeout=10,
                  json={"email": address, "scope": "all", "reason": "hard_bounce"})
    try:
        product, variant = _sellable(requests.get(f"{API}/products", timeout=10).json())
        order = requests.post(f"{API}/orders", timeout=15, json={
            "customer_name": "Bounced", "customer_email": address,
            "items": [{"product_id": product["id"],
                       "variant_id": variant["id"], "quantity": 1}],
        }).json()
        rows = _notifications_for(auth, order["id"], event_type="order.placed")
        assert rows[0]["status"] == "Suppressed"
        assert "suppression" in (rows[0]["last_error"] or "").lower()
    finally:
        requests.delete(f"{API}/notifications/suppressions/{address}",
                        headers=auth, timeout=10)


@live
def test_booking_an_appointment_acknowledges_it(auth):
    email = f"appt{uuid.uuid4().hex[:8]}@example.com"
    appt = requests.post(f"{API}/appointments", timeout=15, json={
        "customer_name": "Appt Test", "customer_email": email,
        "appointment_type": "Closure Install",
        "scheduled_at": "2026-09-15T15:00:00",
    }).json()
    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": appt["id"]}).json()
    assert [r["event_type"] for r in rows] == ["appointment.booked"]

    requests.put(f"{API}/appointments/{appt['id']}/status", headers=auth, timeout=15,
                 json={"status": "Confirmed"})
    rows = requests.get(f"{API}/notifications", headers=auth, timeout=10,
                        params={"reference_id": appt["id"]}).json()
    assert "appointment.confirmed" in [r["event_type"] for r in rows]


@live
def test_the_test_endpoint_proves_the_channel(auth):
    row = requests.post(f"{API}/notifications/test", headers=auth, timeout=30,
                        json={"to": "channel-check@example.com"}).json()
    assert row["event_type"] == "notification.test"
    assert row["status"] in ("Sent", "Suppressed")
    assert row["category"] == "operational"
