# Production deployment: migration 0017 (order lifecycle, colour media, Google Maps)

Runbook for putting `main` (at or after `2cd23fe`) live on hairshalo.com, including
migration `0017_packed_and_map_pins`. Written 2026-10-01.

**Where each command runs** — the prompt matters, commands pasted into the wrong
one have failed before:

| Label | Prompt | What it is |
|---|---|---|
| **[CloudShell]** | `~ $` | AWS CloudShell, account 430030409894, ap-south-1 |
| **[Server]** | `ubuntu@ip-172-31-44-28:…$` | the instance, reached through SSM, after `sudo -iu ubuntu` |
| **[Browser]** | — | your browser, on https://hairshalo.com |

---

## What is shipping

Production was last recorded on the commits up to PR #13/#14 (2026-09-28). The
live site does not report its commit (`/api/version` → `"commit": null`), so step 2
checks the server itself. Everything merged after the live commit ships together:

| PR | Change | Needs anything at deploy? |
|---|---|---|
| #15 | card hover stays on the same colour | no (frontend) |
| #16 | the nine UI states; **Caddyfile**: real 404 page | **yes — `reload-caddy.sh`** (step 4.5) |
| #17 | admin: publish unpriced product as "Price on request" | no |
| #18, #19 | `replace_model_photos` tool changes | no |
| #20 | order lifecycle + colour media + Google Maps, **migration 0017** | **yes — this runbook** |
| #21 | README note | no |

**Migration 0017 is additive.** It adds the order status `packed` (after
`processing`) and eight nullable columns (`orders.shipping_latitude/longitude/
place_id/formatted_address`, `customer_addresses.latitude/longitude/place_id/
formatted_address`). It rewrites no existing row.

**Behaviour change staff must know:** an order can no longer go Processing →
Shipped. It must go Processing → **Packed** → Shipped. Tracking can be entered from
Packed; customers see it from Shipped.

### What has and has not been verified

| | Status |
|---|---|
| Migrations 0001 → 0017 on PostgreSQL 16 (throwaway test database) | ✅ verified |
| 0017 upgrading a database seeded with the pre-merge code, existing rows unchanged | ✅ verified |
| A pre-existing Processing order following the new lifecycle | ✅ verified |
| Full test suite on PostgreSQL 16 (479/2 manual, 461/20 payments off) | ✅ verified |
| The fingerprint and rollback procedures in this runbook | ✅ tested on PostgreSQL 16 |
| **0017 against the real production data** | ❌ **not yet — step 3 does this** |
| Real Google Maps with a valid key | ❌ not yet — part 7 |

### How the deploy works (why the order of steps matters)

- The **api container runs `alembic upgrade head` every time it starts**
  (`backend/docker-entrypoint.sh`). `up -d --build` therefore migrates production
  the moment the new api container starts. There is no separate "migrate" step to
  forget — and no way to start the new code without migrating.
- `frontend/` is a **directory mount**: the new storefront and admin go live the
  instant `git pull` updates the files, before the api has been rebuilt. That is
  why the rehearsal (step 3) builds from a separate worktree and never touches the
  live checkout.
- Alembic runs the whole upgrade in **one transaction**, so if 0017 fails partway,
  Postgres rolls it back and the database stays at 0016.

---

## 1. Connect

**[CloudShell]**
```bash
aws ssm start-session --target i-02f1493e97593548b --region ap-south-1
```

**[Server]** — always first, or git fails with "dubious ownership":
```bash
sudo -iu ubuntu
cd /srv/hairshalo/vera-full-project
C="docker compose -f docker-compose.prod.yml --env-file .env.prod"
```
`C` is a shell variable: if the SSM session drops, reconnect and run these three
lines again before continuing.

## 2. Pre-flight checks (read-only)

**[Server]**
```bash
git status --short                      # expect no output
git log -1 --oneline                    # the commit production runs now
git rev-parse HEAD > ~/deploy-0017-prev.txt && cat ~/deploy-0017-prev.txt
$C exec -T api alembic current          # expect: 0016_hair_colours (head)
$C ps                                   # api, notifier, db, caddy all Up
df -h /                                 # the build needs a few GB free
git fetch origin
git log --oneline HEAD..origin/main     # what will ship; top should be 2cd23fe or later
git diff --stat HEAD origin/main -- Caddyfile    # any output => do step 4.5
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select status, count(*) from orders group by 1 order by 1"'
```

**Stop here if** `alembic current` is not `0016_hair_colours`, `git status` shows
changes, or any container is not Up. Keep the status counts; step 5 compares them.

