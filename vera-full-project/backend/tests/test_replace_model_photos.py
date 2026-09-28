"""Swapping a colour's photographs from a manifest (app.replace_model_photos).

Rules under test:

* A dry run changes nothing.
* The named rows go and the new photographs take exactly their positions,
  so positions elsewhere in the gallery (which the homepage names) stay put.
* A different count renumbers the gallery with the new photographs in place.
* The primary photograph stays primary through the swap.
* A row that belongs to another colour refuses the whole entry.
* Nothing changes if any new file is not uploaded yet.
* Removed files are deleted from storage only after the commit.

Needs DATABASE_URL (creates and removes its own product); S3 is a stand-in.
"""
import uuid

import pytest

from app import models
from app import storage as st
from app.database import SessionLocal
from app.replace_model_photos import run
from app.storage import S3Storage
from tests.test_media_storage import FakeS3


@pytest.fixture
def s3(monkeypatch):
    s3 = S3Storage("hs-products-test", "ap-south-1", client=FakeS3())
    monkeypatch.setattr(st, "_product_storage", s3)
    return s3


@pytest.fixture
def piece():
    """A product with two colours: A has photos 0-4 (0-2 model), B has 5-7."""
    db = SessionLocal()
    tag = uuid.uuid4().hex[:8]
    p = models.Product(name=f"Swap test {tag}", slug=f"swap-test-{tag}", status=models.ProductStatus.draft)
    db.add(p)
    db.flush()
    a = models.ProductVariant(product_id=p.id, sku=f"SWAP-{tag}-A")
    b = models.ProductVariant(product_id=p.id, sku=f"SWAP-{tag}-B")
    db.add_all([a, b])
    db.flush()
    for so in range(8):
        v = a if so < 5 else b
        db.add(models.ProductMedia(product_id=p.id, variant_id=v.id, sort_order=so, is_primary=(so == 0),
                                   url=f"https://x.test/products/old{so}.jpg", storage_key=f"products/old{so}-{tag}.jpg",
                                   media_type=models.MediaType.image))
    db.commit()
    ids = (p.id, p.slug, a.sku, b.sku, tag)
    db.close()
    yield ids
    db = SessionLocal()
    db.query(models.ProductMedia).filter(models.ProductMedia.product_id == ids[0]).delete()
    db.query(models.ProductVariant).filter(models.ProductVariant.product_id == ids[0]).delete()
    db.query(models.Product).filter(models.Product.id == ids[0]).delete()
    db.commit()
    db.close()


def gallery(pid):
    db = SessionLocal()
    rows = (db.query(models.ProductMedia).filter(models.ProductMedia.product_id == pid)
              .order_by(models.ProductMedia.sort_order).all())
    out = [(m.sort_order, m.storage_key, m.is_primary) for m in rows]
    db.close()
    return out


def entry(slug, sku, remove, n):
    return {"product": slug, "sku": sku, "remove": remove,
            "new": [{"key": f"products/new{i}-{uuid.uuid4().hex[:6]}.jpg", "size": 100 + i} for i in range(n)]}


def quiet(*_):
    pass


def test_dry_run_changes_nothing(s3, piece):
    pid, slug, a, _, _ = piece
    before = gallery(pid)
    assert run({"entries": [entry(slug, a, [0, 1, 2], 3)]}, False, exists=lambda u: True, out=quiet) == 0
    assert gallery(pid) == before
    assert s3.client.deleted == []


def test_same_count_reuses_the_positions_and_keeps_primary(s3, piece):
    pid, slug, a, _, tag = piece
    e = entry(slug, a, [0, 1, 2], 3)
    assert run({"entries": [e]}, True, exists=lambda u: True, out=quiet) == 0
    g = gallery(pid)
    assert [k for _, k, _ in g[:3]] == [n["key"] for n in e["new"]]
    assert [so for so, _, _ in g] == list(range(8))           # nothing else moved
    assert g[3][1] == f"products/old3-{tag}.jpg" and g[5][1] == f"products/old5-{tag}.jpg"
    assert g[0][2] is True and sum(p for *_, p in g) == 1      # primary carried over
    assert sorted(k for _, k in s3.client.deleted) == sorted(f"products/old{i}-{tag}.jpg" for i in range(3))


def test_a_different_count_renumbers_in_order(s3, piece):
    pid, slug, a, _, tag = piece
    e = entry(slug, a, [0, 1], 3)
    assert run({"entries": [e]}, True, exists=lambda u: True, out=quiet) == 0
    g = gallery(pid)
    assert [so for so, _, _ in g] == list(range(9))
    assert [k for _, k, _ in g[:3]] == [n["key"] for n in e["new"]]
    assert g[3][1] == f"products/old2-{tag}.jpg"


def test_a_photo_of_another_colour_refuses_the_entry(s3, piece):
    pid, slug, a, _, _ = piece
    before = gallery(pid)
    assert run({"entries": [entry(slug, a, [4, 5], 2)]}, True, exists=lambda u: True, out=quiet) == 1
    assert gallery(pid) == before
    assert s3.client.deleted == []


def test_nothing_changes_until_every_new_file_is_uploaded(s3, piece):
    pid, slug, a, b, _ = piece
    before = gallery(pid)
    m = {"entries": [entry(slug, a, [0, 1, 2], 3), entry(slug, b, [5], 1)]}
    missing = m["entries"][1]["new"][0]["key"]
    assert run(m, True, exists=lambda u: not u.endswith(missing), out=quiet) == 2
    assert gallery(pid) == before
