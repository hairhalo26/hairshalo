"""Generic supplier catalogue import — synthetic supplier data only.

Rules under test:

* Imported products are Draft, invisible to customers, until an admin publishes.
* The supplier price is stored apart from the selling price; the selling price
  is rate -> markup -> rounding, or the admin's override.
* A product already in Hairshalo is shown as a duplicate and only changed when
  the admin chooses "update" — and even then its status, stock and (unless
  asked) price are left alone.
* A bad row costs that row, not the file.
* Only staff can reach any of it, and no URL is fetched without the admin
  confirming the image rights — and then only from allowed, public hosts.

API tests need a backend on port 8010 and DATABASE_URL pointing at the same
database. The URL-safety tests run in-process with the network stubbed out.
"""
import io
import json
import os
import socket
import uuid
from decimal import Decimal

import pytest
import requests

from app import supplier_import as svc

API = os.getenv("VERA_API", "http://127.0.0.1:8010/api")
ADMIN = {"email": "admin@hairshalo.com", "password": "ChangeMe123!"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _api_up():
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="backend not running on port 8010")


def _uid():
    return uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def auth():
    if not _api_up():
        pytest.skip("backend not running on port 8010")
    r = requests.post(f"{API}/auth/login", timeout=10, json=ADMIN)
    if r.status_code != 200:
        pytest.skip("admin credentials rejected")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _supplier(auth, currency="GBP", hosts=None):
    r = requests.post(f"{API}/supplier-import/suppliers", headers=auth, timeout=10, json={
        "name": f"Aurora Hair Supply {_uid()}", "currency": currency,
        "allowed_image_hosts": hosts or ["images.aurora-supply.test"]})
    assert r.status_code == 201, r.text
    return r.json()


def _upload(auth, supplier, name, body, content_type="text/csv"):
    return requests.post(f"{API}/supplier-import/imports", headers=auth, timeout=20,
                         data={"supplier_id": supplier["id"]},
                         files={"file": (name, body if isinstance(body, bytes) else body.encode(), content_type)})


def _preview(auth, batch, mapping=None, category_mapping=None, pricing=None):
    return requests.post(f"{API}/supplier-import/imports/{batch['id']}/preview", headers=auth, timeout=30,
                         json={"mapping": mapping or batch["suggested_mapping"],
                               "category_mapping": category_mapping or {},
                               "pricing": pricing or {"exchange_rate": "100", "markup_percent": "50",
                                                      "markup_fixed": "0", "rounding": "nearest_1"}})


def _commit(auth, batch, selections, **opts):
    return requests.post(f"{API}/supplier-import/imports/{batch['id']}/commit", headers=auth, timeout=60,
                         json={"selections": selections, **opts})


def _select(preview, action=None):
    return [{"key": p["key"], "action": action or p["action"]} for p in preview["products"]]


def _csv(tag, extra_rows="", price="40.00", currency="GBP"):
    return (
        "product_id,title,brand,description,category,subcategory,hair_type,texture,"
        "sku,colour,length,cost_price,currency,availability,quantity,features,care\n"
        f"AUR-{tag},Aurora Body Wave Clip-in {tag},Aurora,<p>Soft <b>body wave</b> clip-ins.</p>,"
        f"Extensions,Clip-in Extensions,Human Hair,Body Wave,AUR-{tag}-1B-18,1B,18\",{price},{currency},"
        "In stock,12,Seven wefts | Silicone grips,Wash in cool water\n"
        f"AUR-{tag},Aurora Body Wave Clip-in {tag},Aurora,,Extensions,Clip-in Extensions,,,"
        f"AUR-{tag}-4-18,4,18\",{price},{currency},Out of stock,0,,\n" + extra_rows)


def _product(auth, pid):
    return requests.get(f"{API}/products/{pid}", headers=auth, timeout=10).json()


