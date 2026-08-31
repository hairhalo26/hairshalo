# Deploying Hairshalo to AWS

A start-to-finish runbook for putting this project on a fresh AWS account with
a domain registered at GoDaddy. It assumes nothing exists yet.

**Target of this guide**

| | |
|---|---|
| Region | `ap-south-1` (Mumbai) — closest to an INR/Razorpay business |
| Compute | One `t3.medium` EC2 instance (2 vCPU, 4 GB), Ubuntu 24.04 LTS |
| Storage | 30 GB gp3 root volume |
| TLS | Caddy, automatic Let's Encrypt certificates |
| Email | Amazon SES (SMTP) |
| Backups | Nightly `pg_dump`, copied to S3 |
| Payments | `manual` — orders hold at Pending Payment for you to confirm |

**Rough monthly cost:** ~$32–40. The instance is ~$30, the Elastic IP is free
while attached to a running instance, 30 GB gp3 is ~$2.40, S3 backups are cents,
and SES is $0.10 per 1,000 emails.

> **Why one box and not ECS/RDS/ALB?** At this traffic level the managed
> versions cost 4–5× more and add moving parts without removing any. The stack
> is already containerised, so moving to ECS or RDS later is a change of
> `DATABASE_URL` and a compose file, not a rewrite. What you would gain today is
> a bigger bill and more things that can be misconfigured.

---

## Order of operations

The sequence matters in one place: **DNS must resolve before you start the
stack.** Caddy asks Let's Encrypt for a certificate on first boot, and Let's
Encrypt proves ownership by connecting to your domain over port 80. If DNS is
not pointing at the box yet that fails, and Caddy falls back to an internal
certificate — every visitor gets a browser warning until you fix it and restart.

