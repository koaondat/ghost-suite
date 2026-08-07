/**
 * server.js — Ghost Web Server
 * ============================
 * Express server that:
 *   • Serves the static web/ files
 *   • Provides PayPal Checkout session API (api/paypal.js)
 *   • Handles admin panel auth with cookie-based JWT sessions
 *   • Proxies /api/auth/*, /api/license/*, /api/purchases,
 *     /api/downloads/*, and explicitly named /api/admin/* routes
 *     to the Ghost shared Python backend (api.py) running at GHOST_API_URL.
 *
 * Start:  node server.js
 * Deps:   npm install express node-fetch dotenv cookie-parser
 *
 * Environment variables: see .env.example
 *
 * ── Session model (Vercel-safe) ───────────────────────────────────────────────
 * Vercel serverless functions are stateless: each invocation is a fresh process.
 * An in-memory Map cannot survive across requests.  We use signed JWTs stored in
 * a server-side HttpOnly cookie instead — no shared state required.
 *
 * ── 508 / self-loop prevention ────────────────────────────────────────────────
 * If GHOST_API_URL points back at the same Vercel deployment, proxying any
 * /api/admin/* request would loop back through the catch-all and recurse until
 * Vercel reports 508 Loop Detected.  All admin routes are handled natively by
 * this file and NEVER forwarded to GHOST_API_URL.  Additionally, _proxyToApi()
 * detects same-host targets and refuses to forward.
 */

'use strict';

require('dotenv').config();

const express      = require('express');
const path         = require('path');
const crypto       = require('crypto');
const cookieParser = require('cookie-parser');
const paypal       = require('./api/paypal');

const app  = express();
const PORT = process.env.PORT || 3000;

// ── Secrets ──────────────────────────────────────────────────────────────────
// Admin panel password hash (SHA-256 hex of the admin password).
// NEVER store the plain-text password — only the digest.
const ADMIN_PANEL_PASSWORD_HASH = (process.env.ADMIN_PANEL_PASSWORD_HASH || '').trim().toLowerCase();

// JWT secret for signing admin panel session cookies.
// Falls back to a derived value so a missing env var doesn't hard-crash, but
// sessions will not survive a server restart / re-deployment with a different
// ADMIN_JWT_SECRET.  Set this explicitly in production.
const ADMIN_JWT_SECRET = (
  process.env.ADMIN_JWT_SECRET ||
  // Derive a fallback from the password hash so it is at least deterministic
  // when ADMIN_PANEL_PASSWORD_HASH is set (no cross-invocation state needed).
  (ADMIN_PANEL_PASSWORD_HASH
    ? crypto.createHash('sha256').update('ghost-panel-jwt-' + ADMIN_PANEL_PASSWORD_HASH).digest('hex')
    : crypto.randomBytes(32).toString('hex'))   // truly random — sessions die on restart
);

// Server-side admin API key for bot / CI integrations.
// Accepted via: Authorization: Bearer <GHOST_ADMIN_API_KEY>
// NEVER returned through any public endpoint, never logged.
const GHOST_ADMIN_API_KEY = (process.env.GHOST_ADMIN_API_KEY || '').trim();

const ADMIN_SESSION_TTL_SECS = 4 * 60 * 60; // 4 hours
const ADMIN_COOKIE_NAME      = 'ghost_admin_session';

// ── JWT helpers ───────────────────────────────────────────────────────────────
function _issueAdminJwt () {
  const header  = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    sub: 'admin',
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + ADMIN_SESSION_TTL_SECS,
  })).toString('base64url');
  const sig = crypto
    .createHmac('sha256', ADMIN_JWT_SECRET)
    .update(`${header}.${payload}`)
    .digest('base64url');
  return `${header}.${payload}.${sig}`;
}