# ================================================================ unit: pricing
def test_price_is_rate_then_markup_then_rounding():
    s = svc.pricing_settings({"exchange_rate": "105.5", "markup_percent": "40",
                              "markup_fixed": "250", "rounding": "nearest_10"}, "GBP")
    # 100 GBP * 105.5 = 10550; +40% = 14770; +250 = 15020; nearest 10 = 15020
    assert svc.calculate_price(Decimal("100"), s) == Decimal("15020.00")
    s["rounding"] = "end_99"
    assert svc.calculate_price(Decimal("100"), s) == Decimal("15099.00")
    s["rounding"] = "nearest_100"
    assert svc.calculate_price(Decimal("100"), s) == Decimal("15000.00")
    s["rounding"] = "none"
    # 33.33 * 105.5 = 3516.315; * 1.4 = 4922.841; + 250 = 5172.841
    assert svc.calculate_price(Decimal("33.33"), s) == Decimal("5172.84")
    assert svc.calculate_price(None, s) is None


def test_pricing_settings_refuse_nonsense():
    for bad in ({"exchange_rate": "0"}, {"exchange_rate": "-1"}, {"exchange_rate": "abc"},
                {"exchange_rate": "1", "markup_percent": "-5"},
                {"exchange_rate": "1", "rounding": "sideways"}):
        with pytest.raises(svc.SupplierImportError):
            svc.pricing_settings(bad, "GBP")


def test_a_suggested_rate_comes_from_the_shop_table():
    assert svc.default_exchange_rate("INR") == Decimal("1")
    gbp = svc.default_exchange_rate("GBP")
    assert gbp and gbp > 50            # ₹ per £, from the static table


def test_money_parsing_is_tolerant_but_strict():
    assert svc.parse_money("£1,234.50") == Decimal("1234.50")
    assert svc.parse_money("1.234,50") == Decimal("1234.50")
    assert svc.parse_money("12,5") == Decimal("12.50")
    assert svc.parse_money("") is None
    for bad in ("free", "-4", "99999999999"):
        with pytest.raises(ValueError):
            svc.parse_money(bad)


def test_supplier_html_becomes_plain_text():
    assert svc.clean_text("<p>Soft <b>wave</b></p><script>alert(1)</script><ul><li>A</li></ul>") \
        == "Soft wave\n• A"
    assert svc.as_lines("One | Two ; Three") == "One\nTwo\nThree"


# ================================================================ unit: parsing
def test_files_that_are_not_catalogues_are_refused():
    for name, body in [("x.csv", b""), ("x.json", b"{not json"), ("x.json", b'{"a": 1}'),
                       ("x.csv", b"\x00\x01binary"), ("x.exe", b"MZ\x90"),
                       ("x.csv", b"a,a\n1,2\n"),
                       ("x.csv", b"a\n" + b"x\n" * (svc.MAX_ROWS + 1)),
                       ("x.csv", b"a\n" + b"x" * (svc.MAX_FILE_BYTES + 1))]:
        with pytest.raises(svc.SupplierImportError):
            svc.parse_file(name, body)


def test_json_variants_flatten_to_one_row_each():
    fmt, cols, rows = svc.parse_file("c.json", json.dumps({"products": [
        {"id": "P1", "title": "Wig", "images": [{"src": "https://x/1.jpg"}, "https://x/2.jpg"],
         "variants": [{"sku": "A", "colour": "1B", "price": "10"}, {"sku": "B", "colour": "2", "price": "11"}]},
        {"id": "P2", "title": "Ponytail", "price": "5"}]}).encode())
    assert fmt == "json" and len(rows) == 3
    assert rows[0]["variants.sku"] == "A" and rows[0]["images"] == "https://x/1.jpg | https://x/2.jpg"
    mapping = svc.suggest_mapping(cols)
    assert mapping["supplier_sku"] == "variants.sku" and mapping["color"] == "variants.colour"
    assert mapping["variant_price"] == "variants.price" and mapping["source_price"] == "price"


# ================================================================ unit: URL safety
def test_image_urls_are_vetted_before_any_fetch(monkeypatch):
    allowed = ["images.aurora-supply.test"]
    for url in ["http://images.aurora-supply.test/a.jpg",           # not https
                "https://evil.test/a.jpg",                         # host not allowed
                "https://user:pw@images.aurora-supply.test/a.jpg", # credentials
                "https://images.aurora-supply.test:8443/a.jpg",    # odd port
                "file:///etc/passwd", "gopher://images.aurora-supply.test/"]:
        with pytest.raises(ValueError):
            svc.vet_image_url(url, allowed)

    def resolves_to(address):
        return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    for private in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.9", "0.0.0.0"):
        monkeypatch.setattr(svc.socket, "getaddrinfo", resolves_to(private))
        with pytest.raises(ValueError, match="private or reserved"):
            svc.vet_image_url("https://images.aurora-supply.test/a.jpg", allowed)
    monkeypatch.setattr(svc.socket, "getaddrinfo", resolves_to("93.184.216.34"))
    assert svc.vet_image_url("https://cdn.images.aurora-supply.test/a.jpg", allowed) == \
        ("cdn.images.aurora-supply.test", "93.184.216.34")


