"""Bring an existing database's storefront copy up to the current defaults.

Why this exists: `site_content.ensure_defaults` only inserts rows that are
missing, so changing `DEFAULTS` never reaches a database that was seeded
before the change. The demo copy that shipped first ("100% Human Hair", HD
lace, ethical sourcing, a 7-day fit guarantee, 24-hour dispatch, a free
sizing kit...) is therefore still live anywhere it was seeded, even though
the defaults no longer say it.

What it does, block by block:

* still exactly the old demo copy  -> replaced with the current default
* already the current default      -> left alone
* anything else (edited by staff)  -> left alone and listed for review;
                                      it is never overwritten
* business, social, payment, shop  -> never touched (their defaults did not
                                      change, and they hold the shop's own
                                      details)

It is a dry run unless --apply is given. With --apply it first writes every
block it is about to change to a JSON backup, and --restore puts a backup
back. Running it twice is harmless.

Production, from vera-full-project/ (backups land in ./backups on the host,
which is git-ignored):

    docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm \
      -v "$PWD/backups:/backups" api python -m app.update_site_content

    # the same command with --apply to write, or --restore /backups/<file>.json
"""
import argparse
import copy
import json
import os
import sys
from datetime import datetime

from app import models, site_content
from app.database import SessionLocal

ACTOR = "content-update"

# Blocks whose stored copy is the shop's own and is never changed here.
NEVER_TOUCH = {"business", "social", "payment", "shop"}

