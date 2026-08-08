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
const { fulfillOrder } = paypal;
const { GHOST_KEY_RE: _GHOST_KEY_RE } = require('./assets/js/license-format');

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
// ── Upstash Redis — persistent storage ───────────────────────────────────────
// Uses the official @upstash/redis SDK (HTTP/REST — works in Vercel serverless).
// Falls back to local JSON file when Redis env vars are not set (dev/CI only).
const { Redis } = require('@upstash/redis');
const fs        = require('fs');
const _INV_FILE   = path.resolve(__dirname, '..', 'key_inventory.json');
const _USERS_FILE = path.resolve(__dirname, '..', 'users.json');

// ── File fallback (dev only — when Redis is not configured) ───────────────────
// Supports ghost:inventory (key_inventory.json) and ghost:users (users.json).
function _fileGet (storeKey) {
  if (storeKey === 'ghost:inventory') {
    try {
      const raw = fs.readFileSync(_INV_FILE, 'utf8');
      const data = JSON.parse(raw);
      return Array.isArray(data) ? data : [];
    } catch (_) { return []; }
  }
  if (storeKey === 'ghost:users') {
    try {
      const raw = fs.readFileSync(_USERS_FILE, 'utf8');
      const data = JSON.parse(raw);
      return Array.isArray(data) ? data : [];
    } catch (_) { return []; }
  }
  return null;
}

function _fileSet (storeKey, value) {
  if (storeKey === 'ghost:inventory') {
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
  if (storeKey === 'ghost:users') {
    try {
      const tmp = _USERS_FILE + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
      fs.renameSync(tmp, _USERS_FILE);
      return true;
    } catch (err) {
      console.error('[ghost/users] file write error:', err.message);
      return false;
    }
  }
  return false;
}

// ── Redis client factory ──────────────────────────────────────────────────────
// Lazily created and cached — one instance per Vercel function instance.
let _redisClient = null;

function _redisConfigured () {
  return !!(
    (process.env.UPSTASH_REDIS_REST_URL   || '').trim() &&
    (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim()
  );
}

function _getRedisClient () {
  if (!_redisClient) {
    _redisClient = new Redis({
      url:   (process.env.UPSTASH_REDIS_REST_URL   || '').trim(),
      token: (process.env.UPSTASH_REDIS_REST_TOKEN || '').trim(),
    });
  }
  return _redisClient;
}

// ── Timeout wrapper ───────────────────────────────────────────────────────────
// Races any Redis promise against a hard deadline.  If Upstash does not respond
// within _REDIS_TIMEOUT_MS the call rejects with Error('redis_timeout').
const _REDIS_TIMEOUT_MS = 8000; // 8 s

function _withTimeout (promise) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error('redis_timeout')), _REDIS_TIMEOUT_MS)
    ),
  ]);
}

// ── Public helpers ────────────────────────────────────────────────────────────
async function _redisGet (key) {
  if (!_redisConfigured()) return _fileGet(key);
  try {
    const redis  = _getRedisClient();
    const result = await _withTimeout(redis.get(key));
    if (result === null || result === undefined) return null;
    // SDK auto-parses JSON strings stored by set().
    // Return the value as-is — callers that expect arrays guard with Array.isArray.
    return result;
  } catch (err) {
    console.error('[ghost/redis] get error key=%s name=%s message=%s', key, err.name, err.message);
    return null;
  }
}

async function _redisSet (key, value) {
  if (!_redisConfigured()) return _fileSet(key, value);
  try {
    const redis = _getRedisClient();
    // redis.set() serialises the value to JSON automatically.
    await _withTimeout(redis.set(key, value));
    return true;
  } catch (err) {
    console.error('[ghost/redis] set error key=%s name=%s message=%s', key, err.name, err.message);
    return false;
  }
}