def test_only_real_images_are_stored():
    for data, ctype in [(b"not an image", "image/jpeg"), (b"GIF89a....", "image/gif"),
                        (b"\x00\x00\x00\x18ftypmp42", "video/mp4"), (b"", "image/jpeg")]:
        with pytest.raises(ValueError):
            svc.store_image_bytes(data, ctype)
    key, ctype, size = svc.store_image_bytes(JPEG, "application/octet-stream")
    assert ctype == "image/jpeg" and size == len(JPEG) and key.endswith(".jpg")


# ================================================================ API flows
@live
def test_every_import_endpoint_is_staff_only(auth):
    email = f"imp-{_uid()}@example.com"
    requests.post(f"{API}/account/register", timeout=10,
                  json={"email": email, "password": "correct-horse-battery-91", "name": "Shopper"})
    tok = requests.post(f"{API}/account/login", timeout=10,
                        json={"email": email, "password": "correct-horse-battery-91"}).json()["access_token"]
    for headers in ({}, {"Authorization": f"Bearer {tok}"}):
        for method, path in [("get", "/supplier-import/fields"), ("get", "/supplier-import/suppliers"),
                             ("post", "/supplier-import/suppliers"), ("post", "/supplier-import/imports"),
                             ("get", "/supplier-import/imports"), ("get", "/supplier-import/imports/x"),
                             ("post", "/supplier-import/imports/x/preview"),
                             ("post", "/supplier-import/imports/x/commit"),
                             ("post", "/supplier-import/imports/x/images"),
                             ("get", "/supplier-import/imports/x/errors.csv"),
                             ("get", "/supplier-import/products/x/source")]:
            r = getattr(requests, method)(f"{API}{path}", headers=headers, timeout=10)
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


@live
def test_a_csv_import_creates_draft_products_the_public_cannot_see(auth):
    sup = _supplier(auth)
    tag = _uid()
    batch = _upload(auth, sup, "aurora.csv", _csv(tag)).json()
    assert batch["total_rows"] == 2 and batch["file_format"] == "csv"
    m = batch["suggested_mapping"]
    assert m["name"] == "title" and m["source_price"] == "cost_price" and m["color"] == "colour"
    assert m["supplier_sku"] == "sku" and m["source_product_id"] == "product_id"

    pv = _preview(auth, batch).json()["preview"]
    assert pv["summary"]["products"] == 1 and pv["summary"]["valid"] == 1
    p = pv["products"][0]
    assert len(p["variants"]) == 2 and p["source_price"] == "40.00" and p["source_currency"] == "GBP"
    assert p["calculated_price"] == "6000.00"          # 40 * 100 * 1.5
    assert p["fields"]["description"] == "Soft body wave clip-ins."
    assert p["fields"]["features"] == "Seven wefts\nSilicone grips"
    assert p["duplicate"]["status"] == "new" and p["action"] == "import"

    done = _commit(auth, batch, _select(pv)).json()
    assert done["status"] == "committed" and done["imported"] == 1 and done["errors"] == 0
    result = done["results"][0]
    assert result["outcome"] == "created"
    pid = result["product_id"]

    product = _product(auth, pid)
    assert product["status"] == "Draft"
    assert float(product["price"]) == 6000.0 and product["hair_type"] == "Human Hair"
    assert product["features"] == "Seven wefts\nSilicone grips"
    assert {v["color"] for v in product["variants"]} == {"1B", "4"}
    assert all(v["stock"] == 0 for v in product["variants"]), "supplier availability is not stock"
    # SKUs are printed on the product page: they must not name the supplier.
    for v in product["variants"]:
        assert v["sku"].startswith("HS") and "AURORA" not in v["sku"].upper(), v["sku"]
    # Invisible to customers, by every route.
    assert requests.get(f"{API}/products/{pid}", timeout=10).status_code == 404
    assert pid not in [x["id"] for x in requests.get(f"{API}/products?limit=200", timeout=10).json()]

    # Supplier facts are kept apart, for staff.
    src = requests.get(f"{API}/supplier-import/products/{pid}/source", headers=auth, timeout=10).json()[0]
    assert src["source_price"] == "40.00" and src["source_currency"] == "GBP"
    assert src["source_product_id"] == f"AUR-{tag}" and src["source_availability"] == "In stock"
    assert {v["source_quantity"] for v in src["variants"]} == {12, 0}