# The demo copy exactly as it was first seeded (site_content.DEFAULTS before
# the trust pass). A block is only replaced while it still matches this
# exactly, so nothing a member of staff has written is ever lost.
OLD_DEMO = json.loads(r"""
{
  "announcement": {
    "text": "Complimentary virtual consultation with every order  ·  Ships pan-India in 3–5 days"
  },
  "hero": {
    "eyebrow": "Premium Human Hair",
    "heading_lines": [
      "Hair that moves",
      "like it's always",
      "been yours."
    ],
    "heading_emphasis": "always",
    "body": "Hand-tied human hair wigs and extensions with a natural hairline, breathable lace, and a fit built for everyday wear — from your first install to your best hair day.",
    "primary_cta_label": "Shop Wigs",
    "primary_cta_href": "#collections",
    "secondary_cta_label": "Book a Fitting",
    "secondary_cta_href": "#faq",
    "chips": [
      "100% Human Hair",
      "HD Lace, Natural Hairline",
      "Free Cap Size Guidance"
    ],
    "image_url": "",
    "image_tag": "Glueless & Beginner-Friendly"
  },
  "trust": {
    "items": [
      {
        "icon": "star",
        "text": "100% Human Hair"
      },
      {
        "icon": "check",
        "text": "Natural Scalp Finish"
      },
      {
        "icon": "card",
        "text": "Secure Payments"
      },
      {
        "icon": "tick",
        "text": "7-Day Fit Guarantee"
      },
      {
        "icon": "arrow",
        "text": "Fast Pan-India Delivery"
      }
    ]
  },
  "collections": {
    "heading": "Find your perfect install",
    "intro": "Every texture and density, made for a natural finish and easy everyday wear."
  },
  "promo": {
    "heading": "Free cap sizing kit with your first wig order.",
    "body": "Get a precise, comfortable fit before you buy — we'll mail a sizing kit and walk you through it over WhatsApp.",
    "cta_label": "Claim Your Kit",
    "cta_href": "#collections"
  },
  "bestsellers": {
    "heading": "Best sellers",
    "intro": "Our most-loved units, chosen by customers who wear them every day."
  },
  "editorial": {
    "eyebrow": "The Hairshalo Difference",
    "heading": "Looks natural. Feels like yours.",
    "paragraphs": [
      "Every unit starts with ethically sourced human hair, hand-tied strand by strand onto breathable Swiss lace. No stiff parting, no plastic shine — just movement, softness, and a scalp-like finish that holds up to real, everyday life.",
      "Our stylists size, trim, and customise the hairline for you before it ships, so what arrives is ready to wear from day one."
    ],
    "cta_label": "Discover Our Hair",
    "cta_href": "#collections",
    "image_url": "",
    "image_tag": "Density: Soft & natural"
  },
  "before_after": {
    "heading": "The difference is in the detail",
    "intro": "Drag to compare — from natural roots to a completely blended install.",
    "before_image": "",
    "after_image": "",
    "before_label": "Before",
    "after_label": "After"
  },
  "why_choose": {
    "heading": "Why customers choose Hairshalo",
    "items": [
      {
        "icon": "star",
        "title": "100% Human Hair",
        "body": "Natural movement, softness, and full styling versatility — heat-safe up to 180°C."
      },
      {
        "icon": "check",
        "title": "Ethically Sourced",
        "body": "Every bundle is hand-selected for quality, consistency, and traceable origin."
      },
      {
        "icon": "layers",
        "title": "Long Lasting",
        "body": "With proper care, our units stay soft and full for 12+ months of regular wear."
      },
      {
        "icon": "eye",
        "title": "Natural Finish",
        "body": "HD lace and hand-plucked hairlines blend seamlessly with your own skin tone."
      }
    ]
  },
  "faq": {
    "heading": "Questions, answered",
    "items": [
      {
        "q": "How do I choose the right cap size?",
        "a": "We send a free sizing kit with a measuring guide, or you can book a virtual fitting and our stylists will measure with you over video call."
      },
      {
        "q": "How long does a human hair wig last?",
        "a": "With proper washing, conditioning, and storage, most customers get 12–18 months of regular wear from a single unit."
      },
      {
        "q": "Can I colour or heat style the hair?",
        "a": "Yes — since it's 100% human hair, it can be safely heat-styled up to 180°C and coloured up to two shades darker at a professional salon."
      },
      {
        "q": "Do you offer returns or exchanges?",
        "a": "Unworn units in original packaging can be exchanged within 7 days. Our fit guarantee covers sizing issues on your first order."
      },
      {
        "q": "How long does shipping take?",
        "a": "Most orders ship within 24 hours and arrive in 3–5 business days across India, with live tracking sent to your email and WhatsApp."
      }
    ]
  },
  "newsletter": {
    "heading": "Your best hair starts here",
    "body": "Styling tips, new arrivals, and offers — straight to your inbox.",
    "cta_label": "Join Us"
  },
  "navigation": {
    "items": [
      {
        "label": "Wigs",
        "href": "#collections"
      },
      {
        "label": "Extensions",
        "href": "#collections"
      },
      {
        "label": "Toppers",
        "href": "#bestsellers"
      },
      {
        "label": "Hair Care",
        "href": "#editorial"
      },
      {
        "label": "Book a Fitting",
        "href": "#faq"
      }
    ]
  },
  "footer": {
    "blurb": "Premium human hair wigs and extensions, fitted and finished for a completely natural, everyday wear.",
    "columns": [
      {
        "heading": "Shop",
        "links": [
          {
            "label": "Wigs",
            "href": "#collections"
          },
          {
            "label": "Extensions",
            "href": "#collections"
          },
          {
            "label": "Toppers",
            "href": "#collections"
          },
          {
            "label": "Best Sellers",
            "href": "#bestsellers"
          }
        ]
      },
      {
        "heading": "Help",
        "links": [
          {
            "label": "Contact",
            "href": "#contact"
          },
          {
            "label": "Shipping",
            "href": "#shipping"
          },
          {
            "label": "Returns",
            "href": "#returns"
          },
          {
            "label": "FAQ",
            "href": "#faq"
          }
        ]
      },
      {
        "heading": "About",
        "links": [
          {
            "label": "Our Story",
            "href": "#editorial"
          },
          {
            "label": "Hair Care Guide",
            "href": "#editorial"
          },
          {
            "label": "Reviews",
            "href": "#testimonials"
          }
        ]
      }
    ],
    "legal_links": [
      {
        "label": "Privacy Policy",
        "href": "#privacy"
      },
      {
        "label": "Terms",
        "href": "#terms"
      },
      {
        "label": "Refund Policy",
        "href": "#returns"
      }
    ],
    "copyright": "© 2026 Hairshalo. All rights reserved."
  },
  "policies": {
    "shipping": "Most orders ship within 24 hours and arrive in 3–5 business days across India, with live tracking sent to your email.",
    "returns": "Unworn units in original packaging can be exchanged within 7 days. Our fit guarantee covers sizing issues on your first order.",
    "terms": "",
    "privacy": ""
  }
}
""")