async function _redisDel (key) {
  if (!_redisConfigured()) return false;
  try {
    const redis = _getRedisClient();
    await _withTimeout(redis.del(key));
    return true;
  } catch (err) {
    console.error('[ghost/redis] del error key=%s name=%s message=%s', key, err.name, err.message);
    return false;
  }
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
    'GHOST_API_URL', 'BASE_URL',
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
    const [inventory, activity] = await Promise.all([
      _redisGet('ghost:inventory'),
      _redisGet('ghost:activity'),
    ]);

    // Orders read directly from Redis — single source of truth
    let ordersArr = [];
    try {
      const redis = _redisConfigured() ? _getRedisClient() : null;
      if (redis) {
        const ids = await _withTimeout(redis.zrange('ghost:orders:index', 0, -1)).catch(() => []);
        if (Array.isArray(ids) && ids.length) {
          const recs = await Promise.all(
            ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
          );
          ordersArr = recs.filter(Boolean).map(r => typeof r === 'string' ? JSON.parse(r) : r);
        }
      }
    } catch (_) {}

    const inventoryArr = Array.isArray(inventory) ? inventory : [];
    const activityArr  = Array.isArray(activity)  ? activity  : [];

    const now       = new Date();
    const todayStr  = now.toISOString().slice(0, 10);
    const monthStr  = now.toISOString().slice(0, 7);

    const completed = ordersArr.filter(o =>
      o.payment_status === 'COMPLETED' || o.payment_status === 'verified' || o.payment_verified === true
    );
    const _amount = o => parseFloat(o.price_usd || o.amount || 0);
    const _date   = o => o.created_at || o.purchase_date || '';
    const revenueToday  = completed.filter(o => _date(o).startsWith(todayStr))
      .reduce((s, o) => s + _amount(o), 0);
    const revenueMonth  = completed.filter(o => _date(o).startsWith(monthStr))
      .reduce((s, o) => s + _amount(o), 0);
    const revenueTotal  = completed.reduce((s, o) => s + _amount(o), 0);

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

    const dailyRevenue   = recent30.map(d => completed.filter(o => _date(o).startsWith(d)).reduce((s, o) => s + _amount(o), 0));
    const dailyOrders    = recent30.map(d => ordersArr.filter(o => _date(o).startsWith(d)).length);
    const dailyCustomers = recent30.map(d => {
      const seen = new Set();
      completed.filter(o => _date(o).startsWith(d)).forEach(o => { if (o.email) seen.add(o.email); });
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
      failed_payments:ordersArr.filter(o => o.payment_status === 'payment_failed' || o.payment_status === 'FAILED').length,
      recent_orders:  completed.slice(-10).reverse(),
      recent_activity:activityArr.slice(-10).reverse(),
      // Admin.js reads cards.X — wrap in cards object
      cards: {
        revenue_today:   revenueToday,
        revenue_month:   revenueMonth,
        revenue_total:   revenueTotal,
        total_orders:    ordersArr.length,
        active_licenses: activeKeys,
        keys_remaining:  availKeys,
        pending_orders:  ordersArr.filter(o => o.delivery_status === 'delivery_pending').length,
        failed_payments: ordersArr.filter(o => o.payment_status === 'payment_failed' || o.payment_status === 'FAILED').length,
      },
      graph: { dates: recent30, revenue: dailyRevenue, orders: dailyOrders, customers: dailyCustomers },
      graphs: { labels: recent30, revenue: dailyRevenue, orders: dailyOrders, customers: dailyCustomers },
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
// Ghost key format: GHOST-XXXX-XXXX-XXXX-XXXX (4 groups of exactly 4 alphanumeric chars).
// _GHOST_KEY_RE is imported from ./assets/js/license-format.js — single source of truth.

/**
 * Normalize a plan label/slug to its canonical backend value.
 * Must mirror inventory.py normalize_plan() exactly.
 *
 * Canonical values: 'pro', 'lifetime', 'trial'
 */
const _PLAN_ALIASES = {
  pro:                 'pro',
  monthly:             'pro',
  ghost_pro_monthly:   'pro',
  'ghost pro monthly': 'pro',
  'ghost pro (monthly)': 'pro',
  ghost_pro:           'pro',
  'ghost pro':         'pro',
  lifetime:            'lifetime',
  ghost_lifetime:      'lifetime',
  'ghost lifetime':    'lifetime',
  trial:               'trial',
  ghost_trial:         'trial',
  'ghost trial':       'trial',
};

function _normalizePlan (plan) {
  if (!plan) return '';
  const key = String(plan).trim().toLowerCase();
  return _PLAN_ALIASES[key] || key;
}

app.get('/api/admin/inventory', _requireAdminSession, async (req, res) => {
  const raw = await _redisGet('ghost:inventory');

  // Defensive normalization: Redis may return an object, string, null, or
  // a nested structure if the value was ever stored in an unexpected format.
  let inventory = Array.isArray(raw) ? raw : [];
  if (!Array.isArray(raw)) {
    console.warn(
      '[ghost/inventory] GET /api/admin/inventory: raw value is not an array — ' +
      'typeof=%s isArray=%s topKeys=%s — coercing to []',
      typeof raw,
      Array.isArray(raw),
      raw && typeof raw === 'object' ? Object.keys(raw).slice(0, 10).join(',') : String(raw).slice(0, 80)
    );
  }

  // Apply optional server-side filters passed as query params
  const { status, plan, search } = req.query;
  if (status) inventory = inventory.filter(k => k.status === status);
  if (plan)   inventory = inventory.filter(k => k.plan   === plan);
  if (search) {
    const q = search.trim().toLowerCase();
    inventory = inventory.filter(k =>
      (k.key   || '').toLowerCase().includes(q) ||
      (k.customer || '').toLowerCase().includes(q) ||
      (k.notes    || '').toLowerCase().includes(q)
    );
  }

  // Stable schema: { ok, items, total }
  // "items" is always an array — never null, never an object.
  return res.json({ ok: true, items: inventory, total: inventory.length });
});

app.get('/api/admin/inventory/stats', _requireAdminSession, async (req, res) => {
  const _raw = await _redisGet('ghost:inventory');
  const inventory = Array.isArray(_raw) ? _raw : [];
  const counts = { available: 0, reserved: 0, sold: 0, activated: 0, revoked: 0, expired: 0 };
  inventory.forEach(k => { if (counts[k.status] !== undefined) counts[k.status]++; });
  return res.json({ ok: true, ...counts, total: inventory.length });
});

app.post('/api/admin/inventory/import', _requireAdminSession, async (req, res) => {
  // ── Hard deadline: respond within 10 s regardless of what hangs ──────────
  // This fires only if the main try/catch itself somehow stalls (defensive).
  let _responded = false;
  const _guardTimer = setTimeout(() => {
    if (!_responded) {
      _responded = true;
      console.error('[inventory/import] FAILED stage=guard name=Timeout message=handler exceeded 10 s');
      res.status(503).json({ ok: false, error: 'redis_timeout' });
    }
  }, 10_000);

  try {
    // ── stage: request_received ───────────────────────────────────────────
    console.log('[inventory/import] request_received');

    const { keys, plan = 'pro', notes = '' } = req.body || {};

    if (!Array.isArray(keys) || !keys.length) {
      clearTimeout(_guardTimer);
      _responded = true;
      return res.status(400).json({ ok: false, error: 'keys array required.' });
    }

    // ── stage: parsed ─────────────────────────────────────────────────────
    console.log('[inventory/import] parsed count=%d', keys.length);

    // ── stage: redis_client_ready ─────────────────────────────────────────
    console.log('[inventory/import] redis_client_ready configured=%s', _redisConfigured());

    let inventory;
    try {
      inventory = await _redisGet('ghost:inventory');
    } catch (err) {
      console.error('[inventory/import] FAILED stage=redis_read name=%s message=%s', err.name, err.message);
      // If the read itself times out return a clear error — do NOT proceed.
      if (err.message === 'redis_timeout') {
        clearTimeout(_guardTimer);
        _responded = true;
        return res.status(503).json({ ok: false, error: 'redis_timeout' });
      }
      inventory = null;
    }
    if (!Array.isArray(inventory)) inventory = [];

    const existing      = new Set(inventory.map(k => k && k.key).filter(Boolean));
    const added         = [];
    const duplicateKeys = [];
    const invalidKeys   = [];
    const now           = new Date().toISOString();

    for (const raw of keys) {
      const k = String(raw).trim().toUpperCase();
      if (!k) continue;
      if (!_GHOST_KEY_RE.test(k)) { invalidKeys.push(k);  continue; }
      if (existing.has(k))        { duplicateKeys.push(k); continue; }
      inventory.push({
        key:          k,
        plan:         _normalizePlan(plan || 'pro'),
        status:       'available',
        customer:     null,
        hwid:         null,
        purchaseDate: null,
        created:      now,
        expiration:   null,
        notes:        notes || '',
        // legacy aliases kept so existing callers that read these fields still work
        created_date:   now,
        added_at:       now,
        purchase_date:  '',
        order_id:       '',
        customer_email: '',
        assigned_user:  '',
      });
      existing.add(k);
      added.push(k);
    }

    const imported   = added.length;
    const duplicates = duplicateKeys.length;
    const invalid    = invalidKeys.length;

    // ── stage: write_started ──────────────────────────────────────────────
    if (imported > 0) {
      console.log('[inventory/import] write_started count=%d total=%d', imported, inventory.length);

      let saved = false;
      try {
        saved = await _redisSet('ghost:inventory', inventory);
      } catch (err) {
        console.error('[inventory/import] FAILED stage=redis_write name=%s message=%s', err.name, err.message);
        if (err.message === 'redis_timeout') {
          clearTimeout(_guardTimer);
          _responded = true;
          return res.status(503).json({ ok: false, error: 'redis_timeout' });
        }
        saved = false;
      }

      // ── stage: write_completed ────────────────────────────────────────
      console.log('[inventory/import] write_completed saved=%s', saved);

      if (!saved) {
        clearTimeout(_guardTimer);
        _responded = true;
        console.error('[inventory/import] FAILED stage=redis_write name=Error message=storage_write_failed');
        return res.status(500).json({ ok: false, error: 'storage_write_failed' });
      }
    }

    // ── stage: inventory_count ────────────────────────────────────────────
    console.log('[inventory/import] inventory_count=%d', inventory.length);

    // ── stage: response_sent ──────────────────────────────────────────────
    clearTimeout(_guardTimer);
    _responded = true;
    console.log('[inventory/import] response_sent imported=%d duplicates=%d invalid=%d',
      imported, duplicates, invalid);

    return res.json({
      ok:              true,
      imported,
      duplicates,
      invalid,
      imported_count:  imported,
      duplicate_count: duplicates,
      invalid_count:   invalid,
      added:           imported,
      skipped:         duplicates,
    });

  } catch (err) {
    clearTimeout(_guardTimer);
    if (!_responded) {
      _responded = true;
      console.error('[inventory/import] FAILED stage=unhandled name=%s message=%s\n%s',
        err.name, err.message, err.stack);
      return res.status(500).json({ ok: false, error: err.message });
    }
  }
});

// ── POST /api/admin/inventory/generate — cryptographic key generator ─────────
// Generates N random license keys, deduplicates against existing inventory,
// saves to Redis in one atomic write, and returns all generated keys + stats.
//
// Body: { plan, quantity, prefix, format, charTypes, expiration, notes }
// Response: { ok, generated, duplicates, keys[], availableInventory }
//
// Performance target: 100 keys < 500 ms, 1000 keys < 2 s
app.post('/api/admin/inventory/generate', _requireAdminSession, async (req, res) => {
  const t0 = Date.now();
  let _responded = false;
  const _guard = setTimeout(() => {
    if (!_responded) {
      _responded = true;
      console.error('[inventory/generate] guard timeout exceeded 12 s');
      res.status(503).json({ ok: false, error: 'redis_timeout' });
    }
  }, 12_000);

  try {
    const {
      plan       = 'pro',
      quantity   = 100,
      prefix     = 'GHOST',
      format     = 'seg4x4',
      charTypes  = { upper: true, numbers: true, symbols: false },
      expiration = 'never',
      notes      = '',
    } = req.body || {};

    // ── Validation ──────────────────────────────────────────────────────────
    const qty = Math.max(1, Math.min(10000, parseInt(quantity, 10) || 100));

    const rawPrefix = String(prefix || 'GHOST').trim().toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 16) || 'GHOST';

    const useUpper  = charTypes && charTypes.upper   !== false;
    const useNum    = charTypes && charTypes.numbers  !== false;
    const useSym    = charTypes && charTypes.symbols  === true;

    // Must have at least one char type
    if (!useUpper && !useNum && !useSym) {
      clearTimeout(_guard); _responded = true;
      return res.status(400).json({ ok: false, error: 'At least one character type must be selected.' });
    }

    // Build alphabet
    let alphabet = '';
    if (useUpper)  alphabet += 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    if (useNum)    alphabet += '0123456789';
    if (useSym)    alphabet += '!@#$%^&*';

    // Segment sizes per format
    const segMap = {
      seg4x4:  [4, 4, 4, 4],
      seg3x5:  [5, 5, 5],
      seg1x12: [12],
      custom:  [4, 4, 4, 4],
    };
    const segs = segMap[format] || segMap['seg4x4'];

    // Expiration ISO string
    let expirationISO = null;
    const expiryDays  = parseInt(expiration, 10);
    if (!isNaN(expiryDays) && expiryDays > 0) {
      const d = new Date();
      d.setDate(d.getDate() + expiryDays);
      expirationISO = d.toISOString();
    }

    // ── Read existing inventory (for dupe check) ────────────────────────────
    let inventory;
    try {
      inventory = await _redisGet('ghost:inventory');
    } catch (err) {
      if (err.message === 'redis_timeout') {
        clearTimeout(_guard); _responded = true;
        return res.status(503).json({ ok: false, error: 'redis_timeout' });
      }
      inventory = null;
    }
    if (!Array.isArray(inventory)) inventory = [];

    const existing = new Set(inventory.map(k => k && k.key).filter(Boolean));

    // ── Generate keys ───────────────────────────────────────────────────────
    function _randSegment (len) {
      const bytes = crypto.randomBytes(len * 2);   // extra headroom
      let out = '';
      for (let i = 0; i < bytes.length && out.length < len; i++) {
        const ch = alphabet[bytes[i] % alphabet.length];
        out += ch;
      }
      return out;
    }

    function _buildKey () {
      return rawPrefix + '-' + segs.map(n => _randSegment(n)).join('-');
    }

    const now        = new Date().toISOString();
    const generated  = [];
    const newKeys    = [];
    let   duplicates = 0;
    const MAX_ATTEMPTS = qty * 8;   // safety valve against infinite loops
    let   attempts     = 0;

    while (newKeys.length < qty && attempts < MAX_ATTEMPTS) {
      attempts++;
      const k = _buildKey();
      if (existing.has(k)) { duplicates++; continue; }
      existing.add(k);
      generated.push(k);
      const record = {
        key:          k,
        plan:         _normalizePlan(plan),
        status:       'available',
        customer:     null,
        hwid:         null,
        purchaseDate: null,
        created:      now,
        expiration:   expirationISO,
        notes:        String(notes || '').slice(0, 200),
        // legacy aliases
        created_date:   now,
        added_at:       now,
        purchase_date:  '',
        order_id:       '',
        customer_email: '',
        assigned_user:  '',
      };
      inventory.push(record);
      newKeys.push(record);
    }

    // ── Save to Redis ────────────────────────────────────────────────────────
    const writeStart = Date.now();
    let saved = false;
    try {
      saved = await _redisSet('ghost:inventory', inventory);
    } catch (err) {
      if (err.message === 'redis_timeout') {
        clearTimeout(_guard); _responded = true;
        return res.status(503).json({ ok: false, error: 'redis_timeout' });
      }
      saved = false;
    }

    if (!saved) {
      clearTimeout(_guard); _responded = true;
      console.error('[inventory/generate] redis write failed');
      return res.status(500).json({ ok: false, error: 'storage_write_failed' });
    }

    const saveDuration = Date.now() - writeStart;
    const totalMs      = Date.now() - t0;

    // Count available for the response stat
    const availableCount = inventory.filter(k => k.status === 'available').length;

    console.log(
      '[inventory/generate] generated=%d duplicates=%d save_duration=%dms total=%dms',
      generated.length, duplicates, saveDuration, totalMs
    );

    clearTimeout(_guard);
    _responded = true;
    return res.status(201).json({
      ok:               true,
      generated:        generated.length,
      duplicates,
      keys:             generated,
      availableInventory: availableCount,
      plan:             String(plan).toLowerCase(),
      prefix:           rawPrefix,
      expiration:       expirationISO,
      save_duration_ms: saveDuration,
      total_ms:         totalMs,
    });

  } catch (err) {
    clearTimeout(_guard);
    if (!_responded) {
      _responded = true;
      console.error('[inventory/generate] unhandled error name=%s message=%s\n%s',
        err.name, err.message, err.stack);
      return res.status(500).json({ ok: false, error: err.message });
    }
  }
});

// ── GET /api/admin/storage-test — diagnostic endpoint ────────────────────────
// Verifies Upstash credentials and round-trip independently from inventory code.
// Protected by admin session. Returns { ok, write, read, delete }.
app.get('/api/admin/storage-test', _requireAdminSession, async (req, res) => {
  if (!_redisConfigured()) {
    return res.status(503).json({ ok: false, error: 'redis_not_configured' });
  }
  const testKey = 'ghost:storage_test_tmp';
  const testVal = { ts: Date.now() };
  let writeOk = false;
  let readOk  = false;
  let delOk   = false;
  try {
    const redis = _getRedisClient();

    // write
    try {
      await _withTimeout(redis.set(testKey, testVal));
      writeOk = true;
    } catch (err) {
      console.error('[storage-test] write error name=%s message=%s', err.name, err.message);
    }

    // read
    if (writeOk) {
      try {
        const got = await _withTimeout(redis.get(testKey));
        readOk = got !== null && got !== undefined;
      } catch (err) {
        console.error('[storage-test] read error name=%s message=%s', err.name, err.message);
      }
    }

    // delete
    try {
      await _withTimeout(redis.del(testKey));
      delOk = true;
    } catch (err) {
      console.error('[storage-test] del error name=%s message=%s', err.name, err.message);
    }

    const ok = writeOk && readOk && delOk;
    return res.status(ok ? 200 : 500).json({ ok, write: writeOk, read: readOk, delete: delOk });
  } catch (err) {
    console.error('[storage-test] unexpected error name=%s message=%s', err.name, err.message);
    return res.status(500).json({ ok: false, error: err.message });
  }
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

// ── Orders endpoints — proxy to Python delivery backend ──────────────────────
// The delivery backend is the authoritative store for all orders (ghost:order:{id}).
// The Node server never writes order records — it only reads them via these proxies.

app.get('/api/admin/orders', _requireAdminSession, async (req, res) => {
  // Read directly from Redis — single source of truth.
  let redisOrders = [];
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const ids = await _withTimeout(redis.zrange('ghost:orders:index', 0, -1)).catch(() => []);
      if (Array.isArray(ids) && ids.length) {
        const records = await Promise.all(
          ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
        );
        redisOrders = records.filter(Boolean).map(r => typeof r === 'string' ? JSON.parse(r) : r);
      }
    } catch (err) {
      console.error('[ghost/admin] Redis orders read error:', err.message);
    }
  }

  return res.json({ ok: true, orders: redisOrders, total: redisOrders.length });
});

app.get('/api/admin/orders/:orderId', _requireAdminSession, async (req, res) => {
  const orderId = req.params.orderId;
  try {
    const redis = _redisConfigured() ? _getRedisClient() : null;
    if (!redis) return res.status(404).json({ ok: false, error: 'Order not found.' });
    const raw = await _withTimeout(redis.get(`ghost:order:${orderId}`)).catch(() => null);
    if (!raw) return res.status(404).json({ ok: false, error: 'Order not found.' });
    const order = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return res.json({ ok: true, order });
  } catch (_) {
    return res.status(404).json({ ok: false, error: 'Order not found.' });
  }
});

// ── POST /api/admin/orders/fulfill-pending ────────────────────────────────────
// Retry license delivery for every paid order that does not yet have a key.
// Uses the same fulfillOrder() function — never re-charges.
app.post('/api/admin/orders/fulfill-pending', _requireAdminSession, async (req, res) => {
  // Gather all orders from Redis
  let allOrders = [];
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const ids   = await _withTimeout(redis.zrange('ghost:orders:index', 0, -1)).catch(() => []);
      if (Array.isArray(ids) && ids.length) {
        const records = await Promise.all(
          ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
        );
        allOrders = records.filter(Boolean).map(r => typeof r === 'string' ? JSON.parse(r) : r);
      }
    } catch (err) {
      console.error('[ghost/admin] fulfill-pending: Redis read error:', err.message);
    }
  }

  // Filter to orders that are paid but key not yet assigned
  const pending = allOrders.filter(o => {
    const ps = (o.payment_status || '').toLowerCase();
    const ds = (o.delivery_status || '').toLowerCase();
    const paid = ps === 'completed' || ps === 'verified' || ps === 'captured';
    const noKey = !o.license_key || ds === 'pending' || ds === 'delivery_pending' || ds === 'out_of_stock';
    return paid && noKey;
  });

  if (!pending.length) {
    return res.json({ ok: true, fulfilled: 0, failed: 0, skipped: allOrders.length, message: 'No pending orders found.' });
  }

  let fulfilled = 0;
  let failed    = 0;
  const skipped = allOrders.length - pending.length;

  for (const order of pending) {
    const orderId = order.order_id;
    try {
      const result = await fulfillOrder(orderId);
      if (result.ok) {
        fulfilled++;
        console.log('[ghost/admin] fulfill-pending: fulfilled orderId=%s', orderId);
      } else {
        failed++;
        console.warn('[ghost/admin] fulfill-pending: could not fulfill orderId=%s reason=%s',
          orderId, result.reason || 'unknown');
      }
    } catch (err) {
      failed++;
      console.error('[ghost/admin] fulfill-pending: error for orderId=%s: %s', orderId, err.message);
    }
  }

  return res.json({ ok: true, fulfilled, failed, skipped, total: allOrders.length });
});

