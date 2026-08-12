/**
 * api/paypal.js — Ghost PayPal Checkout (Orders API v2)
 * ======================================================
 * All routes registered in server.js.
 *
 * Routes
 * ------
 *   POST /api/paypal/create-order      Create a PayPal order (server-controlled price)
 *   POST /api/paypal/capture-order     Capture an approved PayPal order + deliver license
 *   POST /api/paypal/webhook           PayPal webhook receiver (PAYMENT.CAPTURE.COMPLETED)
 *   POST /api/paypal/retry-fulfillment Retry/reissue license for a paid order
 *
 * Required env vars (never hardcode these):
 *   PAYPAL_CLIENT_ID        — from developer.paypal.com
 *   PAYPAL_CLIENT_SECRET    — NEVER expose in browser code
 *   PAYPAL_ENVIRONMENT      — 'sandbox' or 'live'  (default: sandbox)
 *   PAYPAL_WEBHOOK_ID       — webhook ID from PayPal dashboard (for signature verification)
 *   UPSTASH_REDIS_REST_URL  — Upstash Redis REST endpoint
 *   UPSTASH_REDIS_REST_TOKEN— Upstash Redis REST bearer token
 *   BASE_URL                — public URL of this web server
 *
 * NOTE: There is NO separate delivery backend. Fulfillment reads the same
 * ghost:inventory key used by Admin → Key Inventory and assigns a key directly.
 */

'use strict';

const PAYPAL_CLIENT_ID     = process.env.PAYPAL_CLIENT_ID     || '';
const PAYPAL_CLIENT_SECRET = process.env.PAYPAL_CLIENT_SECRET || '';
const PAYPAL_ENV           = (process.env.PAYPAL_ENVIRONMENT  || 'sandbox').toLowerCase();
const PAYPAL_WEBHOOK_ID    = process.env.PAYPAL_WEBHOOK_ID    || '';

const PAYPAL_API_BASE = PAYPAL_ENV === 'live'
  ? 'https://api-m.paypal.com'
  : 'https://api-m.sandbox.paypal.com';

if (!PAYPAL_CLIENT_ID || !PAYPAL_CLIENT_SECRET) {
  console.error(
    '[ghost/paypal] FATAL: PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set. ' +
    'All PayPal routes will return 503 until they are configured.',
  );
}


/* ── Redis-direct order + inventory persistence ──────────────────────────────
   Single source of truth for both orders and key inventory.
   Keys:
     ghost:order:{order_id}      — the order JSON object
     ghost:orders:index          — sorted set of order IDs (score = unix timestamp)
     ghost:paypal-order:{ppId}   — maps PayPal order ID → our captureId
     ghost:inventory             — JSON array of all license keys
─────────────────────────────────────────────────────────────────────────── */
const _REDIS_URL   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
const _REDIS_TOKEN = (process.env.UPSTASH_REDIS_REST_TOKEN || '');

