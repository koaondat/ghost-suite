/**
 * server.js — Ghost Web Server
 * ============================
 * Express server that:
 *   • Serves the static web/ files (injects PAYPAL_CLIENT_ID into checkout.html)
 *   • Provides PayPal Checkout session API (api/paypal.js)
 *   • Proxies all /api/auth/*, /api/license/*, /api/purchases,
 *     /api/downloads/*, and /api/admin/* requests to the Ghost shared
 *     Python backend (api.py) running at GHOST_API_URL.
 *
 * Start:  node server.js
 * Deps:   npm install express node-fetch dotenv
 *
 * Environment variables: see .env.example
 */

'use strict';

require('dotenv').config();

const express = require('express');
const path    = require('path');
const paypal  = require('./api/paypal');

const app  = express();
const PORT = process.env.PORT || 3000;

// On Vercel the working directory is the project root, not necessarily the
// directory that contains server.js.  Resolve the web/ root relative to this
// file so that sendFile / express.static work in both local dev and serverless.
const WEB_ROOT = __dirname;

// Base URL of the Ghost shared Python API (api.py).
// This MUST be set to the deployed API URL in production.
// Falling back to localhost is only acceptable for local development.
const GHOST_API_URL = (process.env.GHOST_API_URL || '').replace(/\/$/, '');

if (!GHOST_API_URL) {
  console.warn(
    '[ghost/server] WARNING: GHOST_API_URL is not set. ' +
    'Auth, license, and admin proxy routes will not work until this is configured. ' +
    'Set it to the deployed URL of your Ghost Python backend (api.py).',
  );
}

// ── Shared API proxy helper ──────────────────────────────────────────────────
async function _proxyToApi (req, res, pathOverride) {
  if (!GHOST_API_URL) {
    return res.status(503).json({
      ok:    false,
      error: 'API service unavailable: GHOST_API_URL is not configured on this server.',
    });
  }

  const { default: fetch } = await import('node-fetch');
  const targetPath = pathOverride || req.url;
  const targetUrl  = `${GHOST_API_URL}${targetPath}`;

  const headers = { ...req.headers };
  delete headers['host'];

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

    // Forward Set-Cookie (JWT cookie) from Python -> browser
    const setCookie = upstream.headers.raw()['set-cookie'];
    if (setCookie) {
      res.set('Set-Cookie', setCookie);
    }

    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[ghost/proxy] upstream error:', err.message);
    return res.status(502).json({
      ok:    false,
      error: 'API service unavailable. Please check your connection and try again.',
    });
  }
}

// ── JSON body parser ─────────────────────────────────────────────────────────
app.use(express.json());

// ── PayPal configuration endpoint (safe — never returns the secret) ──────────
// Returns only the public client ID and environment so the frontend can load the
// PayPal JS SDK without baking PAYPAL_CLIENT_ID into the static JS file.
app.get('/api/paypal/config', (req, res) => {
  const clientId = process.env.PAYPAL_CLIENT_ID || '';
  const env      = (process.env.PAYPAL_ENVIRONMENT || 'sandbox').toLowerCase();

  if (!clientId) {
    console.error('[ghost/paypal-config] PAYPAL_CLIENT_ID is not set — payment will be unavailable');
    return res.status(503).json({
      configured: false,
      clientId:   null,
      environment: env,
      error: 'Payment is not configured on this server. Please contact support.',
    });
  }

  console.log('[ghost/paypal-config] config served env=%s', env);
  return res.json({
    configured:  true,
    clientId,
    environment: env,
  });
});

// ── Runtime variable audit (presence only — never returns secret values) ──────
// Returns a report of which required env vars are present so operators can
// verify configuration without exposing secrets.
app.get('/api/config/audit', (req, res) => {
  const vars = [
    'PAYPAL_CLIENT_ID',
    'PAYPAL_CLIENT_SECRET',
    'PAYPAL_ENVIRONMENT',
    'GHOST_API_URL',
    'GHOST_DELIVERY_URL',
    'BASE_URL',
  ];

  const report = {};
  let allPresent = true;

  for (const name of vars) {
    const present = Boolean(process.env[name]);
    report[name] = { present };
    if (!present) {
      allPresent = false;
      console.warn('[ghost/config-audit] MISSING env var: %s', name);
    }
  }

  console.log('[ghost/config-audit] audit complete allPresent=%s', allPresent);
  return res.json({ ok: true, allPresent, vars: report });
});

// ── PayPal Checkout API routes (handled by Node) ─────────────────────────────
app.post('/api/paypal/create-order',  paypal.createOrder);
app.post('/api/paypal/capture-order', paypal.captureOrder);