@live
def test_supplier_quantity_becomes_stock_only_when_asked(auth):
    sup = _supplier(auth)
    batch = _upload(auth, sup, "aurora.csv", _csv(_uid())).json()
    pv = _preview(auth, batch).json()["preview"]
    done = _commit(auth, batch, _select(pv), stock_from_supplier=True).json()
    product = _product(auth, done["results"][0]["product_id"])
    assert sorted(v["stock"] for v in product["variants"]) == [0, 12]


@live
def test_a_json_file_with_nested_variants(auth):
    sup = _supplier(auth, currency="USD")
    tag = _uid()
    body = json.dumps({"products": [{
        "id": f"J-{tag}", "title": f"Nova Lace Front Wig {tag}", "vendor": "Nova",
        "product_type": "Wig", "construction": "Lace Front", "weight": "180g",
        "heat_resistance": "Up to 180°C", "category": "Wigs", "subcategory": "Lace Front Wigs",
        "variants": [{"sku": f"NV-{tag}-A", "colour": "Natural Black", "length": "20\"", "price": "120"},
                     {"sku": f"NV-{tag}-B", "colour": "Brown", "length": "20\"", "price": "130"}]}]})
    batch = _upload(auth, sup, "nova.json", body, "application/json").json()
    assert batch["file_format"] == "json" and batch["total_rows"] == 2
    pv = _preview(auth, batch, pricing={"exchange_rate": "85", "markup_percent": "0",
                                        "markup_fixed": "0", "rounding": "none"}).json()["preview"]
    p = pv["products"][0]
    assert p["source_price"] == "120.00" and p["calculated_price"] == "10200.00"
    assert [v["calculated_price"] for v in p["variants"]] == ["10200.00", "11050.00"]
    done = _commit(auth, batch, _select(pv)).json()
    product = _product(auth, done["results"][0]["product_id"])
    assert product["weight"] == "180g" and product["construction"] == "Lace Front"
    prices = sorted(float(v["price"]) for v in product["variants"])
    assert prices == [10200.0, 11050.0]          # the dearer variant keeps its own price


@live
def test_custom_columns_are_mapped_and_remembered(auth):
    sup = _supplier(auth)
    tag = _uid()
    body = ("product_title,cost_price,colour,length,item_ref\n"
            f"Crochet Twist {tag},12.50,1B,14\",CT-{tag}-1\n"
            f"Crochet Twist {tag},12.50,2,14\",CT-{tag}-2\n")
    batch = _upload(auth, sup, "twist.csv", body).json()
    mapping = {"name": "product_title", "source_price": "cost_price", "color": "colour",
               "length": "length", "supplier_sku": "item_ref"}
    pv = _preview(auth, batch, mapping=mapping).json()["preview"]
    assert pv["products"][0]["name"] == f"Crochet Twist {tag}" and len(pv["products"][0]["variants"]) == 2
    # The next file from the same supplier starts from those choices.
    again = _upload(auth, sup, "twist-2.csv", body).json()
    assert again["suggested_mapping"]["supplier_sku"] == "item_ref"


