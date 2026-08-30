# Hairshalo — Full-Stack Demo

A complete wig & hairstyling e-commerce platform: FastAPI + PostgreSQL backend,
a customer website with AI shopping tools, and an admin dashboard.

```
vera-project/
├── backend/          FastAPI + PostgreSQL API
├── frontend/          Customer site, admin dashboard, landing page
└── docker-compose.yml Spins up Postgres + backend together
```

**Important — read this first:** this code was written but could not be run or
tested in the environment it was generated in (no internet access, no Postgres
available there). It follows standard, well-established FastAPI/SQLAlchemy
patterns and should run correctly, but treat it as a strong first draft —
run it locally, watch the terminal output, and fix anything Python's error
messages point to. That's normal for a project this size.

---

## Option A — Run with Docker (recommended, easiest)

Requires [Docker](https://www.docker.com/products/docker-desktop/) installed.

```bash
cd vera-project
docker compose up --build
```

This starts PostgreSQL and the API together. On first boot the backend automatically
creates all tables and seeds demo data (products, orders, customers, appointments).

Once it's running:
- API: **http://localhost:8000**
- Interactive API docs (Swagger): **http://localhost:8000/docs**
- Seeded admin login: `admin@verahair.co` / `ChangeMe123!`

To stop: `Ctrl+C`, then `docker compose down` (add `-v` to also wipe the database).

---

## Option B — Run manually (without Docker)

### 1. Install PostgreSQL
Install Postgres locally, then create the database and user:
```sql
CREATE USER vera_user WITH PASSWORD 'vera_pass';
CREATE DATABASE vera_hair_co OWNER vera_user;
```

### 2. Set up the backend
```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # edit DATABASE_URL if your Postgres setup differs
alembic upgrade head            # creates/updates all tables (migrations)
SEED_DEMO_DATA=true python -m app.seed   # OPTIONAL: development demo data
uvicorn app.main:app --reload --port 8010
```

**Schema is owned by Alembic.** The app never creates tables at runtime, so
production upgrades are explicit and reviewable:

| Command | Purpose |
|---|---|
| `alembic upgrade head` | Apply all pending migrations |
| `alembic current` | Show the revision the database is on |
| `alembic history` | List migrations |
| `alembic downgrade 0001_baseline` | Roll back the product-architecture change |
| `alembic stamp 0001_baseline` | Mark a pre-Alembic database as being at the baseline |

`0001_baseline` is the schema as it existed before product hardening;
`0002_product_architecture` adds categories, variants, media and the publishing
workflow, migrating existing data (category text → `categories`, `image_url` →
`product_media`, status `active` → `published`) rather than dropping it.

**Demo data is development-only.** `python -m app.seed` refuses to run unless
`SEED_DEMO_DATA=true` (or `--force`) is set, and every catalog row it creates is
tagged `is_demo=true`, so demo records can always be told apart from real ones
(`GET /api/products?include_demo=false`).
API is now live at **http://localhost:8000** (docs at `/docs`).

---

## Running the frontend

The frontend is plain HTML/CSS/JS — no build step. Serve it with any static
server (opening the file directly via `file://` will work for browsing, but
some browsers block API calls from `file://` origins, so a local server is
safer):

```bash
cd frontend
python3 -m http.server 5500
```

Then open **http://localhost:5500** in your browser. This loads `index.html`,
which links to:
- **Vera-Hair-Co-Demo.html** — the customer site (products load live from the
  API; if the API isn't running, it falls back to demo data automatically)
- **admin-dashboard.html** — sign in with the seeded admin login to see real
  data, or click "Continue in demo mode" to preview with mock data

Both frontends resolve the API base themselves, so a deployment needs no build
step and no edit to the HTML:

| Page served from | API base used |
| --- | --- |
| `file://`, `localhost`, `127.0.0.1`, `::1` | `http://localhost:8010/api` |
| any other host | `/api` — same origin as the page |

Port 8010 rather than 8000 avoids colliding with other local dev servers. In
production the proxy serves the storefront and the API from one origin, so the
relative `/api` is correct and raises no CORS.

Set `window.__VERA_API_BASE__` before the scripts run to override both rules —
that is the escape hatch for a split `api.` subdomain, or for reaching a dev
backend from another device on the LAN (where the page's host is neither
localhost nor the API's origin).

The **Best Sellers** section is rendered from the database, not hardcoded: it
requests `/api/products` and, only if there are too few real products to fill
the row, tops it up from `/api/product-placeholders`. That top-up is opt-in via
`BESTSELLERS_FILL_WITH_PLACEHOLDERS` in `Vera-Hair-Co-Demo.html`.

> **Docker note:** if host port 5432 is already in use, `docker-compose.override.yml`
> publishes Postgres on **5433** instead — set `DATABASE_URL` to match
> (`...@localhost:5433/vera_hair_co`).

---

## API overview

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/login` | — | Admin login, returns JWT |
| GET | `/api/products` | — | List **published** products; supports search/filter/sort (see below) |
| GET | `/api/products/{id}` | — | Single product with media + variants |
| POST | `/api/products` | Admin | Create product (Draft by default) |
| PUT/DELETE | `/api/products/{id}` | Admin | Update / delete product |
| POST | `/api/products/{id}/status` | Admin | Workflow: `submit_for_review`, `publish`, `unpublish`, `archive`, `restore` |
| GET | `/api/products/{id}/preview` | Admin | Full payload for any status (pre-publish preview) |
| GET/POST | `/api/products/{id}/variants` | Admin | List / add variants |
| PUT/DELETE | `/api/products/variants/{id}` | Admin | Update / delete a variant |
| POST | `/api/products/{id}/media` | Admin | Add an image or video |
| DELETE | `/api/products/media/{id}` | Admin | Remove media |
| GET | `/api/categories` | — | Database-driven categories with product counts |
| POST/PUT/DELETE | `/api/categories[/{id}]` | Admin | Manage categories |
| POST | `/api/orders` | — | Place an order (checkout) |
| GET | `/api/orders` | Admin | List all orders |
| PUT | `/api/orders/{id}/status` | Admin | Update order status |
| GET | `/api/customers` | Admin | List customers with order stats |
| POST | `/api/appointments` | — | Book a fitting/consultation |
| GET | `/api/appointments` | Admin | List appointments |
| GET/POST | `/api/inventory` | Admin | Stock levels, add SKUs |
| PUT | `/api/inventory/{id}/adjust` | Admin | Adjust stock quantity |
| POST | `/api/coupons/validate` | — | Validate a coupon code |
| GET | `/api/product-placeholders` | — | List visible placeholders (`?include_hidden=true` for admin) |
| GET | `/api/product-placeholders/{id}` | — | Single placeholder |
| POST | `/api/product-placeholders` | Admin | Create placeholder |
| PUT/DELETE | `/api/product-placeholders/{id}` | Admin | Update / delete placeholder |
| POST | `/api/product-placeholders/{id}/convert-to-product` | Admin | Create a **new** real product from a placeholder |
| GET/POST | `/api/coupons` | Admin | Manage coupons |
| GET | `/api/analytics/summary` | Admin | Revenue, orders, customers KPIs |
| GET | `/api/analytics/top-products` | Admin | Best sellers by units sold |
| GET | `/api/analytics/revenue-trend` | Admin | Daily revenue for charting |
| GET | `/api/payments/config` | — | Which payment provider is active |
| POST | `/api/payments/intent` | — | Create a gateway intent for an order (amount from the DB) |
| POST | `/api/payments/confirm` | — | Confirm a gateway return; signature verified server-side |
| POST | `/api/payments/webhook/{provider}` | — | Gateway webhook; signature verified, idempotent |
| POST | `/api/payments/{id}/mark-paid` | Admin | Confirm an **offline** payment (manual provider only) |
| POST | `/api/payments/{id}/refund` | Admin | Refund, returning stock through the audit trail |
| GET | `/api/notifications` | Admin | The outbox — filter by status, event type, recipient, order |
| GET | `/api/notifications/{id}` | Admin | One message, including the exact body that was sent |
| GET | `/api/notifications/config` | Admin | Active channel, dispatch mode and configuration gaps |
| POST | `/api/notifications/dispatch` | Admin | Drain the queue now (also the worker's endpoint) |
| POST | `/api/notifications/{id}/retry` | Admin | Requeue a dead letter |
| POST | `/api/notifications/{id}/cancel` | Admin | Abandon a queued message |
| POST | `/api/notifications/test` | Admin | Send a test message to prove the channel works |
| GET/POST | `/api/notifications/unsubscribe` | — | Preview / act on a signed opt-out token |
| GET/POST/DELETE | `/api/notifications/suppressions[/{email}]` | Admin | Manage bounces and opt-outs |
| GET | `/api/health` | — | Liveness. Touches nothing else, by design |
| GET | `/api/ready` | — | Readiness: database reachable, migrations applied, configuration findings |
| GET | `/api/version` | — | Version, commit and environment of what is deployed |

Full interactive docs with request/response schemas are auto-generated at
**`/docs`** once the backend is running.

---

## Pricing & discounts

Money is stored as `NUMERIC(12,2)` and computed with `Decimal` — never floats.
`app/pricing.py` is the single source of truth; routers never do their own
arithmetic.

**There is deliberately no `selling_price` column.** The columns that already
existed carry the meaning:

| Column | Meaning |
|---|---|
| `price` | the actual selling price — this is what orders charge |
| `compare_at_price` | the original price, `NULL` when there is no discount |
| `discount_type` | `none` \| `percentage` \| `fixed_amount` |
| `discount_value` | the admin's input (20 for 20%, or 5000 for ₹5,000) |
| `discount_amount` | **derived** (`compare_at_price − price`), never stored |

An admin submits `original_price` + `discount_type` + `discount_value`; the
server derives the rest:

```
₹25,000 · percentage · 20  →  price ₹20,000, compare_at ₹25,000, 20% OFF
₹25,000 · fixed_amount · 5000 →  price ₹20,000, compare_at ₹25,000, 20% OFF
₹25,000 · none              →  price ₹25,000, compare_at NULL, no sale shown
```

Rejected with a readable message: percentage outside 0–100, negative values,
a fixed discount above the original price, and any set where the selling price
would exceed the original.

Variants may carry their own `original_price`/discount, overriding the
product's. A variant without one inherits the product's resolved pricing.

**Sale display rule:** a "Sale" badge and `% OFF` appear only when
`compare_at_price > price`. `0% OFF` is never rendered, and a discount is never
inferred from `compare_at_price` alone for badge purposes — an admin-set badge
always takes precedence.

## Media

Uploads go through `app/storage.py`, an abstraction over the backing store —
`LocalDiskStorage` today, swappable for S3/Cloudinary without touching the
product system. Uploads are validated by extension **and** magic bytes, capped
at 8 MB (images) / 64 MB (video), and stored under a generated UUID filename,
so a client-supplied filename can never influence the path.

`POST /api/products/{id}/media/upload` (multipart) · `POST .../media/reorder` ·
`DELETE /api/products/media/{id}`

## Product architecture

**Publishing workflow.** `Draft → Review → Published → Archived` (plus
`Out of Stock`). Only `Published` products are returned by the public
`/api/products` or accepted by checkout. Publishing is blocked until the
product's readiness checks pass — missing description, price, image, category
or variants — unless an admin explicitly passes `force=true`.

**Variants.** Each product has variants carrying `length`, `density`, `color`,
`lace_type`, `cap_size`, `sku`, optional `price` (overriding the base price) and
`stock`. If a product has variants, checkout requires one to be chosen.

**Media.** Products own an ordered `product_media` list (images and videos) with
one primary image, replacing the old single `image_url` column. `image_url` is
still returned by the API as a derived convenience field, so existing clients
keep working.

**Badges are data, never derived.** `badge` is one of New / Bestseller /
Featured / Limited / Sale, chosen by an admin. A discounted product with no
badge set renders no badge — a `compare_at_price` alone never creates one.

### Search, filter and sort

`GET /api/products` accepts: `q`, `category`, `category_id`, `min_price`,
`max_price`, `length`, `color`, `density`, `texture`, `availability`
(`in_stock` / `out_of_stock`), `featured`, `bestseller`, `new_arrival`,
`include_demo`, `status` (admin; `all` for every status), plus
`sort` (`curated|newest|price_asc|price_desc|bestselling|featured|name`) and
`limit` / `offset`.

### Checkout safety

Every order is validated server-side before it is accepted:

| Check | Result if violated |
|---|---|
| Product exists | 404 |
| Product is a placeholder | 400, explicit message |
| Product is `Published` | 400 |
| Variant belongs to the product | 400 |
| Variant is available | 400 |
| Sufficient stock | 400 with counts |
| Quantity ≥ 1 | 400 |

**Prices are never taken from the request.** `OrderItemIn` has no price field;
the unit price is read from the variant (or product) in the database. A client
sending `"price": 1` is charged the real price.

**Order history is immutable.** `order_items` stores snapshots of
`product_name`, `variant_label`, `variant_sku` and the unit `price` paid, so
later product edits never rewrite past orders.

**Stock is decremented server-side** when an order succeeds.

---

## Payments

**The rule this enforces: an order is never marked paid because the browser
said so.** The frontend can only report "the customer finished the gateway
flow"; the backend either verifies the gateway's HMAC over the ids the gateway
itself issued, or waits for the gateway's own signed webhook.

Choose a provider with `PAYMENT_PROVIDER`:

| Value | Behaviour |
|---|---|
| `none` (default) | Payments disabled. Orders go straight to Processing. |
| `manual` | Offline settlement (bank transfer / COD). The order waits at **Pending Payment** until an admin confirms receipt. |
| `razorpay` | Live gateway — Orders API, HMAC return verification, webhooks, refunds. Implemented with `hmac`/`hashlib`, no SDK. |

```
checkout → POST /api/payments/intent   (amount read from the database, never the client)
         → customer pays at the gateway
         → POST /api/payments/confirm  (HMAC over gateway-issued ids)
           and/or POST /api/payments/webhook/{provider}  (HMAC over the raw body)
         → order → Paid, stock finalised
```

Stock is reserved when the order is created and released if payment fails or
the order is cancelled, so an abandoned checkout does not hold inventory
hostage. Two independent idempotency guards mean a replayed event is a no-op:
`last_event_id`, and a UNIQUE `provider_payment_id`. No card data is ever
accepted or stored — only gateway identifiers, status, amount and a method
label.

Order lifecycle: `Pending Payment → Paid → Processing → Shipped → Out for
Delivery → Delivered`, plus `Cancelled` / `Refunded`. Transitions are enforced
server-side; Delivered can never go back to Processing.

For Razorpay set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`, and point a webhook at
`POST /api/payments/webhook/razorpay`.

---

## Notifications

**The rule this enforces: a notification is a consequence of a committed fact,
never a cause of one — and it is never sent twice.**

Messages are written to a `notifications` table (a transactional outbox) inside
the same database transaction as the thing that caused them, and delivered
afterwards, outside it. Two consequences follow, and both matter:

- A checkout that rolls back takes its emails with it, so a customer can never
  be told about an order that does not exist.
- A mail server being down can never fail a checkout — nothing performs network
  I/O during the request.

`event_key` is UNIQUE and derived from the event (`order.shipped:<order id>`),
so a retried request, a replayed webhook or a double-clicked admin button all
collapse onto one row. The rendered subject and body are stored, not
re-rendered at send time: the email a customer received stays reproducible even
after the product is renamed.

### Channels

| `NOTIFY_CHANNEL` | Behaviour |
|---|---|
| `console` (default) | Renders and logs. Nothing leaves the machine, so a fresh clone cannot email a real customer by accident. |
| `smtp` | Real delivery via stdlib `smtplib`. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_SECURITY`. |
| `null` | Records every message and marks it Suppressed. For staging clones that must generate no mail. |

Failures are classified: a server that is down is temporary and retried with
exponential backoff (1m, 2m, 4m… capped at an hour, `NOTIFY_MAX_ATTEMPTS`
tries); a refused recipient or bad credentials is permanent and fails
immediately rather than burning retries.

### Who gets what

| Event | To | Template |
|---|---|---|
| Order placed | Customer + staff | `order.placed`, `admin.order_placed` |
| Payment received | Customer | `order.paid` |
| Payment failed / amount mismatch | Staff | `admin.payment_failed` |
| Shipped / Out for delivery / Delivered | Customer | `order.shipped`, `order.out_for_delivery`, `order.delivered` |
| Cancelled / Refunded | Customer | `order.cancelled`, `order.refunded` |
| Appointment booked / confirmed / cancelled | Customer | `appointment.*` |
| Stock at or below `LOW_STOCK_ALERT_THRESHOLD` | Staff | `admin.low_stock` |

Staff alerts go to every address in `ADMIN_ALERT_EMAILS`. Low-stock alerts are
keyed on the level the stock fell to, so crossing the line produces one email,
not one per subsequent sale.

### Delivery mode

`NOTIFY_DISPATCH` decides who drains the queue:

- `background` (default) — after the HTTP response, in the API process. Right
  for a single web container.
- `worker` — the API never sends; run the worker instead. **Use this once more
  than one web container is running**, otherwise retries are scattered across
  whichever process happened to serve a request.
- `inline` — send during the request. Test/debug only.

```bash
python -m app.notify_worker --loop --interval 15
```

Concurrent workers are safe either way: due messages are claimed with
`SELECT ... FOR UPDATE SKIP LOCKED`, and each message commits on its own, so a
crash mid-batch retries at most one message rather than losing it.

### Opt-out

Unsubscribe links carry an HMAC token over the address, so no token can be
forged for someone else's address. `GET /api/notifications/unsubscribe` only
*previews* which address a token belongs to — mail clients prefetch links, and
a GET that unsubscribed people would fire on its own; the `POST` is what acts.

A marketing opt-out does not silence order receipts or delivery updates: those
are a record of a transaction the customer entered into. Only a hard bounce or
complaint (`scope=all`, recorded by staff) stops transactional mail too.

### Seeing what was sent

The admin dashboard's **Notifications** view is the outbox: queued / sent /
failed / suppressed counts, per-message status and attempt count, the exact
stored email previewed in a sandboxed frame, and buttons to retry a dead
letter, cancel a queued message, drain the queue, or send a test message.

---

## Real products vs. product placeholders

These are two **completely separate data domains**. Placeholders exist so the
site can show a full, on-brand catalog while real product data is still being
prepared — they are previews, never merchandise.

|  | Real products | Product placeholders |
|---|---|---|
| Table | `public.products` | `vera_product_placeholders.product_placeholders` |
| API | `/api/products` | `/api/product-placeholders` |
| Purchasable | yes | **no** |
| In cart / checkout | yes | **no** |
| Referenced by orders | yes | **no** |
| Has inventory | yes | **no** |
| Counted in analytics / revenue | yes | **no** |
| Seeded by | `python -m app.seed` | `python -m app.seed_placeholders` |

**Why a schema and not a second database.** Orders, inventory and analytics all
depend on foreign keys into `products`. A second physical database would force
cross-database queries (or a second engine and session) for no isolation
benefit that a dedicated schema does not already give. Placeholders therefore
live in their own PostgreSQL schema, `vera_product_placeholders`, inside the
existing `vera_hair_co` database — a separate namespace, with **no** foreign key
in either direction.

Safeguards, each covered by a test:

- `POST /api/orders` with a placeholder id returns **400** with an explicit
  message, not a generic 404.
- `POST /api/inventory` with a placeholder id returns **400** — placeholders
  cannot hold stock.
- `GET /api/products/{placeholder_id}` returns **404** — the domains never mix.
- `display_price` on a placeholder is a **string** ("From ₹14,000"), not a
  number, so it can never be summed into revenue by accident.
- Placeholder cards are rendered without any `[data-add]` element, so there is
  no Quick Add control to reach the cart.

**Convert to Product** (admin → Product Placeholders → → icon) creates a
brand-new `products` row with its own id and a real numeric price. It never
flips a flag on the placeholder; the placeholder is left untouched unless you
tick "Delete the placeholder after converting".

### Catalog unavailable vs. missing image

These are deliberately different states, and neither one invents products:

| Situation | What the customer sees |
|---|---|
| Product image missing / broken | Branded HAIRSHALO panel on an otherwise normal product card |
| Catalog API unreachable | "Our catalog is briefly unavailable" state with a retry button |
| Catalog API reachable, no products | The section simply renders no product cards |

There is **no** fallback product data in the frontend. If the API fails, zero
product cards are rendered — a customer is never shown something they cannot
actually buy, and checkout reports the failure instead of simulating success.

### Product image fallback

Any real product with a missing, empty, or broken `image_url` automatically
falls back to the premium branded HAIRSHALO panel used by placeholder cards — the
same cream/blush/gold palette, same card dimensions. The image is preloaded in
JavaScript and the background is only applied once it genuinely loads, so a
broken URL never produces a broken-image icon, an alt-text box, or a grey
skeleton. No `<img>` tag is used for product media at all.

Seed placeholders separately if you only want that data:

```bash
python -m app.seed_placeholders
```

---

## Customer accounts

**The rule this enforces: who you are comes from the token, never from the
request body.**

No endpoint takes an email, a customer id or an order number as a way of
choosing whose data to return. `GET /api/account/orders` filters on the id
inside the JWT, so changing an id in a URL matches nothing — and returns
**404, not 403**, because "you may not see this" would confirm the record
exists.

### Registering does not grant an address's history

Almost every row in `customers` was created by **checkout**, from an email
typed into an order form — nobody proved they owned that mailbox. So
registering with an address does not reveal the orders already sitting under
it: order history and loyalty require `email_verified`, which only a click in
that mailbox can set. Profile and wishlist work immediately, because they
expose nothing that predates the account.

### The rest of the rules

| Concern | What the code does |
|---|---|
| Account enumeration | Register, login and password-reset answer **identically** for known and unknown addresses. A second registration on a taken address returns the same 202 — and does not overwrite the existing password. |
| Password storage | bcrypt via passlib. Passwords over 72 bytes are **refused, not truncated** — bcrypt ignores the rest, which would make two different passphrases interchangeable. |
| Weak passwords | Minimum 10 characters, rejected if common or if they contain the email's local part. |
| Stolen tokens | Every token carries a `tv` (token version). Changing a password or calling `/logout-all` bumps it, and every token minted earlier stops working immediately. |
| Reset links | Stored **hashed** (SHA-256), single-use, one-hour expiry. Read access to the database cannot take over an account. Completing a reset also verifies the email, since it proves mailbox control. |
| Password changes | Always emailed to the customer — that notice is how someone discovers a takeover they did not perform. |
| Staff vs customer | Customer tokens carry `typ="customer"` and are rejected by staff endpoints; staff tokens are rejected by customer endpoints. Checked explicitly, not by hoping ids do not collide. |

### Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/account/register` | Create an account, send a verification link |
| POST | `/api/account/verify-email` | Confirm the mailbox |
| POST | `/api/account/login` | Returns a customer JWT plus the profile |
| POST | `/api/account/logout` / `/logout-all` | End this session / invalidate every token |
| GET/PUT | `/api/account/me` | Profile; name, phone and saved display currency |
| POST | `/api/account/password` | Change password (ends other sessions) |
| POST | `/api/account/password-reset/request` \| `/confirm` | Reset by email |
| GET | `/api/account/orders` \| `/orders/{id}` | This customer's orders only |
| GET | `/api/account/loyalty` | Balance plus the ledger behind it |
| GET/POST/PUT/DELETE | `/api/account/addresses[/{id}]` | Saved addresses |
| GET/POST/DELETE | `/api/account/wishlist[/{id}]` | Wishlist |

Balances are **admin-only** elsewhere: with no customer login, a public
"how many points does this address have?" endpoint would be an
address-enumeration oracle that also leaks spending.

---

## Reviews, loyalty & marketing

**The rule this enforces: a growth number is never manufactured.** Every star
traces to a review of a real purchase, every point to a ledger entry, and every
marketing email to a confirmed opt-in.

That rule removed things this project used to ship with: products seeded with
`rating=4.9, review_count=312` and no reviews behind them, four hardcoded
five-star testimonials with invented customer names, a newsletter form that
said "you're on the list" and sent nothing anywhere, and loyalty points that
were minted at checkout and could never be spent.

### Reviews

- **Verified by construction.** A review is submitted with an order number and
  the email on that order, and is attached to the order *line* that bought the
  product. "Verified purchase" is therefore a fact about the data, not a badge
  a form can set. There is no field a client can send to claim it.
- **Only delivered orders.** Reviewing something that has not arrived is an
  opinion about a photograph.
- **Moderated before it counts.** `Product.rating` and `review_count` are a
  cache written *only* by `app/reviews.py:recalculate()`, from published
  reviews — the same contract `app/inventory.py` has with stock. Publishing or
  rejecting a review recomputes it; nothing else may assign to it.
- **One review per purchased line**, enforced by a unique constraint. Buying
  the same wig twice earns two reviews; buying it once does not.
- Failures all return the *same* message, so the endpoint cannot be used to
  test whether an order number exists.
- A product with no reviews shows **"No reviews yet"** — not zero stars.

Moderate them in the dashboard under **Reviews**; the storefront's testimonial
strip and hero card read `/api/reviews/recent` and hide themselves when there
is nothing real to show.

### Loyalty

| Setting | Default | Meaning |
|---|---|---|
| `LOYALTY_EARN_PER` | 100 | Earn 1 point per ₹100 spent |
| `LOYALTY_POINT_VALUE` | 1 | Each point is worth ₹1 |
| `LOYALTY_MAX_REDEEM_PCT` | 20 | Points may cover at most 20% of an order |

- **Points are earned when an order is paid**, not when it is placed. An
  abandoned checkout used to mint points; now it mints nothing. Earning is
  idempotent, so a replayed payment webhook awards once.
- **Spending is server-side.** Checkout accepts `redeem_loyalty_points` — a
  number of points, never a discount. The value, the balance and the ceiling
  all come from the server, and the customer row is locked so two simultaneous
  checkouts cannot spend the same balance twice.
- **Cancellations and refunds unwind both directions**: points earned are
  clawed back, points spent are returned.
- **`loyalty_transactions` is an append-only ledger.** `Customer.loyalty_points`
  is the authoritative balance and this table explains it; nothing outside
  `app/loyalty.py` may assign to it. Migration `0009` gave every pre-existing
  balance an opening entry, so the invariant holds from the first row.

Balances are **admin-only**. There are no customer accounts yet, so a public
balance lookup would be an address-enumeration oracle that also leaks spending.

### Marketing

- **Double opt-in.** Typing an address into the storefront form creates a
  `pending` subscriber and sends one confirmation email. Only clicking the
  signed link makes it `confirmed`. Anyone can type someone else's address into
  a form; only the mailbox owner can click the link.
- **Buying is not subscribing.** Checkout creates a Customer, never a
  subscriber, and there is deliberately no endpoint anywhere that mails "all
  customers" — `send_campaign` resolves recipients itself and takes no
  recipient argument.
- **Campaigns go through the notification outbox**, so suppression, hard
  bounces, unsubscribes and idempotency all apply for free (see
  **Notifications**).
- **Every campaign carries an unsubscribe link** — it is built into the
  template, not a field someone can forget.
- A hard-bounced address cannot be re-subscribed.
- The dashboard reports the audience as a **breakdown**, because "900
  subscribers" is the sentence that hides 700 people who never confirmed.

### Endpoints

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| GET | `/api/reviews/product/{id}` | — | Published reviews for a product |
| GET | `/api/reviews/product/{id}/summary` | — | Average, count and the 1–5 histogram |
| GET | `/api/reviews/recent` | — | Published reviews across the catalog |
| GET | `/api/reviews/reviewable` | — | What an order still has left to review |
| POST | `/api/reviews` | — | Submit a review for a delivered purchase |
| GET | `/api/reviews` | Admin | Moderation queue |
| POST | `/api/reviews/{id}/moderate` | Admin | Publish / reject; recomputes the rating |
| POST | `/api/reviews/{id}/reply` | Admin | Public response under a review |
| GET | `/api/loyalty/programme` | — | The terms. No balances |
| GET | `/api/loyalty/customers/{id}` | Admin | Balance and what it is worth |
| GET | `/api/loyalty/customers/{id}/history` | Admin | The ledger behind the balance |
| POST | `/api/loyalty/customers/{id}/adjust` | Admin | Correction; a note is required |
| POST | `/api/marketing/subscribe` | — | Request a subscription (sends the confirmation) |
| POST | `/api/marketing/confirm` | — | Signed token → consent |
| POST | `/api/marketing/unsubscribe` | — | Signed token → opt-out |
| GET | `/api/marketing/audience` | Admin | Mailable / awaiting / unsubscribed breakdown |
| GET/POST | `/api/marketing/campaigns` | Admin | List and draft campaigns |
| POST | `/api/marketing/campaigns/{id}/send` | Admin | Queue to confirmed subscribers only |

---

## Deploying to production

**The rule this enforces: a production deployment fails loudly rather than
running insecurely.**

Every unsafe way to deploy this application is silent — a default signing key,
`CORS_ORIGINS=*`, the seeded admin password, demo products in a real catalog,
a database that never got migrated. The app starts, serves traffic and looks
healthy. So with `APP_ENV=production`, the startup preflight in
[`app/runtime.py`](backend/app/runtime.py) **refuses to boot** on any of those,
printing what is wrong and how to fix it. Off production the same findings are
logged as warnings, because a laptop is allowed to be insecure and a developer
is not helped by a refusal to start.

```
$ APP_ENV=production uvicorn app.main:app
PreflightError: Refusing to start: 5 unsafe setting(s) for APP_ENV=production.
  - default_secret_key: SECRET_KEY is still the value shipped with the repository…
    fix: Set SECRET_KEY to a long random value, e.g. python -c "import secrets; …"
  - wildcard_cors: CORS_ORIGINS is '*', so any website can call this API…
    fix: Set CORS_ORIGINS to your storefront and dashboard origins.
  …
```

| Blocks the boot (error) | Logged, does not block (warning) |
|---|---|
| Shipped or short `SECRET_KEY` | No TLS signal (`FORCE_HTTPS`/`TRUST_PROXY` unset) |
| `CORS_ORIGINS=*` | Rate limiting disabled |
| Empty `ALLOWED_HOSTS` | Remote database without `sslmode` |
| SQLite as the database | `PAYMENT_PROVIDER=none` |
| The example database password | Notifications not actually delivered |
| `SEED_DEMO_DATA=true` | `ADMIN_ALERT_EMAILS` empty |
| Seeded admin password still valid | Demo products present |
| Migrations pending | |

The same findings are readable over HTTP at `GET /api/ready`, so a pipeline can
ask "would this configuration be accepted?" before promoting a build.

### The stack

```bash
cp .env.prod.example .env.prod        # fill it in; it is gitignored
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

| Service | What it does |
|---|---|
| `caddy` | Terminates TLS (automatic Let's Encrypt certificates), serves the storefront and dashboard, proxies `/api` and `/media`. |
| `api` | Gunicorn supervising uvicorn workers. Applies migrations on start, then serves. |
| `notifier` | `python -m app.notify_worker --loop` — the single owner of mail delivery and its retries. |
| `db` | Postgres, publishing **no host port**; only the app network reaches it. |

Compose refuses to start if `SECRET_KEY`, `POSTGRES_PASSWORD`, `CORS_ORIGINS`,
`ALLOWED_HOSTS` or `DOMAIN` are missing — the `${VAR:?message}` form, chosen so
a forgotten secret is a startup error rather than a silent default.

### Deployment settings

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` \| `staging` \| `production`. Production is strict, and hides `/docs`, `/redoc` and `/openapi.json`. |
| `ALLOWED_HOSTS` | Host headers this API answers to. Anything else gets 400. |
| `TRUST_PROXY` | Set when a proxy terminates TLS, so `X-Forwarded-For` is believed. **Only** then — otherwise anyone rotates that header to defeat rate limiting. |
| `FORCE_HTTPS` | Redirect plain HTTP. Use when nothing else terminates TLS. |
| `LOG_FORMAT` | `json` (default in production) or `text`. |
| `RATE_LIMIT_ENABLED` | Defaults to on in production, off elsewhere. |
| `RATE_LIMIT_PUBLIC` / `_WRITE` / `_LOGIN` | `requests/seconds`, e.g. `12/60`. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE` | Connection pool. Recycling matters when a proxy or Postgres closes idle connections. |
| `WEB_CONCURRENCY` | Gunicorn worker count in the container. |

### Probes

| Endpoint | Use it for | Touches the database |
|---|---|---|
| `/api/health` | Liveness — restart the container if this fails | No, deliberately |
| `/api/ready` | Readiness — send traffic here? 503 if the DB is down or migrations are pending | Yes |
| `/api/version` | What is actually deployed (version, commit, environment, providers) | No |

Liveness must not check the database: a probe that does turns a brief database
blip into every API container restarting at once.

### What rate limiting here is and is not

The limiter is in-process. **N API containers therefore allow N times the
configured rate, and a restart forgets every window.** It is a real speed bump
against scripted checkout abuse and credential stuffing, and a poor defence
against a distributed attack. Put the real limiting at the edge (Caddy with the
rate-limit plugin, or a CDN/WAF) and keep this as the second line. Payment
webhooks are exempt by design — a gateway retrying a delivery is not abuse, and
dropping those loses money.

### Backups

```bash
./scripts/backup.sh                    # compressed dump to ./backups, pruned after 14 days
15 3 * * * cd /srv/vera && ./scripts/backup.sh   # from the host's crontab
```

Dumps are written to the host, never into the Postgres volume — a backup that
lives inside the thing it is backing up is not a backup. A dump is only moved
into place after `pg_dump` succeeds *and* `gzip -t` proves the archive is
complete, so a truncated file can never overwrite a good one.

**A backup nobody has restored is a hypothesis.** Rehearse it — the default
restores into a scratch database and prints row counts, and overwriting the
live database needs an explicit flag plus typing the database name:

```bash
./scripts/restore.sh backups/vera-20260826T031500Z.sql.gz --into vera_restore_check
```

### Upgrades

The API container runs `alembic upgrade head` on start. With **more than one**
API container, set `RUN_MIGRATIONS=false` and run migrations as a one-off
release task instead, so several containers do not race on the same DDL:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api alembic upgrade head
```

Roll back with `alembic downgrade <revision>`; every migration in this project
has a working `downgrade()`, including the enum changes Postgres cannot undo
directly.

---

## What's real vs. what's still a stub

**Fully wired to the database:** products, orders, customers, inventory,
appointments, coupons, JWT-based admin auth, analytics aggregation, and
product placeholders (in their own isolated schema).

Also fully wired: payments (see **Payments**), transactional email (see
**Notifications**), and reviews, loyalty and marketing consent (see **Reviews,
loyalty & marketing**) — all queued, verified and audited server-side.

**Removed rather than kept:** product ratings that were seeded rather than
earned, hardcoded five-star testimonials with invented names, a newsletter form
that confirmed nothing, and loyalty points that could never be spent. Where the
real thing does not exist yet, the interface now says so.

**Still placeholders you'd build out for production:**
- Razorpay against live credentials. The code path, signature verification and
  webhook handling are written and unit-tested, but have not been exercised
  against the real API — that needs your keys.
- The storefront checkout does not yet render a gateway step; it posts the
  order and finishes. The backend flow is complete.
- WhatsApp / SMS notifications. The channel abstraction has a place for them
  (`NotificationChannel.sms`); no provider is implemented.
- The AI Wig Finder quiz, Virtual Try-On, and AI chat assistant on the
  frontend are still rule-based/local demos, not connected to a real model —
  wiring the chat assistant to the Claude API is a natural next step
- File uploads for product images (currently just image URLs)
- Traffic/conversion analytics (the numbers shown are illustrative — real
  tracking needs an analytics pipeline, e.g. GA4 or PostHog). These are the
  last invented numbers left in the dashboard, and they are labelled as such.
- Razorpay has never been run against the live sandbox (see **Payments**).
  Everything up to the network call is tested; the call itself is not.
- Abandoned-cart recovery, welcome series and birthday offers. The campaign
  machinery exists; these particular flows do not, and the dashboard lists them
  as **Not built** rather than as drafts.

---

## Security notes before going live

Most of this list is now **enforced** rather than advised: with
`APP_ENV=production` the startup preflight refuses to boot until the first four
are done (see **Deploying to production**). Run `GET /api/ready` against a
staging deployment to see exactly what it would object to.

- Change `SECRET_KEY` in `.env` to a long random value *(enforced)*
- Change the seeded admin password immediately *(enforced — checked against the
  stored hash, so rotating it clears the finding)*
- Restrict `CORS_ORIGINS` to your real domain instead of `*` *(enforced)*
- Set `ALLOWED_HOSTS` to your real hostnames *(enforced)*
- Put the API behind HTTPS — `docker-compose.prod.yml` ships a Caddy service
  that obtains and renews certificates automatically
- Rate limiting is on by default in production, but it is per-process; put the
  real limiting at the edge (see **Deploying to production**)
- Never commit real `SMTP_PASSWORD` values. With Gmail this must be an App
  Password, not the account password — and `.env` stays out of git.
- Set `MAIL_FROM` and `STOREFRONT_URL` to your real domain, and publish SPF,
  DKIM and DMARC records for it. Without them transactional mail lands in spam
  no matter how correct the code is.
- Switch `NOTIFY_DISPATCH` to `worker` and run `python -m app.notify_worker`
  before scaling the API past one container.
