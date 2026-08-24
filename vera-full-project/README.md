# Véra Hair Co. — Full-Stack Demo

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
python -m app.seed              # creates tables + demo data
uvicorn app.main:app --reload --port 8000
```
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

Both frontends point at `http://localhost:8000/api` by default. If you deploy
the backend somewhere else, update the `API_BASE` constant near the bottom of
each HTML file's `<script>` section.

---

## API overview

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/login` | — | Admin login, returns JWT |
| GET | `/api/products` | — | List active products |
| POST | `/api/products` | Admin | Create product |
| PUT/DELETE | `/api/products/{id}` | Admin | Update / delete product |
| POST | `/api/orders` | — | Place an order (checkout) |
| GET | `/api/orders` | Admin | List all orders |
| PUT | `/api/orders/{id}/status` | Admin | Update order status |
| GET | `/api/customers` | Admin | List customers with order stats |
| POST | `/api/appointments` | — | Book a fitting/consultation |
| GET | `/api/appointments` | Admin | List appointments |
| GET/POST | `/api/inventory` | Admin | Stock levels, add SKUs |
| PUT | `/api/inventory/{id}/adjust` | Admin | Adjust stock quantity |
| POST | `/api/coupons/validate` | — | Validate a coupon code |
| GET/POST | `/api/coupons` | Admin | Manage coupons |
| GET | `/api/analytics/summary` | Admin | Revenue, orders, customers KPIs |
| GET | `/api/analytics/top-products` | Admin | Best sellers by units sold |
| GET | `/api/analytics/revenue-trend` | Admin | Daily revenue for charting |

Full interactive docs with request/response schemas are auto-generated at
**`/docs`** once the backend is running.

---

## What's real vs. what's still a stub

**Fully wired to the database:** products, orders, customers, inventory,
appointments, coupons, JWT-based admin auth, analytics aggregation.

**Still placeholders you'd build out for production:**
- Payment processing (Razorpay/Stripe) — checkout currently records the order
  but doesn't charge a card
- Real email/WhatsApp notifications (Brevo, WhatsApp Business API)
- The AI Wig Finder quiz, Virtual Try-On, and AI chat assistant on the
  frontend are still rule-based/local demos, not connected to a real model —
  wiring the chat assistant to the Claude API is a natural next step
- File uploads for product images (currently just image URLs)
- Traffic/conversion analytics (the numbers shown are illustrative — real
  tracking needs an analytics pipeline, e.g. GA4 or PostHog)

---

## Security notes before going live

- Change `SECRET_KEY` in `.env` to a long random value
- Change the seeded admin password immediately
- Restrict `CORS_ORIGINS` to your real domain instead of `*`
- Put the API behind HTTPS (e.g. via a reverse proxy like Caddy or Nginx, or
  a managed host)
- Add rate limiting on public endpoints (`/orders`, `/appointments`,
  `/coupons/validate`) to prevent abuse
