"""Email rendering — pure functions, no database, no I/O.

Every template takes a plain dict and returns a `Rendered(subject, text, html)`.
That keeps them trivially testable, and it is why `app/notifications.py` builds
a context out of ORM objects rather than handing the objects here.

Two deliberate choices:

* **Both parts, always.** Every message has a real plain-text body, not an
  afterthought. HTML-only mail scores badly with spam filters and is unreadable
  in text clients.
* **No template engine.** Jinja would be one more dependency and one more way
  to accidentally render unescaped user input into HTML. Text is built with
  f-strings; anything interpolated into the HTML goes through `esc()`.
"""
import os
from decimal import Decimal
from html import escape as _escape

BRAND = "Hairshalo"
#: Absolute, publicly reachable URL of the Hairshalo mark. Mail clients cannot
#: resolve relative paths or load webfonts, so the logotype cannot be rendered
#: as text in the brand face here. When this is unset the header falls back to
#: the plain-text wordmark, which is also what recipients see whenever their
#: client blocks images — so the brand is never missing.
BRAND_LOGO_URL = os.getenv("BRAND_LOGO_URL", "")
INK = "#1a1a1a"
MUTED = "#6b6b6b"
ACCENT = "#8b5e3c"
LINE = "#e6e0d8"


class Rendered:
    def __init__(self, subject: str, text: str, html: str):
        self.subject, self.text, self.html = subject, text, html


def esc(value) -> str:
    """Escape anything before it goes near the HTML body."""
    return _escape(str(value if value is not None else ""), quote=True)


def money(amount, symbol: str = "₹") -> str:
    """Format an amount using Indian digit grouping (₹1,20,499.00).

    Western grouping would print ₹120,499.00, which reads as wrong on an
    invoice denominated in rupees. Grouping applies to the integer part only:
    the last three digits, then pairs.
    """
    if amount is None:
        return "—"
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{sign}{symbol}{whole}.{frac}"