function _verifyAdminJwt (token) {
  if (!token || typeof token !== 'string') return false;
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  const [header, payload, sig] = parts;
  const expected = crypto
    .createHmac('sha256', ADMIN_JWT_SECRET)
    .update(`${header}.${payload}`)
    .digest('base64url');
  try {
    if (!crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) return false;
  } catch (_) {
    return false;  // length mismatch
  }
  try {
    const claims = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    if (claims.exp && Math.floor(Date.now() / 1000) > claims.exp) {
      console.log('[ghost/admin] session expired sub=%s', claims.sub);
      return false;
    }
    return true;
  } catch (_) {
    return false;
  }
}

// ── Session middleware ────────────────────────────────────────────────────────
function _requireAdminSession (req, res, next) {
  // Path 1: cookie-based session (human admin panel)
  const cookieToken = req.cookies && req.cookies[ADMIN_COOKIE_NAME];
  if (cookieToken) {
    if (_verifyAdminJwt(cookieToken)) return next();
    console.log('[ghost/admin] invalid/expired session cookie ip=%s path=%s', req.ip, req.path);
    return res.status(401).json({ ok: false, error: 'Admin session expired. Please log in again.' });
  }

  // Path 2: server-side API key via Authorization: Bearer (bot / CI)
  const authHeader = (req.headers['authorization'] || '').trim();
  if (authHeader.startsWith('Bearer ')) {
    const providedKey = authHeader.slice(7).trim();
    if (!GHOST_ADMIN_API_KEY) {
      console.warn('[ghost/admin] Bearer token presented but GHOST_ADMIN_API_KEY is not configured');
      return res.status(401).json({ ok: false, error: 'Admin API key not configured on this server.' });
    }
    let match = false;
    try {
      match = crypto.timingSafeEqual(
        Buffer.from(providedKey),
        Buffer.from(GHOST_ADMIN_API_KEY),
      );
    } catch (_) {
      // length mismatch → not equal
    }
    if (!match) {
      console.warn('[ghost/admin] invalid Bearer API key ip=%s path=%s', req.ip, req.path);
      return res.status(401).json({ ok: false, error: 'Invalid admin API key.' });
    }
    return next();
  }

  // Fallback: no credentials at all
  console.log('[ghost/admin] missing session ip=%s path=%s', req.ip, req.path);
  return res.status(401).json({ ok: false, error: 'Admin session required. Please log in.' });
}

// ── Web-root path ─────────────────────────────────────────────────────────────
const WEB_ROOT = __dirname;

// ── Ghost Python API base URL ─────────────────────────────────────────────────
const GHOST_API_URL = (process.env.GHOST_API_URL || '').replace(/\/$/, '');

// Detect own base URL so we can refuse self-referencing proxy requests.
const BASE_URL = (process.env.BASE_URL || '').replace(/\/$/, '').toLowerCase();

if (!GHOST_API_URL) {
  console.warn(
    '[ghost/server] WARNING: GHOST_API_URL is not set. ' +
    'Auth, license, and proxy routes will not work until this is configured. ' +
    'Set it to the deployed URL of your Ghost Python backend (api.py).',
  );
}

// ── Proxy helper — public API routes only (NOT admin routes) ─────────────────
async function _proxyToApi (req, res, pathOverride) {
  if (!GHOST_API_URL) {
    return res.status(503).json({
      ok:    false,
      error: 'API service unavailable: GHOST_API_URL is not configured on this server.',
    });
  }

  const targetPath = pathOverride || req.url;
  const targetUrl  = `${GHOST_API_URL}${targetPath}`;

  // ── Self-loop guard ────────────────────────────────────────────────────────
  // If GHOST_API_URL points at the same Vercel deployment, proxying /api/admin/*
  // would recursively call this server and Vercel would report 508 Loop Detected.
  // Admin routes must NEVER be forwarded through _proxyToApi.
  const targetLower = targetUrl.toLowerCase();
  const ownHosts    = ['localhost', '127.0.0.1', '::1'];
  if (BASE_URL && targetLower.startsWith(BASE_URL)) {
    console.error('[ghost/proxy] SELF-LOOP DETECTED: GHOST_API_URL points back at this server. target=%s', targetUrl);
    return res.status(508).json({
      ok:    false,
      error: 'Configuration error: GHOST_API_URL must not point to this server.',
    });
  }
  for (const h of ownHosts) {
    if (targetLower.includes(`//${h}`)) {
      console.error('[ghost/proxy] SELF-LOOP (localhost) DETECTED: target=%s', targetUrl);
      return res.status(508).json({
        ok:    false,
        error: 'Configuration error: GHOST_API_URL must not point to localhost in production.',
      });
    }
  }

  const { default: fetch } = await import('node-fetch');

  const headers = { ...req.headers };
  delete headers['host'];
  delete headers['cookie'];  // never forward the admin session cookie to the backend

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
    console.error('[ghost/proxy] upstream error path=%s: %s', targetPath, err.message);
    return res.status(502).json({
      ok:    false,
      error: 'API service unavailable. Please check your connection and try again.',
    });
  }
}

