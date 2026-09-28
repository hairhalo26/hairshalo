"""Swap a colour's photographs for new ones already uploaded to S3.

Why this exists: replacing the model photographs of 29 colours by hand in the
admin panel is ~90 uploads and ~90 deletions. The new files are uploaded to
the product bucket once (from CloudShell), and this applies a manifest that
says, per colour, which rows go and which files take their place.

A manifest is JSON:

    {"entries": [
       {"product": "human-hair-crochet",         # slug, or a name "like:Burmese%"
        "sku": "HS-HUMAN-HAIR-CROCHET-14-1",     # null = product-level photos
        "remove": [0, 1, 2],                      # sort orders of rows to replace
        "new": [{"key": "products/<uuid>.jpg", "size": 123456, "alt": "..."}]}
    ]}

Per entry, the rows named in "remove" must belong to that colour (or, with no
sku, to no colour); otherwise the entry is refused and nothing in it changes.
The new photographs take the removed rows' place in the gallery: with as many
new as removed they reuse the same sort orders exactly, so hand-picked
positions elsewhere (the homepage names gallery positions) stay valid. With a
different count, the product's gallery is renumbered in order.

Every new key must already be publicly readable in the bucket (checked with a
HEAD request) before anything is written. The removed files are deleted from
storage only after the database change is committed.

It is a dry run unless --apply is given:

    docker compose -f docker-compose.prod.yml --env-file .env.prod exec api \\
      python -m app.replace_model_photos <manifest.json or https URL> [--apply]
"""
import argparse
import json
import sys
import urllib.request

from app import models
from app.database import SessionLocal
from app.storage import get_product_storage, storage_for_key


def load_manifest(src):
    if src.startswith(("https://", "http://")):
        with urllib.request.urlopen(src, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(src, encoding="utf-8") as fh:
        return json.load(fh)


def default_exists(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:                       # noqa: BLE001
        return False


def find_product(db, ref):
    if ref.startswith("like:"):
        found = db.query(models.Product).filter(models.Product.name.ilike(ref[5:])).all()
        return found[0] if len(found) == 1 else None
    return db.query(models.Product).filter(models.Product.slug == ref).one_or_none()


def plan_entry(db, e):
    """Return (product, variant, rows_to_remove, error)."""
    product = find_product(db, e["product"])
    if product is None:
        return None, None, [], f"product {e['product']!r} not found (or not unique)"
    variant = None
    if e.get("sku"):
        variant = next((v for v in product.variants if v.sku == e["sku"]), None)
        if variant is None:
            return product, None, [], f"colour {e['sku']} not on {product.slug}"
    want_vid = variant.id if variant else None
    rows = []
    for so in e.get("remove", []):
        match = [m for m in product.media if m.sort_order == so]
        if len(match) != 1:
            return product, variant, [], f"expected one photo at position {so}, found {len(match)}"
        if match[0].variant_id != want_vid:
            return product, variant, [], f"photo at position {so} belongs to another colour"
        rows.append(match[0])
    if not e.get("new"):
        return product, variant, rows, "no new photos listed"
    return product, variant, rows, None


def run(manifest, apply, exists=default_exists, out=print):
    storage = get_product_storage()
    db = SessionLocal()
    doomed_keys, errors, done = [], 0, 0
    try:
        entries = manifest["entries"]
        # Every new file must be in place before anything changes.
        missing = [n["key"] for e in entries for n in e["new"] if not exists(storage.url_for(n["key"]))]
        if missing:
            for k in missing:
                out(f"  NOT UPLOADED  {k}")
            out(f"\n{len(missing)} new file(s) are not in the bucket yet; nothing was changed.")
            return 2
        for e in entries:
            product, variant, rows, err = plan_entry(db, e)
            label = f"{e['product']} / {e.get('sku') or 'product photos'}"
            if err:
                errors += 1
                out(f"  SKIP  {label}: {err}")
                continue
            out(f"  {label}: replace {len(rows)} photo(s) with {len(e['new'])}")
            for m in rows:
                out(f"      - {m.sort_order:>3}  {m.url}")
            for n in e["new"]:
                out(f"      + {storage.url_for(n['key'])}")
            if not apply:
                continue
            positions = sorted(m.sort_order for m in rows)
            was_primary = any(m.is_primary for m in rows)
            for m in rows:
                if m.storage_key:
                    doomed_keys.append(m.storage_key)
                db.delete(m)
            db.flush()
            new_rows = [models.ProductMedia(
                product_id=product.id, variant_id=variant.id if variant else None,
                url=storage.url_for(n["key"]), media_type=models.MediaType.image,
                alt_text=n.get("alt") or product.name, storage_key=n["key"],
                content_type="image/jpeg", file_size=n.get("size"), is_primary=False)
                for n in e["new"]]
            if len(new_rows) == len(positions):
                for r, so in zip(new_rows, positions):
                    r.sort_order = so
                db.add_all(new_rows)
            else:
                # Renumber: the new photographs go where the first removed one was.
                at = positions[0] if positions else 0
                rest = sorted((m for m in product.media if m not in rows), key=lambda m: m.sort_order or 0)
                ordered = [m for m in rest if (m.sort_order or 0) < at] + new_rows + \
                          [m for m in rest if (m.sort_order or 0) >= at]
                db.add_all(new_rows)
                for i, m in enumerate(ordered):
                    m.sort_order = i
            if was_primary:
                new_rows[0].is_primary = True
            db.commit()
            db.refresh(product)
            done += 1
    finally:
        db.close()
    # Only once the rows pointing at them are gone.
    for k in doomed_keys:
        storage_for_key(k).delete(k)
    verb = "Replaced" if apply else "Would replace"
    out(f"\n{verb} photos for {done if apply else len(manifest['entries']) - errors} colour(s); {errors} skipped.")
    if not apply:
        out("Dry run: nothing was changed. Run again with --apply.")
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", help="manifest JSON file or https URL")
    parser.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    args = parser.parse_args(argv)
    sys.exit(run(load_manifest(args.manifest), args.apply))


if __name__ == "__main__":
    main()
