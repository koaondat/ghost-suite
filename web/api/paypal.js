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
 *   GET  /api/order/:orderId           Retrieve a stored order record
 *
 * Required env vars (never hardcode these):
 *   PAYPAL_CLIENT_ID        — from developer.paypal.com
 *   PAYPAL_CLIENT_SECRET    — NEVER expose in browser code
 *   PAYPAL_ENVIRONMENT      — 'sandbox' or 'live'  (default: sandbox)
 *   PAYPAL_WEBHOOK_ID       — webhook ID from PayPal dashboard (for signature verification)
 *   GHOST_DELIVERY_URL      — URL of the Python license_delivery server
 *   BASE_URL                — public URL of this web server
 */

'use strict';

const PAYPAL_CLIENT_ID     = process.env.PAYPAL_CLIENT_ID     || '';
const PAYPAL_CLIENT_SECRET = process.env.PAYPAL_CLIENT_SECRET || '';
const PAYPAL_ENV           = (process.env.PAYPAL_ENVIRONMENT  || 'sandbox').toLowerCase();
const PAYPAL_WEBHOOK_ID    = process.env.PAYPAL_WEBHOOK_ID    || '';

const PAYPAL_API_BASE = PAYPAL_ENV === 'live'
  ? 'https://api-m.paypal.com'
  : 'https://api-m.sandbox.paypal.com';

const DELIVERY_BACKEND_URL = (process.env.GHOST_DELIVERY_URL || '').replace(/\/$/, '');

if (!PAYPAL_CLIENT_ID || !PAYPAL_CLIENT_SECRET) {
  console.error(
    '[ghost/paypal] FATAL: PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set. ' +
    'All PayPal routes will return 503 until they are configured.',
  );
}
if (!DELIVERY_BACKEND_URL) {
  console.warn(
    '[ghost/paypal] WARNING: GHOST_DELIVERY_URL is not set. ' +
    'Orders will be persisted directly to Redis when delivery backend is unavailable.',
  );
}


/* ── Redis-direct order persistence ─────────────────────────────────────────
   Used as the primary persistence path so orders are NEVER lost, even when
   the Python delivery backend is unreachable.  The delivery backend can read
   from the same keys when it comes back online.
   Keys mirror what license_delivery.py uses:
     ghost:order:{order_id}      — the order hash
     ghost:orders:index          — sorted set of order IDs (score = unix timestamp)
─────────────────────────────────────────────────────────────────────────── */
const _REDIS_URL   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
const _REDIS_TOKEN = (process.env.UPSTASH_REDIS_REST_TOKEN || '');

async function _redisSaveOrder (orderId, record) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return false;
  const { default: fetch } = await import('node-fetch');
  // Also store a secondary lookup key so we can find the order by PayPal Order ID
  const pipeline = [
    ['SET', `ghost:order:${orderId}`, JSON.stringify(record)],
    ['ZADD', 'ghost:orders:index', String(Math.floor(Date.now() / 1000)), orderId],
  ];
  // If the record contains a paypal_order_id, store the mapping so idempotency
  // checks by PayPal Order ID resolve instantly without scanning all orders.
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

// Look up an order by its PayPal Order ID (returns the order record, not just the captureId)
async function _redisGetOrderByPaypalOrderId (paypalOrderId) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return null;
  const { default: fetch } = await import('node-fetch');
  // First: resolve the captureId from the secondary lookup key
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


/* ── Authoritative server-side plan catalogue ───────────────────────────────
   NEVER trust plan name, price, or currency sent from the browser.
   All billing values here are the single source of truth.
─────────────────────────────────────────────────────────────────────────── */
const PLAN_CATALOGUE = {
  pro: {
    id:         'pro',
    label:      'Ghost Pro (monthly)',
    priceUsd:   '7.00',
    tier:       'PRO',
    expiryDays: 30,
  },
  lifetime: {
    id:         'lifetime',
    label:      'Ghost Lifetime',
    priceUsd:   '79.00',
    tier:       'PRO',
    expiryDays: 0,
  },
};


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
  // Never log the token
  return _cachedToken;
}


/* ── PayPal Orders API helpers ───────────────────────────────────────────── */
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