// ── Order lookup (proxy to delivery backend) ─────────────────────────────────
app.get('/api/order/:orderId', async (req, res) => {
  const DELIVERY_BACKEND_URL = (process.env.GHOST_DELIVERY_URL || '').replace(/\/$/, '');
  if (!DELIVERY_BACKEND_URL) {
    return res.status(503).json({ ok: false, error: 'Order lookup unavailable: GHOST_DELIVERY_URL not configured.' });
  }
  try {
    const { default: fetch } = await import('node-fetch');
    const upstream = await fetch(`${DELIVERY_BACKEND_URL}/api/order/${encodeURIComponent(req.params.orderId)}`);
    const data = await upstream.json().catch(() => ({}));
    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[ghost/server] getOrder proxy error:', err.message);
    return res.status(502).json({ ok: false, error: 'Order lookup unavailable.' });
  }
});

// ── Serve checkout.html ───────────────────────────────────────────────────────
// The PayPal client ID is no longer injected here; the frontend fetches it
// dynamically via GET /api/paypal/config so checkout.html can be served as a
// plain static file.  The route is still explicit so it is served before the
// wildcard static middleware.
app.get('/checkout.html', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
    if (err) { console.error('[ghost/server] checkout.html send error:', err.message); res.status(500).send('Internal server error'); }
  });
});

// Also serve /checkout (no .html extension)
app.get('/checkout', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
    if (err) { console.error('[ghost/server] checkout.html send error:', err.message); res.status(500).send('Internal server error'); }
  });
});

// ── Ghost shared API proxy routes ────────────────────────────────────────────
// Auth
app.all('/api/auth/*',     (req, res) => _proxyToApi(req, res));
// Customer license / account
app.all('/api/license/*',  (req, res) => _proxyToApi(req, res));
app.all('/api/purchases',  (req, res) => _proxyToApi(req, res));
app.all('/api/downloads*', (req, res) => _proxyToApi(req, res));
// Admin
app.all('/api/admin/*',    (req, res) => _proxyToApi(req, res));

// ── Serve static frontend ────────────────────────────────────────────────────
app.get('/', (_req, res) =>
  res.sendFile(path.join(WEB_ROOT, 'index.html')),
);

// Clean-path aliases: /login, /register, /dashboard, /pricing, /checkout
app.get('/:page(login|register|dashboard|pricing|checkout)', (req, res) =>
  res.sendFile(path.join(WEB_ROOT, `${req.params.page}.html`), err => {
    if (err) res.status(404).sendFile(path.join(WEB_ROOT, 'index.html'));
  }),
);

app.use(express.static(WEB_ROOT, {
  index: 'index.html',
  setHeaders (res, filePath) {
    if (filePath.endsWith('.css'))   res.set('Content-Type', 'text/css');
    if (filePath.endsWith('.js'))    res.set('Content-Type', 'application/javascript');
    if (filePath.endsWith('.woff2')) res.set('Content-Type', 'font/woff2');
    if (filePath.endsWith('.woff'))  res.set('Content-Type', 'font/woff');
  },
}));

// ── Health & status endpoints ────────────────────────────────────────────────
const _START_TIME = Date.now();

app.get('/health', (_req, res) =>
  res.json({ ok: true, service: 'ghost-web', status: 'healthy' }),
);

app.get('/status', (_req, res) =>
  res.json({
    ok:            true,
    service:       'ghost-web',
    status:        'ready',
    uptime_secs:   Math.floor((Date.now() - _START_TIME) / 1000),
    ghost_api_url: GHOST_API_URL || '(not configured)',
    paypal_env:    process.env.PAYPAL_ENVIRONMENT || 'sandbox',
    node_version:  process.version,
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
  console.log(`[ghost/server] PayPal environment: ${process.env.PAYPAL_ENVIRONMENT || 'sandbox'}`);
  console.log(`[ghost/server] Ghost shared API proxy: ${GHOST_API_URL || '(GHOST_API_URL not set)'}`);
  if (!process.env.PAYPAL_CLIENT_ID) {
    console.warn('[ghost/server] WARNING: PAYPAL_CLIENT_ID is not set — payment routes will fail');
  }
  if (!process.env.PAYPAL_CLIENT_SECRET) {
    console.warn('[ghost/server] WARNING: PAYPAL_CLIENT_SECRET is not set — payment routes will fail');
  }
  if (!process.env.GHOST_API_URL) {
    console.warn('[ghost/server] WARNING: GHOST_API_URL not set — auth/license proxy will return 503');
  }
  if (!process.env.GHOST_DELIVERY_URL) {
    console.warn('[ghost/server] WARNING: GHOST_DELIVERY_URL not set — license delivery will fail');
  }
});