def _date(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%d %b %Y, %H:%M")
    except AttributeError:
        return str(value)


# ---------------------------------------------------------------- layout


def _brand_header() -> str:
    """Logo lockup for the email header, with a text wordmark as the fallback.

    `alt` carries the brand name so a blocked image still reads as Hairshalo.
    """
    if BRAND_LOGO_URL:
        return (
            f'<img src="{_escape(BRAND_LOGO_URL, quote=True)}" alt="{esc(BRAND)}" '
            f'height="34" style="height:34px;width:auto;border:0;display:block;">'
        )
    return (
        f'<span style="font-size:19px;letter-spacing:.06em;color:{ACCENT};">'
        f'{esc(BRAND)}</span>'
    )


def _wrap(title: str, body_html: str, preheader: str = "",
          footer_note: str = "", unsubscribe_url: str = None) -> str:
    """Shared HTML shell. Table-based and inline-styled, because that is what
    Outlook and Gmail actually render consistently."""
    pre = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(preheader)}</div>'
        if preheader else ""
    )
    unsub = ""
    if unsubscribe_url:
        unsub = (
            f'<br><a href="{esc(unsubscribe_url)}" style="color:{MUTED}">'
            "Unsubscribe from offers</a>"
        )
    default_footer = (
        "You are receiving this because you placed an order or booked an "
        f"appointment with {esc(BRAND)}."
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:#faf7f3;">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#faf7f3;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid {LINE};border-radius:10px;overflow:hidden;font-family:Georgia,serif;color:{INK};">
  <tr><td style="padding:22px 28px;border-bottom:1px solid {LINE};">
    {_brand_header()}
  </td></tr>
  <tr><td style="padding:28px;font-size:15px;line-height:1.65;">
{body_html}
  </td></tr>
  <tr><td style="padding:18px 28px;border-top:1px solid {LINE};font-size:12px;color:{MUTED};font-family:Arial,Helvetica,sans-serif;line-height:1.6;">
    {esc(footer_note) if footer_note else default_footer}{unsub}
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def _items_html(items) -> str:
    if not items:
        return ""
    cell = f'padding:7px 0;border-bottom:1px solid {LINE};'
    head = f'font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.05em;padding-bottom:6px;'
    rows = ""
    for i in items:
        variant = (
            f'<br><span style="font-size:13px;color:{MUTED};">{esc(i.get("variant"))}</span>'
            if i.get("variant") else ""
        )
        rows += (
            f'<tr><td style="{cell}">{esc(i["name"])}{variant}</td>'
            f'<td align="center" style="{cell}">{esc(i["quantity"])}</td>'
            f'<td align="right" style="{cell}">{esc(money(i["line_total"]))}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="font-size:14px;margin:14px 0;">'
        f'<tr><th align="left" style="{head}">Item</th>'
        f'<th style="{head}">Qty</th>'
        f'<th align="right" style="{head}">Total</th></tr>'
        f'{rows}</table>'
    )


def _items_text(items) -> str:
    lines = []
    for i in items or []:
        label = i["name"] + (f' ({i["variant"]})' if i.get("variant") else "")
        lines.append(f'  {i["quantity"]} x {label} — {money(i["line_total"])}')
    return "\n".join(lines)


def _totals_html(ctx) -> str:
    def row(label, value, strong=False):
        weight = "font-weight:bold;" if strong else ""
        return (f'<tr><td style="padding:3px 0;color:{MUTED};">{esc(label)}</td>'
                f'<td align="right" style="padding:3px 0;{weight}">{esc(value)}</td></tr>')

    rows = row("Subtotal", money(ctx.get("subtotal")))
    if ctx.get("discount_total"):
        label = f'Discount ({ctx["coupon_code"]})' if ctx.get("coupon_code") else "Discount"
        rows += row(label, "-" + money(ctx["discount_total"]))
    rows += row("Shipping", money(ctx["shipping_fee"]) if ctx.get("shipping_fee") else "Free")
    rows += row("Total", money(ctx.get("total")), strong=True)
    if ctx.get("display_total") and ctx.get("display_currency"):
        approx = (f'≈ {esc(ctx["display_currency"])} '
                  f'{esc(money(ctx["display_total"], ""))} at the rate on the day of purchase')
        rows += (f'<tr><td colspan="2" align="right" style="padding-top:4px;'
                 f'font-size:12px;color:{MUTED};">{approx}</td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="font-size:14px;margin-top:12px;">{rows}</table>')


def _totals_text(ctx) -> str:
    lines = [f'  Subtotal: {money(ctx.get("subtotal"))}']
    if ctx.get("discount_total"):
        code = f' ({ctx["coupon_code"]})' if ctx.get("coupon_code") else ""
        lines.append(f'  Discount{code}: -{money(ctx["discount_total"])}')
    shipping = money(ctx["shipping_fee"]) if ctx.get("shipping_fee") else "Free"
    lines.append(f'  Shipping: {shipping}')
    lines.append(f'  Total: {money(ctx.get("total"))}')
    if ctx.get("display_total") and ctx.get("display_currency"):
        lines.append(f'  (~ {ctx["display_currency"]} '
                     f'{money(ctx["display_total"], "")} at the rate on the day of purchase)')
    return "\n".join(lines)


def _signoff_text() -> str:
    return f"\n\nWith love,\nThe {BRAND} team"


def _greeting(ctx) -> str:
    return f'<p style="margin:0 0 14px;">Hi {esc(ctx["customer_name"])},</p>'


# ---------------------------------------------------------------- customer


def order_placed(ctx) -> Rendered:
    """Order received. The wording differs depending on whether payment is owed."""
    awaiting = ctx.get("awaiting_payment")
    subject = (f'Your {BRAND} order {ctx["order_number"]} — payment pending'
               if awaiting else f'Order confirmed — {ctx["order_number"]}')
    lead = (
        "We have reserved your items. Your order is confirmed as soon as your "
        "payment reaches us."
        if awaiting else
        "Thank you — your order is confirmed and we have started getting it ready."
    )
    instructions = ctx.get("payment_instructions") if awaiting else None

    text = (
        f'Hi {ctx["customer_name"]},\n\n{lead}\n'
        + (f'\n{instructions}\n' if instructions else "")
        + f'\nOrder {ctx["order_number"]} — placed {_date(ctx.get("created_at"))}\n\n'
        f'{_items_text(ctx.get("items"))}\n\n{_totals_text(ctx)}\n\n'
        f'Shipping to:\n  {ctx.get("shipping_address") or "—"}'
        f'{_signoff_text()}'
    )
    instructions_html = (
        f'<p style="background:#fdf6ee;border:1px solid {LINE};border-radius:6px;'
        f'padding:12px 14px;font-size:14px;">{esc(instructions)}</p>'
        if instructions else ""
    )
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    {instructions_html}
    <p style="margin:18px 0 0;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong>
      · {esc(_date(ctx.get("created_at")))}</p>
    {_items_html(ctx.get("items"))}
    {_totals_html(ctx)}
    <p style="margin:20px 0 0;font-size:13px;color:{MUTED};">Shipping to</p>
    <p style="margin:2px 0 0;font-size:14px;">{esc(ctx.get("shipping_address") or "—")}</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_paid(ctx) -> Rendered:
    amount = ctx.get("amount_paid") or ctx.get("total")
    subject = f'Payment received — {ctx["order_number"]}'
    lead = (f'We have received {money(amount)} for order {ctx["order_number"]}. '
            "It is now being prepared for dispatch.")
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n'
            + (f'\nPayment method: {ctx["method"]}' if ctx.get("method") else "")
            + (f'\nReference: {ctx["payment_reference"]}' if ctx.get("payment_reference") else "")
            + f'\n\n{_items_text(ctx.get("items"))}\n\n{_totals_text(ctx)}{_signoff_text()}')
    meta = ""
    if ctx.get("method"):
        meta += (f'<p style="margin:0 0 4px;font-size:13px;color:{MUTED};">'
                 f'Method: {esc(ctx["method"])}</p>')
    if ctx.get("payment_reference"):
        meta += (f'<p style="margin:0;font-size:13px;color:{MUTED};">'
                 f'Reference: {esc(ctx["payment_reference"])}</p>')
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    {meta}
    {_items_html(ctx.get("items"))}
    {_totals_html(ctx)}
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_shipped(ctx) -> Rendered:
    subject = f'Your order {ctx["order_number"]} has shipped'
    lead = "Your order is on its way."
    track = ""
    if ctx.get("tracking_number"):
        track = f'Tracking: {ctx["tracking_number"]}'
        if ctx.get("carrier"):
            track = f'{ctx["carrier"]} — {track}'
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n\nOrder {ctx["order_number"]}\n'
            + (f'{track}\n' if track else "")
            + f'\n{_items_text(ctx.get("items"))}\n\n'
            f'Shipping to:\n  {ctx.get("shipping_address") or "—"}{_signoff_text()}')
    track_html = (f'<p style="margin:0 0 10px;font-size:14px;">{esc(track)}</p>'
                  if track else "")
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0 0 6px;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong></p>
    {track_html}
    {_items_html(ctx.get("items"))}
    <p style="margin:16px 0 0;font-size:13px;color:{MUTED};">Shipping to</p>
    <p style="margin:2px 0 0;font-size:14px;">{esc(ctx.get("shipping_address") or "—")}</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_out_for_delivery(ctx) -> Rendered:
    subject = f'Out for delivery — {ctx["order_number"]}'
    lead = "Your order is out for delivery and should reach you today."
    text = f'Hi {ctx["customer_name"]},\n\n{lead}\n\nOrder {ctx["order_number"]}{_signoff_text()}'
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong></p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_delivered(ctx) -> Rendered:
    subject = f'Delivered — {ctx["order_number"]}'
    lead = "Your order has been delivered. We hope you love it."
    care = ("Wash with a sulphate-free shampoo, air-dry on a stand, and store it "
            "braided or in the net it arrived in — that is most of what keeps a "
            "unit looking new.")
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n\n{care}\n\n'
            f'Order {ctx["order_number"]}{_signoff_text()}')
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0 0 14px;font-size:14px;color:{MUTED};">{esc(care)}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong></p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_cancelled(ctx) -> Rendered:
    subject = f'Order {ctx["order_number"]} cancelled'
    lead = ("Your order has been cancelled and the items have been returned to "
            "stock. Nothing further is owed.")
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n\n'
            f'Order {ctx["order_number"]} — {money(ctx.get("total"))}{_signoff_text()}')
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong>
      · {esc(money(ctx.get("total")))}</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def order_refunded(ctx) -> Rendered:
    amount = ctx.get("amount_refunded") or ctx.get("total")
    subject = f'Refund issued — {ctx["order_number"]}'
    lead = (f'We have refunded {money(amount)} for order {ctx["order_number"]}. '
            "Banks usually take 5–7 working days to show it.")
    text = f'Hi {ctx["customer_name"]},\n\n{lead}{_signoff_text()}'
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def appointment_booked(ctx) -> Rendered:
    subject = f'Appointment requested — {_date(ctx.get("scheduled_at"))}'
    lead = ("We have your request and will confirm shortly. Nothing is charged "
            "until your appointment is confirmed.")
    detail = (f'{ctx.get("appointment_type")} with {ctx.get("stylist")}\n'
              f'{_date(ctx.get("scheduled_at"))}')
    text = f'Hi {ctx["customer_name"]},\n\n{lead}\n\n{detail}{_signoff_text()}'
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0;font-size:15px;"><strong>{esc(ctx.get("appointment_type"))}</strong>
      with {esc(ctx.get("stylist"))}<br>
      <span style="color:{MUTED};">{esc(_date(ctx.get("scheduled_at")))}</span></p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def appointment_confirmed(ctx) -> Rendered:
    subject = f'Appointment confirmed — {_date(ctx.get("scheduled_at"))}'
    lead = "Your appointment is confirmed. We look forward to seeing you."
    detail = (f'{ctx.get("appointment_type")} with {ctx.get("stylist")}\n'
              f'{_date(ctx.get("scheduled_at"))}')
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n\n{detail}\n\n'
            f'If you need to reschedule, reply to this email.{_signoff_text()}')
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0 0 14px;font-size:15px;"><strong>{esc(ctx.get("appointment_type"))}</strong>
      with {esc(ctx.get("stylist"))}<br>
      <span style="color:{MUTED};">{esc(_date(ctx.get("scheduled_at")))}</span></p>
    <p style="margin:0;font-size:14px;color:{MUTED};">
      If you need to reschedule, reply to this email.</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


