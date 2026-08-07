# Ghost Deployment Guide

This document covers everything you need to run the Ghost license system in production:
the **Python API** (`api.py`), the **Node.js web server** (`web/server.js`), and the
**Discord bot** (`bot.py`).

---

## Architecture

```
Internet
   │
   ▼
Nginx (TLS termination)
 ├── yourdomain.com       → Node.js web server  :3000
 └── api.yourdomain.com   → Python API (gunicorn) :5056
                                    ▲
                    Discord bot ────┘  (outbound only)
```

All three services share the same `.env` file and communicate over the internal
network (or Docker bridge). No service except Nginx should be exposed to the
public internet directly.

---

## Quick start — Docker Compose (recommended)

### 1. Prerequisites

- Docker ≥ 24 and Docker Compose v2
- A domain pointed at your server with DNS A records for `yourdomain.com` and
  `api.yourdomain.com`

### 2. Clone and configure

```bash
git clone https://github.com/your-org/ghost.git
cd ghost

# Copy both .env.example files and fill in real values
cp .env.example .env
# Edit .env — replace every placeholder value (see "Environment variables" below)
```

### 3. Build and start

```bash
docker compose up --build -d
```

Verify all three containers are healthy:

```bash
docker compose ps
```

Check logs:

```bash
docker compose logs -f          # all services
docker compose logs -f api      # Python API only
docker compose logs -f web      # Node.js server only
docker compose logs -f bot      # Discord bot only
```

### 4. Health checks

| Service | URL | Expected |
|---------|-----|----------|
| Website | `https://yourdomain.com/health` | `{"ok":true,"service":"ghost-web"}` |
| Website (deep) | `https://yourdomain.com/status` | `{"ok":true,"status":"ready"}` |
| API | `https://api.yourdomain.com/health` | `{"ok":true,"service":"ghost-api"}` |
| API (deep) | `https://api.yourdomain.com/status` | `{"ok":true,"status":"ready","keys":…}` |

---

## Manual deployment (without Docker)

Use this approach on a plain Linux VPS or if you prefer to manage processes with
**systemd** or **PM2**.

### 1. Install system dependencies

```bash
# Python 3.12+
sudo apt update && sudo apt install -y python3.12 python3.12-venv

# Node.js 20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs

# Nginx + Certbot
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 2. Set up Python environment

```bash
cd /opt/ghost               # or wherever you deployed the code
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Set up Node.js dependencies

```bash
cd web
npm ci --omit=dev
cd ..
```

### 4. Configure environment variables

```bash
cp .env.example .env
nano .env        # fill in all placeholder values (see below)
```

### 5. Make startup scripts executable

```bash
chmod +x deploy/start-api.sh deploy/start-web.sh deploy/start-bot.sh
```

### 6. Start services

#### Option A — systemd (recommended for production)

Create `/etc/systemd/system/ghost-api.service`:

```ini
[Unit]
Description=Ghost API (gunicorn)
After=network.target

[Service]
Type=simple
User=ghost
WorkingDirectory=/opt/ghost
EnvironmentFile=/opt/ghost/.env
ExecStart=/opt/ghost/.venv/bin/gunicorn api:app \
    --bind 0.0.0.0:5056 --workers 2 --timeout 90 \
    --access-logfile - --error-logfile -
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/ghost-web.service`:

```ini
[Unit]
Description=Ghost Web Server (Node.js)
After=network.target ghost-api.service

[Service]
Type=simple
User=ghost
WorkingDirectory=/opt/ghost
EnvironmentFile=/opt/ghost/.env
ExecStart=/usr/bin/node web/server.js
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/ghost-bot.service`:

```ini
[Unit]
Description=Ghost Discord Bot
After=network.target ghost-api.service

[Service]
Type=simple
User=ghost
WorkingDirectory=/opt/ghost
EnvironmentFile=/opt/ghost/.env
ExecStart=/opt/ghost/.venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ghost-api ghost-web ghost-bot
```

#### Option B — PM2 (Node-centric alternative)

```bash
npm install -g pm2

pm2 start deploy/start-api.sh  --name ghost-api  --interpreter bash
pm2 start deploy/start-web.sh  --name ghost-web  --interpreter bash
pm2 start deploy/start-bot.sh  --name ghost-bot  --interpreter bash

pm2 save
pm2 startup    # follow the printed command to enable auto-start on reboot
```

### 7. Configure Nginx

```bash
sudo cp nginx/ghost.conf /etc/nginx/sites-available/ghost.conf
# Replace every "yourdomain.com" placeholder with your real domain:
sudo nano /etc/nginx/sites-available/ghost.conf

sudo ln -sf /etc/nginx/sites-available/ghost.conf \
            /etc/nginx/sites-enabled/ghost.conf

sudo nginx -t && sudo systemctl reload nginx
```

### 8. Obtain TLS certificates

```bash
sudo certbot --nginx -d yourdomain.com -d api.yourdomain.com
```

Certbot auto-renews. Confirm:

```bash
sudo certbot renew --dry-run
```

