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
 *   GHOST_DELIVERY_URL     — Base URL of the Python license_delivery server
 *                            MUST be a fully-qualified public URL in production.
 *                            Do NOT use localhost — the delivery server is a separate
 *                            process that Vercel serverless functions cannot reach.
 *
 * Handled events
 * ──────────────
 *   checkout.session.completed
 *       Payment succeeded (one-time or first sub payment).
 *       -> Calls the Python delivery backend to record the order and
 *         generate a GHOST license key.  The session ID is used as the
 *         unique order key, preventing duplicates.
 *
 *   checkout.session.expired
 *       Session expired without payment.
 *       -> Marks the order as 'expired' if a pending record exists.
 *
 *   invoice.payment_failed   (subscriptions)
 *       Renewal payment failed.
 *       -> Marks the subscription order as 'payment_failed'.
 *
 *   charge.refunded
 *       Charge fully refunded.
 *       -> Marks the order as 'refunded'.  Key revocation is a manual
 *         step for the admin — this handler does NOT auto-revoke keys
 *         because the Python license_manager.py handles that.
 *
 *   customer.subscription.deleted
 *       Subscription cancelled / not renewed.
 *       -> Marks the order as 'cancelled'.
 * =====================================================
 */

'use strict';

const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const WEBHOOK_SECRET = process.env.STRIPE_WEBHOOK_SECRET;

// GHOST_DELIVERY_URL must be set to the deployed Python delivery server.
// Do NOT fall back to localhost — that will silently fail in production
// because the delivery server is a separate process not colocated with
// the Node.js server on Vercel or any other PaaS.
const DELIVERY_BACKEND_URL = (process.env.GHOST_DELIVERY_URL || '').replace(/\/$/, '');

if (!DELIVERY_BACKEND_URL) {
  console.error(
    '[ghost/webhook] FATAL: GHOST_DELIVERY_URL is not set. ' +
    'Webhook events will not trigger license delivery until this is configured.',
  );
}

/* ── Plan catalogue (mirrors api/checkout.js — kept in sync manually) ─────── */
const PLAN_TIER = {
  trial:    { tier: 'TRIAL', expiryDays: 7  },
  pro:      { tier: 'PRO',   expiryDays: 30 },
  lifetime: { tier: 'PRO',   expiryDays: 0  },
};

/* ── Utility ─────────────────────────────────────────────────────────────── */
async function _deliveryFetch (path, init = {}) {
  if (!DELIVERY_BACKEND_URL) {
    throw new Error('GHOST_DELIVERY_URL is not configured.');
  }
  const fetch = (await import('node-fetch')).default;
  return fetch(`${DELIVERY_BACKEND_URL}${path}`, init);
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
    // Never log full error stacks with PII in production — truncate to message only
    detail ? String(detail).slice(0, 400) : '',
  );
}


/**
 * Mark an order's payment status by forwarding to the Python backend.
 * Uses PATCH /api/order/<order_id>/status  (added to license_delivery.py).
 */
async function _updateOrderStatus (orderId, status, extra = {}) {
  try {
    const res = await _deliveryFetch(`/api/order/${encodeURIComponent(orderId)}/status`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ status, ...extra }),
    });
    return res.ok;
  } catch (err) {
    console.error('[ghost/webhook] _updateOrderStatus failed:', err.message);
    return false;
  }
}