async function _redisSaveOrder (orderId, record) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return false;
  const { default: fetch } = await import('node-fetch');
  const pipeline = [
    ['SET', `ghost:order:${orderId}`, JSON.stringify(record)],
    ['ZADD', 'ghost:orders:index', String(Math.floor(Date.now() / 1000)), orderId],
  ];
  if (record.paypal_order_id) {
    pipeline.push(['SET', `ghost:paypal-order:${record.paypal_order_id}`, orderId]);
  }
  try {
    const res = await fetch(`${_REDIS_URL}/pipeline`, {
      method:  'POST',
      headers: {
        Authorization:  `Bearer ${_REDIS_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(pipeline),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      console.error('[ghost/paypal] Redis pipeline save failed status=%d body=%s', res.status, text.slice(0, 200));
      return false;
    }
    return true;
  } catch (err) {
    console.error('[ghost/paypal] Redis save error orderId=%s: %s', orderId, err.message);
    return false;
  }
}

async function _redisGetOrder (orderId) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return null;
  const { default: fetch } = await import('node-fetch');
  const key = encodeURIComponent(`ghost:order:${orderId}`);
  try {
    const res = await fetch(`${_REDIS_URL}/GET/${key}`, {
      headers: { Authorization: `Bearer ${_REDIS_TOKEN}` },
    });
    if (!res.ok) return null;
    const body = await res.json().catch(() => null);
    if (!body || body.result === null || body.result === undefined) return null;
    return typeof body.result === 'string' ? JSON.parse(body.result) : body.result;
  } catch (_) { return null; }
}

async function _redisGetOrderByPaypalOrderId (paypalOrderId) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return null;
  const { default: fetch } = await import('node-fetch');
  const mapKey = encodeURIComponent(`ghost:paypal-order:${paypalOrderId}`);
  try {
    const mapRes = await fetch(`${_REDIS_URL}/GET/${mapKey}`, {
      headers: { Authorization: `Bearer ${_REDIS_TOKEN}` },
    });
    if (!mapRes.ok) return null;
    const mapBody = await mapRes.json().catch(() => null);
    const captureId = mapBody?.result;
    if (!captureId) return null;
    return _redisGetOrder(captureId);
  } catch (_) { return null; }
}

/** GET ghost:inventory — returns parsed array or [] */
async function _redisGetInventory () {
  if (!_REDIS_URL || !_REDIS_TOKEN) return [];
  const { default: fetch } = await import('node-fetch');
  const key = encodeURIComponent('ghost:inventory');
  try {
    const res = await fetch(`${_REDIS_URL}/GET/${key}`, {
      headers: { Authorization: `Bearer ${_REDIS_TOKEN}` },
    });
    if (!res.ok) return [];
    const body = await res.json().catch(() => null);
    if (!body || body.result === null || body.result === undefined) return [];
    const raw = typeof body.result === 'string' ? JSON.parse(body.result) : body.result;
    return Array.isArray(raw) ? raw : (Array.isArray(raw?.keys) ? raw.keys : []);
  } catch (_) { return []; }
}

/** SET ghost:inventory — stores the full array.
 *  Uses the same serialisation as server.js _redisSet() so that both
 *  the @upstash/redis SDK (server.js) and the raw REST client (paypal.js)
 *  can read what the other wrote.
 *
 *  The @upstash/redis SDK auto-JSON.stringify()s its input on write and
 *  auto-JSON.parse()s on read.  The REST API sends the body as-is and
 *  returns it as-is.  To be compatible we must store a JSON string of the
 *  array (i.e. one level of serialisation) so that the SDK reads it back
 *  as an array while the REST GET also returns a parseable JSON string.
 */
async function _redisSetInventory (inventory) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return false;
  const { default: fetch } = await import('node-fetch');
  try {
    // Store exactly one JSON.stringify — the SDK will parse it back to an array on read.
    const body = JSON.stringify(inventory);
    const res = await fetch(`${_REDIS_URL}/SET/ghost:inventory`, {
      method:  'POST',
      headers: {
        Authorization:  `Bearer ${_REDIS_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body,
    });
    return res.ok;
  } catch (_) { return false; }
}


/* ── Duration → expiry-days mapping ─────────────────────────────────────── */
const DURATION_DAYS = {
  day:      1,
  '3days':  3,
  week:     7,
  month:    30,
  '3months': 90,
};

/* ── Plan catalogue ──────────────────────────────────────────────────────── */
const PLAN_CATALOGUE = {
  day: {
    id:         'day',
    label:      'Phantom 1 Day',
    priceUsd:   '2.99',
    tier:       'PRO',
    expiryDays: 1,
  },
  '3days': {
    id:         '3days',
    label:      'Phantom 3 Days',
    priceUsd:   '5.99',
    tier:       'PRO',
    expiryDays: 3,
  },
  week: {
    id:         'week',
    label:      'Phantom 1 Week',
    priceUsd:   '9.99',
    tier:       'PRO',
    expiryDays: 7,
  },
  month: {
    id:         'month',
    label:      'Phantom 1 Month',
    priceUsd:   '24.99',
    tier:       'PRO',
    expiryDays: 30,
  },
  '3months': {
    id:         '3months',
    label:      'Phantom 3 Months',
    priceUsd:   '59.99',
    tier:       'PRO',
    expiryDays: 90,
  },
};

/**
 * Normalize any plan string variant to a canonical slug.
 * Must stay in sync with _normalizePlan() in server.js and _PLAN_ALIASES there.
 */
function _normalizePlan (plan) {
  if (!plan) return '';
  const aliases = {
    // New duration-based slugs
    day:           'day',
    '1day':        'day',
    '1 day':       'day',
    '3days':       '3days',
    '3 days':      '3days',
    week:          'week',
    '7day':        'week',
    '7days':       'week',
    '7 days':      'week',
    month:         'month',
    '30day':       'month',
    '30days':      'month',
    '30 days':     'month',
    '3months':     '3months',
    '90day':       '3months',
    '90days':      '3months',
    '90 days':     '3months',
    // Legacy slugs — map to closest equivalent for backward compat
    pro:           'month',
    monthly:       'month',
    ghost_pro_monthly: 'month',
    'ghost pro monthly': 'month',
    'ghost pro (monthly)': 'month',
    ghost_pro:     'month',
    'ghost pro':   'month',
    lifetime:      '3months',
    ghost_lifetime:'3months',
    'ghost lifetime':'3months',
    trial:         'day',
    ghost_trial:   'day',
    'ghost trial': 'day',
  };
  const key = String(plan).trim().toLowerCase();
  return aliases[key] || key;
}

/**
 * Generate a license key string in GHOST-XXXX-XXXX-XXXX-XXXX format.
 */
function _generateLicenseKey () {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const seg   = () => Array.from({ length: 4 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
  return `GHOST-${seg()}-${seg()}-${seg()}-${seg()}`;
}

/**
 * Compute the expiration ISO date for a given plan.
 * Returns null for plans with 0 expiryDays (permanent — not currently in catalogue).
 */
function _computeExpiry (planId) {
  const days = DURATION_DAYS[planId];
  if (!days) return null;
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString();
}


/* ── PayPal OAuth token cache ────────────────────────────────────────────── */
let _cachedToken    = null;
let _tokenExpiresAt = 0;

async function _getAccessToken () {
  const now = Date.now();
  if (_cachedToken && now < _tokenExpiresAt - 30_000) return _cachedToken;

  const { default: fetch } = await import('node-fetch');
  const credentials = Buffer.from(`${PAYPAL_CLIENT_ID}:${PAYPAL_CLIENT_SECRET}`).toString('base64');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10_000);

  let res;
  try {
    res = await fetch(`${PAYPAL_API_BASE}/v1/oauth2/token`, {
      method:  'POST',
      headers: {
        Authorization:  `Basic ${credentials}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body:   'grant_type=client_credentials',
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`PayPal auth failed (${res.status}): ${text.slice(0, 200)}`);
  }

  const data = await res.json();
  _cachedToken    = data.access_token;
  _tokenExpiresAt = now + (data.expires_in || 32400) * 1000;
  return _cachedToken;
}


/* ── PayPal Orders API helper ────────────────────────────────────────────── */
async function _paypalRequest (method, path, body, timeoutMs = 20_000) {
  const token  = await _getAccessToken();
  const { default: fetch } = await import('node-fetch');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const init = {
    method,
    headers: {
      Authorization:  `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    signal: controller.signal,
  };
  if (body && Object.keys(body).length) init.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(`${PAYPAL_API_BASE}${path}`, init);
  } finally {
    clearTimeout(timer);
  }

  const data = await res.json().catch(() => ({}));

  if (!res.ok) {
    const msg = data.message || data.error_description || `HTTP ${res.status}`;
    console.error('[ghost/paypal] PayPal API %s %s => status=%d message=%s',
      method, path, res.status, msg);
    throw new Error(`PayPal API error [${res.status}]: ${msg}`);
  }
  return data;
}


/* ─────────────────────────────────────────────────────────────────────────
   fulfillOrder(orderId)
   ─────────────────────────────────────────────────────────────────────────
   Single shared fulfillment function.  Called:
     1. Immediately after PayPal capture succeeds
     2. From /api/paypal/retry-fulfillment  (Reissue / Retry)
     3. From /api/admin/orders/fulfill-pending  (Fulfill Pending Orders)
     4. From the webhook handler

   Contract
   --------
   - Loads the order from Redis.
   - If the order already has a license_key, returns it unchanged (idempotent).
   - Confirms payment_status is completed / captured.
   - Normalizes the plan.
   - Loads ghost:inventory (the SAME array shown in Admin → Key Inventory).
   - Finds the first key with status='available' and matching canonical plan.
   - Assigns that one key:
       inventory record: status='sold', customer=email, orderId, purchaseDate=now
   - Updates the order record: licenseKey, deliveryStatus='delivered', status='completed'
   - Saves BOTH records to Redis atomically (pipeline for inventory).
   - Returns { ok, licenseKey?, deliveryStatus, reason? }

   Returns:
     { ok: true,  licenseKey: '...', deliveryStatus: 'delivered' }
     { ok: false, deliveryStatus: 'out_of_stock',     reason: 'no_matching_inventory' }
     { ok: false, deliveryStatus: 'pending',           reason: 'order_not_found' }
     { ok: false, deliveryStatus: <unchanged>,         reason: 'payment_not_completed' }
  ──────────────────────────────────────────────────────────────────────────*/
async function fulfillOrder (orderId) {
  console.log('[fulfill] order=%s', orderId);

  // ── Load order ──────────────────────────────────────────────────────────
  const order = await _redisGetOrder(orderId).catch(() => null);
  if (!order) {
    console.warn('[fulfill] order_not_found order=%s', orderId);
    return { ok: false, deliveryStatus: 'pending', reason: 'order_not_found' };
  }

  // ── Idempotency: already delivered ──────────────────────────────────────
  if (order.license_key && order.delivery_status === 'delivered') {
    console.log('[fulfill] already_delivered order=%s licenseKey=[present]', orderId);
    return { ok: true, licenseKey: order.license_key, deliveryStatus: 'delivered' };
  }

  // ── Confirm payment ──────────────────────────────────────────────────────
  const ps = (order.payment_status || '').toLowerCase();
  if (ps !== 'completed' && ps !== 'captured' && ps !== 'verified') {
    console.warn('[fulfill] payment_not_completed order=%s payment_status=%s', orderId, ps);
    return { ok: false, deliveryStatus: order.delivery_status || 'pending', reason: 'payment_not_completed' };
  }

  // ── Normalize plan ───────────────────────────────────────────────────────
  const rawPlan       = order.plan || '';
  const canonicalPlan = _normalizePlan(rawPlan);
  console.log('[fulfill] order_plan=%s (normalized=%s)', rawPlan, canonicalPlan);

  const now      = new Date().toISOString();
  const expiresAt = _computeExpiry(canonicalPlan);

  // ── Auto-generate a brand-new license key ────────────────────────────────
  // Keys are generated on demand after payment verification — no pre-stocked
  // inventory is needed. The generated key is saved into ghost:inventory so
  // the admin panel shows it alongside any manually-created keys.
  const generatedKey = _generateLicenseKey();
  console.log('[fulfill] generated_key=[present] plan=%s expiresAt=%s', canonicalPlan, expiresAt);

  // ── Persist generated key to inventory so admin panel shows it ───────────
  const inventory = await _redisGetInventory().catch(() => []);
  const keyRecord = {
    key:             generatedKey,
    plan:            canonicalPlan,
    duration:        canonicalPlan,
    status:          'sold',
    customer:        order.email || '',
    customer_email:  order.email || '',
    assigned_user:   order.discord || '',
    discord_username: order.discord || '',
    order_id:        orderId,
    purchase_date:   now,
    created_date:    now,
    added_at:        now,
    expiration:      expiresAt,
    expires_at:      expiresAt,
    payment_id:      orderId,
    notes:           `Auto-generated on purchase — order: ${orderId}`,
    hwid:            '',
  };
  const updatedInventory = [...inventory, keyRecord];
  const inventorySaved = await _redisSetInventory(updatedInventory);
  console.log('[fulfill] key_saved_to_inventory=%s', String(inventorySaved));

  // ── Update order ─────────────────────────────────────────────────────────
  const updatedOrder = {
    ...order,
    license_key:     generatedKey,
    license_status:  'active',
    delivery_status: 'delivered',
    status:          'completed',
    fulfilled_at:    now,
    expires_at:      expiresAt,
    duration:        canonicalPlan,
  };

  const orderSaved = await _redisSaveOrder(orderId, updatedOrder);
  console.log('[fulfill] order_saved=%s', String(orderSaved));
  console.log('[fulfill] final_status=delivered');

  return { ok: true, licenseKey: generatedKey, deliveryStatus: 'delivered' };
}


/* ── In-memory capture lock (prevents double-capture on rapid retries) ─────
   Maps PayPal orderID -> Promise<result> so that a second concurrent request
   for the same orderID waits for (and reuses) the first capture result.
─────────────────────────────────────────────────────────────────────────── */
const _captureInFlight = new Map();


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/create-order
  ──────────────────────────────────────────────────────────────────────────*/
async function createOrder (req, res) {
  const { plan: planRaw, email, discord, discord_id: discordIdRaw, couponCode, finalPrice: clientFinalPrice } = req.body || {};

  const planId = (planRaw || '').trim().toLowerCase();
  const plan   = PLAN_CATALOGUE[planId];

  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: day, 3days, week, month, or 3months.', stage: 'create-order' });
  }
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ ok: false, message: 'A valid email address is required.', stage: 'create-order' });
  }
  if (!discord || discord.trim().length < 2) {
    return res.status(400).json({ ok: false, message: 'Discord username is required.', stage: 'create-order' });
  }

  if (!PAYPAL_CLIENT_ID || !PAYPAL_CLIENT_SECRET) {
    return res.status(503).json({ ok: false, message: 'Payment is not configured on this server.', stage: 'create-order' });
  }

  // ── Resolve authoritative price ──────────────────────────────────────────────
  // If a coupon code is supplied, validate it server-side and compute the final price.
  // We NEVER trust the finalPrice submitted by the client.
  let authorizedPrice = parseFloat(plan.priceUsd);
  let resolvedCouponCode = null;
  let resolvedDiscount = 0;

  if (couponCode) {
    const normCode = String(couponCode).trim().toUpperCase().replace(/[^A-Z0-9_-]/g, '');
    if (normCode) {
      // Lazy-load redis via the module-level REST helpers (same pattern used elsewhere)
      try {
        const { default: fetch } = await import('node-fetch');
        const couponRes = await fetch(`${_REDIS_URL}/GET/${encodeURIComponent('ghost:coupons')}`, {
          headers: { Authorization: `Bearer ${_REDIS_TOKEN}` },
        });
        if (couponRes.ok) {
          const couponBody = await couponRes.json().catch(() => null);
          const raw = couponBody?.result;
          const coupons = Array.isArray(raw) ? raw :
            (typeof raw === 'string' ? JSON.parse(raw) : []);
          const coupon = coupons.find(c => c.code === normCode && c.active);
          if (coupon) {
            const base = parseFloat(plan.priceUsd);
            let disc = 0;
            if (coupon.discount_type === 'percentage') {
              disc = Math.round(base * (parseFloat(coupon.discount_value) / 100) * 100) / 100;
            } else if (coupon.discount_type === 'fixed') {
              disc = Math.min(parseFloat(coupon.discount_value) || 0, base);
            } else if (coupon.discount_type === 'free') {
              disc = base;
            }
            authorizedPrice = Math.max(0, Math.round((base - disc) * 100) / 100);
            resolvedCouponCode = normCode;
            resolvedDiscount   = Math.round(disc * 100) / 100;
          }
        }
      } catch (_) { /* coupon resolution failed — use full price */ }
    }
  }

  // Safety: if coupon makes price $0, the caller should use /api/coupons/redeem-free instead
  if (authorizedPrice === 0) {
    return res.status(400).json({
      ok: false,
      message: 'Free orders must be processed via /api/coupons/redeem-free.',
      stage: 'create-order',
    });
  }

  const priceValue = authorizedPrice.toFixed(2);

  try {
    const order = await _paypalRequest('POST', '/v2/checkout/orders', {
      intent: 'CAPTURE',
      purchase_units: [{
        reference_id: `ghost-${planId}-${Date.now()}`,
        description:  plan.label + (resolvedCouponCode ? ` (${resolvedCouponCode})` : ''),
        amount: {
          currency_code: 'USD',
          value:         priceValue,
        },
        custom_id: JSON.stringify({
          plan:       planId,
          email:      email.trim(),
          discord:    discord.trim(),
          // Only store discord_id if it looks like a valid snowflake.
          // The capture-order handler reads it back from req.body directly,
          // but webhooks (which have no req.body) read it from custom_id.
          discord_id: /^\d{17,19}$/.test(String(discordIdRaw || '').trim())
            ? String(discordIdRaw).trim()
            : '',
          couponCode: resolvedCouponCode || '',
          discount:   resolvedDiscount,
          origPrice:  plan.priceUsd,
        }),
      }],
      application_context: {
        brand_name:          'Ghost',
        user_action:         'PAY_NOW',
        shipping_preference: 'NO_SHIPPING',
      },
    }, 15_000);

    console.log('[ghost/paypal] order created id=%s plan=%s env=%s', order.id, planId, PAYPAL_ENV);
    return res.json({ ok: true, orderID: order.id });

  } catch (err) {
    const isTimeout = err.name === 'AbortError';
    console.error('[ghost/paypal] create-order %s: %s', isTimeout ? 'timeout' : 'error', err.message);
    return res.status(502).json({
      ok:      false,
      message: isTimeout
        ? 'PayPal did not respond in time. Please try again.'
        : 'Could not create PayPal order. Please try again.',
      stage: 'create-order',
    });
  }
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/capture-order
  ──────────────────────────────────────────────────────────────────────────*/
async function captureOrder (req, res) {
  const { orderID, email, discord, plan: planRaw } = req.body || {};

  if (!orderID) {
    return res.status(400).json({ ok: false, message: 'orderID is required.', stage: 'validate' });
  }
  const planId = _normalizePlan((planRaw || '').trim().toLowerCase());
  const plan   = PLAN_CATALOGUE[planId];
  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: day, 3days, week, month, or 3months.', stage: 'validate' });
  }

  // ── Idempotency: check Redis directly ────────────────────────────────────
  const _buildIdempotentResponse = (existing, planMeta) => ({
    ok:             true,
    paymentStatus:  'COMPLETED',
    deliveryStatus: existing.delivery_status || existing.deliveryStatus || 'delivered',
    orderId:        existing.order_id        || existing.orderId,
    captureId:      existing.paypal_capture_id || existing.captureId || existing.order_id,
    paypalOrderId:  orderID,
    invoiceId:      existing.invoice_id      || null,
    plan:           existing.plan            || planMeta.id,
    planLabel:      existing.plan_label      || planMeta.label,
    amount:         String(existing.price_usd || planMeta.priceUsd),
    currency:       existing.currency        || 'USD',
    email:          existing.email           || email,
    discord:        existing.discord         || discord,
    licenseKey:     existing.license_key     || null,
    licenseStatus:  existing.license_status  || (existing.license_key ? 'active' : 'pending'),
    purchaseDate:   existing.created_at      || new Date().toISOString(),
    downloadUrl:    existing.license_key ? `/api/order/${encodeURIComponent(existing.order_id || existing.orderId)}/download` : null,
    tier:           existing.tier            || planMeta.tier,
    instructions: existing.license_key ? [
      'Download GhostConfig.exe.',
      'Open the application.',
      'Copy your license key above.',
      'Paste it into Ghost.',
      'Activate your license.',
    ] : null,
  });

  try {
    const redisOrder = await _redisGetOrderByPaypalOrderId(orderID);
    if (redisOrder && redisOrder.paypal_order_id === orderID) {
      console.log('[ghost/paypal] captureOrder idempotent — paypalOrderId=%s orderId=%s deliveryStatus=%s',
        orderID, redisOrder.order_id, redisOrder.delivery_status);
      return res.json(_buildIdempotentResponse(redisOrder, plan));
    }
  } catch (_) {}

  // ── Prevent concurrent double-capture ────────────────────────────────────
  if (_captureInFlight.has(orderID)) {
    console.log('[ghost/paypal] capture already in-flight for orderID=%s — waiting', orderID);
    try {
      const existing = await _captureInFlight.get(orderID);
      return res.json(existing);
    } catch (_) {
      return res.status(502).json({ ok: false, message: 'Concurrent capture failed.', stage: 'capture' });
    }
  }

  let resolveCapture, rejectCapture;
  const capturePromise = new Promise((resolve, reject) => {
    resolveCapture = resolve;
    rejectCapture  = reject;
  });
  _captureInFlight.set(orderID, capturePromise);

  try {
    const discordId = String((req.body && req.body.discord_id) || '').trim();
    const result  = await _doCaptureOrder({ orderID, email, discord, planId, plan, discordId });
    resolveCapture(result);
    const status  = result.ok ? 200 : (result._status || 500);
    const payload = Object.assign({}, result);
    delete payload._status;
    return res.status(status).json(payload);
  } catch (err) {
    rejectCapture(err);
    // Log the full error server-side for diagnosis while returning a safe
    // customer-facing message that does not expose internal details.
    console.error(
      '[ghost/paypal] CAPTURE UNHANDLED ERROR orderID=%s plan=%s email=%s\n' +
      '  name:    %s\n' +
      '  message: %s\n' +
      '  stack:\n%s',
      orderID, planId, email,
      err.name, err.message, err.stack || '(no stack)',
    );
    return res.status(500).json({
      ok:      false,
      message: 'Payment capture failed. Your card has not been charged. Please try again or contact support.',
      stage:   'capture',
      orderId: orderID,
    });
  } finally {
    setTimeout(() => _captureInFlight.delete(orderID), 60_000);
  }
}