@live
def test_unmapped_categories_wait_for_the_admin(auth):
    sup = _supplier(auth)
    tag = _uid()
    body = ("product_id,title,category,price\n"
            f"C-{tag},Tape-in Set {tag},Tape Ins {tag},20\n")
    batch = _upload(auth, sup, "cats.csv", body).json()
    pv = _preview(auth, batch).json()["preview"]
    cat = pv["categories"][0]
    assert cat["status"] == "unmapped" and cat["category_id"] is None
    assert "No Hairshalo category" in " ".join(pv["products"][0]["warnings"])
    # The admin creates (or picks) a category and maps it.
    new_cat = requests.post(f"{API}/categories", headers=auth, timeout=10,
                            json={"name": f"Tape-in Extensions {tag}"}).json()
    pv = _preview(auth, batch, category_mapping={f"Tape Ins {tag}": new_cat["id"]}).json()["preview"]
    assert pv["categories"][0]["status"] == "mapped" and pv["products"][0]["category_id"] == new_cat["id"]
    done = _commit(auth, batch, _select(pv)).json()
    assert _product(auth, done["results"][0]["product_id"])["category_id"] == new_cat["id"]
    # An existing category with the supplier's name is matched automatically.
    body2 = f"product_id,title,category,price\nC2-{tag},Another {tag},Tape-in Extensions {tag},20\n"
    pv2 = _preview(auth, _upload(auth, sup, "c2.csv", body2).json()).json()["preview"]
    assert pv2["categories"][0]["status"] == "auto"


@live
def test_the_admin_can_override_the_selling_price(auth):
    sup = _supplier(auth)
    batch = _upload(auth, sup, "a.csv", _csv(_uid())).json()
    pv = _preview(auth, batch).json()["preview"]
    done = _commit(auth, batch, [{"key": pv["products"][0]["key"], "action": "import",
                                  "price_override": "4999"}]).json()
    assert done["results"][0]["price_source"] == "override"
    assert float(_product(auth, done["results"][0]["product_id"])["price"]) == 4999.0


@live
def test_bad_rows_are_reported_and_good_rows_still_import(auth):
    sup = _supplier(auth)
    tag = _uid()
    bad = (f"B1-{tag},,Aurora,,,,,,B1-{tag},1B,10\",10,GBP,,,,\n"                # no name
           f"B2-{tag},Bad Price {tag},Aurora,,,,,,B2-{tag},1B,10\",cheap,GBP,,,,\n"
           f"B3-{tag},Wrong Currency {tag},Aurora,,,,,,B3-{tag},1B,10\",10,EUR,,,,\n"
           f"B4-{tag},Bad Currency {tag},Aurora,,,,,,B4-{tag},1B,10\",10,POUNDS,,,,\n"
           f"B5-{tag},Clash A {tag},Aurora,,,,,,DUP-{tag},1B,10\",10,GBP,,,,\n"
           f"B6-{tag},Clash B {tag},Aurora,,,,,,DUP-{tag},2,10\",10,GBP,,,,\n"
           f"B7-{tag},Bad Qty {tag},Aurora,,,,,,B7-{tag},1B,10\",10,GBP,,lots,,\n")
    batch = _upload(auth, sup, "mixed.csv", _csv(tag, extra_rows=bad)).json()
    pv = _preview(auth, batch).json()["preview"]
    by_id = {p["source_product_id"]: p for p in pv["products"]}
    assert by_id[f"AUR-{tag}"]["valid"]
    for key, needle in [("B1", "name is empty"), ("B2", "not a valid price"), ("B3", "this supplier's currency"),
                        ("B4", "not a valid currency"), ("B5", "more than one product"),
                        ("B6", "more than one product"), ("B7", "whole-number quantity")]:
        p = by_id[f"{key}-{tag}"]
        assert not p["valid"] and needle in " ".join(p["errors"]), (key, p["errors"])
    # Selecting everything imports only the good product.
    done = _commit(auth, batch, _select(pv, action="import")).json()
    assert done["imported"] == 1 and done["errors"] == 7
    report = requests.get(f"{API}/supplier-import/imports/{batch['id']}/errors.csv", headers=auth, timeout=10)
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/csv")
    assert "not a valid price" in report.text and "Bad Currency" in report.text