## 3. Rehearsal on a copy of production data

Nothing in this step touches the live database, the live checkout or the running
containers. It runs migration 0017 against a scratch copy of today's production
data and proves no existing row changes.

### 3.1 Fresh backup and a scratch copy

**[Server]**
```bash
./scripts/backup.sh                     # must end with "[backup] uploaded ..." for both files
DUMP=$(ls -t backups/vera-2*.sql.gz | head -1); echo "$DUMP"
./scripts/restore.sh "$DUMP" --into vera_0017_check
```
`restore.sh` without `--into-production` only ever writes the scratch database.

### 3.2 Fingerprint the copy before migrating

**[Server]** — save the query once:
```bash
cat > /tmp/fp.sql <<'SQL'
select 'orders', count(*), md5(coalesce(string_agg((to_jsonb(o) - array['shipping_latitude','shipping_longitude','shipping_place_id','shipping_formatted_address'])::text, '|' order by o.id), '')) from orders o
union all select 'customer_addresses', count(*), md5(coalesce(string_agg((to_jsonb(a) - array['latitude','longitude','place_id','formatted_address'])::text, '|' order by a.id), '')) from customer_addresses a
union all select 'order_items', count(*), md5(coalesce(string_agg(to_jsonb(i)::text, '|' order by i.id), '')) from order_items i
union all select 'order_events', count(*), md5(coalesce(string_agg(to_jsonb(e)::text, '|' order by e.id), '')) from order_events e
union all select 'payments', count(*), md5(coalesce(string_agg(to_jsonb(p)::text, '|' order by p.id), '')) from payments p
union all select 'products', count(*), md5(coalesce(string_agg(to_jsonb(p)::text, '|' order by p.id), '')) from products p
union all select 'product_variants', count(*), md5(coalesce(string_agg(to_jsonb(v)::text, '|' order by v.id), '')) from product_variants v
union all select 'product_media', count(*), md5(coalesce(string_agg(to_jsonb(m)::text, '|' order by m.id), '')) from product_media m
union all select 'customers', count(*), md5(coalesce(string_agg(to_jsonb(c)::text, '|' order by c.id), '')) from customers c;
SQL
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d vera_0017_check -At' < /tmp/fp.sql > /tmp/fp-before.txt
cat /tmp/fp-before.txt
```
The order and address hashes leave out the four new columns, so the same query
gives the same answer before and after a migration that changed nothing else.

### 3.3 Build the new code in a separate worktree

**[Server]**
```bash
git worktree add --detach /tmp/hs-0017 origin/main
docker build -t hairshalo-api:0017-rehearsal /tmp/hs-0017/vera-full-project/backend
```
This takes a few minutes on the t3.small. It does not affect the running site, and
it warms Docker's build cache so the real deploy's build (step 4.3) is quick.

### 3.4 Migrate the scratch copy — and only the scratch copy

**[Server]**
```bash
LIVE_URL=$($C exec -T api printenv DATABASE_URL | tr -d '\r')
NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' $($C ps -q db))
docker run --rm --network "$NET" --entrypoint sh \
  -e DATABASE_URL="${LIVE_URL%/*}/vera_0017_check" \
  hairshalo-api:0017-rehearsal \
  -c 'echo "TARGET DATABASE: ${DATABASE_URL##*/}"; alembic current && alembic upgrade head && alembic current'
```
- The first line printed **must** be `TARGET DATABASE: vera_0017_check`. If it
  shows anything else, press Ctrl+C.
- `--entrypoint sh` skips the image's entrypoint, so nothing migrates the live
  database. `${LIVE_URL%/*}` swaps only the database name; the password never
  appears on screen. Don't `echo "$LIVE_URL"`.
- Expect `0016_hair_colours`, then
  `Running upgrade 0016_hair_colours -> 0017_packed_and_map_pins`, then
  `0017_packed_and_map_pins (head)`.

### 3.5 Check the result

**[Server]**
```bash
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d vera_0017_check -At' < /tmp/fp.sql > /tmp/fp-after.txt
diff /tmp/fp-before.txt /tmp/fp-after.txt && echo "IDENTICAL - no existing row changed"
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d vera_0017_check -At' <<'SQL'
select string_agg(enumlabel, ',' order by enumsortorder)
  from pg_enum e join pg_type t on t.oid = e.enumtypid where typname = 'orderstatus';
select table_name || '.' || column_name || ' ' || data_type
  from information_schema.columns
 where table_name in ('orders', 'customer_addresses')
   and column_name in ('shipping_latitude', 'shipping_longitude', 'shipping_place_id',
                       'shipping_formatted_address', 'latitude', 'longitude', 'place_id',
                       'formatted_address')
 order by 1;
SQL
```
Expect:
- `IDENTICAL - no existing row changed`
- `processing,packed,shipped,...` (with `packed` right after `processing`)
- 8 columns: 4 on `orders`, 4 on `customer_addresses`, `numeric` and `character varying`

