"""Where images are stored: product media in S3, everything else on disk.

Rules under test:

* A product upload goes to the product bucket under products/, with the right
  content type and a year-long immutable cache header; the size cap and the
  empty-file check apply before anything is sent.
* Every other upload stays a plain file under MEDIA_ROOT.
* A delete reaches the store that actually holds the file, including rows
  written before the switch to S3.
* The migration copies an existing product file into the bucket and repoints
  its row, leaves the original on disk, and skips rows already moved.
* Production refuses to start with S3 selected and no bucket.

S3 is never contacted: a stand-in client records what would be sent. The
migration test needs DATABASE_URL (it creates and removes its own product).
"""
import io
import os
import uuid

import pytest

from app import storage as st
from app.storage import S3Storage, S3_PREFIX, UploadRejected

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class FakeS3:
    def __init__(self, fail_delete=False):
        self.objects, self.deleted, self.fail_delete = {}, [], fail_delete

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = (fileobj.read(), dict(ExtraArgs or {}))

    def delete_object(self, Bucket, Key):
        if self.fail_delete:
            raise RuntimeError("network down")
        self.deleted.append((Bucket, Key))


@pytest.fixture
def s3():
    return S3Storage("hs-products-test", "ap-south-1", client=FakeS3())


@pytest.fixture
def stores(monkeypatch, tmp_path, s3):
    """Local disk under a temp dir as site storage, the fake bucket as product storage."""
    disk = st.LocalDiskStorage(str(tmp_path), "/media")
    monkeypatch.setattr(st, "_storage", disk)
    monkeypatch.setattr(st, "_product_storage", s3)
    return disk, s3


# ---------------------------------------------------------------- S3Storage
def test_s3_save_puts_the_file_under_products_with_its_headers(s3):
    key, size = s3.save(io.BytesIO(JPEG), ".jpg", 1024)
    assert key.startswith(S3_PREFIX) and key.endswith(".jpg") and size == len(JPEG)
    body, extra = s3.client.objects[("hs-products-test", key)]
    assert body == JPEG
    assert extra["ContentType"] == "image/jpeg"
    assert "immutable" in extra["CacheControl"]
    assert s3.url_for(key) == f"https://hs-products-test.s3.ap-south-1.amazonaws.com/{key}"


def test_s3_rejects_too_large_and_empty_files_before_sending(s3):
    with pytest.raises(UploadRejected):
        s3.save(io.BytesIO(b"x" * 2048), ".jpg", 1024)
    with pytest.raises(UploadRejected):
        s3.save(io.BytesIO(b""), ".jpg", 1024)
    assert s3.client.objects == {}


def test_s3_public_url_can_be_overridden():
    s3 = S3Storage("b", "ap-south-1", public_url="https://media.example.test/", client=FakeS3())
    assert s3.url_for("products/a.webp") == "https://media.example.test/products/a.webp"


def test_s3_delete_never_raises():
    s3 = S3Storage("b", "ap-south-1", client=FakeS3(fail_delete=True))
    s3.delete("products/gone.jpg")      # logged, not raised


def test_s3_needs_a_bucket():
    with pytest.raises(ValueError):
        S3Storage("", "ap-south-1", client=FakeS3())


# ---------------------------------------------------------------- routing
def test_site_uploads_stay_on_disk_and_product_uploads_go_to_s3(stores):
    disk, s3 = stores
    site_key, _ = st.get_storage().save(io.BytesIO(JPEG), ".jpg", 1024)
    assert not site_key.startswith(S3_PREFIX)
    assert os.path.isfile(os.path.join(disk.root, site_key))
    assert st.get_storage().url_for(site_key) == f"/media/{site_key}"

    product_key, _ = st.get_product_storage().save(io.BytesIO(JPEG), ".jpg", 1024)
    assert product_key.startswith(S3_PREFIX)
    assert ("hs-products-test", product_key) in s3.client.objects