// ── Body parsers ──────────────────────────────────────────────────────────────
// Raw body capture for PayPal webhook signature verification.
app.use((req, _res, next) => {
  if (req.path === '/api/paypal/webhook') {
    let raw = '';
    req.setEncoding('utf8');
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      req.rawBody = raw;
      try { req.body = JSON.parse(raw); } catch (_) { req.body = {}; }
      next();
    });
  } else {
    next();
  }
});

app.use(express.json());
app.use(cookieParser());

// ── PayPal config endpoint ────────────────────────────────────────────────────
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
  return res.json({ configured: true, clientId, environment: env });
});

// ── Runtime variable audit (presence only — never returns secret values) ──────
app.get('/api/config/audit', (req, res) => {
  const vars = [
    'PAYPAL_CLIENT_ID',
    'PAYPAL_CLIENT_SECRET',
    'PAYPAL_ENVIRONMENT',
    'PAYPAL_WEBHOOK_ID',
    'GHOST_API_URL',
    'GHOST_DELIVERY_URL',
    'BASE_URL',
    'ADMIN_PANEL_PASSWORD_HASH',
    'ADMIN_JWT_SECRET',
    'GHOST_ADMIN_API_KEY',
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

// ── Admin panel auth ──────────────────────────────────────────────────────────
// POST /api/admin/panel/auth  { password }
// Issues a signed HttpOnly session cookie valid for ADMIN_SESSION_TTL_SECS.
// Never returns the token in the JSON body — the browser reads it from the cookie.
app.post('/api/admin/panel/auth', (req, res) => {
  if (!ADMIN_PANEL_PASSWORD_HASH) {
    console.error('[ghost/admin] Login attempt but ADMIN_PANEL_PASSWORD_HASH is not set');
    return res.status(503).json({ ok: false, error: 'Admin panel not configured. Set ADMIN_PANEL_PASSWORD_HASH.' });
  }

  const { password } = req.body || {};
  if (!password) {
    return res.status(400).json({ ok: false, error: 'Password is required.' });
  }

  const submitted = crypto.createHash('sha256').update(String(password)).digest('hex');
  let match = false;
  try {
    match = crypto.timingSafeEqual(
      Buffer.from(submitted),
      Buffer.from(ADMIN_PANEL_PASSWORD_HASH),
    );
  } catch (_) {
    // length mismatch → not equal
  }

  if (!match) {
    console.warn('[ghost/admin] login_fail ip=%s reason=wrong_password', req.ip);
    return res.status(401).json({ ok: false, error: 'Invalid password.' });
  }

  const token = _issueAdminJwt();

  // Set a server-side HttpOnly cookie — the browser cannot read or tamper with it.
  res.cookie(ADMIN_COOKIE_NAME, token, {
    httpOnly: true,
    secure:   process.env.NODE_ENV !== 'development',   // Secure in production
    sameSite: 'lax',
    path:     '/',
    maxAge:   ADMIN_SESSION_TTL_SECS * 1000,
  });

  console.log('[ghost/admin] login_success ip=%s', req.ip);
  // Return ok:true — the session is in the cookie, not the body.
  return res.json({ ok: true });
});

// GET /api/admin/panel/verify  — lightweight session check (called on page load)
app.get('/api/admin/panel/verify', _requireAdminSession, (_req, res) => {
  res.json({ ok: true });
});

// POST /api/admin/panel/logout — clear the session cookie
app.post('/api/admin/panel/logout', (_req, res) => {
  res.clearCookie(ADMIN_COOKIE_NAME, { path: '/' });
  res.json({ ok: true });
});

// ── Admin panel data endpoints ────────────────────────────────────────────────
// All of these require a valid session cookie.
// IMPORTANT: These are registered BEFORE the app.all('/api/admin/*') that would
// re-proxy them.  Express uses first-match routing, so specific routes win.
// The catch-all below has been intentionally REMOVED to prevent the 508 loop.

// Dashboard
app.get('/api/admin/dashboard',                         _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.get('/api/admin/stats',                             _requireAdminSession, (req, res) => _proxyToApi(req, res));

// Inventory
app.get('/api/admin/inventory',                         _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.get('/api/admin/inventory/stats',                   _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/inventory/import',                 _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/inventory/bulk-delete',            _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.delete('/api/admin/inventory/:key',                 _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/inventory/${req.params.key}`));
app.patch('/api/admin/inventory/:key',                  _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/inventory/${req.params.key}`));
app.post('/api/admin/inventory/:key/revoke',            _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/inventory/${req.params.key}/revoke`));
app.post('/api/admin/inventory/:key/extend',            _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/inventory/${req.params.key}/extend`));

// Orders
app.get('/api/admin/orders',                            _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.get('/api/admin/orders/:orderId',                   _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/orders/${req.params.orderId}`));

// Customers
app.get('/api/admin/customers',                         _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/customers/:email/revoke',          _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/customers/${req.params.email}/revoke`));
app.post('/api/admin/customers/:email/reset-hwid',      _requireAdminSession, (req, res) => _proxyToApi(req, res, `/api/admin/customers/${req.params.email}/reset-hwid`));

// Downloads
app.get('/api/admin/downloads',                         _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/downloads',                        _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/downloads/increment',              _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/downloads/rollback',               _requireAdminSession, (req, res) => _proxyToApi(req, res));

// Settings
app.get('/api/admin/settings',                          _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/settings',                         _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.post('/api/admin/settings/password',                _requireAdminSession, (req, res) => _proxyToApi(req, res));

// Activity log
app.get('/api/admin/activity',                          _requireAdminSession, (req, res) => _proxyToApi(req, res));
app.delete('/api/admin/activity',                       _requireAdminSession, (req, res) => _proxyToApi(req, res));

// ── Admin panel HTML ──────────────────────────────────────────────────────────
app.get('/admin', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'admin.html'), err => {
    if (err) res.status(500).send('Internal server error');
  });
});
app.get('/admin.html', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'admin.html'), err => {
    if (err) res.status(500).send('Internal server error');
  });
});

