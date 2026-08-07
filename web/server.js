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

// Server-side admin API key for bot / CI integrations.
// Accepted via: Authorization: Bearer <GHOST_ADMIN_API_KEY>
// NEVER returned through any public endpoint, never logged.
const GHOST_ADMIN_API_KEY = (process.env.GHOST_ADMIN_API_KEY || '').trim();

// NOTE: ADMIN_SESSION_SECRET is intentionally NOT captured into a module-level
// constant.  On Vercel, each serverless cold-start is a fresh Node process; if
// the env var is read once at module load time and the var is missing (or not
// yet injected), EVERY subsequent verify call would use an empty string and
// fail — producing the 401 / "session expired" loop ~3 seconds after login.
// Reading process.env.ADMIN_SESSION_SECRET inside _issueAdminSession and
// _verifyAdminSession on every call guarantees the correct value is used
// regardless of which Vercel instance handles the request.
//
// ADMIN_SESSION_TTL_SECS: 12 hours (requirement)
const ADMIN_SESSION_TTL_SECS = 12 * 60 * 60; // 12 hours
// __Host- prefix enforces: Secure, Path=/, no Domain attribute.
// This is the strongest cookie security available in modern browsers.
const ADMIN_COOKIE_NAME = '__Host-ghost_admin_session';

// ── Stateless signed session cookie helpers ───────────────────────────────────
// The session token is a compact HMAC-signed structure:
//   base64url(payload_json) + "." + base64url(hmac_sha256)
// No in-memory state, no database — the signature proves authenticity and the
// expiry claim proves freshness. Any Vercel instance can verify any token as
// long as ADMIN_SESSION_SECRET is the same env var value across all instances.
//
// CRITICAL: Both helpers read process.env.ADMIN_SESSION_SECRET on every call.
// Do NOT hoist this into a module-level const — doing so would capture an empty
// string on Vercel cold-starts where the env var arrives after module init, and
// every subsequent verification would fail with a 401 loop.

function _issueAdminSession () {
  // Read secret fresh on every call — Vercel-safe.
  const secret = (process.env.ADMIN_SESSION_SECRET || '').trim();
  const secretLen = secret.length;
  console.log('[ghost/admin] issue_session secret_present=%s secret_len=%d', secretLen > 0, secretLen);
  if (!secret) {
    console.error('[ghost/admin] CRITICAL: ADMIN_SESSION_SECRET is not set — cannot issue session. Set it in Vercel env vars and redeploy.');
    return null;
  }
  const iat = Math.floor(Date.now() / 1000);
  const exp = iat + ADMIN_SESSION_TTL_SECS;
  const payload = Buffer.from(JSON.stringify({
    sub: 'admin',
    iat,
    exp,
  })).toString('base64url');
  const sig = crypto
    .createHmac('sha256', secret)
    .update(payload)
    .digest('base64url');
  console.log('[ghost/admin] session_issued iat=%d exp=%d exp_iso=%s', iat, exp, new Date(exp * 1000).toISOString());
  return `${payload}.${sig}`;
}

function _verifyAdminSession (token) {
  // Read secret fresh on every call — Vercel-safe.
  const secret = (process.env.ADMIN_SESSION_SECRET || '').trim();
  const secretLen = secret.length;
  console.log('[ghost/admin] verify_session secret_present=%s secret_len=%d cookie_present=%s', secretLen > 0, secretLen, Boolean(token));
  if (!secret) {
    console.error('[ghost/admin] CRITICAL: ADMIN_SESSION_SECRET not set — cannot verify session cookie. Set it in Vercel env vars and redeploy.');
    return false;
  }
  if (!token || typeof token !== 'string') {
    console.log('[ghost/admin] cookie_missing');
    return false;
  }
  const dot = token.lastIndexOf('.');
  if (dot < 1) {
    console.log('[ghost/admin] signature_invalid reason=malformed');
    return false;
  }
  const payload = token.slice(0, dot);
  const sig     = token.slice(dot + 1);
  const expected = crypto
    .createHmac('sha256', secret)
    .update(payload)
    .digest('base64url');
  let sigOk = false;
  try {
    sigOk = crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
  } catch (_) {
    // Buffer length mismatch — signature is wrong
  }
  if (!sigOk) {
    console.log('[ghost/admin] signature_invalid reason=hmac_mismatch');
    return false;
  }
  try {
    const claims = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    const now    = Math.floor(Date.now() / 1000);
    console.log('[ghost/admin] session_claims sub=%s iat=%d exp=%d iat_iso=%s exp_iso=%s now=%d',
      claims.sub,
      claims.iat || 0,
      claims.exp || 0,
      claims.iat ? new Date(claims.iat * 1000).toISOString() : 'none',
      claims.exp ? new Date(claims.exp * 1000).toISOString() : 'none',
      now,
    );
    if (claims.exp && now > claims.exp) {
      console.log('[ghost/admin] session_expired sub=%s exp=%d now=%d delta_secs=%d', claims.sub, claims.exp, now, now - claims.exp);
      return false;
    }
    console.log('[ghost/admin] session_verified sub=%s ttl_remaining_secs=%d', claims.sub, (claims.exp || 0) - now);
    return true;
  } catch (_) {
    console.log('[ghost/admin] signature_invalid reason=payload_parse_error');
    return false;
  }
}