@live
def test_a_second_import_finds_the_existing_product_and_updates_only_on_request(auth):
    sup = _supplier(auth)
    tag = _uid()
    first = _upload(auth, sup, "v1.csv", _csv(tag)).json()
    pv = _preview(auth, first).json()["preview"]
    pid = _commit(auth, first, _select(pv)).json()["results"][0]["product_id"]
    # The admin sets stock and publishes it.
    product = _product(auth, pid)
    for v in product["variants"]:
        requests.put(f"{API}/products/variants/{v['id']}", headers=auth, timeout=10, json={"stock": 5})
    assert requests.post(f"{API}/products/{pid}/status", headers=auth, timeout=10,
                         json={"action": "publish", "force": True}).status_code == 200

    # v2: new description, supplier price up, one new colour.
    v2 = _csv(tag, price="50.00").replace("Soft <b>body wave</b> clip-ins.", "Now even softer.") + \
        (f"AUR-{tag},Aurora Body Wave Clip-in {tag},Aurora,,Extensions,Clip-in Extensions,,,"
         f"AUR-{tag}-27-18,27,18\",50.00,GBP,In stock,3,,\n")
    second = _upload(auth, sup, "v2.csv", v2).json()
    pv2 = _preview(auth, second).json()["preview"]
    p = pv2["products"][0]
    assert p["duplicate"]["status"] == "existing" and p["action"] == "skip"
    assert p["duplicate"]["product"]["id"] == pid
    assert p["duplicate"]["price_now"] == "6000.00" and p["duplicate"]["price_new"] == "7500.00"
    assert any(c["field"] == "description" for c in p["duplicate"]["changes"])
    assert len(p["duplicate"]["new_variants"]) == 1
    assert p["duplicate"]["new_variants"][0].endswith(f"-AUR-{tag}-27-18")

    done = _commit(auth, second, [{"key": p["key"], "action": "update"}]).json()
    r = done["results"][0]
    assert r["outcome"] == "updated" and "description" in r["fields_changed"] and r["price_kept"]
    after = _product(auth, pid)
    assert after["status"] == "Published", "an update never changes status"
    assert float(after["price"]) == 6000.0, "the published price is not moved without asking"
    assert after["description"] == "Now even softer."
    assert len(after["variants"]) == 3
    stock = {v["color"]: v["stock"] for v in after["variants"]}
    assert stock["1B"] == 5 and stock["4"] == 5 and stock["27"] == 0, "existing stock untouched"

    # Recalculating prices is an explicit choice.
    third = _upload(auth, sup, "v3.csv", v2).json()
    pv3 = _preview(auth, third).json()["preview"]
    done = _commit(auth, third, [{"key": pv3["products"][0]["key"], "action": "update"}],
                   update_prices=True).json()
    assert done["results"][0]["price_change"] == {"from": "6000.00", "to": "7500.00"}
    assert float(_product(auth, pid)["price"]) == 7500.0

    # "Import as new" makes a separate draft with its own SKUs.
    fourth = _upload(auth, sup, "v4.csv", v2).json()
    pv4 = _preview(auth, fourth).json()["preview"]
    done = _commit(auth, fourth, [{"key": pv4["products"][0]["key"], "action": "new"}]).json()
    twin = _product(auth, done["results"][0]["product_id"])
    assert twin["id"] != pid and twin["status"] == "Draft"
    assert not {v["sku"] for v in twin["variants"]} & {v["sku"] for v in after["variants"]}


@live
def test_matching_by_name_and_brand_across_suppliers(auth):
    tag = _uid()
    first = _upload(auth, _supplier(auth), "a.csv", _csv(tag)).json()
    _commit(auth, first, _select(_preview(auth, first).json()["preview"]))
    other = _upload(auth, _supplier(auth), "b.csv",
                    _csv(tag).replace(f"AUR-{tag}-", f"OTHER-{tag}-")).json()
    p = _preview(auth, other).json()["preview"]["products"][0]
    assert p["duplicate"]["status"] == "existing" and "name" in p["duplicate"]["reason"].lower()


@live
def test_one_failing_product_does_not_undo_the_others(auth):
    sup = _supplier(auth)
    tag, tag2 = _uid(), _uid()
    first = _upload(auth, sup, "a.csv", _csv(tag)).json()
    pid = _commit(auth, first, _select(_preview(auth, first).json()["preview"])).json()["results"][0]["product_id"]
    both = _upload(auth, sup, "b.csv", _csv(tag) + _csv(tag2).split("\n", 1)[1]).json()
    pv = _preview(auth, both).json()["preview"]
    existing = next(p for p in pv["products"] if p["duplicate"]["status"] == "existing")
    fresh = next(p for p in pv["products"] if p["duplicate"]["status"] == "new")
    # The product it would update disappears between preview and commit.
    requests.delete(f"{API}/products/{pid}", headers=auth, timeout=10)
    done = _commit(auth, both, [{"key": existing["key"], "action": "update"},
                                {"key": fresh["key"], "action": "import"}]).json()
    outcomes = {r["key"]: r["outcome"] for r in done["results"]}
    assert outcomes[existing["key"]] == "error" and outcomes[fresh["key"]] == "created"
    assert done["imported"] == 1 and done["errors"] == 1