// ── PayPal Checkout API routes ────────────────────────────────────────────────
app.post('/api/paypal/create-order',      paypal.createOrder);
app.post('/api/paypal/capture-order',     paypal.captureOrder);
app.post('/api/paypal/webhook',           paypal.handleWebhook);
app.post('/api/paypal/retry-fulfillment', paypal.retryFulfillment);

// ── Order lookup + download (proxy to delivery backend) ──────────────────────
async function _proxyToDelivery (req, res, deliveryPath) {
  const DELIVERY_BACKEND_URL = (process.env.GHOST_DELIVERY_URL || '').replace(/\/$/, '');
  if (!DELIVERY_BACKEND_URL) {
    return res.status(503).json({ ok: false, error: 'Order service unavailable: GHOST_DELIVERY_URL not configured.' });
  }
  try {
    const { default: fetch } = await import('node-fetch');
    const BODY_METHODS = ['POST', 'PATCH', 'PUT'];
    const hasBody = BODY_METHODS.includes(req.method) && req.body !== undefined;
    const upstream = await fetch(`${DELIVERY_BACKEND_URL}${deliveryPath}`, {
      method:  req.method || 'GET',
      headers: hasBody ? { 'Content-Type': 'application/json' } : {},
      body:    hasBody ? JSON.stringify(req.body) : undefined,
    });
    const data = await upstream.json().catch(() => ({}));
    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[ghost/server] delivery proxy error path=%s: %s', deliveryPath, err.message);
    return res.status(502).json({ ok: false, error: 'Service unavailable.' });
  }
}

