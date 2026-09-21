"""The hair catalogue: Hair Category -> Hair Type -> Product -> Colour variant.

The rule this module protects: **the hierarchy is data, and the server is its
judge.** Synthetic Hair, Human Hair, their types and the colours are rows staff
manage in the Back Office — nothing here assumes a particular one exists — and
a product can only be filed in a way the hierarchy allows, whatever the Admin
Panel's dropdowns happen to send.

How it maps onto what already existed (see migration 0016):
* Hair Category = a top-level `Category`; Hair Type = one of its subcategories.
  A product is filed under the type, and its category is derived from it.
* Colour = a row in `colours`; a variant links to one with `colour_id`, and its
  free-text `color` is kept equal to the colour's name.

API tests: they need a backend on VERA_API and DATABASE_URL pointing at the
same database (see the other modules in this directory).
"""
import os
import uuid

import pytest
import requests

from shipping import SHIPPING

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _api_up():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="backend not running")


def _uid():
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def auth():
    r = requests.post(f"{API}/auth/login", timeout=10, json=ADMIN)
    if r.status_code != 200:
        pytest.skip("admin credentials rejected")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Tracker:
    """Everything a test creates, torn down children-first at module end."""

    def __init__(self, auth):
        self.auth = auth
        self.products, self.types, self.categories, self.colours = [], [], [], []

    def post(self, path, body):
        return requests.post(f"{API}{path}", headers=self.auth, timeout=15, json=body)

    def put(self, path, body):
        return requests.put(f"{API}{path}", headers=self.auth, timeout=15, json=body)

    def category(self, name, **extra):
        r = self.post("/categories", {"name": name, **extra})
        assert r.status_code == 201, r.text
        (self.types if extra.get("parent_id") else self.categories).append(r.json()["id"])
        return r.json()

    def colour(self, name, **extra):
        r = self.post("/colours", {"name": name, **extra})
        assert r.status_code == 201, r.text
        self.colours.append(r.json()["id"])
        return r.json()

    def product(self, body, publish=False):
        body = {"description": "Hair catalogue fixture.", "original_price": "5999", **body}
        r = self.post("/products", body)
        assert r.status_code == 201, r.text
        p = r.json()
        self.products.append(p["id"])
        if publish:
            up = requests.post(f"{API}/products/{p['id']}/media/upload", headers=self.auth,
                               timeout=15, files={"file": ("p.jpg", JPEG, "image/jpeg")})
            assert up.status_code == 201, up.text
            pub = self.post(f"/products/{p['id']}/status", {"action": "publish"})
            assert pub.status_code == 200, pub.text
            p = pub.json()
        return p

    def teardown(self):
        for pid in self.products:
            self.post(f"/products/{pid}/status", {"action": "archive"})
            requests.delete(f"{API}/products/{pid}", headers=self.auth, timeout=10)
        # A product that took an order cannot be deleted, so the categories and
        # colours it uses cannot be either: those are hidden instead.
        for cid in self.types + self.categories:
            if requests.delete(f"{API}/categories/{cid}", headers=self.auth,
                               timeout=10).status_code != 204:
                self.put(f"/categories/{cid}", {"is_active": False})
        for cid in self.colours:
            if requests.delete(f"{API}/colours/{cid}", headers=self.auth,
                               timeout=10).status_code != 204:
                self.put(f"/colours/{cid}", {"is_active": False})


@pytest.fixture(scope="module")
def made(auth):
    t = Tracker(auth)
    yield t
    t.teardown()


@pytest.fixture(scope="module")
def tree(made):
    """Two hair categories, each with its own types, and three colours."""
    tag = _uid()
    synthetic = made.category(f"Synthetic Hair {tag}")
    human = made.category(f"Human Hair {tag}")
    return {
        "tag": tag,
        "synthetic": synthetic,
        "human": human,
        "syn_lace": made.category(f"Lace Front Synthetic Wig {tag}", parent_id=synthetic["id"]),
        "syn_bob": made.category(f"Bob Synthetic Wig {tag}", parent_id=synthetic["id"]),
        "hum_lace": made.category(f"Lace Front Human Hair Wig {tag}", parent_id=human["id"]),
        "black": made.colour(f"Black {tag}", hex="#111"),
        "brown": made.colour(f"Dark Brown {tag}", hex="#3B2A20"),
        "blonde": made.colour(f"Blonde {tag}"),
    }


