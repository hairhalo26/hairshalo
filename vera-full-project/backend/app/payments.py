"""Payment gateway abstraction.

Non-negotiable rule this module exists to enforce:

    An order is NEVER marked paid because the browser said so.

The frontend can only report "the customer finished the gateway flow". The
backend then either verifies the gateway's signature over the returned ids, or
waits for the gateway's own webhook. Both paths converge on `confirm_payment()`,
which is idempotent.

Providers
---------
`none`     — payments disabled. Orders go straight to Processing, exactly as
             they did before this module existed. This is the default so a
             fresh checkout still works with no gateway configured.
`manual`   — offline settlement (bank transfer / cash on delivery). The order
             is held at Pending Payment until an ADMIN confirms receipt. No
             customer-facing action can mark it paid.
`razorpay` — real integration. Requires RAZORPAY_KEY_ID / KEY_SECRET, and
             RAZORPAY_WEBHOOK_SECRET for webhooks. Signature verification is
             implemented here with hmac/hashlib — no SDK dependency.

Card data is never accepted, transmitted or stored by this application.
"""
import hashlib
import hmac
import json
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger("vera.payments")


class PaymentError(Exception):
    """Raised when an intent cannot be created or a signature is invalid."""


class Intent:
    """What the frontend needs to open the gateway. Never contains secrets."""

    def __init__(self, provider: str, provider_order_id: str, amount: Decimal,
                 currency: str, public_key: str = None, extra: dict = None,
                 instructions: str = None):
        self.provider = provider
        self.provider_order_id = provider_order_id
        self.amount = amount
        self.currency = currency
        self.public_key = public_key
        self.extra = extra or {}
        self.instructions = instructions


class PaymentEvent:
    """Normalised gateway event, whatever the provider called it."""

    def __init__(self, event_id: str, provider_payment_id: str,
                 provider_order_id: str, status: str, amount: Decimal = None,
                 currency: str = "INR", method: str = None,
                 error_code: str = None, error_message: str = None):
        self.event_id = event_id
        self.provider_payment_id = provider_payment_id
        self.provider_order_id = provider_order_id
        self.status = status           # paid | authorized | failed | refunded | cancelled
        self.amount = amount
        self.currency = currency
        self.method = method
        self.error_code = error_code
        self.error_message = error_message


class PaymentProvider(ABC):
    name = "base"
    #: True when the gateway settles asynchronously and orders must wait.
    holds_order = True

    #: How to pay, in words, for a provider that settles offline. Lets the
    #: order-confirmation email explain what to do without creating an intent.
    checkout_instructions: str = None

    @abstractmethod
    def create_intent(self, order) -> Intent:
        ...

    def verify_return(self, data: dict) -> PaymentEvent:
        """Verify the payload the browser returns from the gateway."""
        raise PaymentError("This provider does not support client-side return verification.")

    def verify_webhook(self, raw_body: bytes, headers: dict) -> PaymentEvent:
        """Verify and parse a gateway webhook."""
        raise PaymentError("This provider does not support webhooks.")

    def refund(self, payment, amount: Decimal) -> Tuple[str, Decimal]:
        raise PaymentError("This provider does not support refunds.")


# --------------------------------------------------------------------------


class NoopProvider(PaymentProvider):
    """Payments disabled — preserves the pre-payment behaviour."""
    name = "none"
    holds_order = False

    def create_intent(self, order):
        raise PaymentError("Payments are not enabled on this deployment.")


class ManualProvider(PaymentProvider):
    """Offline settlement. Only an admin can confirm receipt.

    This is a real workflow (bank transfer, COD), not a simulated gateway —
    nothing here ever marks an order paid on its own.
    """
    name = "manual"
    holds_order = True
    checkout_instructions = (
        "Your order is reserved. Transfer the total to the account on your "
        "invoice quoting the order number; we confirm within one business day."
    )

    def create_intent(self, order):
        return Intent(
            provider=self.name,
            provider_order_id=f"manual_{order.order_number}",
            amount=Decimal(str(order.total)),
            currency=order.currency or "INR",
            instructions=self.checkout_instructions,
        )