1. [Create the AWS infrastructure](#1-aws-infrastructure)
2. [Point the domain at it](#2-dns-at-godaddy) ← before step 5
3. [Set up SES](#3-email-with-ses) (start early: approval takes ~24h)
4. [Create the backup bucket](#4-backups-to-s3)
5. [Deploy](#5-deploy-the-stack)
6. [Create your admin login](#6-create-your-admin-login)
7. [Verify and go live](#7-verify)

---

## 0. The account itself

Hairshalo gets its **own AWS account**, separate from any other business you
run there. This is not ceremony: billing is per-account, so you can see what
the shop actually costs; IAM is per-account, so a credential leaked by another
project cannot reach the shop's database; and a quota or a suspension applies
to one business rather than both.

Creating it needs your identity and a payment method, so it is yours to do:

1. Sign out of any existing AWS console session first — the sign-up flow
   otherwise attaches to the account you are already in.
2. Go to **aws.amazon.com → Create an AWS Account**. Use an email address that
   is not already tied to an AWS account (a `+` alias such as
   `you+hairshalo@gmail.com` works and stays deliverable).
3. Complete identity and payment verification. Activation is usually minutes,
   occasionally a few hours.

Then, before creating anything:

- **Enable MFA on the root user** (Account menu → Security credentials). Root
  can close the account and see every invoice; a password alone is not enough.
- **Create an admin IAM user** for day-to-day work and sign in as that. Root is
  for billing and account settings, not deployment.
- **Set a billing alarm** (Billing → Budgets) at something like $60/month. The
  stack should cost $32–40; an alarm is how you find out early when something
  is wrong rather than at the end of the month.

> **A brand-new account can refuse to launch instances for the first hour or
> two** while verification finishes, with an error about instance limits or
> account status. It is not your configuration — wait and re-run.

### The free plan restricts which instances you may launch

Accounts opened under AWS's newer sign-up land on a **Free plan**, which only
permits free-tier-eligible instance types. Launching anything else fails with:

```
An error occurred (InvalidParameterCombination) when calling the RunInstances
operation: The specified instance type is not eligible for Free Tier.
```

That is the account's plan, not a quota — check the quota separately and you
will see it is fine:

```bash
aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --query 'Quota.Value' --output text
```

Ask the account which types it will accept, rather than assuming — the list is
wider than the old free tier's single `t2.micro`, and it differs by account and
by region:

```bash
aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true --query 'InstanceTypes[].InstanceType' --output text
```

In ap-south-1 in August 2026 that returned `t3.micro`, `t3.small`, `t4g.micro`,
`t4g.small`, `c7i-flex.large` and `m7i-flex.large` — so a 4 GB box is reachable
without upgrading the plan at all.

| Type             | RAM  | Fits the stack?                                       |
| ---------------- | ---- | ----------------------------------------------------- |
| `t3.micro`       | 1 GB | Only just. Builds lean on swap; no headroom.           |
| `t3.small`       | 2 GB | Comfortable for a low-traffic storefront. **In use.**  |
| `c7i-flex.large` | 4 GB | What the stack was sized for, same as a `t3.medium`.   |

Eligible is not the same as free. The plan's credits are drawn down by what you
actually run, so a 4 GB `c7i-flex.large` empties them several times faster than
a 1 GB `t3.micro`. Pick the box you need, then watch the billing alarm from §0.

`t4g.micro` and `t4g.small` are Graviton (ARM). They are cheaper for the RAM,
but the AMI above is x86 — moving to one is a rebuild on an ARM AMI with ARM
images, not a resize. Stay on x86 unless you have a reason.

`.env.prod.example` carries a sizing block for 1, 2 and 4 GB. See
[Resize the instance](#resize-the-instance): it keeps the disk, the data and
the Elastic IP, so choosing again later costs a couple of minutes of downtime,
not a migration.

Upgrading to a paid plan (Billing and Cost Management → Account) removes the
restriction entirely, and is what you will want eventually — but it is not
needed to launch this stack.

---

## 1. AWS infrastructure

Set your region to **Asia Pacific (Mumbai) ap-south-1** in the console's
top-right selector before anything else. Resources are regional: a security
group created in the wrong region will not appear when you launch the instance.

Confirm you are in the new account before creating anything — the account ID is
in the top-right menu, and CloudShell prints it too:

```bash
aws sts get-caller-identity
```

### 1.1 Key pair

**EC2 → Key pairs → Create key pair.** Name it `hairshalo`, type **ED25519**,
format **.pem**. The private key downloads once and cannot be re-downloaded —
lose it and you lose SSH access to any instance that uses it.

```bash
chmod 400 ~/Downloads/hairshalo.pem
```

### 1.2 Security group

**EC2 → Security Groups → Create security group.** Name `hairshalo-sg`.

Inbound rules:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Not `0.0.0.0/0`. An SSH port open to the internet collects thousands of login attempts a day. |
| HTTP | 80 | `0.0.0.0/0` | Required — Let's Encrypt validates over port 80, and Caddy redirects to HTTPS. |
| HTTPS | 443 | `0.0.0.0/0` | The actual site. |

Leave outbound as the default (all traffic).

**Do not add a rule for 5432.** Postgres publishes no host port in
`docker-compose.prod.yml`; only the other containers reach it. Opening 5432 to
the internet is the single most common way a small deployment loses its
database.

### 1.3 Launch the instance

**EC2 → Instances → Launch instances.**

- **Name:** `hairshalo-prod`
- **AMI:** Ubuntu Server 24.04 LTS (64-bit x86)
- **Instance type:** `t3.small` — the scripted path defaults to it, and takes
  `INSTANCE_TYPE=c7i-flex.large bash provision.sh` for the 4 GB box
- **Key pair:** `hairshalo`
- **Network:** default VPC, **Auto-assign public IP: Enable**
- **Security group:** select the existing `hairshalo-sg`
- **Storage:** **30 GiB gp3**

> The 8 GiB default is not enough. Docker images, the Postgres volume, and
> retained backups share this disk, and a full disk on a single box takes the
> database down with it.

### 1.4 Elastic IP

A default public IP changes every time the instance stops and starts, which
would silently break your DNS one morning.

**EC2 → Elastic IPs → Allocate Elastic IP address** → then **Actions →
Associate** it with `hairshalo-prod`.

Write the address down — it is what GoDaddy needs. This guide calls it
`YOUR_ELASTIC_IP`.

> Keep it associated. AWS charges for Elastic IPs that are allocated but idle.

### 1.5 Provision the host

```bash
ssh -i ~/Downloads/hairshalo.pem ubuntu@YOUR_ELASTIC_IP
```

Then, on the instance. Git is preinstalled on most Ubuntu cloud images, but
install it explicitly so this does not depend on which AMI build you got:

```bash
sudo apt-get update && sudo apt-get install -y git
```

```bash
sudo git clone -b deploy/aws-production https://github.com/hairhalo26/hairshalo.git /srv/hairshalo
```

> The deployment lives on `deploy/aws-production` until it has proven itself in
> production. A plain `git clone` takes `main`, which does not yet have
> `deploy/aws/` in it at all — the branch is the point. Drop the `-b` once the
> branch is merged.

> If the repository is private, this prompts for credentials that a server
> should not hold. Add a **deploy key** instead: generate one on the instance
> with `ssh-keygen -t ed25519 -C hairshalo-prod`, add the public half under the
> repository's **Settings → Deploy keys** (read-only), and clone the `git@`
> URL. A deploy key grants access to one repository, so a compromised server
> does not expose your whole GitHub account.

```bash
sudo bash /srv/hairshalo/vera-full-project/deploy/aws/bootstrap.sh
```

This installs Docker, the AWS CLI, a 4 GB swapfile, `ufw`, `fail2ban` and
unattended security upgrades. It is idempotent — safe to re-run.

**Log out and back in** afterwards, so your shell picks up the `docker` group:

```bash
exit
ssh -i ~/Downloads/hairshalo.pem ubuntu@YOUR_ELASTIC_IP
docker ps
```

`docker ps` must work without `sudo` before you continue.

---

## 2. DNS at GoDaddy

Sign in at GoDaddy → **My Products** → your domain → **DNS** → **Manage Zones**.

**Delete the parked records first.** A new GoDaddy domain ships with an `A`
record for `@` pointing at a parking page, and often a `CNAME` for `www`. Both
will fight the records below.

Add:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@` | `YOUR_ELASTIC_IP` | 600 |
| A | `www` | `YOUR_ELASTIC_IP` | 600 |

> Use a second **A** record for `www`, not a CNAME. Caddy holds a certificate
> for `www.hairshalo.com` and 308-redirects it to the apex, which needs the name
> to resolve on its own.

Wait for propagation, then confirm from your laptop:

```bash
nslookup hairshalo.com
```

```bash
nslookup www.hairshalo.com
```

Both must return `YOUR_ELASTIC_IP`. Ten minutes is typical at TTL 600. **Do not
start the stack until they do** — see the note at the top about certificates.

---

## 3. Email with SES

Order confirmations and admin alerts go out over SES. **Start this early** —
leaving the sandbox needs a support request that takes about a day.

### 3.1 Verify the domain

**SES → Identities → Create identity → Domain**, enter `hairshalo.com`, and
enable **Easy DKIM** (RSA 2048).

Make sure you are still in **ap-south-1**. SES identities do not cross regions:
a domain verified in `us-east-1` cannot send from a Mumbai SMTP endpoint.

SES gives you **three CNAME records**. Add all three at GoDaddy:

| Type | Name | Value |
|---|---|---|
| CNAME | `xxxx._domainkey` | `xxxx.dkim.amazonses.com` |
| CNAME | `yyyy._domainkey` | `yyyy.dkim.amazonses.com` |
| CNAME | `zzzz._domainkey` | `zzzz.dkim.amazonses.com` |

> GoDaddy appends the domain automatically. Enter the name as
> `xxxx._domainkey`, **not** `xxxx._domainkey.hairshalo.com` — otherwise you
> create `xxxx._domainkey.hairshalo.com.hairshalo.com` and verification never
> completes.

Verification flips to **Verified** within an hour, usually minutes.

### 3.2 SPF and DMARC

Without these, correct mail still lands in spam.

| Type | Name | Value |
|---|---|---|
| TXT | `@` | `v=spf1 include:amazonses.com ~all` |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:studio@hairshalo.com` |

`p=none` monitors without affecting delivery. Once reports look clean for a few
weeks, tighten to `p=quarantine`.

> If you already have an SPF record, **merge** rather than add a second one.
> Two SPF records is a permanent error and fails both. One record:
> `v=spf1 include:amazonses.com include:_spf.google.com ~all`

### 3.3 Leave the sandbox

New SES accounts are sandboxed: **you can only send to addresses you have
verified, capped at 200 messages a day.** A customer placing an order would
never receive their confirmation.

**SES → Account dashboard → Request production access.** Describe the use case
honestly — transactional order confirmations and shipping updates for your own
store, with the volume you expect. Approval is typically under 24 hours.

While you wait, verify your own address (**Identities → Create identity →
Email address**) so you can test end to end.

### 3.4 SMTP credentials

**SES → SMTP settings → Create SMTP credentials.** This opens IAM and produces
an SMTP username and password.

> These are **not** your AWS access key and secret. An IAM secret key pasted
> into `SMTP_PASSWORD` fails to authenticate — SES derives a different
> credential. Download the CSV; the password is shown once.

The SMTP endpoint for Mumbai is `email-smtp.ap-south-1.amazonaws.com` on port
587 with STARTTLS. That is already the default in `.env.prod.example`.

---

## 4. Backups to S3

Nightly dumps land on the instance's own disk. That protects you from a bad
migration or a dropped table, but **not** from losing the instance — the backup
and the database are on the same volume. One bucket fixes that.

### 4.1 Bucket

**S3 → Create bucket**, region `ap-south-1`, name
`hairshalo-backups-<something-unique>`. Keep **Block all public access ON**.
Enable **Versioning** — it makes an accidental overwrite recoverable.

Optionally add a lifecycle rule to expire objects after 90 days, so storage does
not grow forever.

### 4.2 Let the instance write to it, without keys

**IAM → Roles → Create role → AWS service → EC2.** Attach this inline policy
(replace the bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::hairshalo-backups-CHANGEME",
      "arn:aws:s3:::hairshalo-backups-CHANGEME/*"
    ]
  }]
}
```

Name it `hairshalo-backup-role`, then attach it to the instance:
**EC2 → select `hairshalo-prod` → Actions → Security → Modify IAM role.**

> An instance role means no AWS keys are stored on the box at all. A stolen disk
> image yields nothing, and the credentials rotate themselves.
>
> Note this grants `PutObject` but not `DeleteObject`, so a compromised instance
> cannot erase your backup history.

Verify from the instance:

```bash
aws sts get-caller-identity
```

```bash
aws s3 ls s3://hairshalo-backups-CHANGEME
```

---

## 5. Deploy the stack

On the instance:

```bash
cd /srv/hairshalo/vera-full-project && cp .env.prod.example .env.prod
```

Generate the two secrets:

```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
```

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)"
```

Edit `.env.prod` and fill in: `SECRET_KEY`, `POSTGRES_PASSWORD`, the SES
`SMTP_USERNAME` / `SMTP_PASSWORD`, `ADMIN_ALERT_EMAILS`, and
`BACKUP_S3_BUCKET`. Check that `DOMAIN`, `CORS_ORIGINS` and `ALLOWED_HOSTS`
match your real domain.

```bash
chmod 600 .env.prod
```

That file holds every secret you just generated; `600` keeps it readable only
by its owner.

Start it:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

The first build takes 3–5 minutes. Then:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
```

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f caddy
```

Watch Caddy's log for `certificate obtained successfully`. If you instead see
repeated ACME failures, DNS is not resolving yet — fix step 2 and restart.

### Survive a reboot

```bash
sudo cp deploy/aws/hairshalo.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hairshalo
```

### Schedule the backup

```bash
sudo cp deploy/aws/backup.cron /etc/cron.d/hairshalo-backup && sudo chmod 644 /etc/cron.d/hairshalo-backup && sudo touch /var/log/hairshalo-backup.log && sudo chown ubuntu:ubuntu /var/log/hairshalo-backup.log
```

Prove it works now rather than discovering it at 3am:

```bash
./scripts/backup.sh
```

```bash
aws s3 ls s3://hairshalo-backups-CHANGEME
```

---

## 6. Create your admin login

**This step is not optional and nothing else does it for you.** Demo seeding is
disabled in production, and it is the only other code that creates a user — so
a correctly deployed database has an empty `users` table and the dashboard
login rejects every credential, because none exists.

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api python -m app.create_admin --email you@hairshalo.com --name "Your Name"
```

It prompts for the password (minimum 12 characters) and never takes it as an
argument — a password in `argv` is visible to every process on the machine and
lands in your shell history.

To change it later:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api python -m app.create_admin --email you@hairshalo.com --rotate
```

---

## 7. Verify

From your laptop, not the instance:

```bash
curl -sS https://hairshalo.com/api/health
```

```bash
curl -sS https://hairshalo.com/api/ready
```

```bash
curl -sSI https://www.hairshalo.com | head -3
```

That last one should show `308` redirecting to the apex. Also check that plain
HTTP redirects to HTTPS:

```bash
curl -sSI http://hairshalo.com | head -3
```

`/api/ready` is the important one — it reports every preflight finding. It must
return `200`. If it returns `503`, the body names what is wrong.

Then in a browser:

- `https://hairshalo.com` — the storefront, with a valid padlock
- `https://hairshalo.com/admin-dashboard.html` — sign in with the account from step 6
- Place a test order, confirm it appears in the dashboard, and check that the
  confirmation email arrives (while SES is sandboxed, use a verified address)

### Go-live checklist

- [ ] `/api/ready` returns 200
- [ ] Padlock valid on both the apex and `www`
- [ ] Admin login works; the seed password `ChangeMe123!` is nowhere
- [ ] A test order arrives in the dashboard
- [ ] Order confirmation email received, and not in spam
- [ ] SES production access granted (not sandboxed)
- [ ] `./scripts/backup.sh` produced a file in S3
- [ ] A backup has been **restored** into a scratch database (see below)
- [ ] `sudo reboot`, then confirm the site returns on its own
- [ ] `.env.prod` is `chmod 600` and not in git

---

## Operating it

### Deploy an update

Always back up before a schema change:

```bash
cd /srv/hairshalo/vera-full-project && ./scripts/backup.sh
```

```bash
git pull && docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

The API container runs `alembic upgrade head` on start. Downtime is a few
seconds while it restarts.

### Logs

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api
```

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=100 notifier
```

Rotation is configured (10 MB × 5 per service), so logs cannot fill the disk.

### Restore — rehearse this before you need it

A backup nobody has restored is a hypothesis. The default restores into a
scratch database and prints row counts, so it is safe to try on a live box:

```bash
./scripts/restore.sh backups/vera-20260831T031500Z.sql.gz --into vera_restore_check
```

Pulling a backup back from S3:

```bash
aws s3 cp s3://hairshalo-backups-CHANGEME/vera-20260831T031500Z.sql.gz ./backups/
```

### Resize the instance

Resizing keeps the disk, the data and the Elastic IP — only the RAM and the
bill change. It needs a stop and a start (a reboot will not do it), so the shop
is down for a couple of minutes. Production runs `t3.small`; `c7i-flex.large`
is the 4 GB step up.

If the account is on the free plan, the new type has to be free-tier-eligible
too, or this fails with the same `not eligible for Free Tier` error as at
launch — `t3.micro` → `t3.small` → `c7i-flex.large` all stay inside it.

```bash
aws ec2 stop-instances --instance-ids i-xxxxxxxx
aws ec2 wait instance-stopped --instance-ids i-xxxxxxxx
aws ec2 modify-instance-attribute --instance-id i-xxxxxxxx --instance-type Value=c7i-flex.large
aws ec2 start-instances --instance-ids i-xxxxxxxx
```

Then swap the active sizing block in `.env.prod` for the one matching the new
box — `.env.prod.example` has a set for 1, 2 and 4 GB — and bring the stack up
again:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

Compose recreates the containers whose limits changed. Leaving the micro-sized
caps in place after a resize is the quiet failure mode here: the box has 4 GB
and the stack still behaves as though it has one.

### Health of the machine

```bash
df -h
```

Disk is the thing most likely to bite. Also worth a look:

```bash
free -h && docker stats --no-stream
```

---

## Troubleshooting

**Browser warns the certificate is invalid.** DNS was not resolving when Caddy
first started, so it fell back to an internal certificate. Confirm `nslookup`
returns your Elastic IP, then restart Caddy:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod restart caddy
```

**The API container restarts in a loop.** Almost always the preflight refusing
an unsafe setting, which is it working as designed. The log prints exactly which
setting and how to fix it:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs api
```

**`docker compose` says a variable is not set.** A `${VAR:?...}` in the compose
file has no value in `.env.prod`. The message names the variable. This is
deliberate — a missing secret should stop the deploy, not default silently.

**Emails are not arriving.** Check the notifier log first. Then: is SES still
sandboxed (only verified recipients)? Is `MAIL_FROM` on the verified domain? Are
`SMTP_USERNAME`/`SMTP_PASSWORD` the SES SMTP credentials rather than IAM keys?

**Site is down after a reboot.** The systemd unit was not enabled:

```bash
sudo systemctl enable --now hairshalo && systemctl status hairshalo
```

**Out of disk.** Check `df -h`, reclaim old images with `docker system prune -a`,
and look at `backups/` — `RETENTION_DAYS` defaults to 14.

---

## Known limitations at launch

These are properties of the current build, not of the deployment. None blocks
going live, but you should know them before customers arrive.

- **Payments are manual.** `PAYMENT_PROVIDER=manual` holds orders at Pending
  Payment for you to confirm in the dashboard. The storefront checkout does not
  yet render a gateway step, and the Razorpay path has never run against live
  credentials — rehearse it on staging with test keys before switching.
- **Uploaded media lives on a Docker volume.** It survives container rebuilds
  and reboots, but not the loss of the instance, and it is not in the S3 backup.
  Add it to the backup, or move media to S3 (`app/storage.py`), before the
  catalog has real photographs in it.
- **Rate limiting is per-process.** A real speed bump against scripted abuse,
  not a defence against a distributed attack. CloudFront or a WAF in front is
  the next step if you are ever targeted.
- **One instance means downtime during deploys** (a few seconds) and no
  redundancy if the box fails. Backups are what make that recoverable; the
  restore rehearsal is what makes them real.
- **Analytics numbers in the dashboard are illustrative**, not real traffic
  data. Wiring GA4 or PostHog is a separate task.