async function _deliveryFetch (path, init = {}, timeoutMs = 25_000) {
  if (!DELIVERY_BACKEND_URL) throw new Error('GHOST_DELIVERY_URL is not configured.');
  const { default: fetch } = await import('node-fetch');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${DELIVERY_BACKEND_URL}${path}`, {
      ...init,
      signal: controller.signal,
    });
    return res;
  } finally {
    clearTimeout(timer);
  }
}


/* ── In-memory capture lock (prevents double-capture on rapid retries) ─────
   Maps PayPal orderID -> Promise<result> so that a second concurrent request
   for the same orderID waits for (and reuses) the first capture result.
─────────────────────────────────────────────────────────────────────────── */
const _captureInFlight = new Map();



/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/create-order
   ─────────────────────────────────────────────────────────────────────────
   Body:   { plan: string, email: string, discord: string }
   OK:     { ok: true, orderID: string }
   Error:  { ok: false, message: string, stage: 'create-order' }
  ──────────────────────────────────────────────────────────────────────────*/
async function createOrder (req, res) {
  const { plan: planRaw, email, discord } = req.body || {};

  const planId = (planRaw || '').trim().toLowerCase();
  const plan   = PLAN_CATALOGUE[planId];

  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: pro or lifetime.', stage: 'create-order' });
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

  try {
    const order = await _paypalRequest('POST', '/v2/checkout/orders', {
      intent: 'CAPTURE',
      purchase_units: [{
        reference_id: `ghost-${planId}-${Date.now()}`,
        description:  plan.label,
        amount: {
          currency_code: 'USD',
          value:         plan.priceUsd,
        },
        custom_id: JSON.stringify({
          plan:    planId,
          email:   email.trim(),
          discord: discord.trim(),
        }),
      }],
      application_context: {
        brand_name:          'Ghost',
        user_action:         'PAY_NOW',
        shipping_preference: 'NO_SHIPPING',
      },
    }, 15_000);   // 15-second timeout for order creation

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
   ─────────────────────────────────────────────────────────────────────────
   Body:   { orderID: string, email: string, discord: string, plan: string }
   OK:     { ok, paymentStatus, deliveryStatus, orderId, captureId, plan,
             amount, currency, licenseKey, downloadUrl, ... }
   Error:  { ok: false, message: string, stage: string }

   Security: this endpoint calls PayPal's capture API and verifies the
   returned capture status, amount, and currency BEFORE calling the
   delivery backend.  The order ID from the browser is only used to
   address the PayPal API call — all billing truth comes from PayPal's
   response, never from the client payload.
  ──────────────────────────────────────────────────────────────────────────*/
async function captureOrder (req, res) {
  const { orderID, email, discord, plan: planRaw } = req.body || {};

  if (!orderID) {
    return res.status(400).json({ ok: false, message: 'orderID is required.', stage: 'validate' });
  }
  const planId = (planRaw || '').trim().toLowerCase();
  const plan   = PLAN_CATALOGUE[planId];
  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan.', stage: 'validate' });
  }

  // ── Idempotency: check Redis directly first, then delivery backend ────────
  // Use the PayPal Order ID as the idempotency key — look for any stored order
  // with matching paypal_order_id.
  const _buildIdempotentResponse = (existing, planMeta) => ({
    ok:             true,
    paymentStatus:  'COMPLETED',
    deliveryStatus: existing.delivery_status || existing.deliveryStatus || 'delivered',
    orderId:        existing.order_id        || existing.orderId,
    captureId:      existing.paypal_capture_id || existing.captureId || existing.order_id,
    paypalOrderId:  orderID,
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
      'Download Ghost.',
      'Extract the ZIP if required.',
      'Launch Ghost.',
      'Log in or paste your license key.',
      'Click Activate.',
    ] : null,
  });

  // Check Redis first (fastest, no Python backend required)
  // Use the secondary lookup key ghost:paypal-order:{orderID} -> captureId -> order record
  try {
    const redisOrder = await _redisGetOrderByPaypalOrderId(orderID);
    if (redisOrder && redisOrder.paypal_order_id === orderID) {
      console.log('[ghost/paypal] captureOrder idempotent (Redis) — paypalOrderId=%s orderId=%s deliveryStatus=%s',
        orderID, redisOrder.order_id, redisOrder.delivery_status);
      return res.json(_buildIdempotentResponse(redisOrder, plan));
    }
  } catch (_) {}

  // Also check delivery backend for idempotency
  try {
    const existingRes = await _deliveryFetch(
      `/api/order/paypal-order:${encodeURIComponent(orderID)}`, {}, 8_000
    );
    if (existingRes.ok) {
      const existing = await existingRes.json().catch(() => null);
      if (existing && existing.ok) {
        console.log('[ghost/paypal] captureOrder idempotent (delivery) — paypalOrderId=%s orderId=%s',
          orderID, existing.order_id);
        return res.json(_buildIdempotentResponse(existing, plan));
      }
    }
  } catch (_) {
    // Idempotency check failed — proceed with normal capture
  }

  // ── Prevent concurrent double-capture for the same PayPal order ID ────────
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
    const result  = await _doCaptureOrder({ orderID, email, discord, planId, plan });
    resolveCapture(result);
    const status  = result.ok ? 200 : (result._status || 500);
    const payload = Object.assign({}, result);
    delete payload._status;
    return res.status(status).json(payload);
  } catch (err) {
    rejectCapture(err);
    return res.status(500).json({ ok: false, message: 'Internal error during capture.', stage: 'capture' });
  } finally {
    // Remove the lock after a short delay so genuine retries still work
    setTimeout(() => _captureInFlight.delete(orderID), 60_000);
  }
}


async function _doCaptureOrder ({ orderID, email, discord, planId, plan }) {
  // ── Step 1: Capture the order via PayPal Orders API ──────────────────────
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

  // ── Step 2: Verify the capture is COMPLETED ───────────────────────────────
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

  // ── Step 3: Extract and verify the capture unit ───────────────────────────
  const pu = (capture.purchase_units || [])[0];
  if (!pu) {
    console.error('[ghost/paypal] no purchase_units in capture orderID=%s', orderID);
    return {
      ok: false, message: 'Payment capture data invalid. Contact support.',
      stage: 'capture-parse', orderId: orderID, _status: 500,
    };
  }

  const captureUnit = (pu.payments?.captures || [])[0];
  if (!captureUnit || captureUnit.status !== 'COMPLETED') {
    console.error('[ghost/paypal] capture unit not COMPLETED orderID=%s unitStatus=%s',
      orderID, captureUnit?.status);
    return {
      ok: false, message: 'Capture unit is not complete. Contact support.',
      stage: 'capture-unit', orderId: orderID, _status: 400,
    };
  }

  const captureId        = captureUnit.id;
  const capturedAmount   = captureUnit.amount?.value;
  const capturedCurrency = captureUnit.amount?.currency_code;

  // ── Step 4: Verify amount and currency ───────────────────────────────────
  if (capturedCurrency !== 'USD') {
    console.error('[ghost/paypal] wrong currency orderID=%s currency=%s', orderID, capturedCurrency);
    return {
      ok: false, message: 'Unexpected payment currency. Contact support.',
      stage: 'capture-currency', orderId: orderID, _status: 400,
    };
  }
  if (parseFloat(capturedAmount) !== parseFloat(plan.priceUsd)) {
    console.error('[ghost/paypal] amount mismatch orderID=%s expected=%s got=%s',
      orderID, plan.priceUsd, capturedAmount);
    return {
      ok: false, message: 'Payment amount mismatch. Contact support.',
      stage: 'capture-amount', orderId: orderID, _status: 400,
    };
  }

  // ── Step 5: Resolve payer email ───────────────────────────────────────────
  const payerEmail    = capture.payer?.email_address || email || '';
  const resolvedEmail = payerEmail.trim() || (email || '').trim();
  const resolvedDiscord = (discord || '').trim();

  console.log('[ghost/paypal] capture verified orderID=%s captureId=%s plan=%s amount=%s',
    orderID, captureId, planId, capturedAmount);

  const purchaseDateNow = new Date().toISOString();

  // ── Step 6: Persist the order to Redis IMMEDIATELY after capture ──────────
  // This is the idempotency key — using the PayPal Order ID ensures that
  // even if the delivery backend call fails, the order is never lost.
  // The delivery backend will find this record when retried.
  const pendingOrderRecord = {
    order_id:          captureId,          // capture transaction ID = our internal order ID
    paypal_order_id:   orderID,            // PayPal order ID (idempotency key)
    paypal_capture_id: captureId,
    plan:              planId,
    plan_label:        plan.label,
    tier:              plan.tier,
    email:             resolvedEmail,
    discord:           resolvedDiscord,
    price_usd:         parseFloat(capturedAmount),
    currency:          capturedCurrency,
    created_at:        purchaseDateNow,
    payment_status:    'completed',
    payment_verified:  true,
    delivery_status:   'pending',          // will be updated to 'delivered' below
    license_key:       null,
    license_status:    'pending',
  };

  const savedToRedis = await _redisSaveOrder(captureId, pendingOrderRecord);
  if (savedToRedis) {
    console.log('[ghost/paypal] order persisted to Redis orderId=%s paypalOrderId=%s', captureId, orderID);
  } else {
    console.warn('[ghost/paypal] could not persist order to Redis orderId=%s — will rely on delivery backend', captureId);
  }

  // ── Step 7: Call the delivery backend to assign a license key ────────────
  let data;
  let deliveryFailed = false;
  try {
    const deliveryRes = await _deliveryFetch('/api/payment/confirm', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        order_id:          captureId,
        payment_token:     `paypal:${captureId}`,
        plan:              planId,
        email:             resolvedEmail,
        discord:           resolvedDiscord,
        price_usd:         parseFloat(capturedAmount),
        paypal_order_id:   orderID,
        paypal_capture_id: captureId,
      }),
    }, 25_000);

    data = await deliveryRes.json();
  } catch (err) {
    const isTimeout = err.name === 'AbortError';
    console.error('[ghost/paypal] delivery %s orderID=%s captureId=%s: %s',
      isTimeout ? 'timeout' : 'exception', orderID, captureId, err.message);
    // IMPORTANT: payment was captured and persisted to Redis — do NOT return ok:false.
    // Show delivery_pending so the user can retry, and the order is already in admin panel.
    deliveryFailed = true;
    data = { ok: false, error: isTimeout ? 'delivery_timeout' : 'delivery_unavailable' };
  }

  if (!data.ok) {
    console.error('[ghost/paypal] delivery failed orderID=%s captureId=%s error=%s — order saved as pending',
      orderID, captureId, data.error);
    // Order is already in Redis as pending — admin can retry from the Orders tab.
    return {
      ok:             true,
      paymentStatus:  'COMPLETED',
      deliveryStatus: 'delivery_pending',
      orderId:        captureId,
      captureId,
      paypalOrderId:  orderID,
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

  const purchaseDate = data.created_at || purchaseDateNow;

  // ── Step 8: Update Redis with the delivered state + license key ───────────
  const deliveredOrderRecord = {
    ...pendingOrderRecord,
    delivery_status:  data.delivery_status || 'delivered',
    license_key:      data.key             || null,
    license_status:   data.key ? 'active'  : 'pending',
    created_at:       purchaseDate,
    tier:             data.tier            || plan.tier,
  };
  await _redisSaveOrder(captureId, deliveredOrderRecord).catch(err =>
    console.warn('[ghost/paypal] Redis update after delivery failed orderId=%s: %s', captureId, err.message)
  );

  const successResponse = {
    ok:             true,
    paymentStatus:  'COMPLETED',
    deliveryStatus: data.delivery_status || 'delivered',
    orderId:        captureId,
    captureId,
    paypalOrderId:  orderID,
    plan:           planId,
    planLabel:      plan.label,
    amount:         capturedAmount,
    currency:       capturedCurrency,
    email:          resolvedEmail,
    discord:        resolvedDiscord,
    licenseKey:     data.key || null,
    licenseStatus:  data.key ? 'active' : null,
    purchaseDate,
    downloadUrl:    data.key ? `/api/order/${encodeURIComponent(captureId)}/download` : null,
    tier:           data.tier || plan.tier,
    instructions: [
      'Download Ghost.',
      'Extract the ZIP if required.',
      'Launch Ghost.',
      'Log in or paste your license key.',
      'Click Activate.',
    ],
  };

  console.log(
    '[ghost/paypal] capture complete — orderId=%s paymentStatus=%s deliveryStatus=%s ' +
    'plan=%s amount=%s licenseKey=%s',
    successResponse.orderId,
    successResponse.paymentStatus,
    successResponse.deliveryStatus,
    successResponse.plan,
    successResponse.amount,
    successResponse.licenseKey ? '[present]' : '[missing]',
  );

  return successResponse;
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/retry-fulfillment
   ─────────────────────────────────────────────────────────────────────────
   Retry license delivery for a PayPal order whose payment was captured
   successfully but whose license delivery failed (delivery_pending).

   Body:   { captureId: string }
   OK:     { ok, paymentStatus, deliveryStatus, orderId, licenseKey, downloadUrl, ... }
   Error:  { ok: false, message: string }

   This endpoint NEVER re-captures or re-charges the customer.
   It calls POST /api/order/:captureId/fulfill on the delivery backend,
   which is idempotent — if the order already has a license it is returned
   immediately without running keygen again.
  ──────────────────────────────────────────────────────────────────────────*/
async function retryFulfillment (req, res) {
  const { captureId } = req.body || {};

  if (!captureId || typeof captureId !== 'string' || !captureId.trim()) {
    return res.status(400).json({
      ok: false,
      message: 'captureId is required.',
      stage: 'retry-fulfillment',
    });
  }

  const id = captureId.trim();

  // ── Try delivery backend first (it handles key assignment from inventory) ─
  if (DELIVERY_BACKEND_URL) {
    try {
      const deliveryRes = await _deliveryFetch(`/api/order/${encodeURIComponent(id)}/fulfill`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({}),
      }, 25_000);

      const data = await deliveryRes.json().catch(() => ({}));

      if (deliveryRes.status === 404) {
        // Order not in delivery backend — fall through to check Redis directly
        console.warn('[ghost/paypal] retry-fulfillment: not in delivery backend captureId=%s, checking Redis', id);
      } else if (!data.ok) {
        console.error('[ghost/paypal] retry-fulfillment failed captureId=%s error=%s',
          id, data.error || data.message);
        return res.status(deliveryRes.status || 500).json({
          ok:      false,
          message: data.error || data.message || 'Fulfillment retry failed.',
          stage:   'retry-fulfillment',
        });
      } else {
        console.log('[ghost/paypal] retry-fulfillment success (delivery) captureId=%s deliveryStatus=%s',
          id, data.deliveryStatus || data.delivery_status);

        // If delivery succeeded and returned a license key, update Redis too
        if (data.key || data.license_key) {
          const existingRecord = await _redisGetOrder(id).catch(() => null);
          if (existingRecord) {
            await _redisSaveOrder(id, {
              ...existingRecord,
              delivery_status: data.delivery_status || 'delivered',
              license_key:     data.key || data.license_key || existingRecord.license_key,
              license_status:  'active',
            }).catch(() => {});
          }
        }

        return res.json({
          ok:             true,
          paymentStatus:  data.paymentStatus  || 'COMPLETED',
          deliveryStatus: data.deliveryStatus || data.delivery_status || 'delivered',
          orderId:        data.orderId        || data.order_id        || id,
          plan:           data.plan,
          planLabel:      data.planLabel      || data.plan_label,
          amount:         String(data.amount  || data.price_usd || ''),
          currency:       data.currency       || 'USD',
          purchaseDate:   data.purchaseDate   || data.created_at,
          licenseKey:     data.licenseKey     || data.license_key  || data.key || null,
          licenseStatus:  data.licenseStatus  || data.license_status || (data.licenseKey ? 'active' : null),
          downloadUrl:    data.downloadUrl    || data.download_url  || (data.orderId || id ? `/api/order/${encodeURIComponent(data.orderId || id)}/download` : null),
          tier:           data.tier,
        });
      }
    } catch (err) {
      const isTimeout = err.name === 'AbortError';
      console.warn('[ghost/paypal] retry-fulfillment delivery %s captureId=%s — falling back to Redis: %s',
        isTimeout ? 'timeout' : 'error', id, err.message);
      // Fall through to Redis check below
    }
  }

  // ── Redis-only path: return the stored order (fulfillment must happen via delivery backend) ─
  // When the delivery backend is unavailable we can at least confirm what is stored.
  const redisRecord = await _redisGetOrder(id).catch(() => null);
  if (!redisRecord) {
    return res.status(404).json({
      ok:      false,
      message: 'Order not found. Ensure GHOST_DELIVERY_URL is configured and the delivery backend is running.',
      stage:   'retry-fulfillment',
    });
  }

  const licenseKey = redisRecord.license_key || null;
  return res.json({
    ok:             true,
    paymentStatus:  redisRecord.payment_status  || 'COMPLETED',
    deliveryStatus: redisRecord.delivery_status || 'pending',
    orderId:        redisRecord.order_id        || id,
    plan:           redisRecord.plan,
    planLabel:      redisRecord.plan_label,
    amount:         String(redisRecord.price_usd || ''),
    currency:       redisRecord.currency        || 'USD',
    purchaseDate:   redisRecord.created_at,
    licenseKey:     licenseKey,
    licenseStatus:  redisRecord.license_status  || (licenseKey ? 'active' : 'pending'),
    downloadUrl:    licenseKey ? `/api/order/${encodeURIComponent(id)}/download` : null,
    tier:           redisRecord.tier,
  });
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/webhook
   ─────────────────────────────────────────────────────────────────────────
   PayPal delivers PAYMENT.CAPTURE.COMPLETED events here.
   Verifies the webhook signature before processing.
   Returns 200 for all known events (including duplicates) so PayPal
   does not retry.
  ──────────────────────────────────────────────────────────────────────────*/
async function handleWebhook (req, res) {
  // ── Step 1: Verify webhook signature ─────────────────────────────────────
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
    console.warn('[ghost/paypal/webhook] PAYPAL_WEBHOOK_ID not set — cannot verify signature');
    // In sandbox without a webhook ID, we log and skip verification
    // but still process the event. In production this must be set.
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

  // ── Only process PAYMENT.CAPTURE.COMPLETED ────────────────────────────────
  if (eventType !== 'PAYMENT.CAPTURE.COMPLETED') {
    // Acknowledge all other event types without processing
    return res.json({ ok: true, received: true, processed: false });
  }

  const resource     = event.resource || {};
  const captureId    = resource.id || '';
  const captureStatus = resource.status || '';
  const amount       = resource.amount?.value || '';
  const currency     = resource.amount?.currency_code || 'USD';

  if (captureStatus !== 'COMPLETED') {
    console.log('[ghost/paypal/webhook] Capture not COMPLETED captureId=%s status=%s',
      captureId, captureStatus);
    return res.json({ ok: true, received: true, processed: false });
  }

  // ── Extract custom_id from the supplementary purchase unit ───────────────
  const purchaseUnit = (resource.supplementary_data?.related_ids) || {};
  const orderId      = resource.supplementary_data?.related_ids?.order_id || '';

  // Try to parse custom_id that was set during create-order
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
    // Still return 200 so PayPal doesn't retry; log for manual review
    return res.json({ ok: true, received: true, processed: false, note: 'missing_fields' });
  }

  const planMeta = PLAN_CATALOGUE[plan];
  if (!planMeta) {
    console.warn('[ghost/paypal/webhook] Unknown plan=%s captureId=%s', plan, captureId);
    return res.json({ ok: true, received: true, processed: false, note: 'unknown_plan' });
  }

  // ── Call delivery backend (idempotent — safe to call again for duplicates) ─
  try {
    const deliveryRes = await _deliveryFetch('/api/payment/confirm', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        order_id:          captureId,
        payment_token:     `paypal:${captureId}`,
        plan,
        email,
        discord,
        price_usd:         parseFloat(amount),
        paypal_order_id:   orderId,
        paypal_capture_id: captureId,
      }),
    }, 25_000);

    const data = await deliveryRes.json();

    if (data.ok) {
      console.log('[ghost/paypal/webhook] Delivery confirmed captureId=%s plan=%s', captureId, plan);
    } else {
      console.error('[ghost/paypal/webhook] Delivery failed captureId=%s error=%s',
        captureId, data.error);
    }

    return res.json({ ok: true, received: true, processed: data.ok });

  } catch (err) {
    console.error('[ghost/paypal/webhook] Delivery error captureId=%s: %s',
      captureId, err.message);
    // Return 200 to prevent PayPal from retrying; delivery will be handled by support
    return res.json({ ok: true, received: true, processed: false, error: 'delivery_unavailable' });
  }
}


module.exports = { createOrder, captureOrder, handleWebhook, retryFulfillment, PLAN_CATALOGUE };