// ── Customer Licenses — sold keys from inventory ──────────────────────────────
app.get('/api/admin/customer-licenses', _requireAdminSession, async (req, res) => {
  const inventory = await _redisGet('ghost:inventory') || [];
  const sold = inventory.filter(k => ['sold', 'activated'].includes(k.status));
  return res.json({ ok: true, licenses: sold, total: sold.length });
});

// ── Customers endpoints ───────────────────────────────────────────────────────
app.get('/api/admin/customers', _requireAdminSession, async (req, res) => {
  // Build customer list from inventory (sold keys) + Redis orders — single source of truth
  const inventoryRaw = await _redisGet('ghost:inventory');
  const inventory = Array.isArray(inventoryRaw) ? inventoryRaw : [];

  // Load all orders from Redis
  let orders = [];
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const ids = await _withTimeout(redis.zrange('ghost:orders:index', 0, -1)).catch(() => []);
      if (Array.isArray(ids) && ids.length) {
        const records = await Promise.all(
          ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
        );
        orders = records.filter(Boolean).map(r => typeof r === 'string' ? JSON.parse(r) : r);
      }
    } catch (_) {}
  }

  const map = {};

  // Seed from orders
  for (const o of orders) {
    const email = (o.email || '').toLowerCase();
    if (!email) continue;
    if (!map[email]) {
      map[email] = { email: o.email || email, discord: o.discord || '', orders: [],
        total_spent: 0, active_licenses: 0,
        first_purchase: o.created_at || o.purchase_date,
        last_purchase:  o.created_at || o.purchase_date };
    }
    const c = map[email];
    c.orders.push(o);
    c.total_spent += parseFloat(o.price_usd || 0);
    if (o.license_key && ['active','sold','activated'].includes(o.license_status || o.delivery_status))
      c.active_licenses++;
    const d = o.created_at || o.purchase_date || '';
    if (d && (!c.first_purchase || d < c.first_purchase)) c.first_purchase = d;
    if (d && d > c.last_purchase) c.last_purchase = d;
  }

  // Enrich / merge from inventory sold keys
  for (const k of inventory) {
    if (!['sold', 'activated'].includes(k.status)) continue;
    const email = (k.customer_email || k.customer || '').toLowerCase();
    if (!email) continue;
    if (!map[email]) {
      map[email] = { email: k.customer_email || k.customer || email,
        discord: k.assigned_user || '', orders: [],
        total_spent: 0, active_licenses: 0,
        first_purchase: k.purchase_date || k.created_date,
        last_purchase:  k.purchase_date || k.created_date };
    }
    map[email].active_licenses = Math.max(
      map[email].active_licenses,
      inventory.filter(i => ['sold','activated'].includes(i.status) &&
        (i.customer_email || i.customer || '').toLowerCase() === email).length
    );
  }

  const customers = Object.values(map).map(c => ({
    ...c,
    total_orders: c.orders.length,
  }));
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
    version: '—', filename: 'GhostConfig.exe', url: '/dl/GhostConfig.exe',
    changelog: '', release_date: '', download_count: 0, history: [],
  };
  // current_version is an alias for version (admin.js reads current_version)
  return res.json({ ok: true, ...dl, current_version: dl.version || '—' });
});

