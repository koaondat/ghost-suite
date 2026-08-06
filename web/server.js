/**
 * server.js — Ghost API Server
 * ============================
 * Express server that:
 *   • Serves the static web/ files
 *   • Provides the Stripe Checkout session API (api/checkout.js)
 *   • Handles the Stripe webhook (api/stripe_webhook.js) with raw-body
 *     middleware — MUST be mounted BEFORE express.json()
 *   • Proxies all /api/auth/*, /api/license/*, /api/purchases,
 *     /api/downloads/*, and /api/admin/* requests to the Ghost shared
 *     Python backend (api.py) running at GHOST_API_URL.
 *
 * Start:  node server.js
 * Deps:   npm install express stripe node-fetch dotenv
 *
 * Environment variables: see .env.example
 */

'use strict';

require('dotenv').config();

const express        = require('express');
const path           = require('path');
const checkout       = require('./api/checkout');
const stripeWebhook  = require('./api/stripe_webhook');

const app  = express();
const PORT = process.env.PORT || 3000;

// Base URL of the Ghost shared Python API (api.py)
const GHOST_API_URL = (process.env.GHOST_API_URL || 'http://localhost:5056').replace(/\/$/, '');

// ── Shared API proxy helper ──────────────────────────────────────────────────
async function _proxyToApi (req, res, pathOverride) {
  const { default: fetch } = await import('node-fetch');
  const targetPath  = pathOverride || req.url;
  const targetUrl   = `${GHOST_API_URL}${targetPath}`;

  const headers = { ...req.headers };
  // Prevent express from forwarding the host header to the Python server
  delete headers['host'];

  // Body-bearing methods: always re-serialise as JSON and set the correct
  // Content-Type so the Python Flask server can parse it with get_json().
  const BODY_METHODS = ['POST', 'PATCH', 'PUT', 'DELETE'];
  const hasBody = BODY_METHODS.includes(req.method) && req.body !== undefined;
  if (hasBody) {
    headers['content-type'] = 'application/json';
  }

  try {
    const upstream = await fetch(targetUrl, {
      method:  req.method,
      headers: headers,
      body:    hasBody ? JSON.stringify(req.body) : undefined,
    });

    const data = await upstream.json().catch(() => ({}));

    // Forward Set-Cookie (JWT cookie) from Python → browser
    const setCookie = upstream.headers.raw()['set-cookie'];
    if (setCookie) {
      res.set('Set-Cookie', setCookie);
    }

    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[ghost/proxy] upstream error:', err.message);
    return res.status(502).json({ ok: false, error: 'API service unavailable.' });
  }
}

// ── Stripe webhook MUST receive the raw body before any JSON parser ──────────
app.post(
  '/api/stripe/webhook',
  express.raw({ type: 'application/json' }),
  stripeWebhook.handler,
);

// ── JSON body parser for all other routes ────────────────────────────────────
app.use(express.json());

// ── Checkout / order API routes (handled by Node) ────────────────────────────
app.post('/api/checkout/create-session',   checkout.createSession);
app.post('/api/checkout/validate-coupon',  checkout.validateCoupon);
app.get( '/api/order/:sessionId',          checkout.getOrder);

// ── Ghost shared API proxy routes ────────────────────────────────────────────
// Auth
app.all('/api/auth/*',      (req, res) => _proxyToApi(req, res));
// Customer license / account
app.all('/api/license/*',   (req, res) => _proxyToApi(req, res));
app.all('/api/purchases',   (req, res) => _proxyToApi(req, res));
app.all('/api/downloads*',  (req, res) => _proxyToApi(req, res));
// Admin
app.all('/api/admin/*',     (req, res) => _proxyToApi(req, res));

// ── Serve static frontend ────────────────────────────────────────────────────
app.use(express.static(path.join(__dirname)));

// ── Health & status endpoints ────────────────────────────────────────────────
const _START_TIME = Date.now();

app.get('/health', (_req, res) =>
  res.json({ ok: true, service: 'ghost-web', status: 'healthy' }),
);

app.get('/status', (_req, res) =>
  res.json({
    ok:           true,
    service:      'ghost-web',
    status:       'ready',
    uptime_secs:  Math.floor((Date.now() - _START_TIME) / 1000),
    ghost_api_url: GHOST_API_URL,
    node_version: process.version,
  }),
);

// ── Global error handler ──────────────────────────────────────────────────────
// eslint-disable-next-line no-unused-vars
app.use((err, _req, res, _next) => {
  console.error('[ghost/server] unhandled error:', err);
  res.status(500).json({ ok: false, error: 'Internal server error' });
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`[ghost/server] Listening on http://localhost:${PORT}`);
  console.log(`[ghost/server] Stripe webhook endpoint: POST /api/stripe/webhook`);
  console.log(`[ghost/server] Ghost shared API proxy: ${GHOST_API_URL}`);
  if (!process.env.STRIPE_SECRET_KEY) {
    console.warn('[ghost/server] WARNING: STRIPE_SECRET_KEY is not set — payment routes will fail');
  }
  if (!process.env.STRIPE_WEBHOOK_SECRET) {
    console.warn('[ghost/server] WARNING: STRIPE_WEBHOOK_SECRET is not set — webhook verification will fail');
  }
  if (!process.env.GHOST_API_URL) {
    console.warn('[ghost/server] WARNING: GHOST_API_URL not set — defaulting to http://localhost:5056');
  }
});
