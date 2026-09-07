# QBIT Connect — Deployment & Testing Kit

> Practical runbook for running QBIT Connect in two modes:
> **Part A** — local testing on your own PC (free), and
> **Part B** — webhook testing over the internet via Cloudflare Tunnel (free).
> **Part C** — production VPS deployment (when ready to go live).
>
> Companion docs: `docs/20-deployment-architecture.md` (full architecture),
> `docs/38-whatsapp-webhooks.md`, `docs/40-email-webhooks` docs (`docs/email-webhooks.md`).

---

## What runs in the stack

| Container | Role |
|---|---|
| `qbit-api` | FastAPI app (UI + REST API + webhooks), port 8000 |
| `qbit-worker` | Background worker: scrape jobs, campaign sender, outbox, email tracking |
| `qbit-db` | PostgreSQL 16 (internal only, no published ports) |
| `qbit-redis` | Redis 7 (broker + pub/sub, internal only) |

All data persists in Docker volumes (`qbit-pgdata`, `qbit-redisdata`) and the
`./qbit-data` bind mount — stopping containers never deletes lead data.

---

## Part A — Local testing on your PC (free, ~15 minutes)

### Prerequisites

1. **Docker Desktop** — install from <https://www.docker.com/products/docker-desktop/>
   (Windows: WSL2 backend is set up automatically; needs 4 GB free RAM).
2. **Git** — <https://git-scm.com/downloads>
3. **Python 3.9+** — only for the `.env` generator
   (<https://www.python.org/downloads/>; on Windows tick "Add to PATH").

### Step-by-step

```bash
# 1. Get the code
git clone https://github.com/qbitconnect/Qbit-connect-repo.git
cd Qbit-connect-repo

# 2. Generate .env with random secrets (one command)
python deploy/gen-env.py

# 3. Build and start the whole stack (first run downloads images, be patient)
docker compose up -d --build

# 4. Watch until everything is healthy (qbit-db, qbit-redis, qbit-api, qbit-worker)
docker compose ps

# 5. Create the database schema
docker compose exec qbit-api alembic upgrade head

# 6. Create roles, permissions and your admin login
#    (choose your own email/password — this becomes the UI login)
docker compose exec qbit-api python -m app.cli seed \
    --email admin@example.com --password "YOUR-STRONG-PASSWORD"

# 7. Verify
curl http://localhost:8000/health        # -> healthy
# open http://localhost:8000 in your browser and log in
```

### Everyday commands

```bash
docker compose ps                # status of all services
docker compose logs -f qbit-api  # tail API logs
docker compose logs -f qbit-worker
docker compose stop              # stop (keeps data)
docker compose up -d             # start again
docker compose down              # remove containers (volumes/data are kept)
docker compose down -v           # ⚠️ DELETES database + redis data
```

Optional smoke checks (same ones used during development):

```bash
docker compose exec qbit-api python scripts/phase6_smoke.py   # WhatsApp provider
docker compose exec qbit-api python scripts/phase7_smoke.py   # Email provider
docker compose exec qbit-api python scripts/phase8_smoke.py   # Unified inbox
```

---

## Part B — Testing WhatsApp/Email webhooks from the internet (free)

Meta's WhatsApp Cloud API and inbound email providers must reach your machine
over **public HTTPS**. Use a Cloudflare quick tunnel — no account or domain needed.

```bash
# 1. Install cloudflared
#    Windows: winget install Cloudflare.cloudflared
#    macOS:   brew install cloudflared
#    Linux:   see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

# 2. While `docker compose up` is running, open a second terminal:
cloudflared tunnel --url http://localhost:8000

# 3. Cloudflare prints a temporary URL, e.g.
#    https://random-words-1234.trycloudflare.com
```

Then in your provider console (e.g. Meta App Dashboard → WhatsApp → Webhooks):

- **Callback URL**: `https://random-words-1234.trycloudflare.com/api/v1/webhooks/whatsapp/{account-or-provider-path}` (see `docs/38-whatsapp-webhooks.md` for the exact route and verify-token flow)
- **Verify token**: the value you configured for that sending account in the Connections UI
- Meta sends a `hub.challenge` on save — QBIT answers it automatically.

**Notes**

- A quick-tunnel URL changes every time `cloudflared` restarts — fine for
  testing, not for production (Part C gives you a stable domain).
- `QBIT_BASE_URL` in `.env` should be set to the tunnel URL when testing
  features that build absolute links (email tracking, unsubscribe links):

```bash
# edit .env, then recreate the API container
docker compose up -d qbit-api
```

---

## Part C — Production VPS deployment (when going live)

**Requirement recap:** any Linux VPS with root + Docker works (2 vCPU / 4 GB
minimum, per `docs/20` sizing "Starter"). Shared hosting / cPanel plans without
Docker and PostgreSQL will **not** run this stack.

```bash
# on the VPS (Ubuntu 24.04)
apt update && apt install -y docker.io docker-compose-v2 git
git clone https://github.com/qbitconnect/Qbit-connect-repo.git
cd Qbit-connect-repo

python3 deploy/gen-env.py          # same generator; edit .env if needed
docker compose up -d --build
docker compose exec qbit-api alembic upgrade head
docker compose exec qbit-api python -m app.cli seed \
    --email you@yourcompany.com --password "YOUR-STRONG-PASSWORD"
```

Then put TLS in front (only port 80/443 exposed; PG/Redis stay internal):

- **Caddy** (simplest, automatic Let's Encrypt): a 6-line Caddyfile
  reverse-proxying `yourdomain.com` → `localhost:8000`.
- Or **Nginx + certbot** as described in `docs/20-deployment-architecture.md`.

Point your DNS `A` record at the VPS, set `QBIT_BASE_URL=https://yourdomain.com`,
and register the stable webhook URLs with your providers.

**Free VPS option:** Oracle Cloud "Always Free" (ARM, 24 GB RAM) runs the full
stack; verify Playwright/browser actors on ARM64, or scrape on an x86 worker.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `port is already allocated` | Another app uses 8000 → set `QBIT_API_PORT=8001` in `.env`, then `docker compose up -d qbit-api` |
| `qbit-db` stays `unhealthy` | First boot can take ~30 s; if stuck: `docker compose logs qbit-db` |
| `POSTGRES_PASSWORD ... not set` | You skipped `python deploy/gen-env.py` — run it from repo root |
| Login rejected after seed | Re-run the seed command with the same email to reset, or check caps-lock 🙂 |
| Build fails on Windows | Ensure Docker Desktop is running in Linux (WSL2) mode, not legacy containers mode |
| Scraper actor shows `DEGRADED` | Expected with `QBIT_MAPS_PROVIDER=none`; set a compliant provider to enable it |
| Everything is slow / containers exit | Check free RAM (needs ~4 GB); close other heavy apps |
