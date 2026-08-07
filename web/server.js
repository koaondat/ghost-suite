/**
 * server.js — Ghost Web Server
 * ============================
 * Express server that:
 *   • Serves the static web/ files
 *   • Provides PayPal Checkout session API (api/paypal.js)
 *   • Admin authentication: POST /api/admin/login  (verifies GHOST_ADMIN_API_KEY)
 *   • Admin session:        GET  /api/admin/session
 *   • Admin logout:         POST /api/admin/logout
 *   • All admin data endpoints handled INLINE (no proxy back to self)
 *   • Proxies /api/auth/*, /api/license/*, /api/purchases,
 *     /api/downloads/* to the Ghost shared Python backend (api.py)
 *
 * ── Authentication model ──────────────────────────────────────────────────────
 * 1.  Admin visits /admin — sees loading screen while session is checked.
 * 2.  Browser calls GET /api/admin/session → 401 (no cookie) → login form shown.
 * 3.  Admin enters GHOST_ADMIN_API_KEY in the "Admin API Key" field.
 * 4.  Browser POSTs { key } to POST /api/admin/login.
 * 5.  Server compares key to process.env.GHOST_ADMIN_API_KEY with timingSafeEqual.
 * 6.  On match: server issues a signed, short-lived HttpOnly __Host- cookie.
 * 7.  Subsequent requests carry the cookie automatically; server verifies on each.
 * 8.  Session lasts ADMIN_SESSION_TTL_SECS (12 hours).  Refresh keeps you logged in.
 *
 * ── Security properties ───────────────────────────────────────────────────────
 * • GHOST_ADMIN_API_KEY is never returned through any endpoint, never logged.
 * • Raw key is never stored in localStorage, sessionStorage, cookie, or HTML.
 * • Session cookie: __Host- prefix → Secure, Path=/, no Domain, HttpOnly.
 * • ADMIN_SESSION_SECRET read fresh on every sign/verify (Vercel-safe).
 * • Admin login rate-limited: max 10 attempts per 15 minutes per IP.
 * • All admin data is served directly from Upstash Redis (no self-proxy loop).
 *
 * Start:  node server.js
 * Deps:   npm install express node-fetch dotenv cookie-parser
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

// ── Admin auth constants ──────────────────────────────────────────────────────
// GHOST_ADMIN_API_KEY — read fresh in the login handler so it is always current.
// NEVER captured into a module-level const that could be stale on Vercel cold-starts.
// ADMIN_SESSION_SECRET — same: read inside _issueAdminSession / _verifyAdminSession.

const ADMIN_SESSION_TTL_SECS = 12 * 60 * 60; // 12 hours
const ADMIN_COOKIE_NAME      = '__Host-ghost_admin_session';

// ── Rate limiter (login attempts per IP) ─────────────────────────────────────
// Simple in-memory store — sufficient for Vercel (each instance is isolated; a
// determined attacker hitting multiple instances still faces per-instance limits).
// Window: 15 minutes, max: 10 attempts.
const _loginAttempts = new Map(); // ip → { count, resetAt }
const RATE_WINDOW_MS = 15 * 60 * 1000;
const RATE_MAX       = 10;

function _checkRateLimit (ip) {
  const now   = Date.now();
  const entry = _loginAttempts.get(ip);
  if (!entry || now > entry.resetAt) {
    _loginAttempts.set(ip, { count: 1, resetAt: now + RATE_WINDOW_MS });
    return true; // allowed
  }
  entry.count++;
  return entry.count <= RATE_MAX;
}

// ── Stateless signed session cookie helpers ───────────────────────────────────
// Token format:  base64url(JSON payload) + "." + base64url(HMAC-SHA256)
// No in-memory state required — any Vercel instance can verify any token as long
// as ADMIN_SESSION_SECRET is the same env var value across all instances.
//
// CRITICAL: both helpers read process.env.ADMIN_SESSION_SECRET on every call.
// DO NOT hoist this into a module-level const — if the var arrives after module
// init on Vercel, every verify call would use an empty string and fail (401 loop).

function _issueAdminSession () {
  const secret = (process.env.ADMIN_SESSION_SECRET || '').trim();
  if (!secret) {
    console.error('[ghost/admin] CRITICAL: ADMIN_SESSION_SECRET not set — cannot issue session.');
    return null;
  }
  const iat     = Math.floor(Date.now() / 1000);
  const exp     = iat + ADMIN_SESSION_TTL_SECS;
  const payload = Buffer.from(JSON.stringify({ sub: 'admin', iat, exp })).toString('base64url');
  const sig     = crypto.createHmac('sha256', secret).update(payload).digest('base64url');
  return `${payload}.${sig}`;
}

function _verifyAdminSession (token) {
  const secret = (process.env.ADMIN_SESSION_SECRET || '').trim();
  if (!secret) return false;
  if (!token || typeof token !== 'string') return false;
  const dot = token.lastIndexOf('.');
  if (dot < 1) return false;
  const payload  = token.slice(0, dot);
  const sig      = token.slice(dot + 1);
  const expected = crypto.createHmac('sha256', secret).update(payload).digest('base64url');
  let sigOk = false;
  try { sigOk = crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected)); } catch (_) {}
  if (!sigOk) return false;
  try {
    const claims = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    return !claims.exp || Math.floor(Date.now() / 1000) <= claims.exp;
  } catch (_) { return false; }
}

// ── Session middleware ────────────────────────────────────────────────────────
function _requireAdminSession (req, res, next) {
  const cookieToken = req.cookies && req.cookies[ADMIN_COOKIE_NAME];
  if (cookieToken && _verifyAdminSession(cookieToken)) return next();
  return res.status(401).json({ ok: false, error: 'Admin session required. Please log in.' });
}

// ── Web-root path ─────────────────────────────────────────────────────────────
const WEB_ROOT = __dirname;

// ── Ghost Python API base URL ─────────────────────────────────────────────────
const GHOST_API_URL = (process.env.GHOST_API_URL || '').replace(/\/$/, '');
const BASE_URL      = (process.env.BASE_URL || '').replace(/\/$/, '').toLowerCase();
const GHOST_ADMIN_API_KEY = (process.env.GHOST_ADMIN_API_KEY || '').trim();

if (!GHOST_API_URL) {
  console.warn('[ghost/server] WARNING: GHOST_API_URL is not set. Auth and license routes will return 503.');
}

// ── Proxy helper — public API routes ONLY (never admin routes) ────────────────
async function _proxyToApi (req, res, pathOverride) {
  if (!GHOST_API_URL) {
    return res.status(503).json({ ok: false, error: 'API service unavailable: GHOST_API_URL is not configured.' });
  }
  const targetPath = pathOverride || req.url;
  const targetUrl  = `${GHOST_API_URL}${targetPath}`;

  // Self-loop guard
  const targetLower = targetUrl.toLowerCase();
  if (BASE_URL && targetLower.startsWith(BASE_URL)) {
    console.error('[ghost/proxy] SELF-LOOP: GHOST_API_URL points back at this server.');
    return res.status(508).json({ ok: false, error: 'Configuration error: GHOST_API_URL must not point to this server.' });
  }

  const { default: fetch } = await import('node-fetch');
  const headers = { ...req.headers };
  delete headers['host'];
  delete headers['cookie'];
  if (GHOST_ADMIN_API_KEY) headers['x-admin-key'] = GHOST_ADMIN_API_KEY;

  const BODY_METHODS = ['POST', 'PATCH', 'PUT', 'DELETE'];
  const hasBody = BODY_METHODS.includes(req.method) && req.body !== undefined;
  if (hasBody) headers['content-type'] = 'application/json';

  try {
    const upstream = await fetch(targetUrl, {
      method:  req.method,
      headers,
      body:    hasBody ? JSON.stringify(req.body) : undefined,
    });
    const data = await upstream.json().catch(() => ({}));
    const setCookie = upstream.headers.raw?.()?.['set-cookie'];
    if (setCookie) res.set('Set-Cookie', setCookie);
    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[ghost/proxy] upstream error path=%s: %s', targetPath, err.message);
    return res.status(502).json({ ok: false, error: 'API service unavailable. Please try again.' });
  }
}

// ── Upstash Redis helpers + local-file fallback ───────────────────────────────
// When Upstash env vars are set, all inventory reads/writes go to Redis.
// When they are absent (dev / plain-VPS deployments), reads/writes fall back to
// key_inventory.json on disk (same file that inventory.py uses), so both the
// Python API and this Node server share the same storage.
//
// The Redis key "ghost:inventory" maps 1-to-1 with the JSON array in that file.
const fs   = require('fs');
const _INV_FILE = path.resolve(__dirname, '..', 'key_inventory.json');

function _fileGet (storeKey) {
  if (storeKey !== 'ghost:inventory') return null;
  try {
    const raw = fs.readFileSync(_INV_FILE, 'utf8');
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : [];
  } catch (_) { return []; }
}

function _fileSet (storeKey, value) {
  if (storeKey !== 'ghost:inventory') return false;
  try {
    const tmp = _INV_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
    fs.renameSync(tmp, _INV_FILE);
    return true;
  } catch (err) {
    console.error('[ghost/inventory] file write error:', err.message);
    return false;
  }
}

function _redisConfigured () {
  return !!(
    (process.env.UPSTASH_REDIS_REST_URL   || '').trim() &&
    (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim()
  );
}

async function _redisGet (key) {
  if (!_redisConfigured()) return _fileGet(key);
  const url   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
  const token = (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim();
  try {
    const { default: fetch } = await import('node-fetch');
    const res  = await fetch(`${url}/get/${encodeURIComponent(key)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json();
    if (data.result === null || data.result === undefined) return _fileGet(key);
    return typeof data.result === 'string' ? JSON.parse(data.result) : data.result;
  } catch (_) { return _fileGet(key); }
}

async function _redisSet (key, value) {
  if (!_redisConfigured()) return _fileSet(key, value);
  const url   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
  const token = (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim();
  try {
    const { default: fetch } = await import('node-fetch');
    const r = await fetch(`${url}/set/${encodeURIComponent(key)}`, {
      method:  'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body:    JSON.stringify(JSON.stringify(value)),
    });
    if (!r.ok) {
      console.error('[ghost/redis] set failed status=%d key=%s', r.status, key);
      return false;
    }
    return true;
  } catch (err) {
    console.error('[ghost/redis] set error key=%s: %s', key, err.message);
    return false;
  }
}

async function _redisDel (key) {
  if (!_redisConfigured()) return false;
  const url   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
  const token = (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim();
  try {
    const { default: fetch } = await import('node-fetch');
    await fetch(`${url}/del/${encodeURIComponent(key)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return true;
  } catch (_) { return false; }
}

// ── Body parsers ──────────────────────────────────────────────────────────────
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
    return res.status(503).json({ configured: false, clientId: null, environment: env,
      error: 'Payment is not configured on this server. Please contact support.' });
  }
  return res.json({ configured: true, clientId, environment: env });
});

// ── Runtime config audit (presence only — no secrets returned) ────────────────
app.get('/api/config/audit', (req, res) => {
  const vars = [
    'PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET', 'PAYPAL_ENVIRONMENT', 'PAYPAL_WEBHOOK_ID',
    'GHOST_API_URL', 'GHOST_DELIVERY_URL', 'BASE_URL',
    'ADMIN_SESSION_SECRET', 'GHOST_ADMIN_API_KEY',
    'UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN',
  ];
  const report = {};
  let allPresent = true;
  for (const name of vars) {
    const present = Boolean(process.env[name]);
    report[name] = { present };
    if (!present) allPresent = false;
  }
  return res.json({ ok: true, allPresent, vars: report });
});

// ═════════════════════════════════════════════════════════════════════════════
// ADMIN AUTHENTICATION — single clean flow
// ═════════════════════════════════════════════════════════════════════════════

// ── POST /api/admin/login ─────────────────────────────────────────────────────
// Body: { key: string }
// Verifies key against GHOST_ADMIN_API_KEY.
// On success: sets __Host-ghost_admin_session HttpOnly cookie, returns { ok: true }.
// On failure: returns { ok: false, error: string }.
// GHOST_ADMIN_API_KEY is NEVER returned in any response body.
app.post('/api/admin/login', (req, res) => {
  const ip = req.ip;

  // Rate limit check
  if (!_checkRateLimit(ip)) {
    console.warn('[ghost/admin] login_rate_limited ip=%s', ip);
    return res.status(429).json({
      ok:    false,
      error: 'Too many login attempts. Please wait 15 minutes and try again.',
    });
  }

  // Server-side key — read fresh on every call (Vercel-safe)
  const serverKey = (process.env.GHOST_ADMIN_API_KEY || '').trim();

  if (!serverKey) {
    console.error('[ghost/admin] GHOST_ADMIN_API_KEY not set — login blocked ip=%s', ip);
    return res.status(503).json({ ok: false, error: 'Admin panel not configured. Set GHOST_ADMIN_API_KEY.' });
  }
  if (!process.env.ADMIN_SESSION_SECRET) {
    console.error('[ghost/admin] ADMIN_SESSION_SECRET not set — cannot issue session ip=%s', ip);
    return res.status(503).json({ ok: false, error: 'Admin session secret not configured. Set ADMIN_SESSION_SECRET.' });
  }

  const { key } = req.body || {};
  if (!key || typeof key !== 'string') {
    return res.status(400).json({ ok: false, error: 'Admin API key is required.' });
  }

  // Constant-time comparison — prevents timing attacks
  let match = false;
  try {
    match = crypto.timingSafeEqual(
      Buffer.from(key.trim()),
      Buffer.from(serverKey),
    );
  } catch (_) {
    // Buffer length mismatch means wrong key
  }

  if (!match) {
    console.warn('[ghost/admin] login_rejected ip=%s reason=wrong_key', ip);
    return res.status(401).json({ ok: false, error: 'Invalid admin API key.' });
  }

  const token = _issueAdminSession();
  if (!token) {
    return res.status(500).json({ ok: false, error: 'Failed to issue session. Check ADMIN_SESSION_SECRET.' });
  }

  // __Host- cookie: Secure required, Path=/, no Domain, HttpOnly, SameSite=Lax
  res.cookie(ADMIN_COOKIE_NAME, token, {
    httpOnly: true,
    secure:   true,
    sameSite: 'lax',
    path:     '/',
    maxAge:   ADMIN_SESSION_TTL_SECS * 1000,
  });

  console.log('[ghost/admin] login_success ip=%s', ip);
  return res.json({ ok: true });
});

// ── GET /api/admin/session ────────────────────────────────────────────────────
// Returns 200 + { authenticated: true } if cookie is valid, 401 otherwise.
// Called once on page load to determine whether to show dashboard or login form.
// MUST NOT trigger session-expiry UI on 401 — it is expected before login.
app.get('/api/admin/session', (req, res) => {
  const cookieToken = req.cookies && req.cookies[ADMIN_COOKIE_NAME];
  if (cookieToken && _verifyAdminSession(cookieToken)) {
    return res.status(200).json({ ok: true, authenticated: true });
  }
  return res.status(401).json({ ok: false, authenticated: false });
});

// ── POST /api/admin/logout ────────────────────────────────────────────────────
// Clears the session cookie.
app.post('/api/admin/logout', (_req, res) => {
  res.clearCookie(ADMIN_COOKIE_NAME, { path: '/', secure: true, httpOnly: true, sameSite: 'lax' });
  console.log('[ghost/admin] logout ip=%s', _req.ip);
  return res.json({ ok: true });
});

// ═════════════════════════════════════════════════════════════════════════════
// ADMIN DATA ENDPOINTS — all served inline, no proxy to GHOST_API_URL
// Prevents 508 loops when GHOST_API_URL == this Vercel deployment.
// Uses Upstash Redis for production-grade persistence.
// ═════════════════════════════════════════════════════════════════════════════

// ── GET /api/admin/dashboard ──────────────────────────────────────────────────
app.get('/api/admin/dashboard', _requireAdminSession, async (req, res) => {
  try {
    const [orders, inventory, activity] = await Promise.all([
      _redisGet('ghost:orders'),
      _redisGet('ghost:inventory'),
      _redisGet('ghost:activity'),
    ]);

    const ordersArr    = Array.isArray(orders)    ? orders    : [];
    const inventoryArr = Array.isArray(inventory) ? inventory : [];
    const activityArr  = Array.isArray(activity)  ? activity  : [];

    const now       = new Date();
    const todayStr  = now.toISOString().slice(0, 10);
    const monthStr  = now.toISOString().slice(0, 7);

    const completed = ordersArr.filter(o => o.payment_status === 'COMPLETED');
    const revenueToday  = completed.filter(o => (o.purchase_date || '').startsWith(todayStr))
      .reduce((s, o) => s + parseFloat(o.amount || 0), 0);
    const revenueMonth  = completed.filter(o => (o.purchase_date || '').startsWith(monthStr))
      .reduce((s, o) => s + parseFloat(o.amount || 0), 0);
    const revenueTotal  = completed.reduce((s, o) => s + parseFloat(o.amount || 0), 0);

    const activeKeys = inventoryArr.filter(k => k.status === 'activated').length;
    const availKeys  = inventoryArr.filter(k => k.status === 'available').length;
    const soldKeys   = inventoryArr.filter(k => ['sold', 'activated'].includes(k.status)).length;

    const customers = {};
    completed.forEach(o => { if (o.email) customers[o.email] = true; });

    const recent30  = Array.from({ length: 30 }, (_, i) => {
      const d = new Date(now);
      d.setDate(d.getDate() - (29 - i));
      return d.toISOString().slice(0, 10);
    });

    const dailyRevenue   = recent30.map(d => completed.filter(o => (o.purchase_date || '').startsWith(d)).reduce((s, o) => s + parseFloat(o.amount || 0), 0));
    const dailyOrders    = recent30.map(d => ordersArr.filter(o => (o.purchase_date || '').startsWith(d)).length);
    const dailyCustomers = recent30.map(d => {
      const seen = new Set();
      completed.filter(o => (o.purchase_date || '').startsWith(d)).forEach(o => { if (o.email) seen.add(o.email); });
      return seen.size;
    });

    return res.json({
      ok:             true,
      revenue_today:  revenueToday.toFixed(2),
      revenue_month:  revenueMonth.toFixed(2),
      revenue_total:  revenueTotal.toFixed(2),
      total_orders:   ordersArr.length,
      customers:      Object.keys(customers).length,
      active_licenses:activeKeys,
      available_keys: availKeys,
      sold_keys:      soldKeys,
      pending_orders: ordersArr.filter(o => o.delivery_status === 'delivery_pending').length,
      failed_payments:ordersArr.filter(o => o.payment_status === 'FAILED').length,
      recent_orders:  completed.slice(-10).reverse(),
      recent_activity:activityArr.slice(-10).reverse(),
      graph: { dates: recent30, revenue: dailyRevenue, orders: dailyOrders, customers: dailyCustomers },
    });
  } catch (err) {
    console.error('[ghost/admin] dashboard error:', err.message);
    return res.status(500).json({ ok: false, error: 'Failed to load dashboard.' });
  }
});

// ── GET /api/admin/stats ──────────────────────────────────────────────────────
app.get('/api/admin/stats', _requireAdminSession, async (req, res) => {
  return res.redirect(307, '/api/admin/dashboard');
});

// ── Inventory endpoints ───────────────────────────────────────────────────────
// Ghost key format: at least 3 dash-separated alphanumeric segments (e.g. GHOST-XXXXX-XXXXX)
const _GHOST_KEY_RE = /^[A-Z0-9]{4,}-[A-Z0-9]{4,}-[A-Z0-9]{4,}/i;

app.get('/api/admin/inventory', _requireAdminSession, async (req, res) => {
  let inventory = await _redisGet('ghost:inventory') || [];
  // Apply optional server-side filters passed as query params
  const { status, plan, search } = req.query;
  if (status) inventory = inventory.filter(k => k.status === status);
  if (plan)   inventory = inventory.filter(k => k.plan   === plan);
  if (search) {
    const q = search.trim().toLowerCase();
    inventory = inventory.filter(k =>
      k.key.toLowerCase().includes(q) ||
      (k.customer || '').toLowerCase().includes(q) ||
      (k.notes    || '').toLowerCase().includes(q)
    );
  }
  return res.json({ ok: true, keys: inventory, total: inventory.length });
});

app.get('/api/admin/inventory/stats', _requireAdminSession, async (req, res) => {
  const inventory = await _redisGet('ghost:inventory') || [];
  const counts = { available: 0, reserved: 0, sold: 0, activated: 0, revoked: 0, expired: 0 };
  inventory.forEach(k => { if (counts[k.status] !== undefined) counts[k.status]++; });
  return res.json({ ok: true, ...counts, total: inventory.length });
});

app.post('/api/admin/inventory/import', _requireAdminSession, async (req, res) => {
  const { keys, plan = 'pro', notes = '' } = req.body || {};
  if (!Array.isArray(keys) || !keys.length) {
    return res.status(400).json({ ok: false, error: 'keys array required.' });
  }

  const inventory      = await _redisGet('ghost:inventory') || [];
  const existing       = new Set(inventory.map(k => k.key));
  const added          = [];
  const duplicateKeys  = [];
  const invalidKeys    = [];
  const now            = new Date().toISOString();

  for (const raw of keys) {
    const k = String(raw).trim().toUpperCase();
    if (!k) continue;
    if (!_GHOST_KEY_RE.test(k)) { invalidKeys.push(k); continue; }
    if (existing.has(k))        { duplicateKeys.push(k); continue; }
    inventory.push({
      key:           k,
      plan:          (plan || 'pro').toLowerCase(),
      status:        'available',
      notes:         notes || '',
      created_date:  now,
      added_at:      now,
      customer:      '',
      hwid:          '',
      purchase_date: '',
      expiration:    '',
      order_id:      '',
      customer_email:'',
      assigned_user: '',
    });
    existing.add(k);
    added.push(k);
  }

  const imported_count  = added.length;
  const duplicate_count = duplicateKeys.length;
  const invalid_count   = invalidKeys.length;

  // Only persist — and only report success — when something was actually saved.
  if (imported_count > 0) {
    const saved = await _redisSet('ghost:inventory', inventory);
    if (!saved) {
      console.error('[ghost/inventory] import: _redisSet returned false — keys NOT persisted. imported_count=%d', imported_count);
      return res.status(500).json({
        ok:    false,
        error: 'Storage write failed. Keys were not saved.',
        imported_count:  0,
        saved_count:     0,
        duplicate_count,
        invalid_count,
        inventory_count_after_import: (await _redisGet('ghost:inventory') || []).length,
      });
    }
  }

  const inventory_count_after_import = inventory.length;

  console.log(
    '[ghost/inventory] import: imported_count=%d saved_count=%d duplicate_count=%d invalid_count=%d inventory_count_after_import=%d',
    imported_count, imported_count, duplicate_count, invalid_count, inventory_count_after_import
  );

  return res.json({
    ok:                          imported_count > 0 || duplicate_count > 0,
    imported_count,
    saved_count:                 imported_count,
    duplicate_count,
    invalid_count,
    inventory_count_after_import,
    // Keep legacy field names so the existing frontend stats boxes still work
    added:   imported_count,
    skipped: duplicate_count,
    invalid: invalid_count,
    // Return the actually-saved keys so the frontend can verify
    imported_keys: added,
  });
});

app.post('/api/admin/inventory/bulk-delete', _requireAdminSession, async (req, res) => {
  const { keys } = req.body || {};
  if (!Array.isArray(keys)) return res.status(400).json({ ok: false, error: 'keys array required.' });
  const set = new Set(keys.map(k => String(k).trim().toUpperCase()));
  let inventory = await _redisGet('ghost:inventory') || [];
  const deleted  = inventory.filter(k => set.has(k.key));
  inventory = inventory.filter(k => !set.has(k.key));
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, deleted: deleted.map(k => k.key) });
});

app.delete('/api/admin/inventory/:key', _requireAdminSession, async (req, res) => {
  const target = req.params.key.toUpperCase();
  let inventory = await _redisGet('ghost:inventory') || [];
  const before = inventory.length;
  inventory = inventory.filter(k => k.key !== target);
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, deleted: before - inventory.length });
});

app.patch('/api/admin/inventory/:key', _requireAdminSession, async (req, res) => {
  const target = req.params.key.toUpperCase();
  const inventory = await _redisGet('ghost:inventory') || [];
  const idx = inventory.findIndex(k => k.key === target);
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Key not found.' });
  Object.assign(inventory[idx], req.body, { key: target }); // preserve key value
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, entry: inventory[idx] });
});

app.post('/api/admin/inventory/:key/revoke', _requireAdminSession, async (req, res) => {
  const target = req.params.key.toUpperCase();
  const inventory = await _redisGet('ghost:inventory') || [];
  const idx = inventory.findIndex(k => k.key === target);
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Key not found.' });
  inventory[idx].status = 'revoked';
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, entry: inventory[idx] });
});

app.post('/api/admin/inventory/:key/extend', _requireAdminSession, async (req, res) => {
  const target = req.params.key.toUpperCase();
  const { days = 30 } = req.body || {};
  const inventory = await _redisGet('ghost:inventory') || [];
  const idx = inventory.findIndex(k => k.key === target);
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Key not found.' });
  const base = inventory[idx].expiration ? new Date(inventory[idx].expiration) : new Date();
  base.setDate(base.getDate() + parseInt(days, 10));
  inventory[idx].expiration = base.toISOString().slice(0, 10);
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, entry: inventory[idx] });
});

// ── Orders endpoints ──────────────────────────────────────────────────────────
app.get('/api/admin/orders', _requireAdminSession, async (req, res) => {
  const orders = await _redisGet('ghost:orders') || [];
  return res.json({ ok: true, orders, total: orders.length });
});

app.get('/api/admin/orders/:orderId', _requireAdminSession, async (req, res) => {
  const orders = await _redisGet('ghost:orders') || [];
  const order  = orders.find(o => o.order_id === req.params.orderId || o.paypal_order_id === req.params.orderId);
  if (!order) return res.status(404).json({ ok: false, error: 'Order not found.' });
  return res.json({ ok: true, order });
});

// ── Customers endpoints ───────────────────────────────────────────────────────
app.get('/api/admin/customers', _requireAdminSession, async (req, res) => {
  const orders = await _redisGet('ghost:orders') || [];
  const map    = {};
  for (const o of orders) {
    const email = o.email || '';
    if (!email) continue;
    if (!map[email]) {
      map[email] = { email, discord: o.discord || '', orders: 0, total_spent: 0,
        licenses: [], first_purchase: o.purchase_date, last_purchase: o.purchase_date };
    }
    const c = map[email];
    c.orders++;
    c.total_spent += parseFloat(o.amount || 0);
    if (o.license_key) c.licenses.push(o.license_key);
    if (o.purchase_date && o.purchase_date < c.first_purchase) c.first_purchase = o.purchase_date;
    if (o.purchase_date && o.purchase_date > c.last_purchase)  c.last_purchase  = o.purchase_date;
  }
  const customers = Object.values(map);
  return res.json({ ok: true, customers, total: customers.length });
});

app.post('/api/admin/customers/:email/revoke', _requireAdminSession, async (req, res) => {
  const email = decodeURIComponent(req.params.email);
  const inventory = await _redisGet('ghost:inventory') || [];
  let count = 0;
  for (const k of inventory) {
    if (k.customer === email && k.status === 'activated') { k.status = 'revoked'; count++; }
  }
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, revoked: count });
});

app.post('/api/admin/customers/:email/reset-hwid', _requireAdminSession, async (req, res) => {
  const email = decodeURIComponent(req.params.email);
  const inventory = await _redisGet('ghost:inventory') || [];
  let count = 0;
  for (const k of inventory) {
    if (k.customer === email && k.hwid) { k.hwid = null; count++; }
  }
  await _redisSet('ghost:inventory', inventory);
  return res.json({ ok: true, reset: count });
});

// ── Downloads endpoints ───────────────────────────────────────────────────────
app.get('/api/admin/downloads', _requireAdminSession, async (req, res) => {
  const dl = await _redisGet('ghost:downloads') || {
    version: '—', filename: '—', url: '', changelog: '', release_date: '', download_count: 0, history: [],
  };
  return res.json({ ok: true, ...dl });
});

app.post('/api/admin/downloads', _requireAdminSession, async (req, res) => {
  const { version, filename, url, changelog, release_date } = req.body || {};
  if (!version || !url) return res.status(400).json({ ok: false, error: 'version and url required.' });
  const prev = await _redisGet('ghost:downloads') || { history: [], download_count: 0 };
  const history = prev.history || [];
  if (prev.version && prev.version !== '—') {
    history.unshift({ version: prev.version, filename: prev.filename, url: prev.url,
      changelog: prev.changelog, release_date: prev.release_date,
      replaced_at: new Date().toISOString() });
  }
  const updated = { version, filename: filename || version, url, changelog: changelog || '',
    release_date: release_date || new Date().toISOString().slice(0, 10),
    download_count: prev.download_count || 0, history };
  await _redisSet('ghost:downloads', updated);
  return res.json({ ok: true, ...updated });
});

app.post('/api/admin/downloads/increment', _requireAdminSession, async (req, res) => {
  const dl = await _redisGet('ghost:downloads') || { download_count: 0 };
  dl.download_count = (dl.download_count || 0) + 1;
  await _redisSet('ghost:downloads', dl);
  return res.json({ ok: true, download_count: dl.download_count });
});

app.post('/api/admin/downloads/rollback', _requireAdminSession, async (req, res) => {
  const { version } = req.body || {};
  const dl = await _redisGet('ghost:downloads');
  if (!dl || !Array.isArray(dl.history)) return res.status(404).json({ ok: false, error: 'No history.' });
  const idx = version ? dl.history.findIndex(h => h.version === version) : 0;
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Version not found in history.' });
  const [target] = dl.history.splice(idx, 1);
  const current = { version: dl.version, filename: dl.filename, url: dl.url,
    changelog: dl.changelog, release_date: dl.release_date };
  dl.history.unshift({ ...current, replaced_at: new Date().toISOString() });
  dl.version      = target.version;
  dl.filename     = target.filename;
  dl.url          = target.url;
  dl.changelog    = target.changelog;
  dl.release_date = target.release_date;
  await _redisSet('ghost:downloads', dl);
  return res.json({ ok: true, ...dl });
});

// ── Settings endpoints ────────────────────────────────────────────────────────
app.get('/api/admin/settings', _requireAdminSession, async (req, res) => {
  const settings = await _redisGet('ghost:settings') || {};
  // Never return secrets — strip any that might have been stored
  const { paypal_client_secret, admin_key, ...safe } = settings;
  return res.json({ ok: true, ...safe });
});

app.post('/api/admin/settings', _requireAdminSession, async (req, res) => {
  const prev = await _redisGet('ghost:settings') || {};
  // Never allow overwriting secrets through this endpoint
  const { paypal_client_secret, admin_key, ...updates } = req.body || {};
  const merged = { ...prev, ...updates };
  await _redisSet('ghost:settings', merged);
  const { paypal_client_secret: _s, admin_key: _k, ...safe } = merged;
  return res.json({ ok: true, ...safe });
});

// ── Activity log endpoints ────────────────────────────────────────────────────
app.get('/api/admin/activity', _requireAdminSession, async (req, res) => {
  const log = await _redisGet('ghost:activity') || [];
  return res.json({ ok: true, log, total: log.length });
});

app.delete('/api/admin/activity', _requireAdminSession, async (req, res) => {
  await _redisSet('ghost:activity', []);
  return res.json({ ok: true });
});

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
app.get('/checkout.html', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));
app.get('/checkout', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));

// ── Ghost shared Python API proxy routes ──────────────────────────────────────
// Auth + customer-facing routes only — admin routes are handled natively above.
app.all('/api/auth/*',     (req, res) => _proxyToApi(req, res));
app.all('/api/license/*',  (req, res) => _proxyToApi(req, res));
app.all('/api/purchases',  (req, res) => _proxyToApi(req, res));
app.all('/api/downloads*', (req, res) => _proxyToApi(req, res));

// ── Serve static frontend ─────────────────────────────────────────────────────
app.get('/', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'index.html')));

app.get('/:page(login|register|dashboard|pricing|checkout)', (req, res) =>
  res.sendFile(path.join(WEB_ROOT, `${req.params.page}.html`), err => {
    if (err) res.status(404).sendFile(path.join(WEB_ROOT, 'index.html'));
  }),
);

app.get('/favicon.ico', (_req, res) => res.status(204).end());

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
    ok:           true,
    service:      'ghost-web',
    status:       'ready',
    uptime_secs:  Math.floor((Date.now() - _START_TIME) / 1000),
    ghost_api_url:GHOST_API_URL || '(not configured)',
    paypal_env:   process.env.PAYPAL_ENVIRONMENT || 'sandbox',
    node_version: process.version,
  }),
);

// ── Global error handler ──────────────────────────────────────────────────────
// eslint-disable-next-line no-unused-vars
app.use((err, _req, res, _next) => {
  console.error('[ghost/server] unhandled error:', err);
  res.status(500).json({ ok: false, error: 'Internal server error' });
});

// ── Startup validation ────────────────────────────────────────────────────────
if (!process.env.ADMIN_SESSION_SECRET || !process.env.ADMIN_SESSION_SECRET.trim()) {
  throw new Error(
    'Missing ADMIN_SESSION_SECRET environment variable. ' +
    'Generate with: node -e "const c=require(\'crypto\');console.log(c.randomBytes(64).toString(\'hex\'))" ' +
    'then add to Vercel: vercel env add ADMIN_SESSION_SECRET',
  );
}
if (!GHOST_ADMIN_API_KEY) {
  console.warn('[ghost/server] WARNING: GHOST_ADMIN_API_KEY not set — admin panel login will return 503.');
}

app.listen(PORT, () => {
  console.log(`[ghost/server] Listening on http://localhost:${PORT}`);
  console.log(`[ghost/server] PayPal environment: ${process.env.PAYPAL_ENVIRONMENT || 'sandbox'}`);
  console.log(`[ghost/server] Admin panel: http://localhost:${PORT}/admin`);
  console.log(`[ghost/server] GHOST_ADMIN_API_KEY configured: ${Boolean(GHOST_ADMIN_API_KEY)}`);
});
