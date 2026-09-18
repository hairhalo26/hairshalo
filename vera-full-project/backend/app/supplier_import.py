"""Generic supplier catalogue import — CSV or JSON files Hairshalo is authorised to use.

Supplier-agnostic by design: nothing here knows any particular supplier. A file
goes through four explicit steps, and nothing reaches the storefront without an
admin publishing it by hand:

    upload   parse the file, keep the raw rows, suggest a column mapping
    preview  map columns -> Hairshalo fields, price, validate, find duplicates
    commit   create/update the admin-selected products — always as Draft
    review   the admin edits, adds photos, sets stock, and publishes

Rules this module enforces:

* **Imported products are never published.** New products are Draft; an update
  never changes a product's status.
* **A supplier price is never a selling price.** It is stored on the supplier
  link. The selling price is calculated (rate -> markup -> rounding) or typed by
  the admin, and an existing product's price only changes when the admin
  explicitly asks for it.
* **Supplier availability is not Hairshalo stock.** Stock starts at 0 unless the
  admin opts in to using a supplier quantity column for NEW variants.
* **No arbitrary fetching.** Images are downloaded only when the admin confirms
  the rights for this import, only over HTTPS, only from hosts listed on the
  supplier record, only after DNS resolves to public addresses (and the socket
  connects to exactly the vetted address), with no redirects and a size cap.
* **One bad row costs one row.** Every product commits in its own savepoint.
"""
import csv
import hashlib
import html
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import http.client
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app import models, inventory, currency
from app.pricing import compute_pricing, PricingError, money
from app.storage import get_storage, validate_and_classify, UploadRejected

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_CELL_CHARS = 20000
MAX_IMAGES_PER_IMPORT = 300
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 15

ROUNDING_CHOICES = ("none", "nearest_1", "nearest_10", "nearest_100", "end_99")


class SupplierImportError(ValueError):
    """A problem with a whole file (not a row). The message is shown to the admin."""


# ---------------------------------------------------------------- field catalog
# key -> (label, level, synonyms). `level` says whether a column describes the
# product or one of its variants. Synonyms drive the suggested mapping only; the
# admin always sees and can change it.
TARGET_FIELDS: List[dict] = [
    {"key": "source_product_id", "label": "Supplier product ID", "level": "product",
     "synonyms": ["product_id", "productid", "id", "handle", "item_id", "parent_id", "style_code", "group_id"]},
    {"key": "name", "label": "Product name", "level": "product",
     "synonyms": ["name", "title", "product_title", "product_name", "item_name"]},
    {"key": "brand", "label": "Brand", "level": "product", "synonyms": ["brand", "vendor", "manufacturer"]},
    {"key": "description", "label": "Description", "level": "product",
     "synonyms": ["description", "body", "body_html", "long_description", "details"]},
    {"key": "short_description", "label": "Short description", "level": "product",
     "synonyms": ["short_description", "summary", "subtitle", "tagline"]},
    {"key": "category", "label": "Category", "level": "product",
     "synonyms": ["category", "product_category", "collection", "department"]},
    {"key": "subcategory", "label": "Subcategory", "level": "product",
     "synonyms": ["subcategory", "sub_category", "subcat"]},
    {"key": "product_type", "label": "Product type", "level": "product",
     "synonyms": ["product_type", "type", "item_type"]},
    {"key": "hair_type", "label": "Hair type", "level": "product",
     "synonyms": ["hair_type", "fibre", "fiber", "material", "hair_material"]},
    {"key": "texture", "label": "Texture", "level": "product", "synonyms": ["texture", "curl_pattern", "style"]},
    {"key": "construction", "label": "Construction (lace front, full lace…)", "level": "product",
     "synonyms": ["construction", "cap_construction", "wig_type"]},
    {"key": "weight", "label": "Weight", "level": "product", "synonyms": ["weight", "grams", "net_weight"]},
    {"key": "heat_resistance", "label": "Heat resistance", "level": "product",
     "synonyms": ["heat_resistance", "heat_resistant", "max_heat", "heat"]},
    {"key": "features", "label": "Features", "level": "product", "synonyms": ["features", "key_features"]},
    {"key": "benefits", "label": "Benefits", "level": "product", "synonyms": ["benefits"]},
    {"key": "care_instructions", "label": "Care instructions", "level": "product",
     "synonyms": ["care_instructions", "care", "how_to_care", "aftercare"]},
    {"key": "source_url", "label": "Supplier product URL", "level": "product",
     "synonyms": ["url", "product_url", "source_url", "link"]},
    {"key": "source_price", "label": "Supplier price", "level": "product",
     "synonyms": ["price", "cost_price", "cost", "supplier_price", "wholesale_price", "trade_price"]},
    {"key": "source_currency", "label": "Supplier currency", "level": "product",
     "synonyms": ["currency", "currency_code", "price_currency"]},
    {"key": "availability", "label": "Supplier availability", "level": "product",
     "synonyms": ["availability", "available", "in_stock", "status"]},
    {"key": "image_urls", "label": "Images (URLs or file names)", "level": "product",
     "synonyms": ["images", "image", "image_url", "image_urls", "image_src", "photos", "image_files"]},
    {"key": "source_variant_id", "label": "Supplier variant ID", "level": "variant",
     "synonyms": ["variant_id", "variantid", "option_id"]},
    {"key": "supplier_sku", "label": "Supplier SKU", "level": "variant",
     "synonyms": ["supplier_sku", "sku", "variant_sku", "item_code", "code"]},
    {"key": "sku", "label": "Hairshalo SKU (optional)", "level": "variant",
     "synonyms": ["hairshalo_sku", "our_sku", "retail_sku"]},
    {"key": "length", "label": "Length", "level": "variant", "synonyms": ["length", "size_length", "inches"]},
    {"key": "color", "label": "Colour", "level": "variant", "synonyms": ["colour", "color", "shade"]},
    {"key": "density", "label": "Density", "level": "variant", "synonyms": ["density", "volume"]},
    {"key": "lace_type", "label": "Lace type", "level": "variant", "synonyms": ["lace_type", "lace"]},
    {"key": "cap_size", "label": "Cap size / size", "level": "variant",
     "synonyms": ["cap_size", "cap", "size"]},
    {"key": "variant_price", "label": "Supplier variant price", "level": "variant",
     "synonyms": ["variant_price", "variant_cost", "option_price", "price", "cost"]},
    {"key": "variant_availability", "label": "Supplier variant availability", "level": "variant",
     "synonyms": ["variant_availability", "variant_available", "variant_in_stock", "available", "availability"]},
    {"key": "stock_quantity", "label": "Supplier quantity", "level": "variant",
     "synonyms": ["quantity", "qty", "stock", "inventory", "inventory_quantity", "stock_quantity"]},
    {"key": "variant_image", "label": "Variant image (URL or file name)", "level": "variant",
     "synonyms": ["variant_image", "variant_image_url", "swatch_image", "image", "image_url"]},
]
FIELD_KEYS = {f["key"] for f in TARGET_FIELDS}
VARIANT_ATTRS = ("length", "color", "density", "lace_type", "cap_size")
# Descriptive product fields an "update" may change, with their model columns.
DESCRIPTIVE_FIELDS = ("name", "brand", "description", "short_description", "product_type",
                      "hair_type", "texture", "construction", "weight", "heat_resistance",
                      "features", "benefits", "care_instructions")