@live
def test_uploaded_image_files_attach_and_bad_ones_are_rejected(auth):
    sup = _supplier(auth)
    tag = _uid()
    body = ("product_id,title,price,images\n"
            f"I-{tag},Image Test {tag},10,front-{tag}.jpg | missing-{tag}.jpg\n")
    batch = _upload(auth, sup, "img.csv", body).json()
    r = requests.post(f"{API}/supplier-import/imports/{batch['id']}/images", headers=auth, timeout=20,
                      files=[("files", (f"front-{tag}.jpg", JPEG, "image/jpeg")),
                             ("files", (f"fake-{tag}.jpg", b"<html>not an image</html>", "image/jpeg"))])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert any(f"fake-{tag}.jpg" in x for x in detail["rejected"]) and f"front-{tag}.jpg" in detail["accepted"]
    pv = _preview(auth, batch).json()["preview"]
    statuses = {i["ref"]: i["status"] for i in pv["products"][0]["images"]}
    assert statuses == {f"front-{tag}.jpg": "uploaded", f"missing-{tag}.jpg": "file missing"}
    done = _commit(auth, batch, _select(pv)).json()
    res = done["results"][0]
    assert res["images"]["attached"] == 1 and len(res["images"]["failed"]) == 1
    product = _product(auth, res["product_id"])
    assert len(product["media"]) == 1 and product["media"][0]["is_primary"]
    base = API.rsplit("/api", 1)[0]
    assert requests.get(base + product["media"][0]["url"], timeout=10).content[:3] == b"\xff\xd8\xff"


@live
def test_image_urls_are_never_fetched_without_confirmed_rights(auth):
    sup = _supplier(auth)
    tag = _uid()
    body = ("product_id,title,price,images\n"
            f"U-{tag},Url Test {tag},10,https://images.aurora-supply.test/{tag}.jpg | "
            f"https://elsewhere.test/{tag}.jpg | http://images.aurora-supply.test/{tag}.jpg\n")
    batch = _upload(auth, sup, "urls.csv", body).json()
    pv = _preview(auth, batch).json()["preview"]
    assert [i["status"] for i in pv["products"][0]["images"]] == ["url", "host not allowed", "not https"]
    done = _commit(auth, batch, _select(pv)).json()          # images_authorized defaults to False
    res = done["results"][0]
    assert res["outcome"] == "created", "the product imports without its images"
    assert res["images"]["attached"] == 0 and len(res["images"]["failed"]) == 3
    assert all("not downloaded" in f["reason"] for f in res["images"]["failed"])
    assert _product(auth, res["product_id"])["media"] == []


@live
def test_the_import_log_records_every_batch(auth):
    sup = _supplier(auth)
    batch = _upload(auth, sup, "log.csv", _csv(_uid())).json()
    pv = _preview(auth, batch).json()["preview"]
    _commit(auth, batch, _select(pv))
    log = requests.get(f"{API}/supplier-import/imports", params={"supplier_id": sup["id"]},
                       headers=auth, timeout=10).json()
    entry = next(x for x in log if x["id"] == batch["id"])
    assert entry["status"] == "committed" and entry["imported"] == 1 and entry["total_rows"] == 2
    assert entry["supplier_name"] == sup["name"] and entry["file_name"] == "log.csv"
    detail = requests.get(f"{API}/supplier-import/imports/{batch['id']}", headers=auth, timeout=10).json()
    assert detail["results"][0]["outcome"] == "created" and detail["settings"]["pricing"]["exchange_rate"]
    # A batch commits once; a batch that was never previewed cannot commit.
    assert _commit(auth, batch, _select(pv)).status_code == 409
    fresh = _upload(auth, sup, "log2.csv", _csv(_uid())).json()
    assert _commit(auth, fresh, []).status_code == 400


