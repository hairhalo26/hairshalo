"""Editable storefront copy — the single owner of `site_content` rows.

The rule this file exists to enforce: **content a shop owner should be able to
change must not require a developer.** The storefront's announcement bar,
promotional panels, FAQ, footer and business details were HTML; they are now
rows, and the storefront renders whatever the database holds.

Two design decisions worth stating, because both were tempting to get wrong:

**The defaults are the copy that was already on the page.** `DEFAULTS` below is
the exact text lifted out of `index.html`, so the first read after this feature
ships produces a storefront that is byte-for-byte what it was. Nothing is
invented, and no shop is left with an empty homepage waiting for someone to
type into an admin form they have not seen yet.

**Not everything static became content.** A block is here only if a shop owner
would reasonably change it without wanting a developer. Deliberately excluded:

* Prices, stock, ratings, review counts, subscriber counts — owned by
  `pricing`, `inventory`, `reviews`, `loyalty` and `marketing`. Making those
  editable would mean a number on the storefront that no longer traces to the
  thing it claims to count, which is the failure mode this project has spent
  four phases removing.
* Structural labels ("Add to Bag", "Subtotal", "Checkout") — those are UI, and
  an owner editing them breaks the interface rather than merchandising it.
* Section headings that only name what the section renders ("Best sellers"
  sits above the bestsellers grid). Those did become editable, because a shop
  renaming its bestsellers rail is ordinary merchandising.
"""
import copy
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app import models


