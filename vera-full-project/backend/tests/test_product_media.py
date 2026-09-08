"""Product media: variant assignment, ordering and ownership.

Regression cover for the media work:

* `variant_id` survives the round trip. It was possible for the column to be
  set correctly in the database and still read as null over the API, because
  the product serialiser built its media payload by hand and simply did not
  mention the field.
* A photograph can only be attached to a variant of ITS OWN product. Without
  that check an admin request could file one product's image under another
  product's colour.
* Media is addressed within a product, so guessing an id cannot reach another
  product's row.

    VERA_API=http://127.0.0.1:8010/api python -m pytest tests/test_product_media.py -q

Everything created here is deleted again in the fixture teardown.
"""
import os
import uuid

import pytest
import requests

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}

pytestmark = pytest.mark.skipif(
    os.getenv("VERA_SKIP_API_TESTS") == "1", reason="API tests disabled"
)


def _alive():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def auth():
    if not _alive():
        pytest.skip(f"backend not reachable at {API}")
    r = requests.post(f"{API}/auth/login", json=ADMIN, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_product(auth, colours=("1B", "27")):
    suffix = uuid.uuid4().hex[:8]
    created = requests.post(
        f"{API}/products",
        json={"name": f"Media Test {suffix}", "description": "media test",
              # required by the create endpoint; these products are never
              # published, so the figure is only here to satisfy validation
              "original_price": 1000},
        headers=auth, timeout=15)
    assert created.status_code == 201, created.text
    p = created.json()
    for i, colour in enumerate(colours):
        requests.post(f"{API}/products/{p['id']}/variants",
                      json={"sku": f"MED-{suffix}-{i}", "color": colour, "stock": 0},
                      headers=auth, timeout=15)
    for n in range(3):
        requests.post(f"{API}/products/{p['id']}/media",
                      json={"url": f"https://example.com/{suffix}-{n}.jpg",
                            "sort_order": n},
                      headers=auth, timeout=15)
    return requests.get(f"{API}/products/{p['id']}/preview",
                        headers=auth, timeout=15).json()


@pytest.fixture
def product(auth):
    p = _make_product(auth)
    yield p
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)


@pytest.fixture
def other_product(auth):
    p = _make_product(auth, colours=("4",))
    yield p
    requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)


def test_media_reports_its_variant_over_the_api(auth, product):
    """The field must survive the serialiser, not just reach the database."""
    media_id = product["media"][0]["id"]
    variant_id = product["variants"][0]["id"]

    r = requests.patch(f"{API}/products/{product['id']}/media/{media_id}",
                       json={"variant_id": variant_id}, headers=auth, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["variant_id"] == variant_id

    # ...and again when the whole product is fetched, which is the path the
    # storefront actually uses.
    fetched = requests.get(f"{API}/products/{product['id']}/preview",
                           headers=auth, timeout=15).json()
    got = [m for m in fetched["media"] if m["id"] == media_id][0]
    assert got["variant_id"] == variant_id


def test_media_can_be_moved_back_to_the_whole_product(auth, product):
    media_id = product["media"][0]["id"]
    variant_id = product["variants"][0]["id"]
    requests.patch(f"{API}/products/{product['id']}/media/{media_id}",
                   json={"variant_id": variant_id}, headers=auth, timeout=15)

    r = requests.patch(f"{API}/products/{product['id']}/media/{media_id}",
                       json={"variant_id": ""}, headers=auth, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["variant_id"] is None


def test_media_cannot_be_filed_under_another_products_variant(auth, product, other_product):
    """The check that stops a gallery from lying about what it shows."""
    media_id = product["media"][0]["id"]
    foreign_variant = other_product["variants"][0]["id"]

    r = requests.patch(f"{API}/products/{product['id']}/media/{media_id}",
                       json={"variant_id": foreign_variant}, headers=auth, timeout=15)
    assert r.status_code == 400, r.text
    assert "does not belong" in r.json()["detail"]

    unchanged = requests.get(f"{API}/products/{product['id']}/preview",
                             headers=auth, timeout=15).json()
    got = [m for m in unchanged["media"] if m["id"] == media_id][0]
    assert got["variant_id"] is None


def test_media_of_another_product_is_not_reachable(auth, product, other_product):
    """Addressed within the product, so a guessed id cannot cross the boundary."""
    foreign_media = other_product["media"][0]["id"]
    r = requests.patch(f"{API}/products/{product['id']}/media/{foreign_media}",
                       json={"alt_text": "should not apply"}, headers=auth, timeout=15)
    assert r.status_code == 404


def test_updating_media_requires_an_admin(product):
    media_id = product["media"][0]["id"]
    r = requests.patch(f"{API}/products/{product['id']}/media/{media_id}",
                       json={"variant_id": ""}, timeout=15)
    assert r.status_code in (401, 403)


def test_reordering_renumbers_every_file(auth, product):
    """The admin UI sends the whole order; the server is what decides it."""
    ids = [m["id"] for m in sorted(product["media"], key=lambda m: m["sort_order"])]
    reversed_ids = list(reversed(ids))
    r = requests.post(f"{API}/products/{product['id']}/media/reorder",
                      json={"order": [{"id": mid, "sort_order": i}
                                      for i, mid in enumerate(reversed_ids)]},
                      headers=auth, timeout=15)
    assert r.status_code == 200, r.text
    got = sorted(r.json(), key=lambda m: m["sort_order"])
    assert [m["id"] for m in got] == reversed_ids
    assert [m["sort_order"] for m in got] == [0, 1, 2]


def test_primary_is_exclusive(auth, product):
    ids = [m["id"] for m in product["media"]]
    requests.post(f"{API}/products/{product['id']}/media/reorder",
                  json={"order": [], "primary_id": ids[1]}, headers=auth, timeout=15)
    fetched = requests.get(f"{API}/products/{product['id']}/preview",
                           headers=auth, timeout=15).json()
    primaries = [m["id"] for m in fetched["media"] if m["is_primary"]]
    assert primaries == [ids[1]]