def appointment_cancelled(ctx) -> Rendered:
    subject = f'Appointment cancelled — {_date(ctx.get("scheduled_at"))}'
    lead = "Your appointment has been cancelled. Book again whenever suits you."
    text = f'Hi {ctx["customer_name"]},\n\n{lead}{_signoff_text()}'
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    """, preheader=lead)
    return Rendered(subject, text, html)


# ---------------------------------------------------------------- staff

STAFF_FOOTER = "Operational alert for Hairshalo staff."


def admin_order_placed(ctx) -> Rendered:
    subject = f'New order {ctx["order_number"]} — {money(ctx.get("total"))}'
    text = (f'{ctx["customer_name"]} <{ctx.get("customer_email")}> placed order '
            f'{ctx["order_number"]}.\n\n{_items_text(ctx.get("items"))}\n\n'
            f'{_totals_text(ctx)}\n\nStatus: {ctx.get("status")}\n'
            f'Shipping to:\n  {ctx.get("shipping_address") or "—"}\n')
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;"><strong>{esc(ctx["customer_name"])}</strong>
      &lt;{esc(ctx.get("customer_email"))}&gt; placed order
      <strong>{esc(ctx["order_number"])}</strong>.</p>
    {_items_html(ctx.get("items"))}
    {_totals_html(ctx)}
    <p style="margin:16px 0 0;font-size:13px;color:{MUTED};">
      Status: {esc(ctx.get("status"))}</p>
    <p style="margin:6px 0 0;font-size:13px;color:{MUTED};">
      {esc(ctx.get("shipping_address") or "—")}</p>
    """, preheader=f'{money(ctx.get("total"))} · {ctx.get("status")}',
        footer_note=STAFF_FOOTER)
    return Rendered(subject, text, html)


