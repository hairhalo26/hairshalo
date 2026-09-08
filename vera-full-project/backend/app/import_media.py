"""Import supplied product photography from ZIP archives.

    python -m app.import_media --source ../Product-Media            # dry run
    python -m app.import_media --source ../Product-Media --commit   # write

What this does NOT do, deliberately:

* It never modifies, renames or deletes the supplied archives. They are opened
  read-only and each member is read into memory — nothing is ever written to
  disk under a name the archive chose, which is the simplest way to be immune
  to path traversal and to zip entries pretending to be executables.
* It never executes anything from an archive, and refuses entries whose paths
  try to escape the extraction directory.
* It never publishes. New products are created as DRAFT so the existing
  Draft -> Review -> Published workflow still has the final say.
* It never invents commercial data. Price is left NULL and stock 0, because
  nothing in a photograph tells us what a wig costs or how many are in the
  stockroom. The publish guard already refuses a product with no price, which
  is the behaviour we want.
* It never alters an image. No resizing, cropping or re-encoding — the file
  that ships is the file that was supplied.

Mapping is derived from the archive layout rather than guessed:

    <root>/file.jpg                        -> product-level media
    <root>/<colour>/file.jpg               -> media for that colour variant
    <root>/<product>/<colour>/file.jpg     -> a product per second-level folder

Colour codes (1, 1B, 2, 4, 27, 30, 613, NC) are kept EXACTLY as supplied. They
are industry codes and translating them into names like "Honey Blonde" would be
inventing a claim about the product.
"""
import argparse
import hashlib
import io
import os
import re
import sys
import zipfile
from collections import OrderedDict, defaultdict

from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal
from app.storage import get_storage

# Trusted local files, so the cap is generous. This does NOT change the HTTP
# upload endpoint, which keeps its own 8 MB limit for anonymous callers.
IMPORT_MAX_IMAGE_BYTES = 32 * 1024 * 1024
IMPORT_MAX_VIDEO_BYTES = 128 * 1024 * 1024

MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg", "image"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png", "image"),
]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}

# Supplier layouts that the folder structure alone gets wrong, and the reason.
#
# Human Hair Crochet ships as <root>/<length folder>/<colour>/file, which reads
# as two separate products ("Crochet Deep 14", "Crochet Deep 18"). It is one
# product in two lengths: treating the second level as a LENGTH gives a proper
# length x colour variant matrix, which is what the storefront's variant
# selector is built for. As two products the length was lost entirely -- it
# lived only in the product name, and every variant's length field was null.
SECOND_LEVEL_IS_LENGTH = {
    "Human Hair Crochet.zip": "Human Hair Crochet",
}

# Spelling corrections to supplied folder names. Kept as an explicit table so
# it is obvious that a name was changed and why -- the importer otherwise
# preserves exactly what was supplied.
NAME_FIXES = {
    "Crochet Burmess 18inch": "Crochet Burmese 18 inch",   # supplied misspelling
}

# Longest first: 1B must win over 1, and 613 over 1/3.
COLOUR_CODES = ["natural color", "613", "1b", "27", "30", "nc", "1", "2", "4"]
COLOUR_RE = re.compile(
    r"(?:^|[^0-9a-z])(" + "|".join(re.escape(c) for c in COLOUR_CODES) + r")\s*#?\s*$",
    re.IGNORECASE,
)
LENGTH_RE = re.compile(r"(\d{1,2})\s*(?:''|\"|inch|in\b)", re.IGNORECASE)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s) or "product"


