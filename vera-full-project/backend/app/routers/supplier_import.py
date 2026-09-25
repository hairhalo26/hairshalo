"""Supplier catalogue import — admin/staff only, every endpoint.

Flow: create a supplier -> upload a CSV/JSON file -> (optionally upload image
files) -> preview with a field mapping and pricing -> commit the selected
products as Draft. The import log is every batch, newest first.

Nothing here fetches a URL the caller names at request time. The only outbound
requests are image downloads during commit, gated in app/supplier_import.py.
"""
import re
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app import supplier_import as svc
from app.database import get_db
from app.deps import get_current_admin

router = APIRouter(prefix="/api/supplier-import", tags=["supplier-import"])

MAX_IMAGE_FILES_PER_REQUEST = 50


def _supplier_out(db: Session, s: models.Supplier) -> schemas.SupplierOut:
    count = db.query(func.count(models.SupplierProduct.id)).filter(
        models.SupplierProduct.supplier_id == s.id,
        models.SupplierProduct.product_id.isnot(None)).scalar() or 0
    return schemas.SupplierOut(
        id=s.id, name=s.name, code=s.code, reference=s.reference, currency=s.currency,
        allowed_image_hosts=s.allowed_image_hosts or [], field_mapping=s.field_mapping or {},
        category_mapping=s.category_mapping or {}, pricing=s.pricing or {}, notes=s.notes,
        created_at=s.created_at, product_count=count,
        suggested_exchange_rate=svc.default_exchange_rate(s.currency))


def _summary(b: models.SupplierImport) -> dict:
    return dict(
        id=b.id, supplier_id=b.supplier_id, supplier_name=b.supplier.name if b.supplier else None,
        file_name=b.file_name, file_format=b.file_format, status=b.status,
        total_rows=b.total_rows, valid_rows=b.valid_rows, imported=b.imported, updated=b.updated,
        skipped=b.skipped, duplicates=b.duplicates, errors=b.errors, failed_images=b.failed_images,
        created_by=b.created_by, created_at=b.created_at, committed_at=b.committed_at)


def _detail(b: models.SupplierImport) -> schemas.ImportDetailOut:
    return schemas.ImportDetailOut(
        **_summary(b), columns=b.columns or [],
        sample_rows=[{k: v for k, v in r.items() if not k.startswith("__")}
                     for r in (b.raw_rows or [])[:5]],
        suggested_mapping=svc.suggest_mapping(b.columns or [], (b.supplier.field_mapping if b.supplier else None)),
        settings=b.settings or {}, preview=b.preview or {}, results=b.results or [],
        image_files=sorted((b.image_files or {}).keys()))