def admin_payment_failed(ctx) -> Rendered:
    subject = f'Payment failed — {ctx["order_number"]}'
    reason = ctx.get("error_message") or ctx.get("error_code") or "no reason given"
    text = (f'Payment for order {ctx["order_number"]} ({money(ctx.get("total"))}) '
            f'failed: {reason}\n\n'
            f'Customer: {ctx["customer_name"]} <{ctx.get("customer_email")}>\n'
            "Reserved stock has been released.\n")
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Payment for order
      <strong>{esc(ctx["order_number"])}</strong> ({esc(money(ctx.get("total")))})
      failed.</p>
    <p style="margin:0 0 14px;font-size:14px;color:{MUTED};">{esc(reason)}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      {esc(ctx["customer_name"])} &lt;{esc(ctx.get("customer_email"))}&gt; ·
      reserved stock released</p>
    """, preheader=str(reason), footer_note=STAFF_FOOTER)
    return Rendered(subject, text, html)


def admin_low_stock(ctx) -> Rendered:
    subject = f'Low stock — {ctx["product_name"]} ({ctx["stock"]} left)'
    text = (f'{ctx["product_name"]} — {ctx.get("variant_label")} '
            f'(SKU {ctx.get("sku")}) is down to {ctx["stock"]} unit(s), at or below '
            f'the alert threshold of {ctx.get("threshold")}.\n')
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;"><strong>{esc(ctx["product_name"])}</strong> —
      {esc(ctx.get("variant_label"))}</p>
    <p style="margin:0 0 8px;font-size:14px;">SKU {esc(ctx.get("sku"))}</p>
    <p style="margin:0;font-size:15px;">Stock is down to
      <strong>{esc(ctx["stock"])}</strong>, at or below the alert threshold of
      {esc(ctx.get("threshold"))}.</p>
    """, preheader=f'{ctx["stock"]} left', footer_note=STAFF_FOOTER)
    return Rendered(subject, text, html)