def _variants(tag, *colours):
    return [{"sku": f"HC-{tag}-{i}-{_uid()}", "colour_id": c["id"], "length": '18"', "stock": 5}
            for i, c in enumerate(colours)]


# ------------------------------------------------------------ hair categories

def test_hair_categories_and_types_are_plain_managed_rows(made, tree):
    tag = tree["tag"]
    assert tree["synthetic"]["slug"] == f"synthetic-hair-{tag}"
    assert tree["syn_lace"]["parent_id"] == tree["synthetic"]["id"]
    assert tree["hum_lace"]["parent_id"] == tree["human"]["id"]
    # A type of a type would be a third level: refused, as before.
    r = made.post("/categories", {"name": f"Deep {tag}", "parent_id": tree["syn_lace"]["id"]})
    assert r.status_code == 400


def test_names_are_unique_ignoring_case_on_create_and_on_rename(made, tree):
    tag = tree["tag"]
    dup = made.post("/categories", {"name": f"synthetic hair {tag}".upper()})
    assert dup.status_code == 400
    # Renaming onto another category's name used to be a 500 (IntegrityError).
    clash = made.put(f"/categories/{tree['human']['id']}", {"name": tree["synthetic"]["name"]})
    assert clash.status_code == 400


def test_a_rename_keeps_the_slug_so_links_keep_working(made, tree):
    t = made.category(f"Short Synthetic Wig {tree['tag']}", parent_id=tree["synthetic"]["id"])
    r = made.put(f"/categories/{t['id']}", {"name": f"Short Synthetic Wigs {tree['tag']}"})
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == t["slug"]


def test_types_reorder_within_their_own_category_only(made, tree):
    ids = [tree["syn_bob"]["id"], tree["syn_lace"]["id"]]
    r = made.post("/categories/reorder", {"ids": ids})
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()] == ids
    assert [c["sort_order"] for c in r.json()] == [0, 1]
    mixed = made.post("/categories/reorder", {"ids": [tree["syn_bob"]["id"], tree["hum_lace"]["id"]]})
    assert mixed.status_code == 400


def test_a_hair_category_with_types_cannot_be_deleted(auth, tree):
    r = requests.delete(f"{API}/categories/{tree['synthetic']['id']}", headers=auth, timeout=10)
    assert r.status_code == 400
    assert "subcategor" in r.json()["detail"]


# --------------------------------------------------------------------- colours

def test_colours_are_managed_with_a_normalised_swatch(made, tree):
    assert tree["black"]["hex"] == "#111111"
    assert tree["blonde"]["hex"] is None
    assert tree["black"]["slug"] == f"black-{tree['tag']}"
    bad = made.post("/colours", {"name": f"Red {tree['tag']}", "hex": "red"})
    assert bad.status_code == 400
    dup = made.post("/colours", {"name": tree["black"]["name"].lower()})
    assert dup.status_code == 400


def test_the_public_cannot_manage_colours(tree):
    assert requests.post(f"{API}/colours", json={"name": "Nope"}, timeout=10).status_code == 401
    r = requests.put(f"{API}/colours/{tree['black']['id']}", json={"name": "X"}, timeout=10)
    assert r.status_code == 401


def test_colours_reorder(made, tree):
    ids = [tree["blonde"]["id"], tree["black"]["id"], tree["brown"]["id"]]
    r = made.post("/colours/reorder", {"ids": ids})
    assert r.status_code == 200, r.text
    assert [c["sort_order"] for c in r.json()] == [0, 1, 2]


def test_an_archived_colour_is_hidden_from_the_public_list(made, tree):
    c = made.colour(f"Grey {tree['tag']}")
    assert made.put(f"/colours/{c['id']}", {"is_active": False}).status_code == 200
    public = [x["id"] for x in requests.get(f"{API}/colours", timeout=10).json()]
    staff = [x["id"] for x in requests.get(f"{API}/colours", headers=made.auth,
                                            params={"include_inactive": "true"}, timeout=10).json()]
    assert c["id"] not in public and c["id"] in staff