app.post('/api/admin/downloads', _requireAdminSession, async (req, res) => {
  // admin.js sends current_version; also accept version for backwards compat
  const body    = req.body || {};
  const version = (body.current_version || body.version || '').trim();
  const { filename, url, changelog, release_date } = body;
  if (!version || !url) return res.status(400).json({ ok: false, error: 'version and url required.' });
  const prev = await _redisGet('ghost:downloads') || { history: [], download_count: 0 };
  const history = prev.history || [];
  if (prev.version && prev.version !== '—') {
    history.unshift({ version: prev.version, filename: prev.filename, url: prev.url,
      changelog: prev.changelog, release_date: prev.release_date,
      replaced_at: new Date().toISOString() });
  }
  const updated = { version, filename: filename || 'GhostConfig.exe', url: url || '/dl/GhostConfig.exe',
    changelog: changelog || '',
    release_date: release_date || new Date().toISOString().slice(0, 10),
    download_count: prev.download_count || 0, history };
  await _redisSet('ghost:downloads', updated);
  return res.json({ ok: true, ...updated, current_version: updated.version });
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
  const stored = await _redisGet('ghost:settings') || {};
  // Never return secrets — strip any that might have been stored
  const { paypal_client_secret, admin_key, ...safe } = stored;
  // Merge env-based PayPal config so the admin panel always shows the live values.
  // Env vars are the authoritative source; stored values are only used as fallback
  // when env vars are absent (e.g. the admin has manually set them in the panel).
  const envClientId = (process.env.PAYPAL_CLIENT_ID || '').trim();
  const envEnv      = (process.env.PAYPAL_ENVIRONMENT || '').trim().toLowerCase();
  if (envClientId) safe.paypal_client_id   = envClientId;
  if (envEnv)      safe.paypal_environment = envEnv;
  return res.json({ ok: true, settings: safe });
});

app.post('/api/admin/settings', _requireAdminSession, async (req, res) => {
  const prev = await _redisGet('ghost:settings') || {};
  // Never allow overwriting secrets through this endpoint
  const { paypal_client_secret, admin_key, ...updates } = req.body || {};
  const merged = { ...prev, ...updates };
  await _redisSet('ghost:settings', merged);
  const { paypal_client_secret: _s, admin_key: _k, ...safe } = merged;
  return res.json({ ok: true, settings: safe });
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

// ── Fulfillment Diagnostics — Redis-based log ─────────────────────────────────
// Returns recent orders with their fulfillment state from Redis.
app.get('/api/admin/fulfillment-log', _requireAdminSession, async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit || '20', 10) || 20, 50);

  let attempts = [];
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      // Most-recent orders: reverse the sorted set
      const ids = await _withTimeout(redis.zrange('ghost:orders:index', 0, limit - 1, { rev: true })).catch(() => []);
      if (Array.isArray(ids) && ids.length) {
        const records = await Promise.all(
          ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
        );
        attempts = records.filter(Boolean)
          .map(r => typeof r === 'string' ? JSON.parse(r) : r)
          .map(o => ({
            order_id:        o.order_id,
            plan:            o.plan,
            email:           o.email,
            payment_status:  o.payment_status,
            delivery_status: o.delivery_status,
            license_key:     o.license_key ? '[present]' : null,
            failure_reason:  o.failure_reason || null,
            created_at:      o.created_at,
            fulfilled_at:    o.fulfilled_at || null,
          }));
      }
    } catch (err) {
      console.warn('[ghost/admin] fulfillment-log Redis error:', err.message);
    }
  }

  return res.json({ ok: true, attempts, total: attempts.length });
});

// ── Coupon management endpoints ───────────────────────────────────────────────
// Redis key: ghost:coupons — array of coupon objects
// Redis key: ghost:coupon:uses:{code} — hash of email → use count (for per-customer limits)

function _normalizeCouponCode (code) {
  return String(code || '').trim().toUpperCase().replace(/[^A-Z0-9_-]/g, '');
}

function _applyCoupon (coupon, priceUsd) {
  const base = parseFloat(priceUsd) || 0;
  let discount = 0;
  if (coupon.discount_type === 'percentage') {
    discount = Math.round(base * (parseFloat(coupon.discount_value) / 100) * 100) / 100;
  } else if (coupon.discount_type === 'fixed') {
    discount = Math.min(parseFloat(coupon.discount_value) || 0, base);
  } else if (coupon.discount_type === 'free') {
    discount = base;
  }
  const finalPrice = Math.max(0, Math.round((base - discount) * 100) / 100);
  return { originalPrice: base, discount: Math.round(discount * 100) / 100, finalPrice };
}

// GET /api/admin/coupons — list all coupons
app.get('/api/admin/coupons', _requireAdminSession, async (req, res) => {
  const coupons = await _redisGet('ghost:coupons') || [];
  return res.json({ ok: true, coupons, total: coupons.length });
});

// POST /api/admin/coupons — create coupon
app.post('/api/admin/coupons', _requireAdminSession, async (req, res) => {
  const {
    code, discount_type, discount_value, applies_to = 'all',
    usage_limit = null, usage_per_customer = null,
    start_date = null, expiration_date = null,
    active = true, notes = '',
  } = req.body || {};

  const normalizedCode = _normalizeCouponCode(code);
  if (!normalizedCode) {
    return res.status(400).json({ ok: false, error: 'Coupon code is required.' });
  }
  if (!['percentage', 'fixed', 'free'].includes(discount_type)) {
    return res.status(400).json({ ok: false, error: 'Invalid discount_type. Use percentage, fixed, or free.' });
  }
  if (discount_type !== 'free') {
    const val = parseFloat(discount_value);
    if (isNaN(val) || val <= 0) {
      return res.status(400).json({ ok: false, error: 'discount_value must be a positive number.' });
    }
    if (discount_type === 'percentage' && val > 100) {
      return res.status(400).json({ ok: false, error: 'Percentage discount cannot exceed 100.' });
    }
  }

  const coupons = await _redisGet('ghost:coupons') || [];
  if (coupons.find(c => c.code === normalizedCode)) {
    return res.status(409).json({ ok: false, error: `Coupon code "${normalizedCode}" already exists.` });
  }

  const newCoupon = {
    code:                normalizedCode,
    discount_type,
    discount_value:      discount_type === 'free' ? 100 : parseFloat(discount_value),
    applies_to,
    usage_limit:         usage_limit != null ? parseInt(usage_limit, 10) : null,
    usage_per_customer:  usage_per_customer != null ? parseInt(usage_per_customer, 10) : null,
    uses:                0,
    start_date:          start_date || null,
    expiration_date:     expiration_date || null,
    active:              Boolean(active),
    notes,
    created_at:          new Date().toISOString(),
  };

  coupons.push(newCoupon);
  await _redisSet('ghost:coupons', coupons);
  return res.json({ ok: true, coupon: newCoupon });
});

