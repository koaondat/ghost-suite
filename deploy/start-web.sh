#!/usr/bin/env bash
# deploy/start-web.sh — Start the Ghost Node.js web server
# ----------------------------------------------------------
# Runs the Express server (web/server.js) using the system Node binary.
# In Docker the CMD is overridden here; outside Docker you can run this
# script directly from the project root.
# ----------------------------------------------------------
set -euo pipefail

echo "[start-web] Launching Ghost web server"
exec node web/server.js
