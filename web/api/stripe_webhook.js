/**
 * api/stripe_webhook.js — Ghost Stripe Webhook Handler
 * =====================================================
 * Registered in server.js at:
 *   POST /api/stripe/webhook
 *
 * CRITICAL: this route MUST receive the raw (unparsed) request body
 * so that Stripe's signature verification works.  In server.js, apply
 * express.raw({ type: 'application/json' }) BEFORE express.json() and
 * mount this route before the JSON body-parser middleware.
 *
 * Environment variables required:
 *   STRIPE_SECRET_KEY      — Stripe secret key  (sk_live_... / sk_test_...)
 *   STRIPE_WEBHOOK_SECRET  — Webhook signing secret from Stripe dashboard (whsec_...)
 *   UPSTASH_REDIS_REST_URL  — Upstash Redis REST endpoint
 *   UPSTASH_REDIS_REST_TOKEN— Upstash Redis REST bearer token
 *
 * Handled events
 * ──────────────
 *   checkout.session.completed
 *       Payment succeeded (one-time or first sub payment).
 *       -> Writes a completed order to Redis, then calls fulfillOrder()
 *          to assign a license key from ghost:inventory.
 *
 *   checkout.session.expired
 *       Session expired without payment.
 *       -> Marks the order as 'expired' in Redis if a record exists.
 *
 *   invoice.payment_failed   (subscriptions)
 *       Renewal payment failed.
 *       -> Marks the subscription order as 'payment_failed' in Redis.
 *
 *   charge.refunded
 *       Charge fully refunded.
 *       -> Marks the order as 'refunded' in Redis.
 *
 *   customer.subscription.deleted
 *       Subscription cancelled / not renewed.
 *       -> Marks the order as 'cancelled' in Redis.
 * =====================================================
 */

'use strict';

const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const WEBHOOK_SECRET = process.env.STRIPE_WEBHOOK_SECRET;

const _REDIS_URL   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
const _REDIS_TOKEN = (process.env.UPSTASH_REDIS_REST_TOKEN || '');

/* ── Duration → expiry-days mapping ─────────────────────────────────────── */
const DURATION_DAYS = { day: 1, '3days': 3, week: 7, month: 30, '3months': 90 };

/* ── Plan catalogue ──────────────────────────────────────────────────────── */
const PLAN_TIER = {
  day:      { tier: 'PRO', expiryDays: 1  },
  '3days':  { tier: 'PRO', expiryDays: 3  },
  week:     { tier: 'PRO', expiryDays: 7  },
  month:    { tier: 'PRO', expiryDays: 30 },
  '3months':{ tier: 'PRO', expiryDays: 90 },
  // Legacy aliases kept for backward compatibility
  pro:      { tier: 'PRO', expiryDays: 30 },
  lifetime: { tier: 'PRO', expiryDays: 90 },
  trial:    { tier: 'PRO', expiryDays: 1  },
};

/* ── Normalize plan slug (must match paypal.js) ─────────────────────────── */
function _normalizePlan (plan) {
  if (!plan) return '';
  const aliases = {
    day: 'day', '1day': 'day',
    '3days': '3days',
    week: 'week', '7day': 'week', '7days': 'week',
    month: 'month', '30day': 'month', '30days': 'month',
    '3months': '3months', '90day': '3months', '90days': '3months',
    pro: 'month', monthly: 'month', lifetime: '3months', trial: 'day',
  };
  return aliases[String(plan).trim().toLowerCase()] || String(plan).trim().toLowerCase();
}