// PATCH /api/admin/coupons/:code — update coupon
app.patch('/api/admin/coupons/:code', _requireAdminSession, async (req, res) => {
  const target = _normalizeCouponCode(req.params.code);
  const coupons = await _redisGet('ghost:coupons') || [];
  const idx = coupons.findIndex(c => c.code === target);
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Coupon not found.' });
  const allowed = ['discount_type','discount_value','applies_to','usage_limit',
    'usage_per_customer','start_date','expiration_date','active','notes'];
  const updates = {};
  for (const k of allowed) {
    if (req.body[k] !== undefined) updates[k] = req.body[k];
  }
  Object.assign(coupons[idx], updates);
  await _redisSet('ghost:coupons', coupons);
  return res.json({ ok: true, coupon: coupons[idx] });
});

// DELETE /api/admin/coupons/:code — delete coupon
app.delete('/api/admin/coupons/:code', _requireAdminSession, async (req, res) => {
  const target = _normalizeCouponCode(req.params.code);
  const coupons = await _redisGet('ghost:coupons') || [];
  const idx = coupons.findIndex(c => c.code === target);
  if (idx === -1) return res.status(404).json({ ok: false, error: 'Coupon not found.' });
  coupons.splice(idx, 1);
  await _redisSet('ghost:coupons', coupons);
  return res.json({ ok: true });
});

// POST /api/coupons/validate — public endpoint: validate + calculate discount
// Called by checkout frontend. Server-authoritative; never trusts client-submitted discount.
app.post('/api/coupons/validate', async (req, res) => {
  const { code, plan: planRaw, email = '' } = req.body || {};
  const normalizedCode = _normalizeCouponCode(code);
  if (!normalizedCode) {
    return res.status(400).json({ ok: false, message: 'Coupon code is required.' });
  }

  // Find plan price
  const planId = (planRaw || '').trim().toLowerCase();
  const { PLAN_CATALOGUE: PP } = require('./api/paypal');
  const plan = PP[planId];
  if (!plan) return res.status(400).json({ ok: false, message: 'Invalid plan.' });

  const coupons = await _redisGet('ghost:coupons') || [];
  const coupon = coupons.find(c => c.code === normalizedCode);

  if (!coupon) return res.json({ ok: false, message: 'Invalid coupon code.' });
  if (!coupon.active) return res.json({ ok: false, message: 'This coupon is no longer active.' });

  // Expiration check
  const now = new Date();
  if (coupon.start_date && now < new Date(coupon.start_date)) {
    return res.json({ ok: false, message: 'This coupon is not yet active.' });
  }
  if (coupon.expiration_date && now > new Date(coupon.expiration_date + 'T23:59:59Z')) {
    return res.json({ ok: false, message: 'This coupon has expired.' });
  }

  // Usage limit check
  if (coupon.usage_limit != null && coupon.uses >= coupon.usage_limit) {
    return res.json({ ok: false, message: 'This coupon has reached its usage limit.' });
  }

  // Applies-to check
  if (coupon.applies_to !== 'all') {
    const allowed = Array.isArray(coupon.applies_to) ? coupon.applies_to : [coupon.applies_to];
    if (!allowed.includes(planId)) {
      return res.json({ ok: false, message: 'This coupon does not apply to the selected plan.' });
    }
  }

  // Per-customer check (if email provided)
  if (coupon.usage_per_customer != null && email) {
    const emailKey = 'ghost:coupon:uses:' + normalizedCode;
    let userUses = 0;
    if (_redisConfigured()) {
      try {
        const redis = _getRedisClient();
        const raw = await _withTimeout(redis.hget(emailKey, email.toLowerCase())).catch(() => null);
        userUses = parseInt(raw || '0', 10) || 0;
      } catch (_) {}
    }
    if (userUses >= coupon.usage_per_customer) {
      return res.json({ ok: false, message: 'You have already used this coupon the maximum number of times.' });
    }
  }

  const priceUsd = parseFloat(plan.priceUsd) || 0;
  const { originalPrice, discount, finalPrice } = _applyCoupon(coupon, priceUsd);

  return res.json({
    ok: true,
    couponCode: normalizedCode,
    discountType: coupon.discount_type,
    discountValue: coupon.discount_value,
    originalPrice,
    discount,
    finalPrice,
    isFree: finalPrice === 0,
    label: coupon.discount_type === 'free'
      ? '100% Off'
      : coupon.discount_type === 'percentage'
        ? `${coupon.discount_value}% Off`
        : `$${coupon.discount_value.toFixed(2)} Off`,
  });
});

// POST /api/coupons/redeem-free — redeem a 100%-off coupon without PayPal
// Protected: server validates coupon, increments usage atomically, runs fulfillOrder
app.post('/api/coupons/redeem-free', async (req, res) => {
  const { code, plan: planRaw, email, discord } = req.body || {};

  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ ok: false, message: 'A valid email address is required.' });
  }
  if (!discord || discord.trim().length < 2) {
    return res.status(400).json({ ok: false, message: 'Discord username is required.' });
  }

  const normalizedCode = _normalizeCouponCode(code);
  if (!normalizedCode) {
    return res.status(400).json({ ok: false, message: 'Coupon code is required.' });
  }

  const planId = (planRaw || '').trim().toLowerCase();
  const { PLAN_CATALOGUE: PP, fulfillOrder } = require('./api/paypal');
  const plan = PP[planId];
  if (!plan) return res.status(400).json({ ok: false, message: 'Invalid plan.' });

  // ── Atomic coupon validation + increment ────────────────────────────────────
  const coupons = await _redisGet('ghost:coupons') || [];
  const couponIdx = coupons.findIndex(c => c.code === normalizedCode);
  if (couponIdx === -1) return res.status(400).json({ ok: false, message: 'Invalid coupon code.' });

  const coupon = coupons[couponIdx];
  if (!coupon.active) return res.status(400).json({ ok: false, message: 'This coupon is no longer active.' });

  const now = new Date();
  if (coupon.start_date && now < new Date(coupon.start_date)) {
    return res.status(400).json({ ok: false, message: 'This coupon is not yet active.' });
  }
  if (coupon.expiration_date && now > new Date(coupon.expiration_date + 'T23:59:59Z')) {
    return res.status(400).json({ ok: false, message: 'This coupon has expired.' });
  }
  if (coupon.usage_limit != null && coupon.uses >= coupon.usage_limit) {
    return res.status(400).json({ ok: false, message: 'This coupon has reached its usage limit.' });
  }

  // Applies-to check
  if (coupon.applies_to !== 'all') {
    const allowed = Array.isArray(coupon.applies_to) ? coupon.applies_to : [coupon.applies_to];
    if (!allowed.includes(planId)) {
      return res.status(400).json({ ok: false, message: 'This coupon does not apply to the selected plan.' });
    }
  }

  // Per-customer usage check
  const emailKey = 'ghost:coupon:uses:' + normalizedCode;
  if (coupon.usage_per_customer != null && _redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const raw = await _withTimeout(redis.hget(emailKey, email.toLowerCase())).catch(() => null);
      const userUses = parseInt(raw || '0', 10) || 0;
      if (userUses >= coupon.usage_per_customer) {
        return res.status(400).json({ ok: false, message: 'You have already used this coupon the maximum number of times.' });
      }
    } catch (_) {}
  }

  // Verify finalPrice === 0
  const priceUsd = parseFloat(plan.priceUsd) || 0;
  const { finalPrice } = _applyCoupon(coupon, priceUsd);
  if (finalPrice !== 0) {
    return res.status(400).json({ ok: false, message: 'This coupon does not make the order free.' });
  }

  // ── Increment coupon usage (before fulfillment to prevent race) ─────────────
  coupons[couponIdx].uses = (coupons[couponIdx].uses || 0) + 1;
  await _redisSet('ghost:coupons', coupons);

  // Track per-customer usage in Redis hash
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      await _withTimeout(redis.hincrby(emailKey, email.toLowerCase(), 1)).catch(() => {});
    } catch (_) {}
  }

  // ── Create internal $0 order ─────────────────────────────────────────────────
  const orderId = 'GHOST-FREE-' + Date.now().toString(36).toUpperCase();
  const orderRecord = {
    order_id:         orderId,
    plan:             planId,
    plan_label:       plan.label,
    tier:             plan.tier,
    email:            email.trim(),
    discord:          discord.trim(),
    price_usd:        0,
    currency:         'USD',
    created_at:       new Date().toISOString(),
    payment_status:   'completed',
    payment_method:   'coupon',
    payment_verified: true,
    delivery_status:  'pending',
    license_key:      null,
    license_status:   'pending',
    coupon_code:      normalizedCode,
    coupon_discount:  priceUsd,
  };

  await _redisSet(`ghost:order:${orderId}`, orderRecord);
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      await _withTimeout(redis.zadd('ghost:orders:index', { score: Math.floor(Date.now() / 1000), member: orderId })).catch(() => {});
    } catch (_) {}
  }

  // ── Fulfill the order (same path as paid orders) ─────────────────────────────
  const result = await fulfillOrder(orderId).catch(err => ({ ok: false, reason: err.message }));

  if (!result.ok) {
    // Decrement coupon usage back on fulfillment failure
    coupons[couponIdx].uses = Math.max(0, (coupons[couponIdx].uses || 1) - 1);
    await _redisSet('ghost:coupons', coupons);
    return res.status(503).json({ ok: false, message: 'No keys available at this time. Please try again later.' });
  }

  return res.json({ ok: true, orderId, licenseKey: result.licenseKey, tier: plan.tier });
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