// ── Session middleware ────────────────────────────────────────────────────────
function _requireAdminSession (req, res, next) {
  // Path 1: cookie-based session (human admin panel)
  const cookieToken = req.cookies && req.cookies[ADMIN_COOKIE_NAME];
  if (cookieToken) {
    if (_verifyAdminSession(cookieToken)) return next();
    return res.status(401).json({ ok: false, error: 'Admin session expired. Please log in again.' });
  }

  // Path 2: server-side API key via Authorization: Bearer (bot / CI)
  const authHeader = (req.headers['authorization'] || '').trim();
  if (authHeader.startsWith('Bearer ')) {
    const providedKey = authHeader.slice(7).trim();
    if (!GHOST_ADMIN_API_KEY) {
      console.warn('[ghost/admin] bearer_presented but GHOST_ADMIN_API_KEY is not configured ip=%s', req.ip);
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
      console.warn('[ghost/admin] bearer_invalid ip=%s path=%s', req.ip, req.path);
      return res.status(401).json({ ok: false, error: 'Invalid admin API key.' });
    }
    return next();
  }

  // Fallback: no credentials at all
  console.log('[ghost/admin] cookie_missing ip=%s path=%s', req.ip, req.path);
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
    'ADMIN_SESSION_SECRET',
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
// Issues a signed stateless HttpOnly session cookie valid for ADMIN_SESSION_TTL_SECS.
// The cookie uses the __Host- prefix which enforces Secure, Path=/, no Domain.
// Never returns the token in the JSON body — the browser reads it from the cookie.
app.post('/api/admin/panel/auth', (req, res) => {
  if (!ADMIN_PANEL_PASSWORD_HASH) {
    console.error('[ghost/admin] login_fail reason=ADMIN_PANEL_PASSWORD_HASH_not_set ip=%s', req.ip);
    return res.status(503).json({ ok: false, error: 'Admin panel not configured. Set ADMIN_PANEL_PASSWORD_HASH.' });
  }
  if (!ADMIN_SESSION_SECRET) {
    console.error('[ghost/admin] login_fail reason=ADMIN_SESSION_SECRET_not_set ip=%s', req.ip);
    return res.status(503).json({ ok: false, error: 'Admin session secret not configured. Set ADMIN_SESSION_SECRET.' });
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
    console.warn('[ghost/admin] login_rejected ip=%s reason=wrong_password', req.ip);
    return res.status(401).json({ ok: false, error: 'Invalid password.' });
  }

  const token = _issueAdminSession();

  // __Host- cookie attributes: Secure (required), HttpOnly, SameSite=Lax,
  // Path=/ (required), no Domain attribute (required for __Host-).
  // maxAge uses the same ADMIN_SESSION_TTL_SECS as the token exp claim so the
  // browser and server expiry are always identical — no "cookie present but
  // token expired" or "cookie gone but token still valid" mismatch.
  res.cookie(ADMIN_COOKIE_NAME, token, {
    httpOnly: true,
    secure:   true,    // required for __Host- prefix
    sameSite: 'lax',
    path:     '/',
    maxAge:   1000 * 60 * 60 * 12, // 12 hours in ms — matches ADMIN_SESSION_TTL_SECS
    // No domain attribute — __Host- prefix requires host-only binding
  });

  console.log('[ghost/admin] login_accepted ip=%s cookie_issued=true ttl_secs=%d', req.ip, ADMIN_SESSION_TTL_SECS);
  // Return ok:true — the session is in the cookie, not the body.
  return res.json({ ok: true });
});

// GET /api/admin/session  — canonical session validity check (called on page load)
// Returns 200 + { authenticated: true } when session cookie is valid,
// 401 + { authenticated: false } otherwise.
// The frontend calls this once on load — it must not produce alerts on 401.
app.get('/api/admin/session', (req, res) => {
  const cookieToken = req.cookies && req.cookies[ADMIN_COOKIE_NAME];
  if (!cookieToken) {
    console.log('[ghost/admin] session_check cookie_missing ip=%s', req.ip);
    return res.status(401).json({ ok: false, authenticated: false });
  }
  if (_verifyAdminSession(cookieToken)) {
    return res.status(200).json({ ok: true, authenticated: true });
  }
  return res.status(401).json({ ok: false, authenticated: false });
});

// GET /api/admin/panel/verify  — legacy session check (kept for compatibility)
app.get('/api/admin/panel/verify', _requireAdminSession, (_req, res) => {
  res.json({ ok: true });
});

// POST /api/admin/panel/logout — clear the session cookie
app.post('/api/admin/panel/logout', (_req, res) => {
  // Must match the exact attributes used when setting the cookie:
  // __Host- requires Secure=true and Path=/ — mismatching these prevents clearing.
  res.clearCookie(ADMIN_COOKIE_NAME, {
    path:     '/',
    secure:   true,
    httpOnly: true,
    sameSite: 'lax',
  });
  console.log('[ghost/admin] logout ip=%s', _req.ip);
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
// Check ADMIN_SESSION_SECRET at startup for early warning, but do NOT cache its
// value — _issueAdminSession / _verifyAdminSession read it fresh on every call.
const _startupSecret = (process.env.ADMIN_SESSION_SECRET || '').trim();
if (!_startupSecret) {
  console.error('[ghost/server] CRITICAL: ADMIN_SESSION_SECRET not set — admin sessions will fail on EVERY request. Set this env var in Vercel and redeploy.');
} else {
  console.log('[ghost/server] ADMIN_SESSION_SECRET present length=%d', _startupSecret.length);
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