**Optional — rehearse the schema rollback too:**
```bash
docker run --rm --network "$NET" --entrypoint sh -e DATABASE_URL="${LIVE_URL%/*}/vera_0017_check" \
  hairshalo-api:0017-rehearsal -c 'echo "TARGET DATABASE: ${DATABASE_URL##*/}"; alembic downgrade 0016_hair_colours && alembic upgrade head && alembic current'
```

**Stop and do not deploy if** the diff prints anything, the upgrade errors, or the
target line was not `vera_0017_check`. Keep the output for whoever looks at it.

### 3.6 Clean up the rehearsal

**[Server]**
```bash
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE vera_0017_check"'
git worktree remove --force /tmp/hs-0017
unset LIVE_URL
```
Keep the `hairshalo-api:0017-rehearsal` image until after the deploy (its layers
are the build cache). Remove it in step 6.

---

## 4. Deploy

Pick a quiet time. The api is down for a few seconds while its container is
replaced; the build before that does not interrupt the site.

### 4.1 Backup right before

If more than about an hour has passed since step 3.1, or orders came in since:

**[Server]**
```bash
./scripts/backup.sh
ls -t backups/vera-2*.sql.gz | head -1 | tee ~/deploy-0017-backup.txt
```
This is the backup to restore from if everything else fails.

### 4.2 Google Maps key — leave it for later

Recommended: deploy without `GOOGLE_MAPS_API_KEY`. The picker stays hidden and
checkout is unchanged. Add the key separately in part 7, so a Maps problem can
never be mixed up with a migration problem.

### 4.3 Pull and rebuild

**[Server]**
```bash
git pull --ff-only origin main
git log -1 --oneline                     # 2cd23fe or later
$C up -d --build
```
The new storefront and admin go live when the pull finishes, a minute or so before
the new api does. That brief mix is safe: the Maps picker stays hidden, and the
admin would reject a move to Packed until the new api is up.

### 4.4 Watch the migration

**[Server]**
```bash
$C logs --tail=80 api
```
Expect, in order:
- `[entrypoint] applying migrations`
- `Running upgrade 0016_hair_colours -> 0017_packed_and_map_pins`
- gunicorn starting, with no traceback

If the api keeps restarting or shows an alembic error, go to part 8, case A.

### 4.5 Caddy (only if step 2 showed a Caddyfile difference)

**[Server]**
```bash
./scripts/reload-caddy.sh
```
It validates the new Caddyfile in a throwaway container before replacing the
running one. If it says `config is INVALID`, nothing was changed and the site is
still up; keep the output and stop there.

---

## 5. Verify

**[Server]**
```bash
$C exec -T api alembic current           # 0017_packed_and_map_pins (head)
$C ps                                    # all Up; api (healthy)
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select status, count(*) from orders group by 1 order by 1"'
```
The status counts should match step 2, apart from orders placed or moved since.

**[CloudShell]** (or any terminal)
```bash
curl -s -o /dev/null -w "health %{http_code}\n" https://hairshalo.com/api/health        # 200
curl -s https://hairshalo.com/api/orders/maps-config; echo                              # {"api_key":null,"map_id":null}
curl -s -o /dev/null -w "unknown page %{http_code}\n" https://hairshalo.com/no-such-page  # 404 if step 4.5 ran
```
Before the deploy, `maps-config` answers 401; a `200` with `null` shows the new
code is live.

**[Browser]**
- [ ] The storefront loads with no console errors.
- [ ] Open a product with several colours. Choosing a colour switches the photos;
      choosing a length keeps the colour.
- [ ] Admin → Orders shows a **Packed** tab.
- [ ] Open an order in Processing: "Move to" offers only **Packed, Cancelled,
      Refunded**, and the tracking fields say tracking can be added once the order is
      Packed. Don't save anything unless you mean it.
- [ ] Optional end-to-end check:
      - Place a test order to your own address.
      - Move it Processing → Packed, adding test tracking.
      - Confirm your customer view shows no tracking while it's Packed.
      - Move it to Shipped and confirm tracking now shows, with the shipped email.
      - Then cancel it (Shipped → Cancelled returns the stock).
      - Note: this sends real emails.