# -------------------------------------------------- products in the hierarchy

def test_a_product_is_filed_under_a_type_of_the_chosen_category(made, tree):
    p = made.product({"name": f"Luxury Lace Front Wig {tree['tag']}",
                      "category_id": tree["synthetic"]["id"],
                      "subcategory_id": tree["syn_lace"]["id"],
                      "variants": _variants(tree["tag"], tree["black"], tree["brown"], tree["blonde"])})
    assert p["category_id"] == tree["syn_lace"]["id"]
    assert p["main_category_id"] == tree["synthetic"]["id"]
    assert p["main_category_slug"] == tree["synthetic"]["slug"]
    assert p["subcategory"] == tree["syn_lace"]["name"]
    # The existing field still carries a name the old UI can show.
    assert p["category"] == tree["syn_lace"]["name"]


def test_a_type_from_the_other_category_is_rejected(made, tree):
    r = made.post("/products", {"name": f"Mismatch {tree['tag']}",
                                "category_id": tree["synthetic"]["id"],
                                "subcategory_id": tree["hum_lace"]["id"]})
    assert r.status_code == 400
    assert "does not belong" in r.json()["detail"]


def test_a_top_level_category_is_not_a_hair_type(made, tree):
    r = made.post("/products", {"name": f"Not a type {tree['tag']}",
                                "subcategory_id": tree["human"]["id"]})
    assert r.status_code == 400