FIELD_LIMITS = {"name": 200, "brand": 120, "short_description": 300, "product_type": 120,
                "hair_type": 120, "texture": 120, "construction": 120, "weight": 60,
                "heat_resistance": 120, "sku": 64, "supplier_sku": 120, "length": 60,
                "color": 80, "density": 60, "lace_type": 80, "cap_size": 60}


# ---------------------------------------------------------------- helpers
def _norm_key(text: str) -> str:
    text = str(text or "").lower()
    text = text.split(".", 1)[1] if text.startswith("variants.") else text
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def normalize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def clean_text(value, limit: int = MAX_CELL_CHARS) -> str:
    """Supplier text as plain text: tags removed, whitespace tidied, capped.

    Supplier descriptions often arrive as HTML. The storefront renders product
    text as text, so markup would show literally — and it is never trusted.
    """
    if value is None:
        return ""
    text = str(value)
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:                      # noqa: BLE001 - fall back to escaping
            text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text).replace("\r", "")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return text[:limit].strip()


def as_lines(value) -> str:
    """Features/benefits: one point per line, from a list or a delimited string."""
    if value is None:
        return ""
    if isinstance(value, list):
        items = value
    else:
        text = clean_text(value)
        items = re.split(r"\s*(?:\||;|\n)\s*", text) if re.search(r"[|;\n]", text) else [text]
    return "\n".join(clean_text(i, 500).lstrip("•- ").strip() for i in items if clean_text(i, 500))


def parse_money(value) -> Optional[Decimal]:
    """A supplier price, tolerant of '£1,234.50' and '1.234,50'; None when absent."""
    if value is None or str(value).strip() == "":
        return None
    text = re.sub(r"[^\d,.\-]", "", str(value).strip())
    if text.count(",") and text.count("."):
        # Whichever separator comes last is the decimal point.
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else \
            text.replace(".", "").replace(",", ".")
    elif text.count(",") == 1 and len(text.split(",")[1]) in (1, 2):
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"'{value}' is not a valid price")
    if amount < 0:
        raise ValueError(f"'{value}' is a negative price")
    if amount > Decimal("10000000"):
        raise ValueError(f"'{value}' is implausibly large")
    return amount.quantize(Decimal("0.01"))


def parse_quantity(value) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        qty = int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{value}' is not a whole-number quantity")
    if qty < 0:
        raise ValueError(f"'{value}' is a negative quantity")
    return min(qty, 100000)