app.get('/api/order/:orderId', (req, res) =>
  _proxyToDelivery(req, res, `/api/order/${encodeURIComponent(req.params.orderId)}`),
);
app.get('/api/order/:orderId/download', (req, res) =>
  _proxyToDelivery(req, res, `/api/order/${encodeURIComponent(req.params.orderId)}/download`),
);

// ── Checkout HTML ─────────────────────────────────────────────────────────────
app.get('/checkout.html', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
    if (err) { console.error('[ghost/server] checkout.html send error:', err.message); res.status(500).send('Internal server error'); }
  });
});
app.get('/checkout', (_req, res) => {
  res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
    if (err) { console.error('[ghost/server] checkout.html send error:', err.message); res.status(500).send('Internal server error'); }
  });
});

// ── Ghost shared Python API proxy routes ──────────────────────────────────────
// Auth + customer-facing routes only — admin routes are handled above.
app.all('/api/auth/*',     (req, res) => _proxyToApi(req, res));
app.all('/api/license/*',  (req, res) => _proxyToApi(req, res));
app.all('/api/purchases',  (req, res) => _proxyToApi(req, res));
app.all('/api/downloads*', (req, res) => _proxyToApi(req, res));
// NOTE: /api/admin/* catch-all has been intentionally REMOVED.
// All admin routes are explicitly registered above with authentication.
// A catch-all here would re-proxy authenticated requests to GHOST_API_URL,
// creating a loop when GHOST_API_URL == the Vercel deployment URL (508).

// ── Serve static frontend ─────────────────────────────────────────────────────
app.get('/', (_req, res) =>
  res.sendFile(path.join(WEB_ROOT, 'index.html')),
);

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

// ── Health & status endpoints ─────────────────────────────────────────────────
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

// ── Startup ───────────────────────────────────────────────────────────────────
if (!ADMIN_PANEL_PASSWORD_HASH) {
  console.warn('[ghost/server] WARNING: ADMIN_PANEL_PASSWORD_HASH not set — /admin panel login will return 503');
}
if (!GHOST_ADMIN_API_KEY) {
  console.warn('[ghost/server] WARNING: GHOST_ADMIN_API_KEY not set — Bearer API key auth will be unavailable');
}

app.listen(PORT, () => {
  console.log(`[ghost/server] Listening on http://localhost:${PORT}`);
  console.log(`[ghost/server] PayPal environment: ${process.env.PAYPAL_ENVIRONMENT || 'sandbox'}`);
  console.log(`[ghost/server] Ghost shared API proxy: ${GHOST_API_URL || '(GHOST_API_URL not set — auth/license proxy will return 503)'}`);
  if (!process.env.PAYPAL_CLIENT_ID)   console.warn('[ghost/server] WARNING: PAYPAL_CLIENT_ID not set — payment routes will fail');
  if (!process.env.PAYPAL_CLIENT_SECRET) console.warn('[ghost/server] WARNING: PAYPAL_CLIENT_SECRET not set — payment routes will fail');
  if (!process.env.GHOST_API_URL)      console.warn('[ghost/server] WARNING: GHOST_API_URL not set — auth/license proxy will return 503');
  if (!process.env.GHOST_DELIVERY_URL) console.warn('[ghost/server] WARNING: GHOST_DELIVERY_URL not set — license delivery will fail');
  if (BASE_URL && GHOST_API_URL && GHOST_API_URL.toLowerCase().startsWith(BASE_URL.toLowerCase())) {
    console.error('[ghost/server] CRITICAL: GHOST_API_URL points back at BASE_URL — all proxy requests will loop (508). Fix GHOST_API_URL in environment variables.');
  }
});