def test_the_rule_holds_on_update_too(made, tree):
    p = made.product({"name": f"Movable {tree['tag']}", "category_id": tree["human"]["id"],
                      "subcategory_id": tree["hum_lace"]["id"]})
    bad = made.put(f"/products/{p['id']}", {"category_id": tree["human"]["id"],
                                            "subcategory_id": tree["syn_bob"]["id"]})
    assert bad.status_code == 400
    moved = made.put(f"/products/{p['id']}", {"category_id": tree["synthetic"]["id"],
                                              "subcategory_id": tree["syn_bob"]["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["main_category_id"] == tree["synthetic"]["id"]
    # Clearing the type keeps the product in its hair category.
    cleared = made.put(f"/products/{p['id']}", {"subcategory_id": None})
    assert cleared.json()["category_id"] == tree["synthetic"]["id"]
    assert cleared.json()["subcategory_id"] is None


def test_a_deactivated_type_takes_no_new_products(made, tree):
    t = made.category(f"Curly Synthetic Wig {tree['tag']}", parent_id=tree["synthetic"]["id"])
    kept = made.product({"name": f"Already curly {tree['tag']}",
                         "category_id": tree["synthetic"]["id"], "subcategory_id": t["id"]})
    assert made.put(f"/categories/{t['id']}", {"is_active": False}).status_code == 200
    r = made.post("/products", {"name": f"Late {tree['tag']}",
                                "category_id": tree["synthetic"]["id"], "subcategory_id": t["id"]})
    assert r.status_code == 400
    # A product already filed there is not stranded: its other edits still save.
    edit = made.put(f"/products/{kept['id']}", {"name": f"Still curly {tree['tag']}",
                                                "category_id": tree["synthetic"]["id"],
                                                "subcategory_id": t["id"]})
    assert edit.status_code == 200, edit.text


def test_the_legacy_category_only_payload_still_works(made, tree):
    """What the Admin Panel, importer and older clients send: category_id and a
    free-text colour. Nothing is linked or reclassified behind their back."""
    p = made.product({"name": f"Legacy {tree['tag']}", "category_id": tree["hum_lace"]["id"],
                      "variants": [{"sku": f"LG-{_uid()}", "color": "1B", "stock": 1}]})
    assert p["category_id"] == tree["hum_lace"]["id"]
    assert p["main_category_id"] == tree["human"]["id"]
    v = p["variants"][0]
    assert v["color"] == "1B" and v["colour_id"] is None
    assert p["colours"] == []


# ------------------------------------------------------------ colour variants

def test_variants_link_to_managed_colours(made, tree):
    p = made.product({"name": f"Colours {tree['tag']}", "category_id": tree["human"]["id"],
                      "subcategory_id": tree["hum_lace"]["id"],
                      "variants": _variants(tree["tag"], tree["brown"], tree["black"])})
    by_colour = {v["colour_id"]: v for v in p["variants"]}
    assert by_colour[tree["black"]["id"]]["color"] == tree["black"]["name"]
    assert by_colour[tree["black"]["id"]]["colour_hex"] == "#111111"
    assert {c["id"] for c in p["colours"]} == {tree["black"]["id"], tree["brown"]["id"]}
    # Each colour is its own variant: own SKU, own stock.
    assert len({v["sku"] for v in p["variants"]}) == 2


def test_an_unknown_colour_is_rejected(made, tree):
    r = made.post("/products", {"name": f"Bad colour {tree['tag']}", "variants": [
        {"sku": f"BC-{_uid()}", "colour_id": "no-such-colour"}]})
    assert r.status_code == 400


def test_an_archived_colour_cannot_be_newly_assigned(made, tree):
    c = made.colour(f"Auburn {tree['tag']}")
    made.put(f"/colours/{c['id']}", {"is_active": False})
    p = made.product({"name": f"Auburn test {tree['tag']}"})
    r = made.post(f"/products/{p['id']}/variants", {"sku": f"AU-{_uid()}", "colour_id": c["id"]})
    assert r.status_code == 400


def test_a_variant_can_change_and_unlink_its_colour(made, tree):
    p = made.product({"name": f"Recolour {tree['tag']}",
                      "variants": _variants(tree["tag"], tree["black"])})
    vid = p["variants"][0]["id"]
    r = made.put(f"/products/variants/{vid}", {"colour_id": tree["blonde"]["id"]})
    assert r.status_code == 200 and r.json()["color"] == tree["blonde"]["name"]
    # Free text cannot overwrite a managed colour's name…
    r = made.put(f"/products/variants/{vid}", {"color": "Something else"})
    assert r.json()["color"] == tree["blonde"]["name"]
    # …until the colour is unlinked.
    r = made.put(f"/products/variants/{vid}", {"colour_id": "", "color": "Custom mix"})
    assert r.json()["colour_id"] is None and r.json()["color"] == "Custom mix"


def test_renaming_a_colour_renames_its_variants_but_keeps_the_slug(made, tree):
    c = made.colour(f"Honey {tree['tag']}")
    p = made.product({"name": f"Honey wig {tree['tag']}", "variants": _variants(tree["tag"], c)})
    r = made.put(f"/colours/{c['id']}", {"name": f"Honey Blonde {tree['tag']}"})
    assert r.status_code == 200 and r.json()["slug"] == c["slug"]
    fresh = requests.get(f"{API}/products/{p['id']}/preview", headers=made.auth, timeout=10).json()
    assert fresh["variants"][0]["color"] == f"Honey Blonde {tree['tag']}"


def test_a_colour_in_use_is_archived_not_deleted(auth, made, tree):
    c = made.colour(f"Mixed {tree['tag']}")
    made.product({"name": f"Mixed wig {tree['tag']}", "variants": _variants(tree["tag"], c)})
    r = requests.delete(f"{API}/colours/{c['id']}", headers=auth, timeout=10)
    assert r.status_code == 400 and "archive" in r.json()["detail"]
    spare = made.colour(f"Unused {tree['tag']}")
    assert requests.delete(f"{API}/colours/{spare['id']}", headers=auth,
                           timeout=10).status_code == 204
    made.colours.remove(spare["id"])


# ------------------------------------------------------- storefront filtering

@pytest.fixture(scope="module")
def shop(made, tree):
    """Published products spread over the tree, for the filter tests."""
    tag = tree["tag"]
    return {
        "syn_lace": made.product({"name": f"Syn Lace {tag}", "category_id": tree["synthetic"]["id"],
                                  "subcategory_id": tree["syn_lace"]["id"],
                                  "variants": _variants(tag, tree["black"], tree["blonde"])}, publish=True),
        "syn_bob": made.product({"name": f"Syn Bob {tag}", "category_id": tree["synthetic"]["id"],
                                 "subcategory_id": tree["syn_bob"]["id"],
                                 "variants": _variants(tag, tree["brown"])}, publish=True),
        "hum_lace": made.product({"name": f"Hum Lace {tag}", "category_id": tree["human"]["id"],
                                  "subcategory_id": tree["hum_lace"]["id"],
                                  "variants": _variants(tag, tree["black"])}, publish=True),
    }


def _ids(**params):
    r = requests.get(f"{API}/products", params={"limit": 200, **params}, timeout=15)
    assert r.status_code == 200, r.text
    return {p["id"] for p in r.json()}


def test_a_hair_category_filter_includes_all_its_types(shop, tree):
    got = _ids(category=tree["synthetic"]["slug"])
    assert {shop["syn_lace"]["id"], shop["syn_bob"]["id"]} <= got
    assert shop["hum_lace"]["id"] not in got
    # A name still works, as the filter always accepted names.
    assert _ids(category=tree["synthetic"]["name"]) == got


def test_a_type_filter_narrows_to_that_type(shop, tree):
    got = _ids(category=tree["synthetic"]["slug"], type=tree["syn_lace"]["slug"])
    assert got == {shop["syn_lace"]["id"]}
    # A type that is not under the chosen category matches nothing.
    assert _ids(category=tree["synthetic"]["slug"], type=tree["hum_lace"]["slug"]) == set()


def test_a_colour_filter_is_server_side(shop, tree):
    got = _ids(category=tree["human"]["slug"], colour=tree["black"]["slug"])
    assert got == {shop["hum_lace"]["id"]}
    both = _ids(colour=tree["black"]["slug"])
    assert {shop["syn_lace"]["id"], shop["hum_lace"]["id"]} <= both
    assert shop["syn_bob"]["id"] not in both
    assert _ids(colour="no-such-colour") == set()


def test_the_paged_listing_filters_the_same_way(shop, tree):
    r = requests.get(f"{API}/products/paged", timeout=15, params={
        "category": tree["synthetic"]["slug"], "colour": tree["blonde"]["slug"]})
    assert r.status_code == 200
    assert {p["id"] for p in r.json()["items"]} == {shop["syn_lace"]["id"]}
    assert r.json()["total"] == 1


def test_the_shops_colour_list_only_offers_colours_in_stock_there(shop, tree):
    r = requests.get(f"{API}/colours", timeout=10,
                     params={"in_use": "true", "category": tree["human"]["slug"]})
    ids = {c["id"] for c in r.json()}
    assert tree["black"]["id"] in ids
    assert tree["blonde"]["id"] not in ids      # only a Synthetic product has it


def test_a_hair_category_card_counts_its_types_products(shop, tree):
    cats = {c["id"]: c for c in requests.get(f"{API}/categories", timeout=10).json()}
    assert cats[tree["synthetic"]["id"]]["product_count"] == 2
    assert cats[tree["syn_bob"]["id"]]["product_count"] == 1


def test_drafts_never_leak_through_the_new_filters(made, tree):
    draft = made.product({"name": f"Secret {tree['tag']}", "category_id": tree["synthetic"]["id"],
                          "subcategory_id": tree["syn_lace"]["id"],
                          "variants": _variants(tree["tag"], tree["black"])})
    assert draft["id"] not in _ids(category=tree["synthetic"]["slug"], colour=tree["black"]["slug"])


# ------------------------------------------------------ the order still works

def test_an_order_for_a_colour_variant_charges_that_variant(shop, tree):
    p = shop["syn_lace"]
    blonde = next(v for v in p["variants"] if v["colour_id"] == tree["blonde"]["id"])
    r = requests.post(f"{API}/orders", timeout=15, json={
        "customer_name": "Hair Catalogue Shopper",
        "customer_email": f"hair-cat-{_uid()}@example.com",
        "shipping": SHIPPING,
        "items": [{"product_id": p["id"], "variant_id": blonde["id"], "quantity": 1}],
    })
    assert r.status_code in (200, 201), r.text
    item = r.json()["items"][0]
    assert item["variant_id"] == blonde["id"]
    assert tree["blonde"]["name"] in (item.get("variant_label") or "")
