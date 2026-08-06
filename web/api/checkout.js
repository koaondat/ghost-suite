/**
 * api/checkout.js — Ghost Checkout API (Stripe Checkout Sessions)
 * ================================================================
 * All plan metadata lives here on the server.  The frontend sends
 * only a plan slug — prices, labels, and modes are resolved here
 * and never trusted from the client.
 *
 * Routes (registered in server.js):
 *   POST /api/checkout/create-session   → create a Stripe Checkout Session
 *   POST /api/checkout/validate-coupon  → validate a Stripe coupon/promo code
 *   GET  /api/order/:sessionId          → retrieve a stored order record
 *
 * Secrets required (via environment variables — never hardcoded):
 *   STRIPE_SECRET_KEY       sk_live_… or sk_test_…
 *   STRIPE_WEBHOOK_SECRET   whsec_…  (used only in stripe_webhook.js)
 *   BASE_URL                https://yourdomain.com
 * ================================================================
 */

'use strict';

const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

/* ── Authoritative server-side plan catalogue ───────────────────────────────
   NEVER trust plan name, price, or mode sent by the frontend.
   All values here are the single source of truth.
   'trial' is a free plan — Stripe Checkout is skipped; key is issued directly.
─────────────────────────────────────────────────────────────────────────── */
const PLAN_CATALOGUE = {
  trial: {
    id:         'trial',
    label:      'Ghost Trial (free)',
    priceUsd:   0,
    mode:       'free',      // special: no Stripe session needed
    tier:       'TRIAL',
    expiryDays: 7,
  },
  pro: {
    id:         'pro',
    label:      'Ghost Pro (monthly)',
    priceUsd:   7,
    mode:       'subscription',
    tier:       'PRO',
    expiryDays: 30,
    recurring:  { interval: 'month' },
  },
  lifetime: {
    id:         'lifetime',
    label:      'Ghost Lifetime',
    priceUsd:   79,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 0,           // 0 = never expires
  },
};


/* ── Shared order storage proxy ─────────────────────────────────────────────
   Calls forwarded to the Python license_delivery backend.
   Replace DELIVERY_BACKEND_URL via env if the Python server is on a
   different host (e.g. a container, a VPS, or a serverless function).
─────────────────────────────────────────────────────────────────────────── */
const DELIVERY_BACKEND_URL = process.env.GHOST_DELIVERY_URL || 'http://localhost:5055';


/* ── Utility: safe fetch wrapper ────────────────────────────────────────── */
async function _deliveryFetch (path, init = {}) {
  const fetch = (await import('node-fetch')).default;
  return fetch(`${DELIVERY_BACKEND_URL}${path}`, init);
}


/**
 * POST /api/checkout/create-session
 * -----------------------------------
 * Body:   { plan: string, email: string, discord: string, coupon?: string }
 * OK:     { ok: true, sessionId: string, checkoutUrl: string }
 *         — or for free trial: { ok: true, free: true, orderId, key, tier }
 * Error:  { ok: false, message: string }
 */