def clean_label(raw: str) -> str:
    """Tidy a folder name for display without inventing words.

    Only removes the mojibake left by non-UTF-8 archive filenames and collapses
    whitespace; every real character is kept.
    """
    s = raw.replace("▒", " ").replace("�", " ")
    s = re.sub(r"[—–_]+", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -#")


def parse_colour(folder: str):
    m = COLOUR_RE.search(clean_label(folder))
    if not m:
        return None
    code = m.group(1)
    if code.lower() in ("nc", "natural color"):
        return "Natural Color"
    return code.upper() if code.lower() == "1b" else code


def parse_length(text: str):
    m = LENGTH_RE.search(text or "")
    return m.group(1) + '"' if m else None


def parse_length_folder(folder: str):
    """Length from a folder the archive layout has declared to BE a length.

    Supplier folders are bare -- "Crochet Deep 14", not "Crochet Deep 14 inch"
    -- so the unit-bearing pattern finds nothing and every length collapses
    into one variant. A trailing number is only read as inches here, where the
    folder is known to mean a length; doing it in parse_length would turn any
    product name ending in a digit into a size.
    """
    value = parse_length(folder)
    if value:
        return value
    m = re.search(r"(\d{1,2})\s*$", clean_label(folder or ""))
    return m.group(1) + '"' if m else None


def sniff(head: bytes):
    """(content_type, extension, kind) from magic bytes, or None."""
    for sig, ct, ext, kind in MAGIC:
        if head.startswith(sig):
            return ct, ext, kind
    if head[4:8] == b"ftyp":
        return "video/mp4", ".mp4", "video"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp", "image"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", ".webm", "video"
    return None


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def safe_members(zf: zipfile.ZipFile):
    """Yield entries that are ordinary files with a path that cannot escape."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or any(p == ".." for p in parts):
            yield info, None, "unsafe path"
            continue
        if len(name) > 1 and name[1] == ":":
            yield info, None, "absolute path"
            continue
        yield info, name, None


class Report:
    def __init__(self):
        self.zips_inspected = 0
        self.zips_skipped = []          # (name, why)
        self.media_found = 0
        self.unsupported = []           # (path, why)
        self.duplicates = []            # (path, matches)
        self.not_imported = []          # (path, why)
        self.products_identified = OrderedDict()
        self.products_created = []
        self.products_matched = []
        self.needs_manual = []
        self.media_imported = 0
        self.variants_created = 0


def collect(source: str, report: Report):
    """Read every archive and build product -> variant -> files, in memory."""
    products = OrderedDict()
    for entry in sorted(os.listdir(source)):
        path = os.path.join(source, entry)
        if not entry.lower().endswith(".zip"):
            if os.path.isfile(path):
                # A loose media file is supplied product photography with no
                # archive to say which product it belongs to. Guessing would be
                # worse than saying so, so it is reported for manual mapping
                # rather than dropped silently or attached to something at
                # random.
                if os.path.splitext(entry)[1].lower() in (IMAGE_EXT | VIDEO_EXT):
                    report.needs_manual.append(
                        (entry, "loose media file — needs mapping to a product"))
                else:
                    report.not_imported.append((entry, "not a ZIP archive"))
            continue
        try:
            zf = zipfile.ZipFile(path)
        except Exception as exc:
            report.zips_skipped.append((entry, "unreadable: %s" % exc))
            continue
        report.zips_inspected += 1

        with zf:
            members = list(safe_members(zf))
            files = [(i, n) for i, n, bad in members if bad is None]
            for info, _n, bad in members:
                if bad:
                    report.unsupported.append((info.filename, bad))

            # An archive with no importable media is not product photography.
            media_like = [
                (i, n) for i, n in files
                if os.path.splitext(n)[1].lower() in (IMAGE_EXT | VIDEO_EXT)
            ]
            if not media_like:
                report.zips_skipped.append(
                    (entry, "contains no images or video (%d other files)" % len(files)))
                for _i, n in files:
                    report.not_imported.append((entry + "/" + n, "non-media archive"))
                continue

            for info, name in media_like:
                parts = name.split("/")
                rest = parts[1:-1] if len(parts) > 1 else []
                length_folder = None

                if entry in SECOND_LEVEL_IS_LENGTH and rest:
                    # One product; the second level says which length.
                    product_label = SECOND_LEVEL_IS_LENGTH[entry]
                    length_folder = rest[0]
                    colour_folder = rest[1] if len(rest) >= 2 else None
                elif len(rest) >= 2:
                    product_label, colour_folder = rest[0], rest[1]
                elif len(rest) == 1:
                    product_label, colour_folder = parts[0], rest[0]
                else:
                    product_label, colour_folder = parts[0], None

                product_label = clean_label(product_label)
                product_label = NAME_FIXES.get(product_label, product_label)
                colour = parse_colour(colour_folder) if colour_folder else None
                if colour_folder and colour is None:
                    report.needs_manual.append(
                        (name, "colour not recognised in folder %r" % colour_folder))

                bucket = products.setdefault(product_label, {
                    "archive": entry, "variants": defaultdict(list),
                    "length": parse_length(product_label) or parse_length(colour_folder or ""),
                })
                # A variant is a (length, colour) pair. Length is None unless the
                # archive distinguishes lengths, in which case the product-level
                # length would be wrong for half the files.
                length = parse_length_folder(length_folder) if length_folder else None
                bucket["variants"][(length, colour)].append((path, info.filename, name))
                report.media_found += 1

    report.products_identified = products
    return products


def read_member(archive_path, member_name):
    with zipfile.ZipFile(archive_path) as zf:
        with zf.open(member_name) as fh:
            return fh.read()


def import_all(source: str, db: Session, commit: bool, report: Report):
    products = collect(source, report)
    storage = get_storage()
    seen_hashes = {}

    for label, bucket in products.items():
        slug = slugify(label)
        product = (db.query(models.Product)
                     .filter(models.Product.slug == slug).one_or_none())
        if product is None:
            product = (db.query(models.Product)
                         .filter(models.Product.name.ilike(label)).one_or_none())

        if product is not None:
            report.products_matched.append((label, product.id, product.status.value))
        else:
            product = models.Product(
                name=label,
                slug=slug,
                # DRAFT, and no price: an admin decides both. The publish guard
                # already refuses a product with no price or no image.
                status=models.ProductStatus.draft,
                short_description="",
                description="",
            )
            if commit:
                db.add(product)
                db.flush()
            report.products_created.append(label)

        # Re-running the import must not duplicate anything. Storage keys are
        # fresh UUIDs every run, so they can never match; identity has to come
        # from the CONTENT. Size is checked first because it is free, and the
        # file is only hashed when a size actually collides.
        existing_sizes = defaultdict(list)
        if commit and product.id:
            for m in product.media:
                existing_sizes[m.file_size or 0].append(m)

        def already_imported(blob):
            candidates = existing_sizes.get(len(blob))
            if not candidates:
                return False
            for m in candidates:
                if not m.storage_key:
                    continue
                try:
                    with open(storage._path(m.storage_key), "rb") as fh:
                        if hashlib.sha256(fh.read()).hexdigest() == hashlib.sha256(blob).hexdigest():
                            return True
                except (OSError, AttributeError):
                    continue
            return False
        order = len(product.media) if commit and product.id else 0
        product_has_primary = any(m.is_primary for m in product.media) if commit and product.id else False

        for (vlength, colour), files in bucket["variants"].items():
            variant = None
            # Fall back to the product-level length when the archive does not
            # distinguish lengths, so single-length products keep their size.
            length = vlength or bucket["length"]
            if colour:
                # The length is part of the SKU whenever the archive carries
                # one: without it 14" 1B and 18" 1B collide on the same SKU and
                # the second would silently reuse the first variant's stock.
                parts_sku = [slug.upper()[:18]]
                if vlength:
                    parts_sku.append(re.sub(r"\W+", "", vlength).upper())
                parts_sku.append(re.sub(r"\W+", "", colour).upper())
                sku = "HS-" + "-".join(parts_sku)
                variant = (db.query(models.ProductVariant)
                             .filter(models.ProductVariant.sku == sku).one_or_none())
                if variant is None:
                    variant = models.ProductVariant(
                        product_id=product.id if commit else None,
                        sku=sku,
                        color=colour,
                        length=length,
                        # Nothing in a photograph says how many exist. Zero with
                        # no movement row keeps the ledger invariant true.
                        stock=0,
                    )
                    if commit:
                        db.add(variant)
                        db.flush()
                    report.variants_created += 1

            variant_has_primary = False
            for archive_path, member, display in sorted(files, key=lambda f: natural_key(f[2])):
                data = read_member(archive_path, member)
                kind = sniff(data[:16])
                if kind is None:
                    report.unsupported.append((display, "unrecognised file contents"))
                    continue
                content_type, ext, media_kind = kind

                digest = hashlib.sha256(data).hexdigest()
                if digest in seen_hashes:
                    report.duplicates.append((display, seen_hashes[digest]))
                    continue
                seen_hashes[digest] = display

                cap = (IMPORT_MAX_VIDEO_BYTES if media_kind == "video"
                       else IMPORT_MAX_IMAGE_BYTES)
                if len(data) > cap:
                    report.needs_manual.append(
                        (display, "%.1f MB exceeds the import limit" % (len(data) / 1e6)))
                    continue

                if commit and already_imported(data):
                    report.duplicates.append((display, "already in this product"))
                    continue

                order += 1
                if not commit:
                    report.media_imported += 1
                    continue

                key, written = storage.save(io.BytesIO(data), ext, cap)
                is_primary = (not product_has_primary and media_kind == "image")
                media = models.ProductMedia(
                    product_id=product.id,
                    variant_id=variant.id if variant is not None else None,
                    url=storage.url_for(key),
                    media_type=(models.MediaType.video if media_kind == "video"
                                else models.MediaType.image),
                    alt_text="%s%s%s" % (
                        label,
                        " — %s" % length if length else "",
                        " — colour %s" % colour if colour else ""),
                    sort_order=order,
                    is_primary=is_primary,
                    storage_key=key,
                    content_type=content_type,
                    file_size=written,
                )
                if is_primary:
                    product_has_primary = True
                existing_sizes[written].append(media)
                variant_has_primary = variant_has_primary or is_primary
                db.add(media)
                report.media_imported += 1

        if commit:
            db.flush()

    if commit:
        db.commit()
    else:
        db.rollback()
    return report


def print_report(report: Report, commit: bool):
    w = sys.stdout.write
    w("\n" + "=" * 74 + "\n")
    w("PRODUCT MEDIA IMPORT — %s\n" % ("COMMITTED" if commit else "DRY RUN (nothing written)"))
    w("=" * 74 + "\n")
    w("ZIPs inspected................ %d\n" % report.zips_inspected)
    w("Media files found............. %d\n" % report.media_found)
    w("Products identified........... %d\n" % len(report.products_identified))
    w("  created as Draft............ %d\n" % len(report.products_created))
    w("  matched to existing......... %d\n" % len(report.products_matched))
    w("Variants created.............. %d\n" % report.variants_created)
    w("Media imported................ %d\n" % report.media_imported)
    w("Duplicates skipped............ %d\n" % len(report.duplicates))
    w("Unsupported / corrupt......... %d\n" % len(report.unsupported))
    w("Requires manual attention..... %d\n" % len(report.needs_manual))
    w("Intentionally not imported.... %d\n" % (len(report.not_imported) + len(report.zips_skipped)))

    w("\nPRODUCTS\n")
    for label, bucket in report.identified_items():
        colours = sorted({c for _l, c in bucket["variants"] if c})
        lengths = sorted({l for l, _c in bucket["variants"] if l})
        loose = len(bucket["variants"].get((None, None), []))
        files = sum(len(v) for v in bucket["variants"].values())
        w("  %-44s %2d colours%s  %3d files  (%s)\n" % (
            label, len(colours),
            ", %d unassigned" % loose if loose else "               ",
            files, bucket["archive"]))
        if lengths:
            w("      lengths: %s\n" % ", ".join(lengths))
        if colours:
            w("      colours: %s\n" % ", ".join(colours))

    if report.zips_skipped:
        w("\nARCHIVES NOT IMPORTED\n")
        for name, why in report.zips_skipped:
            w("  %-42s %s\n" % (name[:42], why))
    if report.duplicates:
        w("\nDUPLICATE MEDIA (identical contents, imported once)\n")
        for path, first in report.duplicates[:12]:
            w("  %s\n      same as %s\n" % (path, first))
    if report.needs_manual:
        w("\nREQUIRES MANUAL ATTENTION\n")
        for path, why in report.needs_manual[:20]:
            w("  %-58s %s\n" % (path[-58:], why))
    if report.unsupported:
        w("\nUNSUPPORTED / CORRUPT\n")
        for path, why in report.unsupported[:20]:
            w("  %-58s %s\n" % (path[-58:], why))

    w("\nEvery product above was created as DRAFT with no price and zero stock.\n")
    w("An admin sets price, stock and description, then publishes.\n")


def _identified_items(self):
    return self.products_identified.items()


Report.identified_items = _identified_items


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import supplied product media.")
    ap.add_argument("--source", default=os.path.join("..", "Product-Media"))
    ap.add_argument("--commit", action="store_true",
                    help="write to the database (default is a dry run)")
    args = ap.parse_args(argv)

    source = os.path.abspath(args.source)
    if not os.path.isdir(source):
        sys.exit("No such directory: %s" % source)

    report = Report()
    db = SessionLocal()
    try:
        import_all(source, db, args.commit, report)
    finally:
        db.close()
    print_report(report, args.commit)


if __name__ == "__main__":
    main()
