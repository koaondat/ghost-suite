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

  /* ── Free trial: bypass Stripe and issue key directly from Redis ─────── */
  if (plan.mode === 'free') {
    try {
      // Import fulfillOrder from paypal.js — it reads ghost:inventory directly
      const { fulfillOrder } = require('./paypal');
      const orderId = 'GHOST-TRIAL-' + Date.now().toString(36).toUpperCase();

      // Write a minimal order record so fulfillOrder can find it
      const _REDIS_URL   = (process.env.UPSTASH_REDIS_REST_URL   || '').replace(/\/$/, '');
      const _REDIS_TOKEN = (process.env.UPSTASH_REDIS_REST_TOKEN || '');

      if (_REDIS_URL && _REDIS_TOKEN) {
        const { default: fetch } = await import('node-fetch');
        const orderRecord = {
          order_id:        orderId,
          plan:            planId,
          plan_label:      plan.label,
          tier:            plan.tier,
          email:           email.trim(),
          discord:         discord.trim(),
          price_usd:       0,
          currency:        'USD',
          created_at:      new Date().toISOString(),
          payment_status:  'completed',
          payment_verified: true,
          delivery_status: 'pending',
          license_key:     null,
          license_status:  'pending',
        };
        const pipeline = [
          ['SET', `ghost:order:${orderId}`, JSON.stringify(orderRecord)],
          ['ZADD', 'ghost:orders:index', String(Math.floor(Date.now() / 1000)), orderId],
        ];
        await fetch(`${_REDIS_URL}/pipeline`, {
          method:  'POST',
          headers: { Authorization: `Bearer ${_REDIS_TOKEN}`, 'Content-Type': 'application/json' },
          body:    JSON.stringify(pipeline),
        }).catch(() => {});
      }

      const result = await fulfillOrder(orderId);
      if (!result.ok) {
        return res.status(503).json({ ok: false, message: 'No trial keys available at this time.' });
      }
      return res.json({ ok: true, free: true, orderId, key: result.licenseKey, tier: plan.tier });
    } catch (err) {
      console.error('[ghost/checkout] free-trial fulfillment error:', err);
      return res.status(500).json({ ok: false, message: 'Trial activation failed.' });
    }
  }

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


/* ── Exports ─────────────────────────────────────────────────────────────── */
module.exports = { createSession, validateCoupon, PLAN_CATALOGUE };
