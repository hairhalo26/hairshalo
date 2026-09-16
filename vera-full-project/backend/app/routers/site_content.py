"""Editable storefront copy.

Public reads, admin writes — the same shape as `/api/products`. The storefront
fetches the whole map in one request because it needs a dozen blocks to paint
one page, and twelve round trips is a slow homepage.

Image uploads reuse `app/storage.py` rather than adding a second upload path:
the validation that matters (extension AND magic bytes, size cap, generated
filename so a client-supplied name cannot influence the path) already lives
there, and a second implementation would be a second place for it to be wrong.
"""
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import schemas, site_content
from app.database import get_db
from app.deps import get_current_admin
from app.storage import get_storage, validate_and_classify, UploadRejected

router = APIRouter(prefix="/api/site-content", tags=["site-content"])


@router.get("", response_model=dict)
def storefront_content(db: Session = Depends(get_db)):
    """Every active block, keyed by name — one request for the whole storefront.

    Public: this is the copy printed on the page, so there is nothing here a
    visitor cannot already read by looking at it.
    """
    return site_content.as_map(db)


@router.get("/blocks", response_model=List[schemas.SiteContentOut])
def list_blocks(include_inactive: bool = True,
                db: Session = Depends(get_db),
                _admin=Depends(get_current_admin)):
    """The admin list, including switched-off blocks and who last edited each."""
    return site_content.all_blocks(db, include_inactive=include_inactive)


@router.get("/blocks/{key}", response_model=schemas.SiteContentOut)
def get_block(key: str, db: Session = Depends(get_db),
              _admin=Depends(get_current_admin)):
    block = site_content.get_block(db, key)
    if not block:
        raise HTTPException(status_code=404, detail=f"No content block named '{key}'.")
    return block


@router.put("/blocks/{key}", response_model=schemas.SiteContentOut)
def update_block(key: str, payload: schemas.SiteContentUpdate,
                 db: Session = Depends(get_db),
                 admin=Depends(get_current_admin)):
    """Edit one block. The key comes from the URL, so a block cannot be renamed
    out from under the storefront that reads it."""
    try:
        return site_content.update_block(
            db, key, payload=payload.payload, is_active=payload.is_active,
            actor=admin.email,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No content block named '{key}'.")


@router.post("/blocks/{key}/reset", response_model=schemas.SiteContentOut)
def reset_block(key: str, db: Session = Depends(get_db),
                admin=Depends(get_current_admin)):
    """Restore one block to the copy the storefront shipped with."""
    try:
        return site_content.reset_block(db, key, actor=admin.email)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No content block named '{key}'.")


@router.post("/media", response_model=dict, status_code=201)
async def upload_content_image(file: UploadFile = File(...),
                               _admin=Depends(get_current_admin)):
    """Upload an image for a content block or a category card.

    Returns `{"url": ...}` for the caller to store in the block's payload or on
    the category. Deliberately not tied to a product: these are merchandising
    images, and filing them under a product would make deleting that product
    quietly break the homepage.
    """
    head = await file.read(32)
    try:
        kind, ext, cap = validate_and_classify(file.content_type, head)
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if kind != "image":
        raise HTTPException(status_code=400,
                            detail="Content blocks take images, not video.")

    await file.seek(0)
    try:
        storage_key, size = get_storage().save(file.file, ext, cap)
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "url": get_storage().url_for(storage_key),
        "content_type": file.content_type,
        "file_size": size,
    }
