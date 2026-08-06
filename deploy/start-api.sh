#!/usr/bin/env bash
# deploy/start-api.sh — Start the Ghost shared Python API with gunicorn
# -----------------------------------------------------------------------
# gunicorn options:
#   --workers     2–4 × CPU cores; 2 is safe for a small VPS
#   --bind        listen on all interfaces (nginx / Docker handles TLS)
#   --access-logfile / --error-logfile  — write to stdout/stderr for Docker logs
#   --timeout     90 s per request (generous for heavy admin calls)
#   --graceful-timeout  30 s (drain before killing workers on reload)
# -----------------------------------------------------------------------
set -euo pipefail

PORT="${GHOST_API_PORT:-5056}"
WORKERS="${GUNICORN_WORKERS:-2}"

echo "[start-api] Launching gunicorn on 0.0.0.0:${PORT} with ${WORKERS} workers"

exec gunicorn api:app \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WORKERS}" \
  --timeout 90 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile - \
  --log-level "${LOG_LEVEL:-info}"
