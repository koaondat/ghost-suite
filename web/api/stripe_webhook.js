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

/* ── Plan catalogue ──────────────────────────────────────────────────────── */
const PLAN_TIER = {
  trial:    { tier: 'TRIAL', expiryDays: 7  },
  pro:      { tier: 'PRO',   expiryDays: 30 },
  lifetime: { tier: 'PRO',   expiryDays: 0  },
};

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

  const meta    = session.metadata || {};
  const planId  = (meta.plan    || '').toLowerCase();
  const discord = (meta.discord || '').trim();
  const email   = session.customer_email || session.customer_details?.email || '';
  const orderId = session.id;  // Stripe session ID = unique order ID

  if (!planId || !PLAN_TIER[planId]) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id }, 'unknown_plan', planId);
    return;
  }
  if (!email) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id }, 'missing_email');
    return;
  }

  const amountTotal = session.amount_total != null
    ? session.amount_total / 100
    : planId === 'lifetime' ? 79 : planId === 'trial' ? 0 : 7;

  const planMeta = PLAN_TIER[planId];

  // Write order to Redis so fulfillOrder can find it
  await _redisSaveOrder(orderId, {
    order_id:          orderId,
    stripe_session_id: session.id,
    plan:              planId,
    plan_label:        planId === 'lifetime' ? 'Ghost Lifetime'
                     : planId === 'pro'      ? 'Ghost Pro (monthly)'
                     : 'Ghost Trial (free)',
    tier:              planMeta.tier,
    email,
    discord,
    price_usd:         amountTotal,
    currency:          'USD',
    created_at:        new Date().toISOString(),
    payment_status:    'completed',
    payment_verified:  true,
    delivery_status:   'pending',
    license_key:       null,
    license_status:    'pending',
  }).catch(() => {});

  try {
    // fulfillOrder is the single fulfillment path — reads ghost:inventory directly
    const { fulfillOrder } = require('./paypal');
    const result = await fulfillOrder(orderId);

    if (!result.ok) {
      _logWebhookError({ type: 'checkout.session.completed', id: session.id },
        'fulfillment_failed', result.reason);
    } else {
      _logWebhookEvent({ type: 'checkout.session.completed', id: session.id, livemode: session.livemode },
        { key_issued: '[present]', plan: planId });
    }
  } catch (err) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id },
      'fulfillment_exception', err.message);
  }
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