def admin_review_pending(ctx) -> Rendered:
    subject = f'Review awaiting moderation — {ctx.get("product_name")}'
    stars = "*" * int(ctx.get("rating") or 0)
    body = (ctx.get("body") or "").strip()
    title = f'{ctx["title"]}\n' if ctx.get("title") else ""
    text = (f'{ctx.get("author")} left a {ctx.get("rating")}-star review of '
            f'{ctx.get("product_name")}.\n\n{title}{body}\n\n'
            "It is not visible on the storefront until it is published.\n")
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;"><strong>{esc(ctx.get("author"))}</strong> left a
      {esc(ctx.get("rating"))}-star review of
      <strong>{esc(ctx.get("product_name"))}</strong>.</p>
    <p style="margin:0 0 6px;color:{ACCENT};letter-spacing:.15em;">{esc(stars)}</p>
    {f'<p style="margin:0 0 8px;font-size:15px;"><strong>{esc(ctx["title"])}</strong></p>' if ctx.get("title") else ""}
    <p style="margin:0 0 14px;font-size:14px;color:{MUTED};">{esc(ctx.get("body") or "")}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      Not visible on the storefront until it is published.</p>
    """, preheader=f'{ctx.get("rating")} stars', footer_note=STAFF_FOOTER)
    return Rendered(subject, text, html)


def generic_test(ctx) -> Rendered:
    subject = f'{BRAND} — test email'
    lead = ("If you are reading this, the notification channel is configured "
            "correctly and mail is leaving the server.")
    text = f'{lead}\n\nChannel: {ctx.get("channel")}\nFrom: {ctx.get("mail_from")}\n'
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0;font-size:13px;color:{MUTED};">
      Channel: {esc(ctx.get("channel"))}<br>From: {esc(ctx.get("mail_from"))}</p>
    """, preheader=lead, footer_note="Test message — no action needed.")
    return Rendered(subject, text, html)


# ---------------------------------------------------------------- marketing