def _plan(db):
    site_content.ensure_defaults(db)
    rows = {r.key: r for r in db.query(models.SiteContent).all()}
    plan = []
    for key, spec in site_content.DEFAULTS.items():
        if key in NEVER_TOUCH or key not in OLD_DEMO:
            continue
        row = rows.get(key)
        current = dict(row.payload or {}) if row else None
        new = spec["payload"]
        if current == new:
            state = "current"
        elif current == OLD_DEMO[key]:
            state = "update"
        else:
            state = "edited"
        plan.append((key, state, current, new))
    return plan


def _fields(old, new):
    old, new = old or {}, new or {}
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    return ", ".join(changed) or "-"


def run(apply, backup_dir):
    db = SessionLocal()
    try:
        plan = _plan(db)
        width = max(len(k) for k, *_ in plan)
        print("APPLYING" if apply else "DRY RUN (nothing will be written)")
        print()
        labels = {
            "update": "updating" if apply else "will update",
            "current": "already current",
            "edited": "EDITED BY STAFF, skipped (review by hand)",
        }
        for key, state, old, new in plan:
            extra = "" if state == "current" else f"  [fields: {_fields(old, new)}]"
            print(f"  {key:<{width}}  {labels[state]}{extra}")

        todo = [(k, old, new) for k, state, old, new in plan if state == "update"]
        edited = [k for k, state, *_ in plan if state == "edited"]
        print()
        print(f"{len(todo)} to update, {len(edited)} edited by staff (left alone), "
              f"{len(plan) - len(todo) - len(edited)} already current.")
        if edited:
            print("Edited blocks keep their text. Compare them with the defaults in "
                  "app/site_content.py and change them in the Back Office (Content) if they "
                  "still make claims the shop cannot support.")
        if not todo:
            return 0
        if not apply:
            print()
            print("Run again with --apply to make these changes.")
            return 0

        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(backup_dir, f"site_content_backup_{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"taken_at": stamp, "blocks": {k: old for k, old, _ in todo}},
                      fh, ensure_ascii=False, indent=2)
        print()
        print(f"Backup of the {len(todo)} blocks written to {path}")

        for key, _, new in todo:
            site_content.update_block(db, key, payload=copy.deepcopy(new), actor=ACTOR)
        print(f"Updated {len(todo)} blocks.")
        return 0
    finally:
        db.close()


def restore(path):
    with open(path, encoding="utf-8") as fh:
        blocks = json.load(fh)["blocks"]
    db = SessionLocal()
    try:
        for key, payload in blocks.items():
            site_content.update_block(db, key, payload=payload, actor=ACTOR + "-restore")
            print(f"  restored {key}")
        print(f"Restored {len(blocks)} blocks from {path}.")
        return 0
    finally:
        db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Update the storefront copy to the current defaults (dry run by default).")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--backup-dir", default="/backups",
                    help="where --apply writes its backup (default: /backups)")
    ap.add_argument("--restore", metavar="BACKUP_JSON", help="put a backup back, then exit")
    args = ap.parse_args(argv)
    if args.restore:
        return restore(args.restore)
    return run(args.apply, args.backup_dir)


if __name__ == "__main__":
    sys.exit(main())
