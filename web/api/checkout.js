/**
 * api/checkout.js — Ghost Checkout API (Stripe Checkout Sessions)
 * ================================================================
 * All plan metadata lives here on the server.  The frontend sends
 * only a plan slug — prices, labels, and modes are resolved here
 * and never trusted from the client.
 *
 * Routes (registered in server.js):
 *   POST /api/checkout/create-session   -> create a Stripe Checkout Session
 *   POST /api/checkout/validate-coupon  -> validate a Stripe coupon/promo code
 *
 * Secrets required (via environment variables — never hardcoded):
 *   STRIPE_SECRET_KEY       sk_live_... or sk_test_...
 *   STRIPE_WEBHOOK_SECRET   whsec_...  (used only in stripe_webhook.js)
 *   BASE_URL                https://yourdomain.com
 *   UPSTASH_REDIS_REST_URL  — Upstash Redis REST endpoint
 *   UPSTASH_REDIS_REST_TOKEN— Upstash Redis REST bearer token
 * ================================================================
 */

'use strict';

const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

/* ── Duration → expiry-days mapping ─────────────────────────────────────── */
const DURATION_DAYS = {
  day:      1,
  '3days':  3,
  week:     7,
  month:    30,
  '3months': 90,
};

/* ── Plan normalisation (must match paypal.js / server.js) ──────────────── */
function _normalizePlan (plan) {
  if (!plan) return '';
  const aliases = {
    day: 'day', '1day': 'day', '1 day': 'day',
    '3days': '3days', '3 days': '3days',
    week: 'week', '7day': 'week', '7days': 'week', '7 days': 'week',
    month: 'month', '30day': 'month', '30days': 'month', '30 days': 'month',
    '3months': '3months', '90day': '3months', '90days': '3months', '90 days': '3months',
    pro: 'month', monthly: 'month', lifetime: '3months', trial: 'day',
  };
  return aliases[String(plan).trim().toLowerCase()] || String(plan).trim().toLowerCase();
}

/* ── Authoritative server-side plan catalogue ───────────────────────────────
   NEVER trust plan name, price, or mode sent by the frontend.
   All values here are the single source of truth.
─────────────────────────────────────────────────────────────────────────── */
const PLAN_CATALOGUE = {
  day: {
    id:         'day',
    label:      'Phantom 1 Day',
    priceUsd:   2.99,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 1,
  },
  '3days': {
    id:         '3days',
    label:      'Phantom 3 Days',
    priceUsd:   5.99,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 3,
  },
  week: {
    id:         'week',
    label:      'Phantom 1 Week',
    priceUsd:   9.99,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 7,
  },
  month: {
    id:         'month',
    label:      'Phantom 1 Month',
    priceUsd:   24.99,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 30,
  },
  '3months': {
    id:         '3months',
    label:      'Phantom 3 Months',
    priceUsd:   59.99,
    mode:       'payment',
    tier:       'PRO',
    expiryDays: 90,
  },
};


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
  const planId = _normalizePlan((planRaw || '').trim().toLowerCase());
  if (!PLAN_CATALOGUE[planId]) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: day, 3days, week, month, or 3months.' });
  }
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ ok: false, message: 'A valid email address is required.' });
  }
  if (!discord || discord.trim().length < 2) {
    return res.status(400).json({ ok: false, message: 'Discord username is required.' });
  }

  const plan = PLAN_CATALOGUE[planId];


  /* ── Resolve Stripe discount coupon ─────────────────────────────────── */
  let stripeCouponId;
  if (coupon) {
    try {
      const promoCodes = await stripe.promotionCodes.list({ code: coupon.trim().toUpperCase(), limit: 1, active: true });
      if (promoCodes.data.length > 0) {
        stripeCouponId = promoCodes.data[0].id;
      }
    } catch (err) {
      console.warn('[ghost/checkout] coupon lookup failed:', err.message);
    }
  }

  /* ── Build Stripe Checkout Session ─────────────────────────────────── */
  const baseUrl = (process.env.BASE_URL || '').replace(/\/$/, '');
  if (!baseUrl) {
    console.warn('[ghost/checkout] BASE_URL is not set — Stripe redirect URLs will be broken');
  }

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
    billing_address_collection: 'auto',
    allow_promotion_codes: !stripeCouponId,
  };

  if (stripeCouponId) {
    sessionParams.discounts = [{ promotion_code: stripeCouponId }];
  }

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
 */
async function validateCoupon (req, res) {
  const { code, plan: planRaw } = req.body || {};

  if (!code || typeof code !== 'string') {
    return res.status(400).json({ ok: false, message: 'Coupon code is required.' });
  }
  const planId = _normalizePlan((planRaw || '').trim().toLowerCase());
  if (!PLAN_CATALOGUE[planId]) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: day, 3days, week, month, or 3months.' });
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


/* ── Exports ─────────────────────────────────────────────────────────────── */
module.exports = { createSession, validateCoupon, PLAN_CATALOGUE };