# Every block, in the order the admin panel lists them. `label` and
# `description` are what an admin sees; `payload` is what the storefront reads.
DEFAULTS: Dict[str, dict] = {
    # Every default below describes something the storefront can show to be
    # true from the catalogue itself (hair type, texture, photographs, prices
    # in rupees). The demo copy that used to sit here promised things no one
    # had confirmed: "100% Human Hair" over a catalogue that is mostly
    # synthetic, HD lace on crochet pieces, ethical sourcing, 12-month wear,
    # a 7-day fit guarantee, 24-hour dispatch, a free sizing kit. Promises
    # like those belong to the business; they stay empty here until the shop
    # writes them in the Back Office.
    "announcement": {
        "label": "Announcement bar",
        "description": "The thin strip above the header. Leave the text empty to hide the bar.",
        "sort_order": 10,
        "payload": {
            "text": "",
        },
    },
    "hero": {
        "label": "Homepage hero",
        "description": "The opening panel: eyebrow, headline, supporting copy and the two buttons.",
        "sort_order": 20,
        "payload": {
            "eyebrow": "The Hairshalo collection",
            # Split rather than one string with <br>: the storefront renders
            # each line as its own element, so an admin cannot inject markup
            # by typing a tag into a headline.
            "heading_lines": ["Hair that moves", "like it's always", "been yours."],
            # Which word inside the headline is italicised, matched literally.
            "heading_emphasis": "always",
            "body": ("Curl, wave and straight textures, each piece photographed up close — "
                     "its hair type and texture stated plainly, and its price shown before "
                     "it goes in your bag."),
            "primary_cta_label": "Shop the collection",
            "primary_cta_href": "#shop",
            "secondary_cta_label": "Questions, answered",
            "secondary_cta_href": "#faq",
            "chips": ["Hair type on every piece", "Every texture, up close", "Prices in ₹"],
            "image_url": "",
            "image_tag": "",
        },
    },
    "trust": {
        "label": "Trust strip",
        "description": ("The row of reassurances under the hero. Only list what you can stand "
                        "behind — an empty list hides the strip."),
        "sort_order": 30,
        "payload": {
            "items": [],
        },
    },
    "collections": {
        "label": "Collections section",
        "description": ("Heading and intro above the collection cards. The cards themselves "
                        "are your categories — edit those under Catalog → Categories."),
        "sort_order": 40,
        "payload": {
            "heading": "Find your texture",
            "intro": "Browse by collection. Every piece lists its hair type, texture, lengths and colours.",
        },
    },
    "promo": {
        "label": "Promotional banner",
        "description": "The wide panel between collections and best sellers. Leave the heading empty to hide it.",
        "sort_order": 50,
        "payload": {
            "heading": "",
            "body": "",
            "cta_label": "",
            "cta_href": "#shop",
        },
    },
    "bestsellers": {
        "label": "Best sellers section",
        "description": ("Heading and intro above the best-sellers grid. The products shown are "
                        "whichever published products are flagged Bestseller."),
        "sort_order": 60,
        "payload": {
            "heading": "Featured pieces",
            "intro": "The pieces we are featuring right now.",
        },
    },
    "shop": {
        "label": "Shop — all products",
        "description": ("Heading and intro above the full product listing. Every published "
                        "product appears there automatically, filterable by category."),
        "sort_order": 65,
        "payload": {
            "heading": "Shop the collection",
            "intro": "Every style we sell, ready to add to your bag.",
        },
    },
    "editorial": {
        "label": "Editorial block",
        "description": "The image-and-text story panel.",
        "sort_order": 70,
        "payload": {
            "eyebrow": "The Hairshalo way",
            "heading": "See it before you choose it.",
            "paragraphs": [
                ("Every piece is photographed from several angles, so you can look at the "
                 "curl, the wave and the ends before you decide."),
                ("Each listing states what the hair is — human hair or synthetic fibre — with "
                 "its texture, its construction, and the lengths and colours it comes in."),
            ],
            "cta_label": "Explore the collection",
            "cta_href": "#shop",
            "image_url": "",
            "image_tag": "",
        },
    },
    "before_after": {
        "label": "Before / after slider",
        "description": ("Your own before-and-after pair. Both images must be uploaded before "
                        "the section appears — a stock photo shown as a customer result is "
                        "a claim about work you did not do."),
        "sort_order": 75,
        "payload": {
            "heading": "The difference is in the detail",
            "intro": "Drag to compare.",
            # Empty by default, and the section stays hidden until a shop
            # uploads its OWN pair. This used to be two stock photographs
            # presented as a Hairshalo before and after.
            "before_image": "",
            "after_image": "",
            "before_label": "Before",
            "after_label": "After",
        },
    },
    "why_choose": {
        "label": "Why Hairshalo",
        "description": ("The brand-story statements. Each one should be something a customer "
                        "can check on the product page."),
        "sort_order": 80,
        "payload": {
            "heading": "Why Hairshalo",
            "items": [
                {"icon": "check", "title": "Stated plainly",
                 "body": ("Every piece lists its hair type — human hair or synthetic fibre — "
                          "with its texture and construction. Nothing is left to guess.")},
                {"icon": "eye", "title": "Seen up close",
                 "body": "Each piece is photographed from several angles, down to the curl and the ends."},
                {"icon": "layers", "title": "Your length, your colour",
                 "body": ("The lengths and colours of each piece are listed, and the ones not "
                          "in stock are marked as such.")},
                {"icon": "star", "title": "Priced upfront",
                 "body": "Prices are in rupees, and any delivery charge is shown before you pay."},
            ],
        },
    },
    "faq": {
        "label": "FAQ",
        "description": "Questions shown on the homepage. They appear in the order listed; an empty list hides the section.",
        "sort_order": 90,
        "payload": {
            "heading": "Questions, answered",
            "items": [
                {"q": "Is the hair human hair or synthetic?",
                 "a": ("It depends on the piece. Every product states its hair type on its page; "
                       "the collection includes both human hair and synthetic fibre.")},
                {"q": "How do I know what is in stock?",
                 "a": ("Open any piece. The lengths and colours you can order are selectable; "
                       "those that are out of stock are marked.")},
                {"q": "How is delivery charged?",
                 "a": "Any delivery charge is worked out for your order and shown before you pay."},
            ],
        },
    },
    "newsletter": {
        "label": "Newsletter panel",
        "description": "Copy above the signup form. The double opt-in behaviour is not editable.",
        "sort_order": 100,
        "payload": {
            "heading": "Your best hair starts here",
            "body": "New pieces and styling notes, by email. Every email has an unsubscribe link.",
            "cta_label": "Join Us",
        },
    },
    "navigation": {
        "label": "Header navigation",
        "description": "The links across the top of the storefront, and in the mobile menu.",
        "sort_order": 110,
        "payload": {
            "items": [
                {"label": "Shop", "href": "#shop"},
                {"label": "Collections", "href": "#collections"},
                {"label": "Our way", "href": "#editorial"},
                {"label": "Questions", "href": "#faq"},
            ],
        },
    },
    "footer": {
        "label": "Footer",
        "description": ("Brand blurb, link columns, legal links and the copyright line. The "
                        "Privacy, Terms, Cookie and Refund pages are always linked, whatever "
                        "is listed here."),
        "sort_order": 120,
        "payload": {
            "blurb": ("Hair pieces photographed up close and described plainly — hair type, "
                      "texture and price, stated before you buy."),
            "columns": [
                {"heading": "Shop", "links": [
                    {"label": "Shop all", "href": "#shop"},
                    {"label": "Collections", "href": "#collections"},
                ]},
                {"heading": "Help", "links": [
                    {"label": "Contact", "href": "#contact"},
                    {"label": "Shipping", "href": "#shipping"},
                    {"label": "Refunds & cancellations", "href": "/refunds"},
                    {"label": "FAQ", "href": "#faq"},
                ]},
                {"heading": "About", "links": [
                    {"label": "Our way", "href": "#editorial"},
                ]},
            ],
            "legal_links": [
                {"label": "Privacy Policy", "href": "/privacy"},
                {"label": "Terms & Conditions", "href": "/terms"},
                {"label": "Cookie Policy", "href": "/cookies"},
                {"label": "Refunds & Cancellations", "href": "/refunds"},
            ],
            "copyright": "© 2026 Hairshalo. All rights reserved.",
        },
    },
    "social": {
        "label": "Social links",
        "description": "Shown in the footer. Leave a URL empty to hide that icon.",
        "sort_order": 130,
        "payload": {
            "instagram": "",
            "facebook": "",
            "whatsapp": "",
            "youtube": "",
            "tiktok": "",
        },
    },
    "business": {
        "label": "Business & contact details",
        "description": ("Your shop's legal name, contact details and address. Used in the "
                        "footer and on the Privacy, Terms and Refund pages, which say "
                        "\"not yet provided\" wherever a detail here is empty."),
        "sort_order": 140,
        "payload": {
            "legal_name": "Hairshalo",
            "email": "",
            "phone": "",
            "whatsapp": "",
            "address": "",
            "hours": "",
            "gstin": "",
        },
    },
    "policies": {
        "label": "Shipping, returns & legal",
        "description": ("Your own shipping and returns rules. Shipping opens from the footer; "
                        "returns appears on the Refunds & Cancellations page. Leave one empty "
                        "and the site says the policy is not yet published rather than "
                        "inventing one."),
        "sort_order": 150,
        "payload": {
            "shipping": "",
            "returns": "",
            "terms": "",
            "privacy": "",
        },
    },
    "payment": {
        "label": "Payment details (bank transfer / UPI)",
        "description": ("What a customer is told after placing an order, when payment is taken "
                        "manually. Fill in at least a UPI ID or bank account, or customers have "
                        "no way to pay. Shown only to a customer who has an order to pay."),
        "sort_order": 160,
        "payload": {
            "instructions": ("Your order is reserved. Pay the total using the details below and "
                             "quote your order number as the reference. We confirm your order as "
                             "soon as the payment reaches us, usually within one business day."),
            "upi_id": "",
            "upi_name": "",
            "account_name": "",
            "bank_name": "",
            "account_number": "",
            "ifsc": "",
            "whatsapp_for_receipts": "",
        },
    },
}