---

## Environment variables

All secrets live in `.env` at the project root. Copy `.env.example` to `.env`
and replace every placeholder. **Never commit `.env` to version control.**

| Variable | Required by | Description |
|---|---|---|
| `GHOST_API_PORT` | api | Port gunicorn listens on (default `5056`) |
| `GHOST_API_URL` | web, bot | Full URL of the deployed API — **must not be localhost in production** |
| `GHOST_ADMIN_API_KEY` | api, bot | Long random hex secret; gates all admin endpoints |
| `GHOST_JWT_SECRET` | api | Long random hex secret for signing JWTs |
| `GHOST_JWT_TTL_SECS` | api | Session lifetime in seconds (default `604800` = 7 days) |
| `GHOST_ALLOWED_ORIGINS` | api | CORS allowed origins — set to your domain in production |
| `GHOST_CDN_SECRET` | api | Secret used to sign download tokens |
| `DISCORD_TOKEN` | bot | Bot token from Discord Developer Portal |
| `GUILD_ID` | bot | Discord server ID (leave `0` for global sync) |
| `ADMIN_ROLE_IDS` | bot | Comma-separated role IDs with admin access |
| `ADMIN_USER_IDS` | bot | Comma-separated user IDs with admin access |
| `STRIPE_SECRET_KEY` | web | Stripe live or test secret key |
| `STRIPE_PUBLISHABLE_KEY` | web | Stripe publishable key (safe to expose) |
| `STRIPE_WEBHOOK_SECRET` | web | Stripe webhook signing secret |
| `BASE_URL` | web | Public URL of the web server (used in Stripe redirects) |
| `PORT` | web | Node.js listen port (default `3000`) |
| `LOG_LEVEL` | all | Log verbosity: `DEBUG`, `INFO`, `WARNING` (default `INFO`) |
| `PAYPAL_CLIENT_ID` | web | PayPal app client ID (safe in frontend) |
| `PAYPAL_CLIENT_SECRET` | web | PayPal app client secret — **server-side only, never expose** |
| `PAYPAL_ENVIRONMENT` | web | `sandbox` or `live` |
| `PAYPAL_WEBHOOK_ID` | web | Webhook ID from PayPal dashboard (for signature verification) |
| `GHOST_DELIVERY_URL` | web | URL of the Python `license_delivery.py` server |
| `RESEND_API_KEY` | web | Resend API key for purchase receipt emails |
| `RECEIPT_FROM_EMAIL` | web | From address e.g. `Ghost <receipts@yourdomain.com>` |
| `SUPPORT_EMAIL` | web | Support email shown in receipts and error messages |

### PayPal Webhook URL

Register the following URL as a PayPal webhook in **developer.paypal.com → My Apps & Credentials → your app → Webhooks → Add Webhook**:

```
https://yourdomain.com/api/paypal/webhook
```

Subscribe to the event type: **`PAYMENT.CAPTURE.COMPLETED`**

After saving, copy the **Webhook ID** and set `PAYPAL_WEBHOOK_ID` in your environment.

Generate secrets:

```bash
# Admin API key
python -c "import secrets; print(secrets.token_hex(32))"

# JWT secret
python -c "import secrets; print(secrets.token_hex(48))"

# CDN secret
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Keeping secrets out of source code

- All tokens, keys, and passwords are read from environment variables at runtime.
- `.env` is listed in `.gitignore` — never commit it.
- Only `.env.example` (with placeholder values) is committed.
- The Nginx config contains no secrets; TLS certificates are managed by Certbot
  outside the repository.

---

## Logging

All services write structured logs to stdout/stderr:

- **Docker Compose**: `docker compose logs -f <service>`
- **systemd**: `journalctl -u ghost-api -f`
- **PM2**: `pm2 logs ghost-api`

Log rotation is configured in `docker-compose.yml` (`max-size: 10m`, `max-file: 5`).
For systemd, `journald` handles rotation automatically.

---

## Health monitoring

Poll the health endpoints from your monitoring tool (UptimeRobot, Grafana, etc.):

```
GET https://yourdomain.com/health          → 200 {"ok":true,"status":"healthy"}
GET https://yourdomain.com/status          → 200 {"ok":true,"status":"ready"}
GET https://api.yourdomain.com/health      → 200 {"ok":true,"status":"healthy"}
GET https://api.yourdomain.com/status      → 200/503
```

`/status` on the API returns `503` if the data files are unreadable, so it can
be used as a readiness probe in Docker or Kubernetes.

---

## Automatic restarts

| Method | Mechanism |
|---|---|
| Docker Compose | `restart: unless-stopped` on every service |
| systemd | `Restart=always` + `RestartSec=5` |
| PM2 | Built-in process monitor |
| Discord gateway drops | `discord.py` reconnects automatically (`reconnect=True`) |

---

## Updating

### Docker Compose

```bash
git pull
docker compose up --build -d
```

### systemd / manual

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
cd web && npm ci --omit=dev && cd ..
sudo systemctl restart ghost-api ghost-web ghost-bot
```