class RazorpayProvider(PaymentProvider):
    """Razorpay Orders + webhook verification.

    Amounts are in paise (integer minor units). Requires network access and
    valid keys; without them `create_intent` fails loudly rather than
    pretending a payment succeeded.
    """
    name = "razorpay"
    holds_order = True
    API = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self.key_id, self.key_secret = key_id, key_secret
        self.webhook_secret = webhook_secret

    def _require_keys(self):
        if not self.key_id or not self.key_secret:
            raise PaymentError(
                "Razorpay is selected but RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set."
            )

    def _post(self, path: str, payload: dict) -> dict:
        import base64
        import urllib.error
        import urllib.request
        body = json.dumps(payload).encode()
        auth = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        req = urllib.request.Request(
            f"{self.API}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise PaymentError(f"Razorpay rejected the request: {detail}")
        except Exception as exc:
            raise PaymentError(f"Could not reach Razorpay: {exc}")

    def create_intent(self, order):
        self._require_keys()
        minor = int((Decimal(str(order.total)) * 100).to_integral_value())
        data = self._post("/orders", {
            "amount": minor,
            "currency": order.currency or "INR",
            "receipt": order.order_number,
            "notes": {"order_id": order.id, "order_number": order.order_number},
        })
        return Intent(
            provider=self.name,
            provider_order_id=data["id"],
            amount=Decimal(str(order.total)),
            currency=order.currency or "INR",
            public_key=self.key_id,          # publishable id only, never the secret
            extra={"amount_minor": minor},
        )

    def verify_return(self, data: dict) -> PaymentEvent:
        """Verify razorpay_signature = HMAC_SHA256(order_id|payment_id, key_secret)."""
        self._require_keys()
        order_id = data.get("razorpay_order_id")
        payment_id = data.get("razorpay_payment_id")
        signature = data.get("razorpay_signature")
        if not (order_id and payment_id and signature):
            raise PaymentError("Incomplete payment confirmation from the gateway.")
        expected = hmac.new(
            self.key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PaymentError("Payment signature verification failed.")
        return PaymentEvent(
            event_id=f"return_{payment_id}",
            provider_payment_id=payment_id,
            provider_order_id=order_id,
            status="paid",
        )

    def verify_webhook(self, raw_body: bytes, headers: dict) -> PaymentEvent:
        if not self.webhook_secret:
            raise PaymentError("RAZORPAY_WEBHOOK_SECRET is not configured.")
        signature = headers.get("x-razorpay-signature") or ""
        expected = hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PaymentError("Webhook signature verification failed.")

        payload = json.loads(raw_body.decode())
        event = payload.get("event", "")
        entity = (
            payload.get("payload", {}).get("payment", {}).get("entity")
            or payload.get("payload", {}).get("refund", {}).get("entity")
            or {}
        )
        mapping = {
            "payment.captured": "paid",
            "payment.authorized": "authorized",
            "payment.failed": "failed",
            "refund.processed": "refunded",
            "order.paid": "paid",
        }
        status = mapping.get(event)
        if not status:
            raise PaymentError(f"Unhandled webhook event '{event}'")
        amount = entity.get("amount")
        return PaymentEvent(
            event_id=payload.get("id") or f"{event}_{entity.get('id')}",
            provider_payment_id=entity.get("payment_id") or entity.get("id"),
            provider_order_id=entity.get("order_id"),
            status=status,
            amount=Decimal(amount) / 100 if amount is not None else None,
            currency=entity.get("currency", "INR"),
            method=entity.get("method"),
            error_code=entity.get("error_code"),
            error_message=entity.get("error_description"),
        )

    def refund(self, payment, amount: Decimal):
        self._require_keys()
        minor = int((Decimal(str(amount)) * 100).to_integral_value())
        data = self._post(f"/payments/{payment.provider_payment_id}/refund", {"amount": minor})
        return data["id"], Decimal(data.get("amount", minor)) / 100


# --------------------------------------------------------------------------

_provider: Optional[PaymentProvider] = None


#: Spellings that all mean "payments are off". Accepted explicitly rather than
#: by falling through, so that a TYPO cannot quietly disable payments: a shop
#: configured with "razorpy" would otherwise accept orders and never charge for
#: any of them, and nothing would say so.
DISABLED_ALIASES = {"none", "disabled", "off", "false", ""}


def known_provider(name: str) -> bool:
    name = (name or "").strip().lower()
    return name in DISABLED_ALIASES or name in {"manual", "razorpay"}


def get_provider() -> PaymentProvider:
    global _provider
    if _provider is None:
        name = (settings.PAYMENT_PROVIDER or "none").strip().lower()
        if name == "razorpay":
            _provider = RazorpayProvider(
                settings.RAZORPAY_KEY_ID,
                settings.RAZORPAY_KEY_SECRET,
                settings.RAZORPAY_WEBHOOK_SECRET,
            )
        elif name == "manual":
            _provider = ManualProvider()
        else:
            if name not in DISABLED_ALIASES:
                logger.error(
                    "PAYMENT_PROVIDER=%r is not a provider this build knows. "
                    "Payments are DISABLED: orders will be accepted without being "
                    "charged for. Use one of: none, manual, razorpay.", name,
                )
            _provider = NoopProvider()
    return _provider


def reset_provider() -> None:
    """Forget the cached provider — used by tests after changing settings."""
    global _provider
    _provider = None


def payments_enabled() -> bool:
    return get_provider().name != "none"