async function _doCaptureOrder ({ orderID, email, discord, planId, plan, discordId }) {
  console.log(
    '[ghost/paypal] _doCaptureOrder START orderID=%s plan=%s email=%s env=%s',
    orderID, planId, email, PAYPAL_ENV,
  );
  console.log(
    '[ghost/paypal] _doCaptureOrder config clientId=%s hasSecret=%s apiBase=%s',
    PAYPAL_CLIENT_ID ? PAYPAL_CLIENT_ID.slice(0, 8) + '…' : '(MISSING)',
    PAYPAL_CLIENT_SECRET ? 'yes' : '(MISSING)',
    PAYPAL_API_BASE,
  );

  // ── Step 1: Capture via PayPal ────────────────────────────────────────────
  let capture;
  try {
    capture = await _paypalRequest('POST', `/v2/checkout/orders/${orderID}/capture`, {}, 30_000);
  } catch (err) {
    const isTimeout = err.name === 'AbortError';
    console.error('[ghost/paypal] capture %s orderID=%s: %s',
      isTimeout ? 'timeout' : 'error', orderID, err.message);
    return {
      ok:      false,
      message: isTimeout
        ? 'PayPal capture timed out. Check your PayPal dashboard to confirm payment status, then use Retry Status below.'
        : 'Payment capture failed. No charge was made.',
      stage:   'capture',
      orderId: orderID,
      _status: 502,
    };
  }

  // ── Step 2: Verify COMPLETED ──────────────────────────────────────────────
  console.log('[ghost/paypal] capture response orderID=%s status=%s', orderID, capture.status);
  if (capture.status !== 'COMPLETED') {
    console.warn('[ghost/paypal] capture not COMPLETED orderID=%s status=%s', orderID, capture.status);
    return {
      ok:      false,
      message: `Payment not completed (status: ${capture.status}). No charge was made.`,
      stage:   'capture-status',
      orderId: orderID,
      _status: 400,
    };
  }

  // ── Step 3: Extract capture unit ─────────────────────────────────────────
  const pu = (capture.purchase_units || [])[0];
  if (!pu) {
    console.error('[ghost/paypal] no purchase_units in capture orderID=%s', orderID);
    return { ok: false, message: 'Payment capture data invalid. Contact support.',
      stage: 'capture-parse', orderId: orderID, _status: 500 };
  }

  const captureUnit = (pu.payments?.captures || [])[0];
  if (!captureUnit || captureUnit.status !== 'COMPLETED') {
    console.error('[ghost/paypal] capture unit not COMPLETED orderID=%s unitStatus=%s',
      orderID, captureUnit?.status);
    return { ok: false, message: 'Capture unit is not complete. Contact support.',
      stage: 'capture-unit', orderId: orderID, _status: 400 };
  }

  const captureId        = captureUnit.id;
  const capturedAmount   = captureUnit.amount?.value;
  const capturedCurrency = captureUnit.amount?.currency_code;

  // ── Step 4: Verify amount + currency ─────────────────────────────────────
  if (capturedCurrency !== 'USD') {
    console.error('[ghost/paypal] wrong currency orderID=%s currency=%s', orderID, capturedCurrency);
    return { ok: false, message: 'Unexpected payment currency. Contact support.',
      stage: 'capture-currency', orderId: orderID, _status: 400 };
  }

  // Extract coupon data stored in custom_id during create-order
  let _couponCode = '', _couponDiscount = 0, _origPrice = parseFloat(plan.priceUsd);
  try {
    const customData = JSON.parse(pu.custom_id || '{}');
    _couponCode     = customData.couponCode || '';
    _couponDiscount = parseFloat(customData.discount || 0) || 0;
    _origPrice      = parseFloat(customData.origPrice || plan.priceUsd) || _origPrice;
  } catch (_) {}

  const _authorizedPrice = Math.max(0, Math.round((_origPrice - _couponDiscount) * 100) / 100);

  if (Math.abs(parseFloat(capturedAmount) - _authorizedPrice) > 0.02) {
    console.error('[ghost/paypal] amount mismatch orderID=%s expected=%s got=%s',
      orderID, _authorizedPrice, capturedAmount);
    return { ok: false, message: 'Payment amount mismatch. Contact support.',
      stage: 'capture-amount', orderId: orderID, _status: 400 };
  }

  // ── Step 5: Resolve payer email ───────────────────────────────────────────
  const payerEmail      = capture.payer?.email_address || email || '';
  const resolvedEmail   = payerEmail.trim() || (email || '').trim();
  const resolvedDiscord = (discord || '').trim();

  console.log('[ghost/paypal] capture verified orderID=%s captureId=%s plan=%s amount=%s coupon=%s',
    orderID, captureId, planId, capturedAmount, _couponCode || 'none');

  const purchaseDateNow = new Date().toISOString();

  // ── Generate invoice ID ───────────────────────────────────────────────────
  const _invoiceRaw  = captureId.replace(/[^A-Z0-9]/gi, '').toUpperCase();
  const _invoiceSufx = _invoiceRaw.slice(-8).padStart(8, '0');
  const invoiceId    = `GHOST-INV-${_invoiceSufx}`;

  // ── Step 6: Persist order to Redis (payment confirmed, delivery pending) ──
  // discord_id — numeric Discord snowflake supplied by the checkout page.
  // Stored as a string.  Only set if it looks like a valid snowflake
  // (17–19 digits) to avoid storing garbage.  The bot will independently
  // validate this before granting any role.
  const rawDiscordId  = String(discordId || '').trim();
  const resolvedDiscordId = /^\d{17,19}$/.test(rawDiscordId) ? rawDiscordId : '';

  const pendingOrderRecord = {
    order_id:              captureId,
    paypal_order_id:       orderID,
    paypal_capture_id:     captureId,
    invoice_id:            invoiceId,
    plan:                  planId,
    plan_label:            plan.label,
    tier:                  plan.tier,
    email:                 resolvedEmail,
    discord:               resolvedDiscord,
    discord_id:            resolvedDiscordId || null,
    discord_role_granted:  false,
    price_usd:             parseFloat(capturedAmount),
    original_price:        _origPrice,
    coupon_code:           _couponCode || null,
    coupon_discount:       _couponDiscount || 0,
    currency:              capturedCurrency,
    created_at:            purchaseDateNow,
    payment_status:        'completed',
    payment_verified:      true,
    delivery_status:       'pending',
    license_key:           null,
    license_status:        'pending',
  };

  const savedToRedis = await _redisSaveOrder(captureId, pendingOrderRecord);
  if (savedToRedis) {
    console.log('[ghost/paypal] order persisted to Redis orderId=%s paypalOrderId=%s', captureId, orderID);
  } else {
    console.warn('[ghost/paypal] could not persist order to Redis orderId=%s', captureId);
  }

  // ── Step 7: Fulfill immediately (assign license from Redis inventory) ─────
  const fulfillResult = await fulfillOrder(captureId);

  if (!fulfillResult.ok) {
    console.warn('[ghost/paypal] fulfillment failed orderId=%s reason=%s — order saved as pending',
      captureId, fulfillResult.reason);
    return {
      ok:             true,
      paymentStatus:  'COMPLETED',
      deliveryStatus: fulfillResult.deliveryStatus || 'pending',
      orderId:        captureId,
      captureId,
      paypalOrderId:  orderID,
      invoiceId,
      plan:           planId,
      planLabel:      plan.label,
      amount:         capturedAmount,
      currency:       capturedCurrency,
      email:          resolvedEmail,
      discord:        resolvedDiscord,
      licenseKey:     null,
      licenseStatus:  'pending',
      purchaseDate:   purchaseDateNow,
      downloadUrl:    null,
      tier:           plan.tier,
      instructions:   null,
      _status:        200,
    };
  }

  // ── Step 8: Build success response from the now-updated order ────────────
  const deliveredOrder = await _redisGetOrder(captureId).catch(() => pendingOrderRecord);

  console.log(
    '[ghost/paypal] capture complete — orderId=%s paymentStatus=COMPLETED deliveryStatus=%s ' +
    'plan=%s amount=%s licenseKey=%s',
    captureId,
    fulfillResult.deliveryStatus,
    planId,
    capturedAmount,
    fulfillResult.licenseKey ? '[present]' : '[missing]',
  );

  return {
    ok:             true,
    paymentStatus:  'COMPLETED',
    deliveryStatus: fulfillResult.deliveryStatus,
    orderId:        captureId,
    captureId,
    paypalOrderId:  orderID,
    invoiceId,
    plan:           planId,
    planLabel:      plan.label,
    amount:         capturedAmount,
    currency:       capturedCurrency,
    email:          resolvedEmail,
    discord:        resolvedDiscord,
    licenseKey:     fulfillResult.licenseKey || null,
    licenseStatus:  fulfillResult.licenseKey ? 'active' : null,
    purchaseDate:   deliveredOrder?.created_at || purchaseDateNow,
    downloadUrl:    fulfillResult.licenseKey ? `/api/order/${encodeURIComponent(captureId)}/download` : null,
    tier:           plan.tier,
    instructions: [
      'Download GhostConfig.exe.',
      'Open the application.',
      'Copy your license key above.',
      'Paste it into Ghost.',
      'Activate your license.',
    ],
  };
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/retry-fulfillment
   ─────────────────────────────────────────────────────────────────────────
   Retry / Reissue — calls the same fulfillOrder() function.
   Never re-captures or re-charges.
  ──────────────────────────────────────────────────────────────────────────*/
async function retryFulfillment (req, res) {
  const { captureId } = req.body || {};

  if (!captureId || typeof captureId !== 'string' || !captureId.trim()) {
    return res.status(400).json({
      ok:      false,
      message: 'captureId is required.',
      stage:   'retry-fulfillment',
    });
  }

  const id = captureId.trim();

  // Load the stored order first (need it for response fields)
  const redisRecord = await _redisGetOrder(id).catch(() => null);
  if (!redisRecord) {
    return res.status(404).json({
      ok:      false,
      message: 'Order not found.',
      stage:   'retry-fulfillment',
    });
  }

  // Run fulfillment (idempotent — returns existing key if already delivered)
  const result = await fulfillOrder(id);

  // Re-read order to get final state
  const finalOrder = await _redisGetOrder(id).catch(() => redisRecord);

  if (!result.ok) {
    return res.status(result.deliveryStatus === 'out_of_stock' ? 409 : 500).json({
      ok:      false,
      message: result.reason === 'no_matching_inventory'
        ? 'No available keys match this plan. Add keys in Admin → Key Inventory.'
        : result.reason === 'payment_not_completed'
          ? 'Payment is not completed for this order.'
          : 'Fulfillment failed.',
      stage:   'retry-fulfillment',
      deliveryStatus: result.deliveryStatus,
    });
  }

  return res.json({
    ok:             true,
    paymentStatus:  finalOrder?.payment_status  || 'COMPLETED',
    deliveryStatus: result.deliveryStatus,
    orderId:        finalOrder?.order_id        || id,
    plan:           finalOrder?.plan,
    planLabel:      finalOrder?.plan_label,
    amount:         String(finalOrder?.price_usd || ''),
    currency:       finalOrder?.currency        || 'USD',
    purchaseDate:   finalOrder?.created_at,
    licenseKey:     result.licenseKey           || null,
    licenseStatus:  result.licenseKey ? 'active' : null,
    downloadUrl:    result.licenseKey ? `/api/order/${encodeURIComponent(id)}/download` : null,
    tier:           finalOrder?.tier,
  });
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/webhook
  ──────────────────────────────────────────────────────────────────────────*/
async function handleWebhook (req, res) {
  // ── Verify webhook signature ──────────────────────────────────────────────
  const transmissionId   = req.headers['paypal-transmission-id']   || '';
  const transmissionTime = req.headers['paypal-transmission-time'] || '';
  const certUrl          = req.headers['paypal-cert-url']          || '';
  const transmissionSig  = req.headers['paypal-transmission-sig']  || '';
  const authAlgo         = req.headers['paypal-auth-algo']         || '';

  if (!transmissionId || !transmissionSig) {
    console.warn('[ghost/paypal/webhook] Missing signature headers — rejected');
    return res.status(400).json({ ok: false, error: 'Missing PayPal signature headers' });
  }

  if (!PAYPAL_WEBHOOK_ID) {
    console.warn('[ghost/paypal/webhook] PAYPAL_WEBHOOK_ID not set — skipping signature verification');
  } else {
    try {
      const rawBody = req.rawBody || JSON.stringify(req.body);
      const token   = await _getAccessToken();
      const { default: fetch } = await import('node-fetch');

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10_000);

      const verifyRes = await fetch(
        `${PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature`, {
          method:  'POST',
          headers: {
            Authorization:  `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            auth_algo:          authAlgo,
            cert_url:           certUrl,
            transmission_id:    transmissionId,
            transmission_sig:   transmissionSig,
            transmission_time:  transmissionTime,
            webhook_id:         PAYPAL_WEBHOOK_ID,
            webhook_event:      req.body,
          }),
          signal: controller.signal,
        });
      clearTimeout(timer);

      const verifyData = await verifyRes.json().catch(() => ({}));
      if (!verifyRes.ok || verifyData.verification_status !== 'SUCCESS') {
        console.warn('[ghost/paypal/webhook] Signature verification failed status=%s',
          verifyData.verification_status);
        return res.status(400).json({ ok: false, error: 'Webhook signature verification failed' });
      }
    } catch (err) {
      console.error('[ghost/paypal/webhook] Signature verification error:', err.message);
      return res.status(500).json({ ok: false, error: 'Could not verify webhook signature' });
    }
  }

  const event     = req.body || {};
  const eventType = event.event_type || '';

  console.log('[ghost/paypal/webhook] received event_type=%s id=%s', eventType, event.id);

  if (eventType !== 'PAYMENT.CAPTURE.COMPLETED') {
    return res.json({ ok: true, received: true, processed: false });
  }

  const resource      = event.resource || {};
  const captureId     = resource.id || '';
  const captureStatus = resource.status || '';
  const amount        = resource.amount?.value || '';
  const currency      = resource.amount?.currency_code || 'USD';

  if (captureStatus !== 'COMPLETED') {
    console.log('[ghost/paypal/webhook] Capture not COMPLETED captureId=%s status=%s',
      captureId, captureStatus);
    return res.json({ ok: true, received: true, processed: false });
  }

  const orderId = resource.supplementary_data?.related_ids?.order_id || '';

  let customId = {};
  try {
    const raw = resource.custom_id || '';
    if (raw) customId = JSON.parse(raw);
  } catch (_) {}

  const plan    = customId.plan    || '';
  const email   = customId.email   || '';
  const discord = customId.discord || '';

  if (!plan || !email || !captureId) {
    console.warn('[ghost/paypal/webhook] Missing fields captureId=%s plan=%s email=[%s]',
      captureId, plan, email ? 'set' : 'empty');
    return res.json({ ok: true, received: true, processed: false, note: 'missing_fields' });
  }

  const planMeta = PLAN_CATALOGUE[_normalizePlan(plan)];
  if (!planMeta) {
    console.warn('[ghost/paypal/webhook] Unknown plan=%s captureId=%s', plan, captureId);
    return res.json({ ok: true, received: true, processed: false, note: 'unknown_plan' });
  }

  // ── Ensure order record exists in Redis before fulfilling ─────────────────
  // If the capture-order route already wrote it we'll get idempotency from fulfillOrder.
  // If the webhook fires first (race), write a minimal record so fulfillOrder can find it.
  const existing = await _redisGetOrder(captureId).catch(() => null);
  if (!existing) {
    const invoiceRaw   = captureId.replace(/[^A-Z0-9]/gi, '').toUpperCase();
    const invoiceSufx  = invoiceRaw.slice(-8).padStart(8, '0');
    // discord_id may have been stored in custom_id at create-order time.
    const webhookDiscordId = customId.discord_id
      ? (String(customId.discord_id).trim())
      : '';
    const resolvedWebhookDiscordId = /^\d{17,19}$/.test(webhookDiscordId) ? webhookDiscordId : '';
    await _redisSaveOrder(captureId, {
      order_id:             captureId,
      paypal_order_id:      orderId,
      paypal_capture_id:    captureId,
      invoice_id:           `GHOST-INV-${invoiceSufx}`,
      plan:                 _normalizePlan(plan),
      plan_label:           planMeta.label,
      tier:                 planMeta.tier,
      email,
      discord,
      discord_id:           resolvedWebhookDiscordId || null,
      discord_role_granted: false,
      price_usd:            parseFloat(amount),
      currency,
      created_at:           new Date().toISOString(),
      payment_status:       'completed',
      payment_verified:     true,
      delivery_status:      'pending',
      license_key:          null,
      license_status:       'pending',
    }).catch(() => {});
  }

  // ── Fulfill (idempotent) ──────────────────────────────────────────────────
  try {
    const result = await fulfillOrder(captureId);
    if (result.ok) {
      console.log('[ghost/paypal/webhook] Fulfillment confirmed captureId=%s plan=%s', captureId, plan);
    } else {
      console.error('[ghost/paypal/webhook] Fulfillment failed captureId=%s reason=%s',
        captureId, result.reason);
    }
    return res.json({ ok: true, received: true, processed: result.ok });
  } catch (err) {
    console.error('[ghost/paypal/webhook] Fulfillment error captureId=%s: %s', captureId, err.message);
    return res.json({ ok: true, received: true, processed: false, error: 'fulfillment_error' });
  }
}


module.exports = { createOrder, captureOrder, handleWebhook, retryFulfillment, fulfillOrder, PLAN_CATALOGUE };