## 6. After the deploy

- [ ] Tell staff about the change:
  - Processing → **Packed** → Shipped is now required.
  - Enter tracking at Packed or Shipped.
  - Customers see tracking from Shipped, and the shipped email carries it.
- [ ] Orders already in Processing at deploy time now need moving to Packed before
      they can be shipped.
- [ ] **[Server]** `docker image rm hairshalo-api:0017-rehearsal`
- [ ] Record the deploy: the commit (`git log -1 --oneline`), the time, and the backup
      file from `~/deploy-0017-backup.txt`.

---

## 7. Google Maps (separate step, when ready)

1. **[Browser]** In Google Cloud Console, in the same project as Google Login
   ("Hairshalo"):
   - Make sure billing is enabled.
   - Enable **Maps JavaScript API** and **Places API (New)**.
   - Credentials → Create credentials → API key.
   - Application restrictions: **Websites**, `https://hairshalo.com/*` and
     `https://www.hairshalo.com/*`.
   - API restrictions: only those two APIs.
   - Set a budget alert in Billing.
2. **[Server]** Add the key to `.env.prod` with your editor. Don't paste it into
   chat, a ticket or git:
   ```
   GOOGLE_MAPS_API_KEY=<the key>
   GOOGLE_MAPS_MAP_ID=
   ```
3. **[Server]** Recreate the api so it reads the new value. The migration is
   already applied, so this runs no migration:
   ```bash
   $C up -d api notifier
   ```
4. **[CloudShell]** `curl -s https://hairshalo.com/api/orders/maps-config` now
   returns the key. It is meant to be public; the referrer restriction is what
   protects it.
5. **[Browser]** The real-key test, which has never been done yet:
   - [ ] **Checkout:** the address search appears. Choosing a result fills the
         address fields and drops a pin. Dragging the pin and tapping the map both
         move it.
   - [ ] Place a test order: Admin shows "Open in Google Maps" and it opens at the
         pin. Cancel the test order afterwards.
   - [ ] **My Account → Addresses:** the same picker. The pin is saved with the
         address, and editing the address keeps the pin.
   - [ ] If the picker disappears straight after loading, Google refused the key.
         Check that the referrer restriction includes `https://hairshalo.com/*`,
         that both APIs are enabled, and that billing is active. Checkout keeps
         working either way.

To switch Maps off again, empty `GOOGLE_MAPS_API_KEY` and repeat step 3.

---

## 8. Rollback

Decide which case you are in before typing anything.

### A. The migration failed (api restarting, alembic error in the logs)

Alembic ran 0017 in one transaction, so Postgres rolled it back.

**[Server]**
```bash
$C logs --tail=200 api > ~/deploy-0017-failure.log     # keep the evidence
git checkout --detach "$(cat ~/deploy-0017-prev.txt)"
$C up -d --build
./scripts/reload-caddy.sh                               # only if step 4.5 ran
$C exec -T api alembic current                          # expect 0016_hair_colours
```
The server checkout is now detached at the old commit. `git checkout main`
returns to `main` for the next attempt.

### B. The migration succeeded, but you need the old code back

The old code works with the 0017 schema — it ignores the new columns — **except**
that it cannot read an order whose status is Packed. This was tested: it fails
with `LookupError: 'packed' is not among the defined enum values` until those
orders are moved back.

**[Server]**
```bash
# 1. Any Packed orders? Note them; staff will re-pack them after a fix.
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
select order_number, tracking_number from orders where status = 'packed';
SQL
# 2. Move them back to Processing. Their tracking details stay stored.
$C exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
update orders set status = 'processing' where status = 'packed';
SQL
# 3. Old code back. No schema downgrade is needed.
git checkout --detach "$(cat ~/deploy-0017-prev.txt)"
$C up -d --build
./scripts/reload-caddy.sh                               # only if step 4.5 ran
```

Only if the new columns themselves must go: run the downgrade **with the new code
still running** — the old code has no 0017 file to downgrade with — and only
*before* step 3 above:
```bash
$C exec -T api alembic downgrade 0016_hair_colours
```
This deletes any map pins saved since the deploy. The `packed` status value stays
defined (Postgres cannot remove an enum value) but unused, which does no harm.

### C. Data is wrong and nothing else fixes it (last resort)

Restore the backup from step 4.1. **Every order and payment written since that
backup is lost**, so compare against the admin panel first.

**[Server]**
```bash
./scripts/restore.sh "$(cat ~/deploy-0017-backup.txt)" --into-production
```
It asks you to type the database name before overwriting anything.