def valid_currency(code: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{3}", code or ""))


def default_exchange_rate(code: str) -> Optional[Decimal]:
    """A suggested rate, INR per unit of `code`, from the shop's rate table.

    Only a suggestion — the admin sets the rate an import actually uses.
    """
    code = (code or "").upper()
    if code == currency.BASE:
        return Decimal("1")
    rate, _source = currency.rate_for(code)          # units of `code` per 1 INR
    if not rate or code not in currency.STATIC_RATES:
        return None
    return (Decimal("1") / Decimal(rate)).quantize(Decimal("0.0001"))


# ---------------------------------------------------------------- pricing
def pricing_settings(raw: dict, import_currency: str) -> dict:
    """Validate the admin's pricing configuration. Raises SupplierImportError."""
    raw = raw or {}
    try:
        rate = Decimal(str(raw.get("exchange_rate") if raw.get("exchange_rate") not in (None, "")
                           else (default_exchange_rate(import_currency) or "")))
    except InvalidOperation:
        raise SupplierImportError("Enter an exchange rate: how many ₹ one unit of the supplier's currency is worth.")
    if rate <= 0:
        raise SupplierImportError("The exchange rate must be more than zero.")
    try:
        pct = Decimal(str(raw.get("markup_percent") or 0))
        fixed = Decimal(str(raw.get("markup_fixed") or 0))
    except InvalidOperation:
        raise SupplierImportError("Markup must be a number.")
    if pct < 0 or pct > 1000 or fixed < 0:
        raise SupplierImportError("Markup must be between 0% and 1000%, and the fixed markup cannot be negative.")
    rounding = raw.get("rounding") or "nearest_1"
    if rounding not in ROUNDING_CHOICES:
        raise SupplierImportError(f"Rounding must be one of: {', '.join(ROUNDING_CHOICES)}.")
    return {"exchange_rate": str(rate), "markup_percent": str(pct), "markup_fixed": str(fixed),
            "rounding": rounding, "currency": import_currency}


def calculate_price(source_price: Optional[Decimal], settings: dict) -> Optional[Decimal]:
    """Supplier price -> ₹ via rate -> + markup % -> + fixed markup -> rounding."""
    if source_price is None:
        return None
    value = Decimal(source_price) * Decimal(settings["exchange_rate"])
    value = value * (Decimal("1") + Decimal(settings["markup_percent"]) / Decimal("100"))
    value = value + Decimal(settings["markup_fixed"])
    rounding = settings["rounding"]
    if rounding == "nearest_1":
        value = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    elif rounding == "nearest_10":
        value = (value / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 10
    elif rounding == "nearest_100":
        value = (value / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 100
    elif rounding == "end_99":
        # Up to the next price ending in 99: 4,850 -> 4,899; 4,900 -> 4,999.
        value = Decimal(math.floor(value / 100) * 100 + 99)
    return money(value)


# ---------------------------------------------------------------- parsing
def parse_file(filename: str, data: bytes) -> Tuple[str, List[str], List[dict]]:
    """Return (format, columns, rows). Every row carries __line and __group."""
    if not data:
        raise SupplierImportError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise SupplierImportError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if b"\x00" in data[:4096]:
        raise SupplierImportError("This does not look like a text file. Upload a CSV or JSON export.")
    lower = (filename or "").lower()
    head = data.lstrip()[:1]
    if lower.endswith(".json") or head in (b"[", b"{"):
        fmt, rows = "json", _parse_json(data)
    elif lower.endswith((".csv", ".txt", ".tsv")) or not lower:
        fmt, rows = "csv", _parse_csv(data)
    else:
        raise SupplierImportError("Only CSV and JSON files can be imported.")
    if not rows:
        raise SupplierImportError("The file has no product rows.")
    if len(rows) > MAX_ROWS:
        raise SupplierImportError(f"The file has {len(rows)} rows; the limit is {MAX_ROWS}. Split it into smaller files.")
    columns: List[str] = []
    for row in rows:
        for key in row:
            if not key.startswith("__") and key not in columns:
                columns.append(key)
    return fmt, columns, rows


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SupplierImportError("The file's text encoding could not be read. Save it as UTF-8.")


def _parse_csv(data: bytes) -> List[dict]:
    text = _decode(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
        if not headers:
            raise SupplierImportError("The CSV has no header row.")
        if len(set(headers)) != len(headers):
            raise SupplierImportError("The CSV header has duplicate column names.")
        rows = []
        for index, raw in enumerate(reader, start=2):
            row = {}
            for key, value in raw.items():
                if key is None:           # more cells than headers
                    continue
                key = key.strip()
                if key:
                    row[key] = (value or "").strip()[:MAX_CELL_CHARS]
            if any(v for v in row.values()):
                row["__line"] = index
                row["__group"] = None
                rows.append(row)
            if len(rows) > MAX_ROWS:
                break
        return rows
    except csv.Error as exc:
        raise SupplierImportError(f"The CSV could not be read: {exc}")


def _scalar(value):
    if isinstance(value, (dict,)):
        return None
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("src") or item.get("url") or item.get("name")
            if item not in (None, ""):
                items.append(str(item))
        return " | ".join(items)
    return value if value is None else str(value)[:MAX_CELL_CHARS]


def _parse_json(data: bytes) -> List[dict]:
    try:
        payload = json.loads(_decode(data))
    except (ValueError, SupplierImportError) as exc:
        raise SupplierImportError(f"The JSON could not be read: {exc}")
    if isinstance(payload, dict):
        payload = next((payload[k] for k in ("products", "items", "data", "catalog")
                        if isinstance(payload.get(k), list)), None)
    if not isinstance(payload, list):
        raise SupplierImportError('The JSON must be a list of products, or an object with a "products" list.')
    rows: List[dict] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise SupplierImportError(f"Product {index} in the JSON is not an object.")
        base = {k: _scalar(v) for k, v in item.items()
                if k != "variants" and not isinstance(v, dict)}
        base = {k: v for k, v in base.items() if v is not None}
        variants = item.get("variants")
        if isinstance(variants, list) and variants:
            for vindex, variant in enumerate(variants, start=1):
                if not isinstance(variant, dict):
                    raise SupplierImportError(f"Product {index}, variant {vindex} is not an object.")
                row = dict(base)
                for k, v in variant.items():
                    s = _scalar(v)
                    if s is not None:
                        row[f"variants.{k}"] = s
                row["__line"] = f"{index}.{vindex}"
                row["__group"] = index
                rows.append(row)
        else:
            base["__line"] = str(index)
            base["__group"] = index
            rows.append(base)
    return rows


def suggest_mapping(columns: List[str], remembered: Optional[dict] = None) -> Dict[str, str]:
    """Hairshalo field -> supplier column. Remembered choices win when still present."""
    mapping: Dict[str, str] = {}
    for key, column in (remembered or {}).items():
        if key in FIELD_KEYS and column in columns:
            mapping[key] = column
    used = set(mapping.values())
    normalized = {c: _norm_key(c) for c in columns}
    for field in TARGET_FIELDS:
        if field["key"] in mapping:
            continue
        variant_level = field["level"] == "variant"
        # A product field never takes a per-variant column ("variants.price");
        # a variant field prefers one, and falls back to a flat CSV column.
        ordered = sorted((c for c in columns if variant_level or not c.startswith("variants.")),
                         key=lambda c: (c.startswith("variants.") != variant_level))
        for syn in field["synonyms"]:
            match = next((c for c in ordered if c not in used and normalized[c] == syn), None)
            if match:
                mapping[field["key"]] = match
                used.add(match)
                break
    return mapping


# ---------------------------------------------------------------- preview
def _get(row: dict, mapping: dict, key: str):
    column = mapping.get(key)
    if not column:
        return None
    value = row.get(column)
    return None if value is None or str(value).strip() == "" else value


def _split_refs(value) -> List[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"\s*[|,\n]\s*", str(value)) if p.strip()][:40]


def _is_url(ref: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", ref, re.I))


def host_allowed(host: str, allowed: List[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for entry in allowed or []:
        entry = str(entry).lower().strip().lstrip("*.").rstrip(".")
        if entry and (host == entry or host.endswith("." + entry)):
            return True
    return False


def _image_status(ref: str, supplier: models.Supplier, uploaded: dict) -> str:
    if _is_url(ref):
        parts = urlsplit(ref)
        if parts.scheme != "https":
            return "not https"
        if not host_allowed(parts.hostname, supplier.allowed_image_hosts or []):
            return "host not allowed"
        return "url"
    return "uploaded" if ref.lower().rsplit("/", 1)[-1] in uploaded else "file missing"


def _category_key(category: str, subcategory: str) -> str:
    category, subcategory = clean_text(category, 120), clean_text(subcategory, 120)
    return f"{category} > {subcategory}" if category and subcategory else (category or subcategory)


def auto_category(db: Session, key: str) -> Optional[str]:
    """Match a supplier category to an existing Hairshalo category by name."""
    if not key:
        return None
    parts = [p.strip().lower() for p in key.split(">")]
    cats = db.query(models.Category).all()
    by_name = {c.name.strip().lower(): c for c in cats}
    for part in reversed(parts):             # the most specific name first
        if part in by_name:
            return by_name[part].id
    return None


def build_preview(db: Session, supplier: models.Supplier, batch: models.SupplierImport,
                  mapping: dict, category_mapping: dict, pricing: dict) -> dict:
    """Transform, validate and duplicate-check the batch. Nothing is written but the batch."""
    mapping = {k: v for k, v in (mapping or {}).items() if k in FIELD_KEYS and v in (batch.columns or [])}
    if "name" not in mapping:
        raise SupplierImportError("Map a column to Product name before previewing.")
    import_currency = (supplier.currency or currency.BASE).upper()
    settings = pricing_settings(pricing, import_currency)
    uploaded = batch.image_files or {}
    category_mapping = dict(category_mapping or {})

    # ---- group rows into products
    groups: Dict[str, dict] = {}
    for row in batch.raw_rows or []:
        pid = _get(row, mapping, "source_product_id")
        name = _get(row, mapping, "name")
        if pid:
            key = f"id:{clean_text(pid, 200)}"
        elif row.get("__group") is not None:
            key = f"json:{row['__group']}"
        else:
            key = f"name:{normalize_name(name or '') or row.get('__line')}"
        groups.setdefault(key, {"key": key, "rows": []})["rows"].append(row)

    products: List[dict] = []
    sku_owners: Dict[str, List[str]] = {}
    name_owners: Dict[str, List[str]] = {}
    for key, group in groups.items():
        products.append(_transform_group(db, supplier, group, mapping, settings,
                                         import_currency, uploaded, sku_owners))

    # ---- in-file duplicates
    for p in products:
        if p["name"]:
            name_owners.setdefault(normalize_name(p["name"]) + "|" + normalize_name(p["brand"] or ""),
                                   []).append(p["key"])
    for p in products:
        for v in p["variants"]:
            owners = sku_owners.get(v["sku"].lower(), [])
            if len(set(owners)) > 1:
                p["errors"].append(f"SKU {v['sku']} appears in more than one product in this file.")
        same = name_owners.get(normalize_name(p["name"] or "") + "|" + normalize_name(p["brand"] or ""), [])
        if p["name"] and len(same) > 1 and same[0] != p["key"]:
            p["duplicate"] = {"status": "in_file", "reason": "Same name and brand as another product in this file"}

    # ---- categories
    categories: Dict[str, dict] = {}
    for p in products:
        ck = p["supplier_category"]
        if not ck:
            continue
        entry = categories.setdefault(ck, {"supplier_category": ck, "products": 0})
        entry["products"] += 1
    for ck, entry in categories.items():
        chosen = category_mapping.get(ck)
        if chosen and db.query(models.Category.id).filter(models.Category.id == chosen).first():
            entry.update(category_id=chosen, status="mapped")
        else:
            auto = auto_category(db, ck)
            entry.update(category_id=auto, status="auto" if auto else "unmapped")
        category_mapping[ck] = entry["category_id"]
    for p in products:
        p["category_id"] = category_mapping.get(p["supplier_category"]) if p["supplier_category"] else None
        if not p["category_id"]:
            p["warnings"].append("No Hairshalo category — choose one before publishing.")

    # ---- duplicates against the database
    for p in products:
        if not p["errors"] and p["duplicate"]["status"] == "new":
            _detect_existing(db, supplier, p, settings)
        p["valid"] = not p["errors"]
        p["action"] = ("import" if p["valid"] and p["duplicate"]["status"] == "new" else "skip")

    summary = {
        "total_rows": len(batch.raw_rows or []),
        "products": len(products),
        "valid": sum(1 for p in products if p["valid"]),
        "invalid": sum(1 for p in products if not p["valid"]),
        "duplicates": sum(1 for p in products if p["duplicate"]["status"] != "new"),
    }
    return {"summary": summary, "products": products,
            "categories": sorted(categories.values(), key=lambda c: c["supplier_category"]),
            "settings": {"mapping": mapping, "pricing": settings,
                         "category_mapping": {k: v for k, v in category_mapping.items() if v}}}


def _transform_group(db, supplier, group, mapping, settings, import_currency, uploaded, sku_owners):
    rows = group["rows"]
    first = rows[0]
    errors: List[str] = []
    warnings: List[str] = []

    def product_value(key):
        for row in rows:                          # first non-empty across the group
            value = _get(row, mapping, key)
            if value is not None:
                return value
        return None

    name = clean_text(product_value("name"), FIELD_LIMITS["name"])
    if not name:
        errors.append("Product name is empty.")
    description = clean_text(product_value("description"))
    short = clean_text(product_value("short_description"), FIELD_LIMITS["short_description"])
    if not short and description:
        first_sentence = re.split(r"(?<=[.!?])\s", description.replace("\n", " "), maxsplit=1)[0]
        short = first_sentence[:FIELD_LIMITS["short_description"]]

    row_currency = str(product_value("source_currency") or import_currency).strip().upper()
    if not valid_currency(row_currency):
        errors.append(f"'{row_currency}' is not a valid currency code.")
    elif row_currency != import_currency:
        errors.append(f"Priced in {row_currency}, but this supplier's currency is {import_currency}. "
                      "Import each currency as its own file.")

    try:
        source_price = parse_money(product_value("source_price"))
    except ValueError as exc:
        source_price = None
        errors.append(f"Supplier price: {exc}.")

    source_pid = clean_text(product_value("source_product_id"), 200)
    variants: List[dict] = []
    seen_attrs = set()
    for row in rows:
        v = {attr: clean_text(_get(row, mapping, attr), FIELD_LIMITS[attr]) or None for attr in VARIANT_ATTRS}
        supplier_sku = clean_text(_get(row, mapping, "supplier_sku"), FIELD_LIMITS["supplier_sku"]) or None
        own_sku = clean_text(_get(row, mapping, "sku"), FIELD_LIMITS["sku"]) or None
        line = row.get("__line")
        try:
            vprice = parse_money(_get(row, mapping, "variant_price"))
        except ValueError as exc:
            vprice = None
            errors.append(f"Line {line}: variant price {exc}.")
        try:
            qty = parse_quantity(_get(row, mapping, "stock_quantity"))
        except ValueError as exc:
            qty = None
            errors.append(f"Line {line}: {exc}.")
        attrs_key = tuple(v[a] or "" for a in VARIANT_ATTRS)
        if len(rows) > 1 and not any(attrs_key) and not supplier_sku:
            errors.append(f"Line {line}: a variant needs a SKU or at least one of length, colour, "
                          "density, lace type or size.")
        if any(attrs_key) and attrs_key in seen_attrs:
            errors.append(f"Line {line}: repeats another variant of this product.")
        seen_attrs.add(attrs_key)
        sku = own_sku or _generate_sku(supplier, supplier_sku, source_pid or name, v, len(variants))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-/]{0,63}", sku):
            errors.append(f"Line {line}: SKU '{sku}' may only contain letters, digits, - _ . /")
        sku_owners.setdefault(sku.lower(), []).append(group["key"])
        image_refs = _split_refs(_get(row, mapping, "variant_image"))
        variants.append({
            "line": line, "sku": sku, "supplier_sku": supplier_sku,
            "source_variant_id": clean_text(_get(row, mapping, "source_variant_id"), 200) or None,
            **v,
            "source_price": str(vprice) if vprice is not None else None,
            "calculated_price": str(calculate_price(vprice, settings)) if vprice is not None else None,
            "availability": clean_text(_get(row, mapping, "variant_availability"), 60) or None,
            "quantity": qty,
            "images": [{"ref": r, "status": _image_status(r, supplier, uploaded)} for r in image_refs],
        })
    skus_here = [x["sku"].lower() for x in variants]
    for dup in sorted({s for s in skus_here if skus_here.count(s) > 1}):
        errors.append(f"SKU {dup.upper()} is repeated within this product.")

    if source_price is None:
        prices = [Decimal(v["source_price"]) for v in variants if v["source_price"]]
        source_price = min(prices) if prices else None
    if source_price is None:
        warnings.append("No supplier price — set a Hairshalo price by hand.")
    calculated = calculate_price(source_price, settings)
    if calculated is not None and calculated <= 0:
        errors.append("The calculated price is zero; check the exchange rate and markup.")

    images = [{"ref": r, "status": _image_status(r, supplier, uploaded)}
              for r in _split_refs(product_value("image_urls"))]
    if not images and not any(v["images"] for v in variants):
        warnings.append("No images — add Hairshalo photos before publishing.")

    source_url = clean_text(product_value("source_url"), 500) or None
    if source_url and not re.match(r"^https?://", source_url, re.I):
        warnings.append("The supplier URL is not a web address and was not kept.")
        source_url = None

    fields = {
        "name": name, "brand": clean_text(product_value("brand"), FIELD_LIMITS["brand"]) or None,
        "description": description, "short_description": short,
        **{k: clean_text(product_value(k), FIELD_LIMITS[k]) or None
           for k in ("product_type", "hair_type", "texture", "construction", "weight", "heat_resistance")},
        "features": as_lines(product_value("features")) or None,
        "benefits": as_lines(product_value("benefits")) or None,
        "care_instructions": clean_text(product_value("care_instructions")) or None,
    }
    return {
        "key": group["key"],
        "lines": [r.get("__line") for r in rows],
        "source_product_id": source_pid or group["key"],
        "source_url": source_url,
        "name": name, "brand": fields["brand"],
        "fields": fields,
        "supplier_category": _category_key(product_value("category"), product_value("subcategory")),
        "category_id": None,
        "source_price": str(source_price) if source_price is not None else None,
        "source_currency": row_currency,
        "calculated_price": str(calculated) if calculated is not None else None,
        "availability": clean_text(product_value("availability"), 60) or None,
        "variants": variants,
        "images": images,
        "image_count": len(images) + sum(len(v["images"]) for v in variants),
        "errors": errors, "warnings": warnings,
        "duplicate": {"status": "new"},
        "raw": {k: v for k, v in first.items() if not k.startswith("__")},
    }


def _generate_sku(supplier, supplier_sku, base, attrs, index) -> str:
    """A Hairshalo SKU: HS<tag>-<supplier sku>, or a stable code from the name.

    SKUs are printed on the product page, so the prefix must not name the
    supplier. <tag> is four characters derived from the supplier's internal id:
    it keeps two suppliers' identical SKUs apart and means nothing to a customer.
    """
    code = "HS" + hashlib.sha1(str(supplier.id or supplier.name).encode()).hexdigest()[:4].upper()
    if supplier_sku:
        body = re.sub(r"[^A-Za-z0-9._\-/]+", "-", supplier_sku).strip("-")
    else:
        stem = hashlib.sha1(normalize_name(base).encode()).hexdigest()[:6].upper()
        tail = "-".join(re.sub(r"[^A-Za-z0-9]+", "", attrs[a] or "")[:6].upper()
                        for a in VARIANT_ATTRS if attrs.get(a))
        body = f"{stem}-{tail}" if tail else f"{stem}-{index + 1}"
    return f"{code}-{body}"[:64]


def _detect_existing(db: Session, supplier, p: dict, settings: dict) -> None:
    """Match against products already in Hairshalo — by the strongest evidence first."""
    match, reason = None, None
    link = db.query(models.SupplierProduct).filter(
        models.SupplierProduct.supplier_id == supplier.id,
        models.SupplierProduct.source_product_id == p["source_product_id"],
        models.SupplierProduct.product_id.isnot(None)).first()
    if link:
        match, reason = link.product, "Same supplier product ID, imported before"
    if match is None:
        skus = [v["supplier_sku"] for v in p["variants"] if v["supplier_sku"]]
        if skus:
            sv = (db.query(models.SupplierVariant).join(models.SupplierProduct)
                  .filter(models.SupplierProduct.supplier_id == supplier.id,
                          models.SupplierVariant.source_sku.in_(skus),
                          models.SupplierProduct.product_id.isnot(None)).first())
            if sv:
                match, reason = sv.supplier_product.product, f"Supplier SKU {sv.source_sku} already imported"
    if match is None:
        all_skus = [v["sku"] for v in p["variants"]] + [v["supplier_sku"] for v in p["variants"] if v["supplier_sku"]]
        variant = db.query(models.ProductVariant).filter(models.ProductVariant.sku.in_(all_skus)).first()
        if variant:
            match, reason = variant.product, f"SKU {variant.sku} already exists on a Hairshalo product"
    if match is None and p["source_url"]:
        link = db.query(models.SupplierProduct).filter(
            models.SupplierProduct.source_url == p["source_url"],
            models.SupplierProduct.product_id.isnot(None)).first()
        if link:
            match, reason = link.product, "Same supplier product URL"
    if match is None and p["name"]:
        target = normalize_name(p["name"])
        brand = normalize_name(p["brand"] or "")
        for candidate in db.query(models.Product).filter(
                models.Product.name.ilike(p["name"][:3] + "%")).all():
            if normalize_name(candidate.name) == target and \
                    (not brand or normalize_name(candidate.brand or "") in ("", brand)):
                match, reason = candidate, "Same product name" + (" and brand" if brand else "")
                break
    if match is None:
        return

    existing_skus = {v.sku.lower(): v for v in match.variants}
    linked_skus = {}
    for sp in db.query(models.SupplierProduct).filter(models.SupplierProduct.product_id == match.id):
        for sv in sp.variants:
            if sv.source_sku and sv.variant:
                linked_skus[sv.source_sku.lower()] = sv.variant
    changes = []
    for field in DESCRIPTIVE_FIELDS:
        new = p["fields"].get(field)
        current = getattr(match, field, None)
        if new and (current or "") != new:
            changes.append({"field": field, "current": (current or "")[:300], "new": new[:300]})
    new_variants, matched_variants = [], []
    for v in p["variants"]:
        hit = existing_skus.get(v["sku"].lower()) or \
            (linked_skus.get(v["supplier_sku"].lower()) if v["supplier_sku"] else None)
        (matched_variants if hit else new_variants).append(v["sku"])
    p["duplicate"] = {
        "status": "existing", "reason": reason,
        "product": {"id": match.id, "name": match.name, "status": match.status.value if match.status else "Draft",
                    "price": str(match.price) if match.price is not None else None,
                    "variants": [{"sku": v.sku, "label": v.label, "stock": v.stock or 0} for v in match.variants]},
        "changes": changes,
        "new_variants": new_variants, "matched_variants": matched_variants,
        "price_now": str(match.price) if match.price is not None else None,
        "price_new": p["calculated_price"],
    }


# ---------------------------------------------------------------- images
class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a hostname, but over a socket to one pre-vetted IP address.

    Resolving once and connecting to that exact address closes the DNS-rebinding
    gap: a second lookup inside the HTTP library could return 127.0.0.1.
    """

    def __init__(self, host, ip, **kw):
        super().__init__(host, 443, **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def vet_image_url(url: str, allowed_hosts: List[str]) -> Tuple[str, str]:
    """Return (host, ip) for a URL the importer may fetch, or raise ValueError."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("only https image URLs are fetched")
    if parts.username or parts.password:
        raise ValueError("URLs with credentials are not fetched")
    if parts.port not in (None, 443):
        raise ValueError("only the standard https port is allowed")
    host = (parts.hostname or "").lower()
    if not host or not host_allowed(host, allowed_hosts):
        raise ValueError(f"{host or 'the host'} is not on this supplier's allowed image hosts")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f"{host} does not resolve")
    addresses = {info[4][0] for info in infos}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"{host} resolves to a private or reserved address")
    return host, sorted(addresses)[0]


def _fetch_bytes(url: str, host: str, ip: str) -> Tuple[bytes, str]:
    """GET one image. No redirects, capped size. Replaced by a stub in tests."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    conn = _PinnedHTTPSConnection(host, ip, timeout=IMAGE_TIMEOUT_SECONDS)
    try:
        conn.request("GET", path, headers={"User-Agent": "Hairshalo-SupplierImport/1.0",
                                           "Accept": "image/*"})
        resp = conn.getresponse()
        if 300 <= resp.status < 400:
            raise ValueError("the server redirected; redirects are not followed")
        if resp.status != 200:
            raise ValueError(f"the server answered {resp.status}")
        data = resp.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        return data, (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
    finally:
        conn.close()


def store_image_bytes(data: bytes, content_type: str) -> Tuple[str, str, int]:
    """Validate (type + magic bytes + size) and store. Returns (key, content_type, size)."""
    if not data:
        raise ValueError("empty file")
    sniffed = _sniff_image_type(data[:32])
    if content_type not in ("image/jpeg", "image/png", "image/webp"):
        content_type = sniffed or content_type
    try:
        kind, ext, cap = validate_and_classify(content_type, data[:32])
    except UploadRejected as exc:
        raise ValueError(str(exc))
    if kind != "image":
        raise ValueError("only images can be attached to imported products")
    try:
        key, size = get_storage().save(io.BytesIO(data), ext, min(cap, MAX_IMAGE_BYTES))
    except UploadRejected as exc:
        raise ValueError(str(exc))
    return key, content_type, size


def _sniff_image_type(head: bytes) -> Optional[str]:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------- commit
def commit(db: Session, supplier: models.Supplier, batch: models.SupplierImport,
           selections: List[dict], options: dict, actor: str) -> dict:
    """Create/update the selected products, one savepoint each. Never publishes."""
    preview = batch.preview or {}
    products = {p["key"]: p for p in preview.get("products", [])}
    chosen = {s["key"]: s for s in selections or [] if s.get("key") in products}
    settings = (batch.settings or {}).get("pricing") or {}
    results, counts = [], {"imported": 0, "updated": 0, "skipped": 0, "errors": 0, "failed_images": 0}
    uploaded = dict(batch.image_files or {})
    used_uploads = set()
    images_budget = [MAX_IMAGES_PER_IMPORT]

    for key, p in products.items():
        sel = chosen.get(key)
        action = (sel or {}).get("action", "skip")
        entry = {"key": key, "name": p["name"], "lines": p["lines"], "action": action}
        if action == "skip" or sel is None:
            entry["outcome"] = "skipped"
            counts["skipped"] += 1
            results.append(entry)
            continue
        if not p.get("valid"):
            entry.update(outcome="error", message="; ".join(p["errors"]) or "Invalid product")
            counts["errors"] += 1
            results.append(entry)
            continue
        if action == "update" and p["duplicate"].get("status") != "existing":
            entry.update(outcome="error", message="There is no existing product to update.")
            counts["errors"] += 1
            results.append(entry)
            continue
        stored_keys: List[str] = []
        savepoint = db.begin_nested()
        try:
            if action == "update":
                product = db.query(models.Product).filter(
                    models.Product.id == p["duplicate"]["product"]["id"]).first()
                if product is None:
                    raise ValueError("The existing product was deleted after the preview.")
                info = _apply_update(db, supplier, batch, p, sel, product, settings, options, actor)
                entry.update(outcome="updated", product_id=product.id, **info)
                counts["updated"] += 1
            elif action in ("import", "new"):
                product, info = _create_product(db, supplier, batch, p, sel, settings, options, actor)
                entry.update(outcome="created", product_id=product.id, **info)
                counts["imported"] += 1
            else:
                raise ValueError(f"Unknown action '{action}'.")
            img = _attach_images(db, supplier, product, p, options, uploaded, used_uploads,
                                 stored_keys, images_budget)
            entry["images"] = img
            counts["failed_images"] += len(img["failed"])
            db.flush()
            savepoint.commit()
        except Exception as exc:                 # noqa: BLE001 - one product, not the batch
            savepoint.rollback()
            for k in stored_keys:
                get_storage().delete(k)
            entry.update(outcome="error", message=str(exc)[:500])
            entry.pop("product_id", None)
            counts["errors"] += 1
        results.append(entry)

    # Uploaded files no product used are removed, not left orphaned on disk.
    for name, info in uploaded.items():
        if name not in used_uploads and info.get("storage_key"):
            get_storage().delete(info["storage_key"])

    batch.results = results
    for k, v in counts.items():
        setattr(batch, k, v)
    batch.status = "committed"
    batch.committed_at = datetime.utcnow()
    return {"counts": counts, "results": results}


def _category_for(db, p, sel):
    wanted = (sel or {}).get("category_id") or p.get("category_id")
    if wanted and db.query(models.Category.id).filter(models.Category.id == wanted).first():
        return wanted
    return None


def _selling_price(p, sel):
    override = (sel or {}).get("price_override")
    if override not in (None, ""):
        try:
            value = money(Decimal(str(override)))
        except InvalidOperation:
            raise ValueError(f"'{override}' is not a valid price")
        if value is None or value <= 0:
            raise ValueError("The price override must be more than zero.")
        return value, "override"
    if p.get("calculated_price"):
        return Decimal(p["calculated_price"]), "calculated"
    return None, "none"


def _apply_product_price(target, price):
    try:
        new_price, compare_at, _amt, dtype, dvalue = compute_pricing(price, "none", 0)
    except PricingError as exc:
        raise ValueError(str(exc))
    target.price, target.compare_at_price = new_price, compare_at
    target.discount_type = models.DiscountKind(dtype)
    target.discount_value = dvalue


def _unique_slug(db, name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "product"
    slug, n = base, 1
    while db.query(models.Product.id).filter(models.Product.slug == slug).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _free_sku(db, sku):
    candidate, n = sku, 1
    while db.query(models.ProductVariant.id).filter(models.ProductVariant.sku == candidate).first():
        n += 1
        candidate = f"{sku[:60]}-{n}"
    return candidate


def _create_product(db, supplier, batch, p, sel, settings, options, actor):
    price, price_source = _selling_price(p, sel)
    fields = p["fields"]
    product = models.Product(
        name=fields["name"], slug=_unique_slug(db, fields["name"]),
        category_id=_category_for(db, p, sel),
        status=models.ProductStatus.draft,            # never published by an import
        is_demo=False,
        **{k: fields.get(k) for k in DESCRIPTIVE_FIELDS if k != "name"},
    )
    if price is not None:
        _apply_product_price(product, price)
    db.add(product)
    db.flush()
    link = _upsert_link(db, supplier, batch, p, product)
    created = []
    for v in p["variants"]:
        variant = _new_variant(db, product, v, settings, price, options, actor)
        _link_variant(db, link, v, variant)
        created.append(variant.sku)
    return product, {"variants_created": created, "price": str(price) if price is not None else None,
                     "price_source": price_source}


def _new_variant(db, product, v, settings, product_price, options, actor):
    variant = models.ProductVariant(
        product_id=product.id, sku=_free_sku(db, v["sku"]),
        **{a: v.get(a) for a in VARIANT_ATTRS},
        stock=0, is_available=True,
    )
    if v.get("calculated_price") and product_price is not None \
            and Decimal(v["calculated_price"]) != product_price:
        _apply_product_price(variant, Decimal(v["calculated_price"]))
    db.add(variant)
    db.flush()
    opening = v.get("quantity") if options.get("stock_from_supplier") and v.get("quantity") else 0
    inventory.open_stock(db, variant, int(opening or 0), actor=actor)
    return variant


def _upsert_link(db, supplier, batch, p, product):
    link = db.query(models.SupplierProduct).filter(
        models.SupplierProduct.supplier_id == supplier.id,
        models.SupplierProduct.source_product_id == p["source_product_id"]).first()
    if link is None:
        link = models.SupplierProduct(supplier_id=supplier.id,
                                      source_product_id=p["source_product_id"],
                                      first_imported_at=datetime.utcnow())
        db.add(link)
    link.product_id = product.id
    link.source_sku = next((v["supplier_sku"] for v in p["variants"] if v["supplier_sku"]), None)
    link.source_url = p["source_url"]
    link.source_name = p["name"]
    link.source_price = Decimal(p["source_price"]) if p["source_price"] else None
    link.source_currency = p["source_currency"]
    link.source_availability = p["availability"]
    link.source_file = batch.file_name
    link.source_data = {"row": p["raw"], "images": [i["ref"] for i in p["images"]],
                        "supplier_category": p["supplier_category"]}
    link.last_import_id = batch.id
    link.last_synced_at = datetime.utcnow()
    db.flush()
    return link


def _link_variant(db, link, v, variant):
    sv = None
    if v.get("supplier_sku"):
        sv = next((x for x in link.variants if (x.source_sku or "").lower() == v["supplier_sku"].lower()), None)
    if sv is None:
        sv = next((x for x in link.variants if x.variant_id == variant.id), None)
    if sv is None:
        sv = models.SupplierVariant(supplier_product=link)
        db.add(sv)
    sv.variant_id = variant.id
    sv.source_sku = v.get("supplier_sku")
    sv.source_variant_id = v.get("source_variant_id")
    sv.source_price = Decimal(v["source_price"]) if v.get("source_price") else None
    sv.source_availability = v.get("availability")
    sv.source_quantity = v.get("quantity")
    sv.last_synced_at = datetime.utcnow()


def _apply_update(db, supplier, batch, p, sel, product, settings, options, actor):
    """Update an existing product from the supplier row — never its status or stock."""
    changed = []
    for field in DESCRIPTIVE_FIELDS:
        new = p["fields"].get(field)
        if new and (getattr(product, field, None) or "") != new:
            setattr(product, field, new)
            changed.append(field)
    category = _category_for(db, p, sel)
    if category and ((sel or {}).get("category_id") or not product.category_id):
        if product.category_id != category:
            product.category_id = category
            changed.append("category")
    price_change = None
    price, price_source = _selling_price(p, sel)
    # The selling price moves only on an explicit instruction: a typed override
    # for this product, or "recalculate prices" ticked for the whole import.
    if price is not None and (price_source == "override" or options.get("update_prices")):
        old = product.price
        if old is None or Decimal(old) != price:
            _apply_product_price(product, price)
            price_change = {"from": str(old) if old is not None else None, "to": str(price)}
    link = _upsert_link(db, supplier, batch, p, product)
    by_sku = {v.sku.lower(): v for v in product.variants}
    by_supplier = {(sv.source_sku or "").lower(): sv.variant for sv in link.variants if sv.variant}
    added, updated_variants = [], []
    for v in p["variants"]:
        variant = by_sku.get(v["sku"].lower()) or (by_supplier.get(v["supplier_sku"].lower())
                                                   if v.get("supplier_sku") else None)
        if variant is None:
            variant = _new_variant(db, product, v, settings, product.price, options, actor)
            added.append(variant.sku)
        else:
            touched = False
            for attr in VARIANT_ATTRS:
                if v.get(attr) and getattr(variant, attr) != v[attr]:
                    setattr(variant, attr, v[attr])
                    touched = True
            if touched:
                updated_variants.append(variant.sku)
        _link_variant(db, link, v, variant)
    return {"fields_changed": changed, "variants_added": added,
            "variants_updated": updated_variants, "price_change": price_change,
            "price_kept": price_change is None}


def _attach_images(db, supplier, product, p, options, uploaded, used_uploads, stored_keys, budget):
    """Attach uploaded files and (only with confirmed rights) allowed-host URLs."""
    result = {"attached": 0, "failed": []}
    link = db.query(models.SupplierProduct).filter(
        models.SupplierProduct.product_id == product.id,
        models.SupplierProduct.supplier_id == supplier.id).first()
    already = set((link.source_data or {}).get("images_attached", [])) if link else set()
    variant_ids = {v.sku.lower(): v.id for v in product.variants}
    refs = [(i["ref"], None) for i in p["images"]]
    for v in p["variants"]:
        vid = variant_ids.get(v["sku"].lower())
        refs += [(i["ref"], vid) for i in v["images"]]
    has_image = any(m.media_type == models.MediaType.image for m in product.media)
    order = len(product.media)
    for ref, variant_id in refs:
        if ref in already:
            continue
        try:
            if _is_url(ref):
                if not options.get("images_authorized"):
                    raise ValueError("image rights not confirmed for this import — not downloaded")
                if budget[0] <= 0:
                    raise ValueError("image limit for one import reached")
                budget[0] -= 1
                host, ip = vet_image_url(ref, supplier.allowed_image_hosts or [])
                data, content_type = _fetch_bytes(ref, host, ip)
                key, content_type, size = store_image_bytes(data, content_type)
            else:
                info = uploaded.get(ref.lower().rsplit("/", 1)[-1])
                if not info:
                    raise ValueError("no uploaded file with this name")
                key, content_type, size = info["storage_key"], info["content_type"], info["size"]
                used_uploads.add(ref.lower().rsplit("/", 1)[-1])
                if any(m.storage_key == key for m in product.media):
                    continue
            if _is_url(ref):
                stored_keys.append(key)
            db.add(models.ProductMedia(
                product_id=product.id, url=get_storage().url_for(key),
                media_type=models.MediaType.image, alt_text=product.name,
                sort_order=order, is_primary=not has_image,
                storage_key=key, content_type=content_type, file_size=size,
                variant_id=variant_id))
            has_image = True
            order += 1
            already.add(ref)
            result["attached"] += 1
        except Exception as exc:                 # noqa: BLE001 - an image never fails a product
            result["failed"].append({"ref": ref[:300], "reason": str(exc)[:200]})
    if link is not None:
        data = dict(link.source_data or {})
        data["images_attached"] = sorted(already)
        link.source_data = data
    return result


def error_report_csv(batch: models.SupplierImport) -> str:
    """Rows that failed validation or commit, as CSV for the admin to fix."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["source lines", "product", "stage", "problem"])
    for p in (batch.preview or {}).get("products", []):
        for err in p.get("errors", []):
            writer.writerow([" ".join(str(x) for x in p.get("lines", [])), p.get("name"), "validation", err])
        for img in p.get("images", []):
            if img.get("status") not in ("url", "uploaded"):
                writer.writerow([" ".join(str(x) for x in p.get("lines", [])), p.get("name"),
                                 "image", f"{img['ref']}: {img['status']}"])
    for r in batch.results or []:
        if r.get("outcome") == "error":
            writer.writerow([" ".join(str(x) for x in r.get("lines", [])), r.get("name"), "import", r.get("message")])
        for f in (r.get("images") or {}).get("failed", []):
            writer.writerow([" ".join(str(x) for x in r.get("lines", [])), r.get("name"), "image",
                             f"{f['ref']}: {f['reason']}"])
    return out.getvalue()