def _clean_hosts(hosts: List[str]) -> List[str]:
    cleaned = []
    for h in hosts or []:
        h = str(h).strip().lower()
        h = re.sub(r"^https?://", "", h).split("/")[0]
        if not re.fullmatch(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+", h):
            raise HTTPException(status_code=400, detail=f"'{h}' is not a host name (e.g. cdn.supplier.com).")
        cleaned.append(h)
    return sorted(set(cleaned))[:20]


def _get_supplier(db, supplier_id) -> models.Supplier:
    s = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return s


def _get_batch(db, import_id) -> models.SupplierImport:
    b = db.query(models.SupplierImport).filter(models.SupplierImport.id == import_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Import not found")
    return b


# ---------------------------------------------------------------- reference data
@router.get("/fields")
def target_fields(_admin=Depends(get_current_admin)):
    """The Hairshalo fields a supplier column can be mapped to."""
    return [{k: f[k] for k in ("key", "label", "level")} for f in svc.TARGET_FIELDS] + \
        [{"key": "_rounding", "label": "", "level": "meta", "choices": list(svc.ROUNDING_CHOICES)}]


# ---------------------------------------------------------------- suppliers
@router.get("/suppliers", response_model=List[schemas.SupplierOut])
def list_suppliers(db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    return [_supplier_out(db, s) for s in
            db.query(models.Supplier).order_by(models.Supplier.name.asc()).all()]


@router.post("/suppliers", response_model=schemas.SupplierOut, status_code=201)
def create_supplier(payload: schemas.SupplierIn, db: Session = Depends(get_db),
                    _admin=Depends(get_current_admin)):
    name = payload.name.strip()
    cur = payload.currency.strip().upper()
    if not svc.valid_currency(cur):
        raise HTTPException(status_code=400, detail="Currency must be a 3-letter code such as GBP or INR.")
    code = re.sub(r"[^A-Z0-9]", "", (payload.code or name).upper())[:8] or "SUP"
    base, n = code, 1
    while db.query(models.Supplier.id).filter(models.Supplier.code == code).first():
        n += 1
        code = f"{base[:6]}{n}"
    if db.query(models.Supplier.id).filter(func.lower(models.Supplier.name) == name.lower()).first():
        raise HTTPException(status_code=400, detail="A supplier with that name already exists.")
    s = models.Supplier(name=name, code=code, reference=payload.reference, currency=cur,
                        allowed_image_hosts=_clean_hosts(payload.allowed_image_hosts),
                        notes=payload.notes)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _supplier_out(db, s)


@router.put("/suppliers/{supplier_id}", response_model=schemas.SupplierOut)
def update_supplier(supplier_id: str, payload: schemas.SupplierUpdate, db: Session = Depends(get_db),
                    _admin=Depends(get_current_admin)):
    s = _get_supplier(db, supplier_id)
    data = payload.model_dump(exclude_unset=True)
    if "currency" in data and data["currency"]:
        data["currency"] = data["currency"].strip().upper()
        if not svc.valid_currency(data["currency"]):
            raise HTTPException(status_code=400, detail="Currency must be a 3-letter code.")
    if "allowed_image_hosts" in data:
        data["allowed_image_hosts"] = _clean_hosts(data["allowed_image_hosts"] or [])
    for k, v in data.items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return _supplier_out(db, s)


# ---------------------------------------------------------------- imports
@router.post("/imports", response_model=schemas.ImportDetailOut, status_code=201)
async def upload_file(supplier_id: str = Form(...), file: UploadFile = File(...),
                      db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    """Parse an uploaded .xlsx/CSV/JSON file. Nothing is imported yet."""
    supplier = _get_supplier(db, supplier_id)
    data = await file.read(svc.MAX_FILE_BYTES + 1)
    name = re.sub(r"[^\w.\- ]+", "_", (file.filename or "upload").split("/")[-1].split("\\")[-1])[:120]
    try:
        fmt, columns, rows = svc.parse_file(name, data)
    except svc.SupplierImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    batch = models.SupplierImport(supplier_id=supplier.id, file_name=name, file_format=fmt,
                                  status="uploaded", columns=columns, raw_rows=rows,
                                  total_rows=len(rows), created_by=admin.email, image_files={})
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return _detail(batch)


@router.post("/imports/{import_id}/images", response_model=schemas.ImportDetailOut)
async def upload_images(import_id: str, files: List[UploadFile] = File(...),
                        db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Image files referenced BY NAME in the supplier file. Validated like any upload."""
    batch = _get_batch(db, import_id)
    if batch.status == "committed":
        raise HTTPException(status_code=409, detail="This import has already been committed.")
    if len(files) > MAX_IMAGE_FILES_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"Upload at most {MAX_IMAGE_FILES_PER_REQUEST} images at a time.")
    stored = dict(batch.image_files or {})
    rejected = []
    for f in files:
        fname = (f.filename or "").split("/")[-1].split("\\")[-1].lower()[:200]
        data = await f.read(svc.MAX_IMAGE_BYTES + 1)
        try:
            if not fname:
                raise ValueError("missing file name")
            if len(data) > svc.MAX_IMAGE_BYTES:
                raise ValueError(f"larger than {svc.MAX_IMAGE_BYTES // (1024 * 1024)} MB")
            key, ctype, size = svc.store_image_bytes(data, (f.content_type or "").lower())
            old = stored.get(fname)
            if old and old.get("storage_key"):
                from app.storage import get_storage
                get_storage().delete(old["storage_key"])
            stored[fname] = {"storage_key": key, "content_type": ctype, "size": size}
        except ValueError as exc:
            rejected.append(f"{fname or 'file'}: {exc}")
    batch.image_files = stored
    db.commit()
    db.refresh(batch)
    if rejected:
        raise HTTPException(status_code=400, detail={"message": "Some images were rejected.",
                                                     "rejected": rejected,
                                                     "accepted": sorted(stored.keys())})
    return _detail(batch)


@router.post("/imports/{import_id}/preview", response_model=schemas.ImportDetailOut)
def preview(import_id: str, payload: schemas.ImportPreviewRequest, db: Session = Depends(get_db),
            _admin=Depends(get_current_admin)):
    """Map, price, validate and duplicate-check. Remembers the choices for the supplier."""
    batch = _get_batch(db, import_id)
    if batch.status == "committed":
        raise HTTPException(status_code=409, detail="This import has already been committed.")
    supplier = batch.supplier
    try:
        result = svc.build_preview(db, supplier, batch, payload.mapping, payload.category_mapping,
                                   payload.pricing.model_dump(mode="json"))
    except svc.SupplierImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    batch.preview = {"summary": result["summary"], "products": result["products"],
                     "categories": result["categories"]}
    batch.settings = result["settings"]
    batch.valid_rows = sum(len(p["lines"]) for p in result["products"] if p["valid"])
    batch.duplicates = result["summary"]["duplicates"]
    batch.status = "previewed"
    supplier.field_mapping = result["settings"]["mapping"]
    supplier.category_mapping = {**(supplier.category_mapping or {}), **result["settings"]["category_mapping"]}
    supplier.pricing = {k: v for k, v in result["settings"]["pricing"].items() if k != "currency"}
    db.commit()
    db.refresh(batch)
    return _detail(batch)


@router.post("/imports/{import_id}/commit", response_model=schemas.ImportDetailOut)
def commit(import_id: str, payload: schemas.ImportCommitRequest, db: Session = Depends(get_db),
           admin=Depends(get_current_admin)):
    """Create/update the selected products — Draft, always. One savepoint each."""
    batch = _get_batch(db, import_id)
    if batch.status == "committed":
        raise HTTPException(status_code=409, detail="This import has already been committed.")
    if batch.status != "previewed" or not batch.preview:
        raise HTTPException(status_code=400, detail="Preview the import before committing it.")
    actions = {"import", "update", "new", "skip"}
    for s in payload.selections:
        if s.action not in actions:
            raise HTTPException(status_code=400, detail=f"Unknown action '{s.action}'.")
    try:
        svc.commit(db, batch.supplier, batch, [s.model_dump(mode="json") for s in payload.selections],
                   {"images_authorized": payload.images_authorized,
                    "stock_from_supplier": payload.stock_from_supplier,
                    "update_prices": payload.update_prices}, admin.email)
        db.commit()
    except Exception:
        db.rollback()
        batch = _get_batch(db, import_id)
        batch.status = "failed"
        db.commit()
        raise
    db.refresh(batch)
    return _detail(batch)


@router.get("/imports", response_model=List[schemas.ImportSummaryOut])
def import_log(supplier_id: str = None, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    q = db.query(models.SupplierImport)
    if supplier_id:
        q = q.filter(models.SupplierImport.supplier_id == supplier_id)
    return [_summary(b) for b in q.order_by(models.SupplierImport.created_at.desc()).limit(200).all()]


@router.get("/imports/{import_id}", response_model=schemas.ImportDetailOut)
def import_detail(import_id: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    return _detail(_get_batch(db, import_id))


@router.get("/imports/{import_id}/errors.csv")
def error_report(import_id: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    batch = _get_batch(db, import_id)
    body = svc.error_report_csv(batch)
    safe = re.sub(r"[^\w.-]+", "_", batch.file_name.rsplit(".", 1)[0])[:60]
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}-errors.csv"'})


@router.get("/products/{product_id}/source")
def product_source(product_id: str, db: Session = Depends(get_db), _admin=Depends(get_current_admin)):
    """Where an imported product came from — for the Admin product editor only."""
    links = db.query(models.SupplierProduct).filter(models.SupplierProduct.product_id == product_id).all()
    return [{
        "supplier": link.supplier.name if link.supplier else None,
        "source_product_id": link.source_product_id, "source_sku": link.source_sku,
        "source_url": link.source_url, "source_price": str(link.source_price) if link.source_price is not None else None,
        "source_currency": link.source_currency, "source_availability": link.source_availability,
        "source_file": link.source_file, "first_imported_at": link.first_imported_at,
        "last_synced_at": link.last_synced_at,
        "variants": [{"sku": v.variant.sku if v.variant else None, "source_sku": v.source_sku,
                      "source_price": str(v.source_price) if v.source_price is not None else None,
                      "source_availability": v.source_availability, "source_quantity": v.source_quantity}
                     for v in link.variants],
    } for link in links]