def marketing_confirm(ctx) -> Rendered:
    """Double opt-in confirmation.

    Transactional, not marketing: it is the direct answer to an action this
    person just took, and it is the only thing sent before consent exists. It
    therefore carries no offers and no unsubscribe link — there is nothing yet
    to unsubscribe from.
    """
    subject = f"Confirm your {BRAND} subscription"
    lead = ("Please confirm you would like offers and new arrivals from "
            f"{BRAND}. If you did not request this, ignore this email and "
            "nothing further will be sent.")
    url = ctx.get("confirm_url", "")
    text = (f'Hi {ctx.get("customer_name", "there")},\n\n{lead}\n\n'
            f'Confirm here:\n{url}\n')
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Hi {esc(ctx.get("customer_name", "there"))},</p>
    <p style="margin:0 0 18px;">{esc(lead)}</p>
    <p style="margin:0 0 18px;">
      <a href="{esc(url)}" style="display:inline-block;background:{ACCENT};color:#ffffff;
         text-decoration:none;padding:11px 20px;border-radius:6px;font-size:15px;">
        Confirm subscription</a></p>
    <p style="margin:0;font-size:12px;color:{MUTED};word-break:break-all;">
      Or paste this into your browser: {esc(url)}</p>
    """, preheader=lead,
        footer_note=("You received this because this address was entered on our "
                     "website. No further email is sent unless you confirm."))
    return Rendered(subject, text, html)


def marketing_campaign(ctx) -> Rendered:
    """A campaign to a confirmed subscriber.

    The unsubscribe link is not optional and is not a setting — it is built
    into the template, so a campaign cannot be composed without one.
    """
    subject = ctx.get("subject") or f"News from {BRAND}"
    body = ctx.get("body") or ""
    unsubscribe = ctx.get("unsubscribe_url", "")

    cta_text = ""
    cta_html = ""
    if ctx.get("cta_url") and ctx.get("cta_label"):
        cta_text = f'\n\n{ctx["cta_label"]}: {ctx["cta_url"]}'
        cta_html = (
            f'<p style="margin:20px 0 0;">'
            f'<a href="{esc(ctx["cta_url"])}" style="display:inline-block;'
            f'background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;'
            f'border-radius:6px;font-size:15px;">{esc(ctx["cta_label"])}</a></p>'
        )

    text = (f'Hi {ctx.get("customer_name", "there")},\n\n{body}{cta_text}'
            f'{_signoff_text()}\n\n'
            f'Unsubscribe from offers: {unsubscribe}\n')

    # Paragraphs, escaped: campaign bodies are typed by staff into an admin
    # form, and that form is not a licence to inject markup into an email.
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;">{esc(para.strip())}</p>'
        for para in body.split("\n\n") if para.strip()
    )
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Hi {esc(ctx.get("customer_name", "there"))},</p>
    {paragraphs}
    {cta_html}
    """, preheader=ctx.get("preheader") or body[:120],
        footer_note=("You are receiving this because you confirmed a subscription "
                     f"to {BRAND} offers."),
        unsubscribe_url=unsubscribe)
    return Rendered(subject, text, html)


def review_request(ctx) -> Rendered:
    """Asks a customer to review what they bought, once it has arrived.

    Transactional: it is about an order this person placed, it carries no
    offers, and it can only be sent for a delivered order.
    """
    subject = f'How is your {ctx.get("product_name") or "order"}?'
    lead = ("Your order arrived a little while ago. If you have a moment, a "
            "short review helps other people choose — and helps us.")
    url = ctx.get("review_url", "")
    items = ", ".join(ctx.get("item_names") or [])
    text = (f'Hi {ctx["customer_name"]},\n\n{lead}\n\n'
            f'Order {ctx["order_number"]}'
            + (f' — {items}' if items else "")
            + (f'\n\nLeave a review:\n{url}' if url else "")
            + _signoff_text())
    html = _wrap(subject, f"""
    {_greeting(ctx)}
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0 0 6px;font-size:13px;color:{MUTED};">
      Order <strong style="color:{INK};">{esc(ctx["order_number"])}</strong>
      {esc(" — " + items if items else "")}</p>
    {f'<p style="margin:18px 0 0;"><a href="{esc(url)}" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;border-radius:6px;font-size:15px;">Leave a review</a></p>' if url else ""}
    """, preheader=lead)
    return Rendered(subject, text, html)


# ---------------------------------------------------------------- accounts


def _button(url, label):
    return (f'<p style="margin:18px 0 0;"><a href="{esc(url)}" '
            f'style="display:inline-block;background:{ACCENT};color:#ffffff;'
            f'text-decoration:none;padding:11px 20px;border-radius:6px;'
            f'font-size:15px;">{esc(label)}</a></p>')