/**
 * Handle checkout.session.completed
 * -----------------------------------
 * This is the ONLY place where a GHOST license key is generated for a
 * Stripe payment.  We use the Checkout Session ID as the unique order ID
 * so that re-delivery of the same webhook event does not produce a
 * duplicate key (the Python backend is idempotent on order_id).
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
  if (!discord) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id }, 'missing_discord_metadata');
    return;
  }

  // Resolve actual amount paid from Stripe (cents -> dollars).
  // When amount_total is null (e.g. free trial), fall back to the plan's
  // catalogue price using the planId string — not the plan object.
  const amountTotal = session.amount_total != null
    ? session.amount_total / 100
    : planId === 'lifetime' ? 79 : planId === 'trial' ? 0 : 7;

  try {
    const deliveryRes = await _deliveryFetch('/api/payment/confirm', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        order_id:          orderId,
        payment_token:     `stripe:${session.payment_intent || session.id}`,
        plan:              planId,
        email,
        discord,
        price_usd:         amountTotal,
        stripe_session_id: session.id,
      }),
    });

    const data = await deliveryRes.json();

    if (!data.ok) {
      _logWebhookError({ type: 'checkout.session.completed', id: session.id },
        'delivery_failed', data.error);
    } else {
      _logWebhookEvent({ type: 'checkout.session.completed', id: session.id, livemode: session.livemode },
        { key_issued: data.key ? data.key.slice(0, 10) + '...' : 'none', plan: planId });
    }
  } catch (err) {
    _logWebhookError({ type: 'checkout.session.completed', id: session.id },
      'delivery_exception', err.message);
  }
}


/**
 * Handle checkout.session.expired
 * Marks any pending/pre-created order for this session as expired.
 */
async function handleSessionExpired (session) {
  _logWebhookEvent({ type: 'checkout.session.expired', id: session.id, livemode: session.livemode });
  await _updateOrderStatus(session.id, 'expired');
}


/**
 * Handle invoice.payment_failed (subscription renewals)
 */
async function handleInvoicePaymentFailed (invoice) {
  _logWebhookEvent({ type: 'invoice.payment_failed', id: invoice.id, livemode: invoice.livemode });
  // subscription_id lives on the invoice; use it as fallback order identifier
  const orderId = invoice.subscription || invoice.id;
  await _updateOrderStatus(orderId, 'payment_failed', { invoice_id: invoice.id });
}


/**
 * Handle charge.refunded
 */
async function handleChargeRefunded (charge) {
  _logWebhookEvent({ type: 'charge.refunded', id: charge.id, livemode: charge.livemode });
  // Resolve the Checkout Session ID from the payment intent if present
  const orderId = charge.metadata?.order_id || charge.payment_intent || charge.id;
  await _updateOrderStatus(orderId, 'refunded', {
    refund_amount: (charge.amount_refunded / 100).toFixed(2),
  });
}


/**
 * Handle customer.subscription.deleted
 */
async function handleSubscriptionDeleted (sub) {
  _logWebhookEvent({ type: 'customer.subscription.deleted', id: sub.id, livemode: sub.livemode });
  await _updateOrderStatus(sub.id, 'cancelled');
}


/**
 * Express route handler: POST /api/stripe/webhook
 *
 * Must receive the raw request body (Buffer), not parsed JSON.
 * In server.js wire it BEFORE express.json():
 *
 *   app.post('/api/stripe/webhook',
 *     express.raw({ type: 'application/json' }),
 *     stripeWebhook.handler,
 *   );
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
    // constructEvent requires the raw body Buffer — ensured by express.raw()
    event = stripe.webhooks.constructEvent(req.body, sig, WEBHOOK_SECRET);
  } catch (err) {
    // Signature mismatch: reject — log only the message, never the payload
    console.error('[ghost/webhook] Signature verification failed:', err.message);
    return res.status(400).json({ error: 'Webhook signature verification failed.' });
  }

  // Acknowledge receipt immediately — Stripe retries if we don't respond 200 within 30s
  res.status(200).json({ received: true });

  // Process the event asynchronously (after the 200 ACK)
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
          // Unhandled events are silently ignored (Stripe will stop sending
          // unsubscribed events if you configure your webhook endpoint correctly)
          break;
      }
    } catch (err) {
      // Non-fatal: we already returned 200 — log but do not crash the process
      _logWebhookError(event, 'unhandled_exception', err.message);
    }
  });
}


module.exports = { handler };