# Blocks that are NOT part of the public storefront bundle. The payment block
# holds account details a customer needs only once they have an order to pay,
# so they are handed out with that order's payment step rather than to every
# page view (and every scraper).
PRIVATE_KEYS = {"payment"}

# The fields of the payment block that make up "how to pay us".
PAYMENT_DETAIL_FIELDS = ("upi_id", "upi_name", "account_name", "bank_name",
                         "account_number", "ifsc", "whatsapp_for_receipts")


def _row_to_dict(row: models.SiteContent) -> dict:
    return {
        "key": row.key,
        "label": row.label,
        "description": row.description or "",
        "payload": row.payload or {},
        "is_active": bool(row.is_active),
        "sort_order": row.sort_order or 0,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def ensure_defaults(db: Session) -> int:
    """Create any block that does not exist yet. Returns how many were created.

    Idempotent, and it never touches a block that already exists — an admin's
    edit is not overwritten by a later deploy that added a new default.
    Existing rows are left exactly as they are even if `DEFAULTS` has since
    changed, because the alternative is a deploy silently reverting someone's
    copy.
    """
    existing = {k for (k,) in db.query(models.SiteContent.key).all()}
    created = 0
    for key, spec in DEFAULTS.items():
        if key in existing:
            continue
        db.add(models.SiteContent(
            key=key,
            label=spec["label"],
            description=spec["description"],
            payload=copy.deepcopy(spec["payload"]),
            sort_order=spec["sort_order"],
            is_active=True,
        ))
        created += 1
    if created:
        db.commit()
    return created


def all_blocks(db: Session, *, include_inactive: bool = False) -> List[dict]:
    ensure_defaults(db)
    q = db.query(models.SiteContent)
    if not include_inactive:
        q = q.filter(models.SiteContent.is_active == True)  # noqa: E712
    rows = q.order_by(models.SiteContent.sort_order.asc(),
                      models.SiteContent.key.asc()).all()
    return [_row_to_dict(r) for r in rows]


def get_block(db: Session, key: str) -> Optional[dict]:
    ensure_defaults(db)
    row = db.query(models.SiteContent).filter(models.SiteContent.key == key).first()
    return _row_to_dict(row) if row else None


def as_map(db: Session) -> dict:
    """Every active block keyed by name — one request for the whole storefront.

    The storefront needs a dozen blocks to paint one page. Twelve round trips
    to render a homepage is a slow homepage, so this is deliberately one call.
    """
    return {b["key"]: b["payload"] for b in all_blocks(db)
            if b["is_active"] and b["key"] not in PRIVATE_KEYS}


def manual_payment(db: Session) -> dict:
    """The manual-payment instructions and the account details behind them.

    Returns {"instructions": str, "details": {...non-empty fields...},
    "configured": bool}. `configured` is False when the shop has entered no
    UPI ID and no account number — the checkout then says so honestly instead
    of pointing the customer at an invoice that does not exist.
    """
    block = get_block(db, "payment") or {"payload": DEFAULTS["payment"]["payload"]}
    payload = block["payload"] or {}
    details = {k: str(payload.get(k) or "").strip() for k in PAYMENT_DETAIL_FIELDS}
    details = {k: v for k, v in details.items() if v}
    configured = bool(details.get("upi_id") or details.get("account_number"))
    text = str(payload.get("instructions") or "").strip()         or DEFAULTS["payment"]["payload"]["instructions"]
    lines = [text]
    if details.get("upi_id"):
        lines.append(f"UPI: {details['upi_id']}"
                     + (f" ({details['upi_name']})" if details.get("upi_name") else ""))
    if details.get("account_number"):
        bank = [details.get("account_name"), details.get("bank_name"),
                f"A/c {details['account_number']}",
                f"IFSC {details['ifsc']}" if details.get("ifsc") else None]
        lines.append("Bank transfer: " + ", ".join(b for b in bank if b))
    if details.get("whatsapp_for_receipts"):
        lines.append(f"Send your payment receipt on WhatsApp: {details['whatsapp_for_receipts']}")
    if not configured:
        lines = [("Your order is reserved. We will contact you shortly with payment details "
                  "— nothing has been charged.")]
    return {"instructions": "\n".join(lines), "details": details, "configured": configured}


def update_block(db: Session, key: str, *, payload: Optional[dict] = None,
                 is_active: Optional[bool] = None, actor: str = None) -> dict:
    """Replace a block's payload. Unknown keys are rejected, not created.

    Rejecting an unknown key matters: the storefront reads specific keys, so a
    typo'd key would write a row that nothing ever renders and leave the admin
    convinced they had edited something.
    """
    ensure_defaults(db)
    row = db.query(models.SiteContent).filter(models.SiteContent.key == key).first()
    if not row:
        raise KeyError(key)

    if payload is not None:
        # Merge at the top level so a partial update ("just the heading") does
        # not silently drop the fields it did not mention.
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
        # JSON columns are mutated in place, which SQLAlchemy cannot see. The
        # reassignment above is what marks it dirty; this is belt and braces
        # for the nested case.
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "payload")

    if is_active is not None:
        row.is_active = bool(is_active)

    row.updated_by = actor
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


def reset_block(db: Session, key: str, *, actor: str = None) -> dict:
    """Restore one block to the copy the storefront shipped with."""
    if key not in DEFAULTS:
        raise KeyError(key)
    ensure_defaults(db)
    row = db.query(models.SiteContent).filter(models.SiteContent.key == key).first()
    row.payload = copy.deepcopy(DEFAULTS[key]["payload"])
    row.is_active = True
    row.updated_by = actor
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)