def test_a_delete_reaches_the_store_that_holds_the_file(stores):
    disk, s3 = stores
    old_key, _ = disk.save(io.BytesIO(JPEG), ".jpg", 1024)      # written before the switch
    st.storage_for_key(old_key).delete(old_key)
    assert not os.path.exists(os.path.join(disk.root, old_key))
    assert s3.client.deleted == []

    st.storage_for_key("products/new.jpg").delete("products/new.jpg")
    assert s3.client.deleted == [("hs-products-test", "products/new.jpg")]


def test_without_s3_product_media_stays_on_disk(monkeypatch, tmp_path):
    from app.config import settings
    disk = st.LocalDiskStorage(str(tmp_path), "/media")
    monkeypatch.setattr(st, "_storage", disk)
    monkeypatch.setattr(st, "_product_storage", None)
    monkeypatch.setattr(settings, "PRODUCT_MEDIA_STORAGE", "local")
    assert st.get_product_storage() is disk


# ---------------------------------------------------------------- migration
def test_migration_copies_to_s3_repoints_and_keeps_the_original(stores):
    from app import models
    from app.database import SessionLocal
    from app.migrate_product_media import run
    disk, s3 = stores
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]
    product = models.Product(name=f"Migration test {tag}", slug=f"migration-test-{tag}",
                             status=models.ProductStatus.draft)
    try:
        db.add(product)
        db.flush()
        local_key, _ = disk.save(io.BytesIO(JPEG), ".jpg", 1024)
        db.add_all([
            models.ProductMedia(product_id=product.id, url=disk.url_for(local_key),
                                storage_key=local_key, media_type=models.MediaType.image),
            models.ProductMedia(product_id=product.id, url="https://x.test/products/done.jpg",
                                storage_key="products/done.jpg", media_type=models.MediaType.image),
            models.ProductMedia(product_id=product.id, url="/media/lost.jpg",
                                storage_key="lost.jpg", media_type=models.MediaType.image),
        ])
        db.commit()

        # Dry run: nothing sent, nothing changed.
        assert run(False, product_ids=[product.id]) == 1          # 1 = a file was missing
        assert s3.client.objects == {}
        db.expire_all()
        assert {m.storage_key for m in product.media} == {local_key, "products/done.jpg", "lost.jpg"}

        assert run(True, product_ids=[product.id]) == 1
        db.expire_all()
        moved = next(m for m in product.media if m.storage_key == S3_PREFIX + local_key)
        assert moved.url == s3.url_for(S3_PREFIX + local_key)
        assert s3.client.objects[("hs-products-test", S3_PREFIX + local_key)][0] == JPEG
        assert os.path.isfile(os.path.join(disk.root, local_key))      # original kept
        assert any(m.storage_key == "lost.jpg" for m in product.media)  # missing left alone
        assert len(s3.client.objects) == 1                              # done.jpg not re-sent

        # A second run has nothing left to move.
        s3.client.objects.clear()
        run(True, product_ids=[product.id])
        assert s3.client.objects == {}
    finally:
        db.rollback()
        db.query(models.ProductMedia).filter(models.ProductMedia.product_id == product.id).delete()
        db.query(models.Product).filter(models.Product.id == product.id).delete()
        db.commit()
        db.close()


# ---------------------------------------------------------------- preflight
def _findings(monkeypatch, **overrides):
    from app import runtime
    from app.config import settings
    for k, v in overrides.items():
        monkeypatch.setattr(settings, k, v)
    return {f.code: f.level for f in runtime.collect_findings()}


def test_preflight_refuses_s3_without_a_bucket(monkeypatch):
    found = _findings(monkeypatch, PRODUCT_MEDIA_STORAGE="s3", PRODUCT_MEDIA_BUCKET="")
    assert found.get("product_media_bucket_missing") == "error"


def test_preflight_refuses_an_unknown_store(monkeypatch):
    found = _findings(monkeypatch, PRODUCT_MEDIA_STORAGE="dropbox")
    assert found.get("product_media_storage_unknown") == "error"


def test_preflight_accepts_s3_with_a_bucket(monkeypatch):
    found = _findings(monkeypatch, PRODUCT_MEDIA_STORAGE="s3", PRODUCT_MEDIA_BUCKET="hs-products")
    assert not {"product_media_bucket_missing", "product_media_storage_unknown",
                "media_on_local_disk"} & set(found)
