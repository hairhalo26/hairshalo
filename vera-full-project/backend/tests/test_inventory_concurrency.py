"""Inventory under concurrent checkout: the ledger, not just the total.

test_readiness.py already proves the important thing — simultaneous buyers
cannot oversell. This adds the invariant that test does not check: after the
burst, the movement log must still *explain* the quantity.

Stock landing on the right number while movements are missing or duplicated is
the failure this catches. It is the same invariant as
`test_the_movement_log_always_explains_the_stock`, asserted at the moment it is
most likely to break.

    VERA_API=http://127.0.0.1:8010/api python -m pytest tests/test_inventory_concurrency.py -q

NOTE ON WHAT THIS PROVES WHERE: the protection in production is
`SELECT ... FOR UPDATE` (app/inventory.lock_variant). SQLite has no row locks
and serialises writers with a database-level lock instead, so against SQLite a
pass shows the invariant holds but does NOT exercise the row-lock path. Run it
against PostgreSQL to test the mechanism that actually ships.
"""
import os
import threading
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


@pytest.fixture
def sellable(auth):
    """A published product of our own, with a clean ledger and known stock.

    Deliberately not "the first published product with stock": that variant
    carries whatever history the rest of the suite left behind, and its
    movement list can exceed the endpoint's page size — which is how the first
    version of this test managed to accuse the application of losing writes
    that it had not lost.
    """
    suffix = uuid.uuid4().hex[:8]
    category = requests.get(f"{API}/categories", timeout=15).json()[0]["id"]
    p = requests.post(f"{API}/products", headers=auth, timeout=15,
                      json={"name": f"Concurrency Fixture {suffix}",
                            "description": "temporary fixture",
                            "category_id": category,
                            "original_price": 1000}).json()
    requests.post(f"{API}/products/{p['id']}/media", headers=auth, timeout=15,
                  json={"url": "https://example.com/c.jpg", "is_primary": True})
    v = requests.post(f"{API}/products/{p['id']}/variants", headers=auth, timeout=15,
                      json={"sku": f"CONC-{suffix}", "color": "Natural Black",
                            "stock": 4}).json()
    published = requests.post(f"{API}/products/{p['id']}/status", headers=auth,
                              timeout=15, json={"action": "publish"})
    if published.status_code != 200:
        requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)
        pytest.skip(f"could not publish the fixture: {published.text}")

    full = requests.get(f"{API}/products/{p['id']}", timeout=15).json()
    variant = next(x for x in full["variants"] if x["id"] == v["id"])
    yield full, variant
    # The test places real orders against this product, after which the API
    # refuses to delete it -- deliberately, to protect order history. The
    # response was being ignored, so every run left another fixture PUBLISHED
    # in the catalog; they eventually outnumbered the real products and started
    # being picked up by other tests. Archive is the API's own suggested
    # remedy, and it keeps the orders intact.
    removed = requests.delete(f"{API}/products/{p['id']}", headers=auth, timeout=15)
    if removed.status_code >= 300:
        requests.post(f"{API}/products/{p['id']}/status", headers=auth, timeout=15,
                      json={"action": "archive"})


def _movements(auth, variant_id):
    """The FULL ledger for a variant.

    `limit` matters: the endpoint returns the 50 most recent by default, so
    counting the length of an unbounded call is only valid while a variant has
    fewer than 50 movements.
    """
    r = requests.get(f"{API}/inventory/movements/{variant_id}",
                     params={"limit": 200}, headers=auth, timeout=15)
    r.raise_for_status()
    return r.json()


def _stock(product_id, variant_id):
    p = requests.get(f"{API}/products/{product_id}", timeout=15).json()
    return next(v["stock"] for v in p["variants"] if v["id"] == variant_id)


def _set_stock_to(auth, product_id, variant_id, target):
    current = _stock(product_id, variant_id)
    if current == target:
        return
    r = requests.post(f"{API}/inventory/adjust", headers=auth, timeout=15,
                      json={"variant_id": variant_id, "delta": target - current,
                            "reason": "adjustment",
                            "note": "concurrency test setup"})
    if r.status_code != 200:
        pytest.skip(f"could not set up stock: {r.text}")


def _place(product, variant):
    return requests.post(
        f"{API}/orders", timeout=30,
        json={
            "customer_name": "Concurrency Test",
            "customer_email": f"conc{uuid.uuid4().hex[:8]}@example.com",
            "shipping_address": "1 Test Street",
            "items": [{"product_id": product["id"], "variant_id": variant["id"],
                       "quantity": 1}],
        },
    )


def test_ledger_still_explains_the_stock_after_a_concurrent_burst(auth, sellable):
    """More buyers than units, all at once — then reconcile the audit trail."""
    product, variant = sellable
    units = 4
    attempts = 10
    assert _stock(product["id"], variant["id"]) == units

    before = len(_movements(auth, variant["id"]))
    results = []
    barrier = threading.Barrier(attempts)

    def buy():
        barrier.wait()                       # release every thread together
        try:
            results.append(_place(product, variant).status_code)
        except Exception:
            results.append(0)

    threads = [threading.Thread(target=buy) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    succeeded = sum(1 for code in results if code == 201)
    remaining = _stock(product["id"], variant["id"])

    # 1. No overselling, and no phantom refusals.
    assert succeeded == units, f"{succeeded} orders succeeded against {units} units"
    assert remaining == 0, f"{remaining} units left after selling out"

    # 2. One movement per successful order — not fewer (a lost write) and not
    #    more (a retry that deducted twice).
    after = _movements(auth, variant["id"])
    new = len(after) - before
    assert new == succeeded, f"{new} movements recorded for {succeeded} orders"

    # 3. The invariant the whole design rests on, checked at the point of most
    #    stress: the log adds up to the quantity on the shelf.
    assert sum(m["delta"] for m in after) == remaining, (
        "movement log no longer explains the stock after concurrent checkout"
    )

    # 4. Every deduction is attributed to an order, not left unexplained.
    order_moves = [m for m in after if m["delta"] < 0][-succeeded:]
    assert all(m["reason"] == "order" for m in order_moves), \
        [m["reason"] for m in order_moves]


def test_a_sold_out_variant_refuses_further_orders(auth, sellable):
    """The follow-on state: once empty, buying is refused, not allowed negative."""
    product, variant = sellable
    _set_stock_to(auth, product["id"], variant["id"], 0)

    r = _place(product, variant)
    assert r.status_code == 400, r.text
    assert "stock" in r.text.lower()
    assert _stock(product["id"], variant["id"]) == 0, "stock went negative"
