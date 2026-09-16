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
    "announcement": {
        "label": "Announcement bar",
        "description": "The thin strip above the header. Leave the text empty to hide the bar.",
        "sort_order": 10,
        "payload": {
            "text": "Complimentary virtual consultation with every order  ·  Ships pan-India in 3–5 days",
        },
    },
    "hero": {
        "label": "Homepage hero",
        "description": "The opening panel: eyebrow, headline, supporting copy and the two buttons.",
        "sort_order": 20,
        "payload": {
            "eyebrow": "Premium Human Hair",
            # Split rather than one string with <br>: the storefront renders
            # each line as its own element, so an admin cannot inject markup
            # by typing a tag into a headline.
            "heading_lines": ["Hair that moves", "like it's always", "been yours."],
            # Which word inside the headline is italicised, matched literally.
            "heading_emphasis": "always",
            "body": ("Hand-tied human hair wigs and extensions with a natural hairline, "
                     "breathable lace, and a fit built for everyday wear — from your first "
                     "install to your best hair day."),
            "primary_cta_label": "Shop Wigs",
            "primary_cta_href": "#collections",
            "secondary_cta_label": "Book a Fitting",
            "secondary_cta_href": "#faq",
            "chips": ["100% Human Hair", "HD Lace, Natural Hairline", "Free Cap Size Guidance"],
            "image_url": "",
            "image_tag": "Glueless & Beginner-Friendly",
        },
    },
    "trust": {
        "label": "Trust strip",
        "description": "The row of reassurances under the hero.",
        "sort_order": 30,
        "payload": {
            "items": [
                {"icon": "star", "text": "100% Human Hair"},
                {"icon": "check", "text": "Natural Scalp Finish"},
                {"icon": "card", "text": "Secure Payments"},
                {"icon": "tick", "text": "7-Day Fit Guarantee"},
                {"icon": "arrow", "text": "Fast Pan-India Delivery"},
            ],
        },
    },
    "collections": {
        "label": "Collections section",
        "description": ("Heading and intro above the collection cards. The cards themselves "
                        "are your categories — edit those under Catalog → Categories."),
        "sort_order": 40,
        "payload": {
            "heading": "Find your perfect install",
            "intro": "Every texture and density, made for a natural finish and easy everyday wear.",
        },
    },
    "promo": {
        "label": "Promotional banner",
        "description": "The wide panel between collections and best sellers. Switch it off to hide it.",
        "sort_order": 50,
        "payload": {
            "heading": "Free cap sizing kit with your first wig order.",
            "body": ("Get a precise, comfortable fit before you buy — we'll mail a sizing kit "
                     "and walk you through it over WhatsApp."),
            "cta_label": "Claim Your Kit",
            "cta_href": "#collections",
        },
    },
    "bestsellers": {
        "label": "Best sellers section",
        "description": ("Heading and intro above the best-sellers grid. The products shown are "
                        "whichever published products are flagged Bestseller."),
        "sort_order": 60,
        "payload": {
            "heading": "Best sellers",
            "intro": "Our most-loved units, chosen by customers who wear them every day.",
        },
    },
    "editorial": {
        "label": "Editorial block",
        "description": "The image-and-text story panel.",
        "sort_order": 70,
        "payload": {
            "eyebrow": "The Hairshalo Difference",
            "heading": "Looks natural. Feels like yours.",
            "paragraphs": [
                ("Every unit starts with ethically sourced human hair, hand-tied strand by "
                 "strand onto breathable Swiss lace. No stiff parting, no plastic shine — "
                 "just movement, softness, and a scalp-like finish that holds up to real, "
                 "everyday life."),
                ("Our stylists size, trim, and customise the hairline for you before it ships, "
                 "so what arrives is ready to wear from day one."),
            ],
            "cta_label": "Discover Our Hair",
            "cta_href": "#collections",
            "image_url": "",
            "image_tag": "Density: Soft & natural",
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
            "intro": "Drag to compare — from natural roots to a completely blended install.",
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
        "label": "Why customers choose us",
        "description": "The four-up grid of selling points.",
        "sort_order": 80,
        "payload": {
            "heading": "Why customers choose Hairshalo",
            "items": [
                {"icon": "star", "title": "100% Human Hair",
                 "body": "Natural movement, softness, and full styling versatility — heat-safe up to 180°C."},
                {"icon": "check", "title": "Ethically Sourced",
                 "body": "Every bundle is hand-selected for quality, consistency, and traceable origin."},
                {"icon": "layers", "title": "Long Lasting",
                 "body": "With proper care, our units stay soft and full for 12+ months of regular wear."},
                {"icon": "eye", "title": "Natural Finish",
                 "body": "HD lace and hand-plucked hairlines blend seamlessly with your own skin tone."},
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
                {"q": "How do I choose the right cap size?",
                 "a": ("We send a free sizing kit with a measuring guide, or you can book a "
                       "virtual fitting and our stylists will measure with you over video call.")},
                {"q": "How long does a human hair wig last?",
                 "a": ("With proper washing, conditioning, and storage, most customers get "
                       "12–18 months of regular wear from a single unit.")},
                {"q": "Can I colour or heat style the hair?",
                 "a": ("Yes — since it's 100% human hair, it can be safely heat-styled up to "
                       "180°C and coloured up to two shades darker at a professional salon.")},
                {"q": "Do you offer returns or exchanges?",
                 "a": ("Unworn units in original packaging can be exchanged within 7 days. "
                       "Our fit guarantee covers sizing issues on your first order.")},
                {"q": "How long does shipping take?",
                 "a": ("Most orders ship within 24 hours and arrive in 3–5 business days across "
                       "India, with live tracking sent to your email and WhatsApp.")},
            ],
        },
    },
    "newsletter": {
        "label": "Newsletter panel",
        "description": "Copy above the signup form. The double opt-in behaviour is not editable.",
        "sort_order": 100,
        "payload": {
            "heading": "Your best hair starts here",
            "body": "Styling tips, new arrivals, and offers — straight to your inbox.",
            "cta_label": "Join Us",
        },
    },
    "navigation": {
        "label": "Header navigation",
        "description": "The links across the top of the storefront, and in the mobile menu.",
        "sort_order": 110,
        "payload": {
            "items": [
                {"label": "Wigs", "href": "#collections"},
                {"label": "Extensions", "href": "#collections"},
                {"label": "Toppers", "href": "#bestsellers"},
                {"label": "Hair Care", "href": "#editorial"},
                {"label": "Book a Fitting", "href": "#faq"},
            ],
        },
    },
    "footer": {
        "label": "Footer",
        "description": "Brand blurb, link columns, legal links and the copyright line.",
        "sort_order": 120,
        "payload": {
            "blurb": ("Premium human hair wigs and extensions, fitted and finished for a "
                      "completely natural, everyday wear."),
            "columns": [
                {"heading": "Shop", "links": [
                    {"label": "Wigs", "href": "#collections"},
                    {"label": "Extensions", "href": "#collections"},
                    {"label": "Toppers", "href": "#collections"},
                    {"label": "Best Sellers", "href": "#bestsellers"},
                ]},
                {"heading": "Help", "links": [
                    {"label": "Contact", "href": "#contact"},
                    {"label": "Shipping", "href": "#shipping"},
                    {"label": "Returns", "href": "#returns"},
                    {"label": "FAQ", "href": "#faq"},
                ]},
                {"heading": "About", "links": [
                    {"label": "Our Story", "href": "#editorial"},
                    {"label": "Hair Care Guide", "href": "#editorial"},
                    {"label": "Reviews", "href": "#testimonials"},
                ]},
            ],
            "legal_links": [
                {"label": "Privacy Policy", "href": "#privacy"},
                {"label": "Terms", "href": "#terms"},
                {"label": "Refund Policy", "href": "#returns"},
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
        "description": ("Your shop's name, contact details and address. Used on the storefront "
                        "and in the footer."),
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
        "description": ("Long-form policy text. Each is shown on its own page; leave one empty "
                        "to hide that link."),
        "sort_order": 150,
        "payload": {
            "shipping": ("Most orders ship within 24 hours and arrive in 3–5 business days "
                         "across India, with live tracking sent to your email."),
            "returns": ("Unworn units in original packaging can be exchanged within 7 days. "
                        "Our fit guarantee covers sizing issues on your first order."),
            "terms": "",
            "privacy": "",
        },
    },
}


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
    return {b["key"]: b["payload"] for b in all_blocks(db) if b["is_active"]}


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