// ── Order access token store (in-memory, short-lived) ────────────────────────
// Maps  token → { orderId, expiresAt }
// Tokens are generated during capture and passed to the success page URL.
// They expire after 2 hours — enough to cover any reasonable session.
const _orderAccessTokens = new Map();
const _ORDER_TOKEN_TTL_MS = 2 * 60 * 60 * 1000; // 2 hours

function _issueOrderToken (orderId) {
  const token = crypto.randomBytes(32).toString('hex');
  _orderAccessTokens.set(token, { orderId, expiresAt: Date.now() + _ORDER_TOKEN_TTL_MS });
  return token;
}

function _verifyOrderToken (token, orderId) {
  const entry = _orderAccessTokens.get(token);
  if (!entry) return false;
  if (Date.now() > entry.expiresAt) { _orderAccessTokens.delete(token); return false; }
  // Token must match the requested orderId
  return entry.orderId === orderId;
}

// Periodically clean up expired tokens (every 30 minutes)
setInterval(() => {
  const now = Date.now();
  for (const [t, e] of _orderAccessTokens) {
    if (now > e.expiresAt) _orderAccessTokens.delete(t);
  }
}, 30 * 60 * 1000);

// ── POST /api/order/:orderId/issue-token ──────────────────────────────────────
// Called immediately after capture to obtain a short-lived access token for the
// success page.  Order existence is verified in Redis before the token is issued.
app.post('/api/order/:orderId/issue-token', async (req, res) => {
  const orderId = req.params.orderId;
  if (!orderId) return res.status(400).json({ ok: false, error: 'orderId required.' });

  // Verify the order exists in Redis (prevents token farming)
  let found = false;
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const raw = await _withTimeout(redis.get(`ghost:order:${orderId}`)).catch(() => null);
      if (raw) found = true;
    } catch (_) {}
  }
  if (!found) return res.status(404).json({ ok: false, error: 'Order not found.' });

  const token = _issueOrderToken(orderId);
  return res.json({ ok: true, token });
});

// ── GET /api/order/:orderId ───────────────────────────────────────────────────
// License key is only returned when a valid per-order token is present
// OR the requester holds an active admin session.
// Without a token, non-sensitive order metadata is still returned so the
// dashboard and processing-screen pollers can read order status.
app.get('/api/order/:orderId', async (req, res) => {
  const orderId = req.params.orderId;
  const token   = req.query.token || '';

  // ── Determine whether the caller has elevated access ─────────────────────
  const hasAdminCookie = !!req.cookies?.[ADMIN_COOKIE_NAME];
  const adminOk = hasAdminCookie && _verifyAdminSession(req.cookies[ADMIN_COOKIE_NAME]);
  const tokenOk = token && _verifyOrderToken(token, orderId);
  const fullAccess = adminOk || tokenOk;   // can see license_key + sensitive fields

  // Read directly from Redis — single source of truth
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const raw = await _withTimeout(redis.get(`ghost:order:${orderId}`)).catch(() => null);
      if (raw) {
        const record = typeof raw === 'string' ? JSON.parse(raw) : raw;
        const safe = { ...record, ok: true };
        delete safe.payment_verified;
        if (!fullAccess) { delete safe.license_key; }
        if (!safe.download_url && safe.delivery_status === 'delivered') {
          safe.download_url = `/api/order/${encodeURIComponent(orderId)}/download`;
        }
        return res.json(safe);
      }
    } catch (_) {}
  }
  return res.status(404).json({ ok: false, error: 'Order not found.' });
});

// ── GET /api/order/:orderId/download ──────────────────────────────────────────
// Serves the download file directly (no delivery backend proxy needed).
app.get('/api/order/:orderId/download', async (req, res) => {
  const orderId = req.params.orderId;
  // Verify the order is delivered before allowing a download
  if (_redisConfigured()) {
    try {
      const redis = _getRedisClient();
      const raw = await _withTimeout(redis.get(`ghost:order:${orderId}`)).catch(() => null);
      if (raw) {
        const record = typeof raw === 'string' ? JSON.parse(raw) : raw;
        if (record.delivery_status === 'delivered' || record.license_key) {
          // Redirect to the direct download
          return res.redirect('/dl/GhostConfig.exe');
        }
      }
    } catch (_) {}
  }
  return res.status(403).json({ ok: false, error: 'Order not delivered.' });
});