async function createSession (req, res) {
  const { plan: planRaw, email, discord, coupon } = req.body || {};

  /* ── Input validation ────────────────────────────────────────────────── */
  const planId = (planRaw || '').trim().toLowerCase();
  if (!PLAN_CATALOGUE[planId]) {
    return res.status(400).json({ ok: false, message: 'Invalid plan.' });
  }
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ ok: false, message: 'A valid email address is required.' });
  }
  if (!discord || discord.trim().length < 2) {
    return res.status(400).json({ ok: false, message: 'Discord username is required.' });
  }

  const plan = PLAN_CATALOGUE[planId];

  /* ── Free trial: bypass Stripe and issue key directly ────────────────── */
  if (plan.mode === 'free') {
    try {
      const orderId = 'GHOST-TRIAL-' + Date.now().toString(36).toUpperCase();
      const deliveryRes = await _deliveryFetch('/api/payment/confirm', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          order_id:      orderId,
          payment_token: 'FREE_TRIAL',
          plan:          planId,
          email:         email.trim(),
          discord:       discord.trim(),
          price_usd:     0,
        }),
      });
      const data = await deliveryRes.json();
      if (!data.ok) {
        return res.status(500).json({ ok: false, message: data.error || 'Trial activation failed.' });
      }
      return res.json({ ok: true, free: true, orderId, key: data.key, tier: data.tier });
    } catch (err) {
      console.error('[ghost/checkout] free-trial delivery error:', err);
      return res.status(502).json({ ok: false, message: 'License delivery service unavailable.' });
    }
  }

  /* ── Resolve Stripe discount coupon ─────────────────────────────────── */
  let stripeCouponId;
  if (coupon) {
    try {
      // Validate the promo code against Stripe — never trust client-side discount amounts
      const promoCodes = await stripe.promotionCodes.list({ code: coupon.trim().toUpperCase(), limit: 1, active: true });
      if (promoCodes.data.length > 0) {
        stripeCouponId = promoCodes.data[0].id;
      }
      // If not found as promo code, silently skip — invalid coupons are not applied
    } catch (err) {
      console.warn('[ghost/checkout] coupon lookup failed:', err.message);
      // Non-fatal: proceed without discount
    }
  }

  /* ── Build Stripe Checkout Session ─────────────────────────────────── */
  const baseUrl = (process.env.BASE_URL || 'http://localhost:3000').replace(/\/$/, '');

  const lineItem = {
    price_data: {
      currency:     'usd',
      unit_amount:  Math.round(plan.priceUsd * 100),  // Stripe uses cents
      product_data: { name: plan.label },
      ...(plan.mode === 'subscription' && { recurring: plan.recurring }),
    },
    quantity: 1,
  };

  const sessionParams = {
    mode:              plan.mode,
    customer_email:    email.trim(),
    line_items:        [lineItem],
    success_url:       `${baseUrl}/checkout.html?state=success&session_id={CHECKOUT_SESSION_ID}`,
    cancel_url:        `${baseUrl}/checkout.html?plan=${planId}&state=cancelled`,
    metadata: {
      plan:    planId,
      discord: discord.trim(),
      coupon:  coupon ? coupon.trim().toUpperCase() : '',
    },
    // Collect billing address for fraud prevention
    billing_address_collection: 'auto',
    // Allow promotion codes to be applied at checkout if not already supplied
    allow_promotion_codes: !stripeCouponId,
  };

  // Apply the validated coupon if one was resolved
  if (stripeCouponId) {
    sessionParams.discounts = [{ promotion_code: stripeCouponId }];
  }

  // For subscriptions, allow customers to manage their sub via portal
  if (plan.mode === 'subscription') {
    sessionParams.subscription_data = {
      metadata: { plan: planId, discord: discord.trim() },
    };
  }

  try {
    const session = await stripe.checkout.sessions.create(sessionParams);
    return res.json({ ok: true, sessionId: session.id, checkoutUrl: session.url });
  } catch (err) {
    console.error('[ghost/checkout] Stripe session creation error:', err);
    const msg = err.type === 'StripeInvalidRequestError'
      ? err.message
      : 'Payment provider error. Please try again.';
    return res.status(502).json({ ok: false, message: msg });
  }
}


/**
 * POST /api/checkout/validate-coupon
 * ------------------------------------
 * Validates a Stripe promotion code for a given plan.
 * Body:   { code: string, plan: string }
 * OK:     { ok: true, discountPct: number, label: string }
 * Error:  { ok: false, message: string }
 */
async function validateCoupon (req, res) {
  const { code, plan: planRaw } = req.body || {};

  if (!code || typeof code !== 'string') {
    return res.status(400).json({ ok: false, message: 'Coupon code is required.' });
  }
  const planId = (planRaw || '').trim().toLowerCase();
  if (!PLAN_CATALOGUE[planId]) {
    return res.status(400).json({ ok: false, message: 'Invalid plan.' });
  }

  try {
    const promoCodes = await stripe.promotionCodes.list({
      code:   code.trim().toUpperCase(),
      limit:  1,
      active: true,
    });

    if (promoCodes.data.length === 0) {
      return res.json({ ok: false, message: 'This coupon code is invalid or has expired.' });
    }

    const promo  = promoCodes.data[0];
    const coupon = promo.coupon;

    if (!coupon.valid) {
      return res.json({ ok: false, message: 'This coupon is no longer valid.' });
    }

    const discountPct = coupon.percent_off || 0;
    const label       = coupon.name || `${discountPct}% off`;

    return res.json({ ok: true, discountPct, label });
  } catch (err) {
    console.error('[ghost/checkout] coupon validation error:', err);
    return res.status(502).json({ ok: false, message: 'Could not validate coupon. Please try again.' });
  }
}


/**
 * GET /api/order/:sessionId
 * --------------------------
 * Proxy to the Python delivery backend order lookup.
 * Used by the checkout success page and dashboard to retrieve
 * order status + license key after a Stripe redirect.
 */
async function getOrder (req, res) {
  const { sessionId } = req.params || {};
  if (!sessionId) return res.status(400).json({ ok: false, error: 'sessionId is required.' });

  try {
    const deliveryRes = await _deliveryFetch(`/api/order/${encodeURIComponent(sessionId)}`);
    const data        = await deliveryRes.json();
    return res.status(deliveryRes.status).json(data);
  } catch (err) {
    console.error('[ghost/checkout] getOrder proxy error:', err);
    return res.status(502).json({ ok: false, error: 'Order lookup unavailable.' });
  }
}


/* ── Exports ─────────────────────────────────────────────────────────────── */
module.exports = { createSession, validateCoupon, getOrder, PLAN_CATALOGUE };
