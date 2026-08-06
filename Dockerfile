# ─────────────────────────────────────────────────────────────────────────────
# Ghost — Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1 (python-base)  — Python deps for api.py and bot.py
#   Stage 2 (node-base)    — Node.js deps for web/server.js
#   Stage 3 (final)        — Combined runtime image
#
# Each service (api, bot, web) is started by its own entry-point script.
# Use docker-compose.yml to run all three as separate containers.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1 — Python dependencies ─────────────────────────────────────────────
FROM python:3.12-slim AS python-base

WORKDIR /app

# Install Python deps into a virtual env so they can be copied cleanly
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2 — Node.js dependencies ────────────────────────────────────────────
FROM node:20-slim AS node-base

WORKDIR /app/web

COPY web/package*.json ./
RUN npm ci --omit=dev


# ── Stage 3 — Final runtime image ─────────────────────────────────────────────
FROM python:3.12-slim AS final

# Install Node.js runtime (LTS) into the Python-based final image
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy Python venv from stage 1
COPY --from=python-base /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash ghost
WORKDIR /app

# Copy Python sources
COPY --chown=ghost:ghost *.py ./
COPY --chown=ghost:ghost requirements.txt ./

# Copy Node sources + pre-installed node_modules
COPY --chown=ghost:ghost web/ ./web/
COPY --from=node-base --chown=ghost:ghost /app/web/node_modules ./web/node_modules

# Copy deploy scripts
COPY --chown=ghost:ghost deploy/ ./deploy/

# Data files (json stores) — created at runtime if absent
# Mount a named volume at /app/data in production for persistence.
RUN mkdir -p /app/data \
 && chown ghost:ghost /app/data

USER ghost

# Expose service ports (override via docker-compose)
# 3000 = web server, 5056 = Python API
EXPOSE 3000 5056

# Default: start the Python API.
# Override in docker-compose.yml for the bot and web services.
CMD ["/app/deploy/start-api.sh"]