# ================================================================ in-process commit with a stubbed network
def test_authorised_url_images_are_downloaded_into_media(monkeypatch):
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        tag = _uid()
        sup = models.Supplier(name=f"Stub Supplier {tag}", code=f"STB{tag[:4]}".upper(), currency="GBP",
                              allowed_image_hosts=["images.aurora-supply.test"])
        db.add(sup)
        db.flush()
        rows = [{"product_id": f"S-{tag}", "title": f"Stubbed {tag}", "price": "10",
                 "images": f"https://images.aurora-supply.test/{tag}.jpg", "__line": 2, "__group": None}]
        batch = models.SupplierImport(supplier_id=sup.id, file_name="s.csv", file_format="csv",
                                      columns=["product_id", "title", "price", "images"],
                                      raw_rows=rows, total_rows=1, image_files={})
        db.add(batch)
        db.flush()
        pv = svc.build_preview(db, sup, batch, svc.suggest_mapping(batch.columns), {},
                               {"exchange_rate": "100", "markup_percent": "0", "rounding": "none"})
        batch.preview, batch.settings = {"products": pv["products"]}, pv["settings"]

        calls = []
        monkeypatch.setattr(svc, "vet_image_url", lambda url, hosts: ("images.aurora-supply.test", "93.184.216.34"))
        monkeypatch.setattr(svc, "_fetch_bytes", lambda url, host, ip: (calls.append(url) or (JPEG, "image/jpeg")))
        out = svc.commit(db, sup, batch, [{"key": pv["products"][0]["key"], "action": "import"}],
                         {"images_authorized": True}, "tester")
        res = out["results"][0]
        assert res["outcome"] == "created" and res["images"]["attached"] == 1 and len(calls) == 1
        product = db.query(models.Product).get(res["product_id"])
        assert product.status == models.ProductStatus.draft
        assert product.media[0].url.startswith("/media/") and product.media[0].storage_key
    finally:
        db.rollback()
        db.close()


def test_a_product_that_fails_midway_leaves_nothing_behind(monkeypatch):
    """The failure happens AFTER the product and its variants were written:
    its savepoint must undo them, while the product before it stays."""
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        tag = _uid()
        sup = models.Supplier(name=f"Rollback Supplier {tag}", code=f"RB{tag[:4]}".upper(), currency="INR")
        db.add(sup)
        db.flush()
        rows = [{"product_id": f"OK-{tag}", "title": f"Kept {tag}", "price": "100", "sku": f"OK-{tag}",
                 "__line": 2, "__group": None},
                {"product_id": f"BAD-{tag}", "title": f"Broken {tag}", "price": "100", "sku": f"BAD-{tag}",
                 "__line": 3, "__group": None}]
        batch = models.SupplierImport(supplier_id=sup.id, file_name="r.csv", file_format="csv",
                                      columns=["product_id", "title", "price", "sku"],
                                      raw_rows=rows, total_rows=2, image_files={})
        db.add(batch)
        db.flush()
        pv = svc.build_preview(db, sup, batch, svc.suggest_mapping(batch.columns), {},
                               {"exchange_rate": "1", "markup_percent": "0", "rounding": "none"})
        batch.preview, batch.settings = {"products": pv["products"]}, pv["settings"]

        real_link = svc._link_variant

        def explode_on_broken(db_, link, v, variant):
            real_link(db_, link, v, variant)
            if v["supplier_sku"] == f"BAD-{tag}":
                raise RuntimeError("simulated failure after writing")
        monkeypatch.setattr(svc, "_link_variant", explode_on_broken)

        out = svc.commit(db, sup, batch, [{"key": p["key"], "action": "import"} for p in pv["products"]],
                         {}, "tester")
        outcomes = {r["name"]: r["outcome"] for r in out["results"]}
        assert outcomes == {f"Kept {tag}": "created", f"Broken {tag}": "error"}
        db.flush()
        assert db.query(models.Product).filter(models.Product.name == f"Kept {tag}").count() == 1
        assert db.query(models.Product).filter(models.Product.name == f"Broken {tag}").count() == 0
        assert db.query(models.ProductVariant).filter(models.ProductVariant.sku.like(f"%BAD-{tag}")).count() == 0
        assert db.query(models.SupplierProduct).filter(
            models.SupplierProduct.source_product_id == f"BAD-{tag}").count() == 0
    finally:
        db.rollback()
        db.close()