def account_verify_email(ctx) -> Rendered:
    """Confirms the mailbox. Order history stays hidden until this is clicked,
    so the email says so plainly rather than treating it as a formality."""
    subject = f"Confirm your {BRAND} account"
    lead = ("Please confirm this email address to finish setting up your "
            "account. Until you do, your order history stays hidden — that is "
            "deliberate, so nobody else can claim it.")
    url = ctx.get("verify_url", "")
    text = (f'Hi {ctx.get("customer_name", "there")},\n\n{lead}\n\n'
            f'Confirm here:\n{url}\n\n'
            f'This link is valid for {ctx.get("expires_days", 7)} days. '
            "If you did not create an account, ignore this email.\n")
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Hi {esc(ctx.get("customer_name", "there"))},</p>
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    {_button(url, "Confirm my email")}
    <p style="margin:16px 0 0;font-size:12px;color:{MUTED};word-break:break-all;">
      Or paste this into your browser: {esc(url)}</p>
    <p style="margin:10px 0 0;font-size:12px;color:{MUTED};">
      Valid for {esc(ctx.get("expires_days", 7))} days. If you did not create an
      account, ignore this email.</p>
    """, preheader=lead,
        footer_note="You received this because an account was created with this address.")
    return Rendered(subject, text, html)


def account_password_reset(ctx) -> Rendered:
    subject = f"Reset your {BRAND} password"
    lead = ("Use the link below to set a new password. It can only be used "
            "once.")
    url = ctx.get("reset_url", "")
    minutes = ctx.get("expires_minutes", 60)
    text = (f'Hi {ctx.get("customer_name", "there")},\n\n{lead}\n\n{url}\n\n'
            f'This link expires in {minutes} minutes. If you did not ask for a '
            "password reset, ignore this email — nothing has changed.\n")
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Hi {esc(ctx.get("customer_name", "there"))},</p>
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    {_button(url, "Set a new password")}
    <p style="margin:16px 0 0;font-size:12px;color:{MUTED};word-break:break-all;">
      Or paste this into your browser: {esc(url)}</p>
    <p style="margin:10px 0 0;font-size:12px;color:{MUTED};">
      Expires in {esc(minutes)} minutes. If you did not ask for this, ignore the
      email — nothing has changed.</p>
    """, preheader=lead,
        footer_note="You received this because a password reset was requested for this address.")
    return Rendered(subject, text, html)


def account_password_changed(ctx) -> Rendered:
    """Sent after every password change.

    This is the message through which a customer discovers a takeover they did
    not perform, so it is sent unconditionally and cannot be turned off.
    """
    subject = f"Your {BRAND} password was changed"
    lead = ("Your password was just changed, and any other devices signed in to "
            "your account have been signed out.")
    warning = ("If this was not you, reset your password immediately and contact "
               "us — someone else may have had access.")
    text = (f'Hi {ctx.get("customer_name", "there")},\n\n{lead}\n\n'
            f'{_date(ctx.get("changed_at"))}\n\n{warning}\n')
    html = _wrap(subject, f"""
    <p style="margin:0 0 14px;">Hi {esc(ctx.get("customer_name", "there"))},</p>
    <p style="margin:0 0 14px;">{esc(lead)}</p>
    <p style="margin:0 0 14px;font-size:13px;color:{MUTED};">
      {esc(_date(ctx.get("changed_at")))}</p>
    <p style="margin:0;font-size:14px;">{esc(warning)}</p>
    """, preheader=lead,
        footer_note="Security notice for your account.")
    return Rendered(subject, text, html)


#: event type -> renderer. `app/notifications.py` refuses to queue anything not
#: registered here, so a mistyped event name fails loudly at the call site
#: instead of producing an email with a blank body.
TEMPLATES = {
    "order.placed": order_placed,
    "order.paid": order_paid,
    "order.shipped": order_shipped,
    "order.out_for_delivery": order_out_for_delivery,
    "order.delivered": order_delivered,
    "order.cancelled": order_cancelled,
    "order.refunded": order_refunded,
    "appointment.booked": appointment_booked,
    "appointment.confirmed": appointment_confirmed,
    "appointment.cancelled": appointment_cancelled,
    "admin.order_placed": admin_order_placed,
    "admin.payment_failed": admin_payment_failed,
    "admin.low_stock": admin_low_stock,
    "marketing.confirm": marketing_confirm,
    "marketing.campaign": marketing_campaign,
    "review.request": review_request,
    "admin.review_pending": admin_review_pending,
    "account.verify_email": account_verify_email,
    "account.password_reset": account_password_reset,
    "account.password_changed": account_password_changed,
    "notification.test": generic_test,
}


def render(event_type: str, ctx: dict) -> Rendered:
    template = TEMPLATES.get(event_type)
    if not template:
        raise KeyError(f"No email template registered for '{event_type}'")
    return template(ctx)