// ── Checkout / order-success HTML ────────────────────────────────────────────
app.get('/checkout.html', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));
app.get('/checkout', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'checkout.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));
app.get('/order-success.html', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'order-success.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));
app.get('/order-success', (_req, res) => res.sendFile(path.join(WEB_ROOT, 'order-success.html'), err => {
  if (err) res.status(500).send('Internal server error');
}));

// ═════════════════════════════════════════════════════════════════════════════
// USER REGISTRATION & LOGIN — handled natively, stored in Upstash Redis
// Redis key: "ghost:users"  →  Array of user objects
// Each user: { id, username, email, passwordHash, passwordSalt, tier,
//              createdAt, licenseKey? }
//
// Password hashing: Node built-in crypto.scrypt  (64-byte key, 32-byte salt)
// No plaintext passwords are ever stored or logged.
// ═════════════════════════════════════════════════════════════════════════════

/** Derive a strong hash from password+salt using scrypt. */
// ── Customer session verifier ─────────────────────────────────────────────────
// Verifies the token issued by POST /api/auth/login (payload.sig format).
// Returns the decoded claims { sub, username, tier, iat } or null.
function _verifyCustomerToken (token) {
  if (!token || typeof token !== 'string') return null;
  // Strip "Bearer " prefix if caller forgot to strip it
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();

  const dot = token.lastIndexOf('.');
  if (dot < 1) return null;
  const payload = token.slice(0, dot);
  const sig     = token.slice(dot + 1);
  const sessionSecret = (process.env.ADMIN_SESSION_SECRET || '').trim();
  if (sessionSecret && sig !== 'nosig') {
    // Use base64url encoding for both sides of the comparison so buffer
    // lengths always match (base64url output is deterministic length).
    const expected = crypto.createHmac('sha256', sessionSecret).update(payload).digest('base64url');
    let ok = false;
    try {
      ok = crypto.timingSafeEqual(
        Buffer.from(sig,      'base64url'),
        Buffer.from(expected, 'base64url'),
      );
    } catch (_) {}
    if (!ok) return null;
  }
  try {
    return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
  } catch (_) { return null; }
}

function _hashPassword (password, salt) {
  return new Promise((resolve, reject) => {
    crypto.scrypt(password, salt, 64, { N: 16384, r: 8, p: 1 }, (err, derived) => {
      if (err) reject(err);
      else     resolve(derived.toString('hex'));
    });
  });
}

/** Constant-time comparison of two hex strings. */
function _safeCompare (a, b) {
  try {
    return crypto.timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(b, 'hex'));
  } catch (_) {
    return false;
  }
}

// ── POST /api/auth/register ────────────────────────────────────────────────────
// Body: { username, email, password, license_key? }
// Stores user in Upstash Redis (ghost:users array).
// Returns { ok: true } on success or { ok: false, error, field? } on failure.
app.post('/api/auth/register', async (req, res) => {
  console.log('[ghost/register] registration_received');

  const { username, email, password, license_key } = req.body || {};

  // ── Validation ────────────────────────────────────────────────────────────
  if (!username || typeof username !== 'string' || username.trim().length < 3) {
    return res.status(400).json({ ok: false, field: 'username', error: 'Username must be at least 3 characters.' });
  }
  if (!/^[a-zA-Z0-9_\-]+$/.test(username.trim())) {
    return res.status(400).json({ ok: false, field: 'username', error: 'Username may only contain letters, numbers, underscores, and hyphens.' });
  }
  if (!email || typeof email !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
    return res.status(400).json({ ok: false, field: 'email', error: 'A valid email address is required.' });
  }
  if (!password || typeof password !== 'string' || password.length < 8) {
    return res.status(400).json({ ok: false, field: 'password', error: 'Password must be at least 8 characters.' });
  }

  const cleanUsername = username.trim().toLowerCase();
  const cleanEmail    = email.trim().toLowerCase();

  console.log('[ghost/register] validation_passed username=%s', cleanUsername);

  try {
    // ── Load existing users ────────────────────────────────────────────────
    const raw   = await _redisGet('ghost:users');
    const users = Array.isArray(raw) ? raw : [];

    // ── Duplicate check ────────────────────────────────────────────────────
    if (users.some(u => u.email === cleanEmail)) {
      return res.status(409).json({ ok: false, field: 'email', error: 'An account with that email already exists.' });
    }
    if (users.some(u => u.username === cleanUsername)) {
      return res.status(409).json({ ok: false, field: 'username', error: 'That username is already taken.' });
    }

    // ── License key lookup (Redis is the source of truth) ─────────────────
    // No format pre-rejection: trim + uppercase then look up directly in Redis.
    let resolvedTier = 'free';
    const cleanKey = license_key ? license_key.trim().toUpperCase() : null;
    if (cleanKey) {
      console.log('[ghost/register] license_key_lookup key=%s', cleanKey);
      const inventory = await _redisGet('ghost:inventory');
      const inventoryArr = Array.isArray(inventory) ? inventory : [];
      const keyRecord = inventoryArr.find(k => k && k.key && k.key.toUpperCase() === cleanKey);
      if (!keyRecord) {
        console.log('[ghost/register] license_key_not_found key=%s', cleanKey);
        return res.status(400).json({ ok: false, field: 'license_key', error: 'License key not found.' });
      }
      // Revoked/banned keys are unusable
      if (keyRecord.status === 'revoked' || keyRecord.status === 'banned') {
        console.log('[ghost/register] license_key_revoked key=%s status=%s', cleanKey, keyRecord.status);
        return res.status(400).json({ ok: false, field: 'license_key', error: 'License key has been revoked.' });
      }
      // Expired keys are unusable
      if (keyRecord.expiration && new Date(keyRecord.expiration) < new Date()) {
        console.log('[ghost/register] license_key_expired key=%s', cleanKey);
        return res.status(400).json({ ok: false, field: 'license_key', error: 'License key has expired.' });
      }
      // Already activated/sold — check if bound to another account
      if (keyRecord.status === 'activated' || keyRecord.status === 'sold') {
        const alreadyBound = users.some(u => u.licenseKey === cleanKey);
        if (alreadyBound) {
          console.log('[ghost/register] license_key_already_in_use key=%s', cleanKey);
          return res.status(409).json({ ok: false, field: 'license_key', error: 'License key already in use.' });
        }
      }
      resolvedTier = keyRecord.plan || keyRecord.tier || 'pro';
      console.log('[ghost/register] license_key_valid key=%s tier=%s', cleanKey, resolvedTier);
    }

    // ── Hash password ──────────────────────────────────────────────────────
    const salt         = crypto.randomBytes(32).toString('hex');
    const passwordHash = await _hashPassword(password, salt);

    // ── Build user record ──────────────────────────────────────────────────
    const user = {
      id:           crypto.randomUUID(),
      username:     cleanUsername,
      email:        cleanEmail,
      passwordHash,
      passwordSalt: salt,
      tier:         resolvedTier,
      createdAt:    new Date().toISOString(),
      licenseKey:   cleanKey,
    };

    users.push(user);
    const saved = await _redisSet('ghost:users', users);

    if (!saved) {
      console.error('[ghost/register] user_save_failed username=%s', cleanUsername);
      return res.status(500).json({ ok: false, error: 'Account could not be saved. Please try again.' });
    }

    // ── Mark key as activated in inventory ────────────────────────────────
    if (cleanKey) {
      try {
        const inv = await _redisGet('ghost:inventory');
        const invArr = Array.isArray(inv) ? inv : [];
        const ki = invArr.findIndex(k => k && k.key && k.key.toUpperCase() === cleanKey);
        if (ki !== -1) {
          invArr[ki] = { ...invArr[ki], status: 'activated', customer_email: cleanEmail, activated_at: new Date().toISOString() };
          await _redisSet('ghost:inventory', invArr);
        }
      } catch (_) { /* non-fatal — user account is already saved */ }
    }

    console.log('[ghost/register] user_saved id=%s username=%s email=%s tier=%s', user.id, user.username, user.email, user.tier);
    console.log('[ghost/register] registration_complete username=%s', user.username);

    return res.status(201).json({ ok: true });

  } catch (err) {
    console.error('[ghost/register] error name=%s message=%s', err.name, err.message);
    return res.status(500).json({ ok: false, error: 'Registration failed. Please try again.' });
  }
});

// ── POST /api/auth/login ──────────────────────────────────────────────────────
// Body: { identity, password }  (identity = username OR email)
// Returns { ok: true, token, username, tier } on success.
app.post('/api/auth/login', async (req, res) => {
  const { identity, password } = req.body || {};

  if (!identity || typeof identity !== 'string' || !identity.trim()) {
    return res.status(400).json({ ok: false, field: 'identity', error: 'Username or email is required.' });
  }
  if (!password || typeof password !== 'string') {
    return res.status(400).json({ ok: false, field: 'password', error: 'Password is required.' });
  }

  const cleanIdentity = identity.trim().toLowerCase();

  try {
    const raw   = await _redisGet('ghost:users');
    const users = Array.isArray(raw) ? raw : [];

    const user = users.find(u =>
      u.username === cleanIdentity || u.email === cleanIdentity
    );

    if (!user) {
      return res.status(401).json({ ok: false, error: 'Invalid username or password.' });
    }

    const hash = await _hashPassword(password, user.passwordSalt);
    if (!_safeCompare(hash, user.passwordHash)) {
      return res.status(401).json({ ok: false, error: 'Invalid username or password.' });
    }

    // Issue a simple signed token for the customer session
    const sessionSecret = (process.env.ADMIN_SESSION_SECRET || '').trim();
    const iat     = Math.floor(Date.now() / 1000);
    const payload = Buffer.from(JSON.stringify({ sub: user.id, username: user.username, tier: user.tier, iat })).toString('base64url');
    const sig     = sessionSecret
      ? crypto.createHmac('sha256', sessionSecret).update(payload).digest('base64url')
      : 'nosig';
    const token   = `${payload}.${sig}`;

    return res.json({ ok: true, token, username: user.username, tier: user.tier });

  } catch (err) {
    console.error('[ghost/login] error name=%s message=%s', err.name, err.message);
    return res.status(500).json({ ok: false, error: 'Login failed. Please try again.' });
  }
});

// ── GET /api/download/current — public download redirect ─────────────────────
// Returns the current production download URL from Redis settings, or falls
// back to the bundled /dl/GhostConfig.exe if not configured in admin.
app.get('/api/download/current', async (_req, res) => {
  try {
    const dl = await _redisGet('ghost:downloads');
    const url = dl && dl.url ? dl.url : null;
    return res.json({ ok: true, url: url || '/dl/GhostConfig.exe', filename: (dl && dl.filename) || 'GhostConfig.exe' });
  } catch (_) {
    return res.json({ ok: true, url: '/dl/GhostConfig.exe', filename: 'GhostConfig.exe' });
  }
});

// ── GET /dl/GhostConfig.exe — production binary download ─────────────────────
// Serves the bundled GhostConfig.exe with a forced-download Content-Disposition.
// The admin can configure an external URL via the Downloads admin panel instead;
// this route is the self-hosted fallback when no external URL is set.
app.get('/dl/GhostConfig.exe', (req, res) => {
  const filePath = path.join(WEB_ROOT, 'downloads', 'GhostConfig.exe');
  res.setHeader('Content-Disposition', 'attachment; filename="GhostConfig.exe"');
  res.setHeader('Content-Type', 'application/octet-stream');
  res.sendFile(filePath, err => {
    if (err) {
      console.error('[ghost/download] GhostConfig.exe not found at', filePath);
      res.status(404).json({ ok: false, error: 'Download file not found. Please contact support.' });
    }
  });
});

// ── Shared customer auth middleware ───────────────────────────────────────────
// Used by /api/customer/dashboard and all /api/account/* aliases.
// Sets req.customerClaims on success or calls next() with 401 response.
function _requireCustomerSession (req, res, next) {
  console.log('[customer] request_received path=%s ip=%s', req.path, req.ip);
  const authHeader = req.headers['authorization'] || '';
  const rawToken   = authHeader.startsWith('Bearer ')
    ? authHeader.slice(7).trim()
    : (req.cookies && req.cookies['ghost_token']) || '';

  const sessionPresent = Boolean(rawToken);
  console.log('[customer] session_present=%s', sessionPresent);

  if (!sessionPresent) {
    return res.status(401).json({ ok: false, error: 'Authentication required.' });
  }

  const claims = _verifyCustomerToken(rawToken);
  const sessionValid = Boolean(claims);
  console.log('[customer] session_valid=%s', sessionValid);

  if (!sessionValid) {
    return res.status(401).json({ ok: false, error: 'Session expired or invalid.' });
  }

  console.log('[customer] user_id=%s', claims.sub || '');
  req.customerClaims = claims;
  next();
}

// ── GET /api/customer/dashboard ───────────────────────────────────────────────
// Authenticated customer dashboard data — reads from Redis directly.
// All tabs (License, Downloads, Purchases, Support, Settings) are served
// from one endpoint. Returns empty collections for new users with no data.
// ── Shared dashboard data handler — called by both /api/customer/dashboard and
// the /api/account/* aliases so all tabs use identical auth + Redis reads.
async function _serveDashboard (req, res) {
  const claims   = req.customerClaims;
  const userId   = claims.sub;
  const username = claims.username || '';

  try {
    // ── Load users from the same Redis key used by registration/login ─────
    const raw   = await _redisGet('ghost:users');
    const users = Array.isArray(raw) ? raw : [];
    // Match by user id (primary) or username (fallback for older tokens)
    const user  = users.find(u => u.id === userId || u.username === username);
    const emailPresent = Boolean(user && user.email);
    console.log('[customer] email_present=%s', emailPresent);
    const accountFound = Boolean(user);
    console.log('[customer] account_found=%s', accountFound);

    if (!accountFound) {
      return res.status(404).json({ ok: false, error: 'Account not found.' });
    }

    const licenseKey = user.licenseKey || null;

    // ── License ───────────────────────────────────────────────────────────
    let license = {
      key:         null,
      tier:        user.tier || 'free',
      status:      'none',
      activatedAt: user.createdAt || null,
      expiresAt:   null,
      valid:       false,
      banned:      false,
      expired:     false,
    };
    let licensesFound = 0;

    if (licenseKey) {
      const inventory = await _redisGet('ghost:inventory');
      const invArr    = Array.isArray(inventory) ? inventory : [];
      const keyRecord = invArr.find(k => k && k.key && k.key.toUpperCase() === licenseKey.toUpperCase());
      if (keyRecord) {
        licensesFound = 1;
        const now     = new Date();
        const expiry  = keyRecord.expiration ? new Date(keyRecord.expiration) : null;
        const expired = expiry ? now > expiry : false;
        const banned  = keyRecord.status === 'revoked';
        license = {
          key:         keyRecord.key,
          tier:        keyRecord.plan || keyRecord.tier || user.tier || 'pro',
          status:      banned ? 'banned' : expired ? 'expired' : (keyRecord.status === 'activated' ? 'active' : keyRecord.status || 'active'),
          activatedAt: keyRecord.activated_at || user.createdAt || null,
          expiresAt:   keyRecord.expiration || null,
          valid:       !banned && !expired,
          banned,
          expired,
        };
      }
    }
    console.log('[customer] licenses_count=%d', licensesFound);

    // ── Orders / Purchases ────────────────────────────────────────────────
    // A brand-new account legitimately has 0 orders — never error on empty.
    let orders = [];
    let ordersFound = 0;
    try {
      if (_redisConfigured()) {
        const redis = _getRedisClient();
        // Try sorted-set index first; fall back to flat ghost:orders list
        let ids = await _withTimeout(redis.zrange('ghost:orders:index', 0, -1)).catch(() => null);
        if (Array.isArray(ids) && ids.length) {
          const recs = await Promise.all(
            ids.map(id => _withTimeout(redis.get(`ghost:order:${id}`)).catch(() => null))
          );
          const allOrders = recs.filter(Boolean).map(r => typeof r === 'string' ? JSON.parse(r) : r);
          orders = allOrders.filter(o =>
            (o.email && user.email && o.email.toLowerCase() === user.email.toLowerCase()) ||
            (licenseKey && o.license_key && o.license_key.toUpperCase() === licenseKey.toUpperCase())
          );
          ordersFound = orders.length;
        } else {
          // Fallback: flat ghost:orders array
          const flat = await _withTimeout(redis.get('ghost:orders')).catch(() => null);
          if (Array.isArray(flat)) {
            orders = flat.filter(o =>
              (o.email && user.email && o.email.toLowerCase() === user.email.toLowerCase()) ||
              (licenseKey && o.license_key && o.license_key.toUpperCase() === licenseKey.toUpperCase())
            );
            ordersFound = orders.length;
          }
        }
      }
    } catch (_) { /* non-fatal — empty orders is valid for new accounts */ }
    console.log('[customer] orders_count=%d', ordersFound);

    // ── Downloads ─────────────────────────────────────────────────────────
    let downloads = [];
    let downloadsFound = 0;
    try {
      const dlData = await _redisGet('ghost:downloads');
      if (Array.isArray(dlData)) {
        downloads     = dlData;
        downloadsFound = dlData.length;
      }
    } catch (_) { /* non-fatal */ }

    // ── Settings ──────────────────────────────────────────────────────────
    const settings = await _redisGet('ghost:settings').catch(() => null) || {};

    // ── Format purchases list ─────────────────────────────────────────────
    const purchases = orders.map(o => ({
      orderId:       o.order_id || o.stripe_session_id || '',
      purchaseDate:  (o.created_at || o.purchase_date || '').slice(0, 10),
      plan:          o.plan_label || o.plan || '',
      planTier:      o.plan || 'pro',
      billingPeriod: o.plan === 'lifetime' ? 'Once' : o.plan === 'trial' ? '7 days' : 'Monthly',
      amount:        o.price_usd != null ? Number(o.price_usd) : 0,
      paymentStatus: (o.payment_status === 'COMPLETED' || o.payment_status === 'verified') ? 'paid' : o.payment_status || 'pending',
      licenseKey:    licenseKey || '',
      licenseStatus: license.valid ? 'active' : license.status,
      receiptToken:  `rcpt:${(o.order_id || '').replace(/^#/, '')}`,
    }));

    const payload = {
      ok:       true,
      username: user.username,
      email:    user.email || '',
      memberSince: (user.createdAt || '').slice(0, 10),
      tier:     user.tier || 'free',
      license,
      purchases,
      downloads,
      settings: {
        version:       settings.ghost_latest_version || 'v2.4.1',
        release_date:  settings.ghost_release_date   || '2025-07-01',
        download_url:  settings.download_url         || null,
      },
      // empty-state friendly counts — never throw just because counts are 0
      counts: {
        licenses:  licensesFound,
        orders:    ordersFound,
        downloads: downloadsFound,
      },
    };

    console.log('[customer] response_sent=true username=%s licenses=%d orders=%d',
      user.username, licensesFound, ordersFound);
    return res.json(payload);

  } catch (err) {
    console.error('[customer] dashboard_error name=%s message=%s', err.name, err.message);
    return res.status(500).json({ ok: false, error: 'Failed to load dashboard. Please try again.' });
  }
}

app.get('/api/customer/dashboard', _requireCustomerSession, (req, res) => _serveDashboard(req, res));

// ── /api/account/* aliases — same session system as /api/customer/dashboard ───
// Every alias authenticates via _requireCustomerSession and delegates to the
// same Redis data used by registration and the main dashboard endpoint.
// This ensures all tabs use one unified auth and one data source.

app.get('/api/account/me', _requireCustomerSession, async (req, res) => {
  const { sub: userId, username } = req.customerClaims;
  try {
    const raw   = await _redisGet('ghost:users');
    const users = Array.isArray(raw) ? raw : [];
    const user  = users.find(u => u.id === userId || u.username === username);
    if (!user) return res.status(404).json({ ok: false, error: 'Account not found.' });
    return res.json({ ok: true, id: user.id, username: user.username, email: user.email,
                      tier: user.tier, memberSince: (user.createdAt || '').slice(0, 10) });
  } catch (err) {
    console.error('[customer] /api/account/me error:', err.message);
    return res.status(500).json({ ok: false, error: 'Failed to load account.' });
  }
});

// /api/account/dashboard, /api/account/licenses, /api/account/orders,
// /api/account/downloads, /api/account/settings all delegate to the shared
// dashboard handler so a single Redis read serves all tabs.
['dashboard', 'licenses', 'orders', 'downloads', 'settings'].forEach(tab => {
  app.get(`/api/account/${tab}`, _requireCustomerSession, (req, res) => _serveDashboard(req, res));
});

// ── POST /api/auth/logout — native handler (no proxy needed) ─────────────────
// Clears the ghost_token cookie. The JS client also removes it from localStorage.
// Defined before the catch-all proxy so it runs natively without a Python backend.
app.post('/api/auth/logout', (req, res) => {
  res.clearCookie('ghost_token', { path: '/', sameSite: 'Lax' });
  console.log('[customer] logout ip=%s', req.ip);
  return res.json({ ok: true });
});

// ── Ghost shared Python API proxy routes ──────────────────────────────────────
// Auth + customer-facing routes only — admin routes are handled natively above.
// NOTE: /api/auth/register, /api/auth/login, /api/auth/logout are handled natively
//       above. Remaining /api/auth/* sub-routes (if any) proxy to Python backend
//       only if GHOST_API_URL is set.
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

app.use(express.static(path.join(WEB_ROOT, 'public')));

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