/* ── Generate a license key (GHOST-XXXX-XXXX-XXXX-XXXX) ─────────────────── */
function _generateLicenseKey () {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const seg   = () => Array.from({ length: 4 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
  return `GHOST-${seg()}-${seg()}-${seg()}-${seg()}`;
}

/* ── Compute expiration ISO date from plan ───────────────────────────────── */
function _computeExpiry (planId) {
  const days = DURATION_DAYS[planId];
  if (!days) return null;
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString();
}

/* ── Save generated key to ghost:inventory ───────────────────────────────── */
async function _saveKeyToInventory (keyRecord) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return false;
  const { default: fetch } = await import('node-fetch');
  try {
    // Read current inventory
    const invKey = encodeURIComponent('ghost:inventory');
    const getRes = await fetch(`${_REDIS_URL}/GET/${invKey}`, {
      headers: { Authorization: `Bearer ${_REDIS_TOKEN}` },
    });
    let inventory = [];
    if (getRes.ok) {
      const body = await getRes.json().catch(() => null);
      const raw = body?.result;
      inventory = Array.isArray(raw) ? raw
        : (typeof raw === 'string' ? JSON.parse(raw) : []);
    }
    inventory.push(keyRecord);
    // Write back
    const setRes = await fetch(`${_REDIS_URL}/SET/ghost:inventory`, {
      method:  'POST',
      headers: { Authorization: `Bearer ${_REDIS_TOKEN}`, 'Content-Type': 'application/json' },
      body:    JSON.stringify(JSON.stringify(inventory)),
    });
    return setRes.ok;
  } catch (_) { return false; }
}

/* ── Redis helpers ───────────────────────────────────────────────────────── */
async function _redisSaveOrder (orderId, record) {
  if (!_REDIS_URL || !_REDIS_TOKEN) return false;
  const { default: fetch } = await import('node-fetch');
  const pipeline = [
    ['SET', `ghost:order:${orderId}`, JSON.stringify(record)],
    ['ZADD', 'ghost:orders:index', String(Math.floor(Date.now() / 1000)), orderId],
  ];
  try {
    const res = await fetch(`${_REDIS_URL}/pipeline`, {
      method:  'POST',
      headers: {
        Authorization:  `Bearer ${_REDIS_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(pipeline),
    });
    return res.ok;
  } catch (_) { return false; }
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
    if (!body || body.result == null) return null;
    return typeof body.result === 'string' ? JSON.parse(body.result) : body.result;
  } catch (_) { return null; }
}

async function _redisUpdateOrderStatus (orderId, status, extra = {}) {
  const existing = await _redisGetOrder(orderId).catch(() => null);
  if (!existing) return false;
  return _redisSaveOrder(orderId, { ...existing, ...extra, status, payment_status: status });
}

/** Safe JSON log — never log raw Stripe payloads in full production logs. */
function _logWebhookEvent (event, extra = {}) {
  console.log(
    '[ghost/webhook]',
    event.type,
    'id=' + event.id,
    'livemode=' + event.livemode,
    extra,
  );
}

function _logWebhookError (event, reason, detail = '') {
  console.error(
    '[ghost/webhook] ERROR',
    event ? event.type : '?',
    'id=' + (event ? event.id : '?'),
    reason,
    detail ? String(detail).slice(0, 400) : '',
  );
}


/**
 * Handle checkout.session.completed
 * -----------------------------------
 * Writes the order to Redis and then calls fulfillOrder() to assign a key.
 * The Checkout Session ID is used as the order ID — idempotent.
 */
async function handleSessionCompleted (session) {
  _logWebhookEvent({ type: 'checkout.session.completed', id: session.id, livemode: session.livemode });

  const meta          = session.metadata || {};
  const rawPlan       = (meta.plan    || '').toLowerCase();
  const planId        = _normalizePlan(rawPlan);
  const discord       = (meta.discord || '').trim();
  const email         = session.customer_email || session.customer_details?.email || '';
  const orderId       = session.id;  // Stripe session ID = unique order ID

  if (!planId || !PLAN_TIER[planId]) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id }, 'unknown_plan', planId);
    return;
  }
  if (!email) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id }, 'missing_email');
    return;
  }

  // ── Idempotency: check if already fulfilled ────────────────────────────
  const existingOrder = await _redisGetOrder(orderId).catch(() => null);
  if (existingOrder && existingOrder.license_key && existingOrder.delivery_status === 'delivered') {
    _logWebhookEvent({ type: 'checkout.session.completed', id: session.id, livemode: session.livemode },
      { idempotent: true, key_issued: '[present]' });
    return;
  }

  const amountTotal = session.amount_total != null
    ? session.amount_total / 100
    : 0;

  const planMeta  = PLAN_TIER[planId];
  const now       = new Date().toISOString();
  const expiresAt = _computeExpiry(planId);

  // ── Auto-generate a brand-new license key server-side ──────────────────
  const generatedKey = _generateLicenseKey();

  // ── Save generated key to inventory ────────────────────────────────────
  await _saveKeyToInventory({
    key:             generatedKey,
    plan:            planId,
    duration:        planId,
    status:          'sold',
    customer:        email,
    customer_email:  email,
    assigned_user:   discord,
    order_id:        orderId,
    purchase_date:   now,
    created_date:    now,
    added_at:        now,
    expiration:      expiresAt,
    expires_at:      expiresAt,
    payment_id:      orderId,
    notes:           `Stripe auto-generated — order: ${orderId}`,
    hwid:            '',
  }).catch(() => {});

  // ── Persist completed order with license key ────────────────────────────
  await _redisSaveOrder(orderId, {
    order_id:          orderId,
    stripe_session_id: session.id,
    plan:              planId,
    duration:          planId,
    plan_label:        planMeta ? `Phantom ${planId}` : planId,
    tier:              planMeta ? planMeta.tier : 'PRO',
    email,
    discord,
    price_usd:         amountTotal,
    currency:          'USD',
    created_at:        now,
    payment_status:    'completed',
    payment_verified:  true,
    delivery_status:   'delivered',
    license_key:       generatedKey,
    license_status:    'active',
    status:            'completed',
    fulfilled_at:      now,
    expires_at:        expiresAt,
  }).catch(() => {});

  _logWebhookEvent({ type: 'checkout.session.completed', id: session.id, livemode: session.livemode },
    { key_issued: '[present]', plan: planId, expiresAt });
}


/**
 * Handle checkout.session.expired
 */
async function handleSessionExpired (session) {
  _logWebhookEvent({ type: 'checkout.session.expired', id: session.id, livemode: session.livemode });
  await _redisUpdateOrderStatus(session.id, 'expired').catch(() => {});
}


/**
 * Handle invoice.payment_failed (subscription renewals)
 */
async function handleInvoicePaymentFailed (invoice) {
  _logWebhookEvent({ type: 'invoice.payment_failed', id: invoice.id, livemode: invoice.livemode });
  const orderId = invoice.subscription || invoice.id;
  await _redisUpdateOrderStatus(orderId, 'payment_failed', { invoice_id: invoice.id }).catch(() => {});
}


/**
 * Handle charge.refunded
 */
async function handleChargeRefunded (charge) {
  _logWebhookEvent({ type: 'charge.refunded', id: charge.id, livemode: charge.livemode });
  const orderId = charge.metadata?.order_id || charge.payment_intent || charge.id;
  await _redisUpdateOrderStatus(orderId, 'refunded', {
    refund_amount: (charge.amount_refunded / 100).toFixed(2),
  }).catch(() => {});
}


/**
 * Handle customer.subscription.deleted
 */
async function handleSubscriptionDeleted (sub) {
  _logWebhookEvent({ type: 'customer.subscription.deleted', id: sub.id, livemode: sub.livemode });
  await _redisUpdateOrderStatus(sub.id, 'cancelled').catch(() => {});
}


/**
 * Express route handler: POST /api/stripe/webhook
 */
async function handler (req, res) {
  if (!WEBHOOK_SECRET) {
    console.error('[ghost/webhook] STRIPE_WEBHOOK_SECRET is not set — refusing all webhook events');
    return res.status(500).json({ error: 'Webhook secret not configured.' });
  }

  const sig = req.headers['stripe-signature'];
  if (!sig) {
    return res.status(400).json({ error: 'Missing stripe-signature header.' });
  }

  let event;
  try {
    event = stripe.webhooks.constructEvent(req.body, sig, WEBHOOK_SECRET);
  } catch (err) {
    console.error('[ghost/webhook] Signature verification failed:', err.message);
    return res.status(400).json({ error: 'Webhook signature verification failed.' });
  }

  // Acknowledge receipt immediately
  res.status(200).json({ received: true });

  // Process asynchronously after the 200 ACK
  setImmediate(async () => {
    try {
      switch (event.type) {
        case 'checkout.session.completed':
          await handleSessionCompleted(event.data.object);
          break;
        case 'checkout.session.expired':
          await handleSessionExpired(event.data.object);
          break;
        case 'invoice.payment_failed':
          await handleInvoicePaymentFailed(event.data.object);
          break;
        case 'charge.refunded':
          await handleChargeRefunded(event.data.object);
          break;
        case 'customer.subscription.deleted':
          await handleSubscriptionDeleted(event.data.object);
          break;
        default:
          break;
      }
    } catch (err) {
      _logWebhookError(event, 'unhandled_exception', err.message);
    }
  });
}


module.exports = { handler };
