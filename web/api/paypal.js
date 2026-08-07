/**
 * api/paypal.js — Ghost PayPal Checkout (Orders API v2)
 * ======================================================
 * All routes registered in server.js.
 *
 * Routes
 * ------
 *   POST /api/paypal/create-order   Create a PayPal order (server-controlled price)
 *   POST /api/paypal/capture-order  Capture an approved PayPal order + deliver license
 *   GET  /api/order/:orderId        Proxy to license_delivery backend (reused by server.js)
 *
 * Required env vars (never hardcode these):
 *   PAYPAL_CLIENT_ID      — from developer.paypal.com
 *   PAYPAL_CLIENT_SECRET  — NEVER expose in browser code
 *   PAYPAL_ENVIRONMENT    — 'sandbox' or 'live'  (default: sandbox)
 *   GHOST_DELIVERY_URL    — URL of the Python license_delivery server
 *   BASE_URL              — public URL of this web server
 */

'use strict';

const PAYPAL_CLIENT_ID     = process.env.PAYPAL_CLIENT_ID     || '';
const PAYPAL_CLIENT_SECRET = process.env.PAYPAL_CLIENT_SECRET || '';
const PAYPAL_ENV           = (process.env.PAYPAL_ENVIRONMENT  || 'sandbox').toLowerCase();

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
  console.error(
    '[ghost/paypal] FATAL: GHOST_DELIVERY_URL is not set. ' +
    'License delivery will fail until this is configured.',
  );
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
  const res = await fetch(`${PAYPAL_API_BASE}/v1/oauth2/token`, {
    method:  'POST',
    headers: {
      Authorization:  `Basic ${credentials}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: 'grant_type=client_credentials',
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`PayPal auth failed (${res.status}): ${text.slice(0, 200)}`);
  }

  const data = await res.json();
  _cachedToken    = data.access_token;
  _tokenExpiresAt = now + (data.expires_in || 32400) * 1000;
  return _cachedToken;
}


/* ── PayPal Orders API helpers ───────────────────────────────────────────── */
async function _paypalRequest (method, path, body) {
  const token  = await _getAccessToken();
  const { default: fetch } = await import('node-fetch');

  const init = {
    method,
    headers: {
      Authorization:  `Bearer ${token}`,
      'Content-Type': 'application/json',
      'PayPal-Request-Id': body?.reference_id || undefined,
    },
  };
  if (body) init.body = JSON.stringify(body);

  const res = await fetch(`${PAYPAL_API_BASE}${path}`, init);
  const data = await res.json().catch(() => ({}));

  if (!res.ok) {
    const msg = data.message || data.error_description || `HTTP ${res.status}`;
    throw new Error(`PayPal API error [${res.status}]: ${msg}`);
  }
  return data;
}

async function _deliveryFetch (path, init = {}) {
  if (!DELIVERY_BACKEND_URL) throw new Error('GHOST_DELIVERY_URL is not configured.');
  const { default: fetch } = await import('node-fetch');
  return fetch(`${DELIVERY_BACKEND_URL}${path}`, init);
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/create-order
   ─────────────────────────────────────────────────────────────────────────
   Body:   { plan: string, email: string, discord: string }
   OK:     { ok: true, orderID: string }
   Error:  { ok: false, message: string }
 ──────────────────────────────────────────────────────────────────────────*/
async function createOrder (req, res) {
  const { plan: planRaw, email, discord } = req.body || {};

  const planId = (planRaw || '').trim().toLowerCase();
  const plan   = PLAN_CATALOGUE[planId];

  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan. Choose: pro or lifetime.' });
  }
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ ok: false, message: 'A valid email address is required.' });
  }
  if (!discord || discord.trim().length < 2) {
    return res.status(400).json({ ok: false, message: 'Discord username is required.' });
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
      // PayPal sets the payer's email from their PayPal account;
      // we also record the supplied email for delivery.
      application_context: {
        brand_name:          'Ghost',
        user_action:         'PAY_NOW',
        shipping_preference: 'NO_SHIPPING',
      },
    });

    console.log('[ghost/paypal] order created id=%s plan=%s env=%s', order.id, planId, PAYPAL_ENV);
    return res.json({ ok: true, orderID: order.id });

  } catch (err) {
    console.error('[ghost/paypal] create-order error:', err.message);
    return res.status(502).json({ ok: false, message: 'Could not create PayPal order. Please try again.' });
  }
}


/* ─────────────────────────────────────────────────────────────────────────
   POST /api/paypal/capture-order
   ─────────────────────────────────────────────────────────────────────────
   Body:   { orderID: string, email: string, discord: string, plan: string }
   OK:     { ok: true, key, tier, orderId, plan, priceUsd, email, discord }
   Error:  { ok: false, message: string }

   Security: this endpoint calls PayPal's capture API and verifies the
   returned capture status, amount, and currency BEFORE calling the
   delivery backend.  The order ID from the browser is only used to
   address the PayPal API call — all billing truth comes from PayPal's
   response, never from the client payload.
 ──────────────────────────────────────────────────────────────────────────*/
async function captureOrder (req, res) {
  const { orderID, email, discord, plan: planRaw } = req.body || {};

  if (!orderID) {
    return res.status(400).json({ ok: false, message: 'orderID is required.' });
  }
  const planId = (planRaw || '').trim().toLowerCase();
  const plan   = PLAN_CATALOGUE[planId];
  if (!plan) {
    return res.status(400).json({ ok: false, message: 'Invalid plan.' });
  }

  // ── Step 1: Capture the order via PayPal Orders API ──────────────────────
  let capture;
  try {
    capture = await _paypalRequest('POST', `/v2/checkout/orders/${orderID}/capture`, {});
  } catch (err) {
    console.error('[ghost/paypal] capture error orderID=%s: %s', orderID, err.message);
    return res.status(502).json({ ok: false, message: 'Payment capture failed. No charge was made.' });
  }

  // ── Step 2: Verify the capture is COMPLETED ───────────────────────────────
  if (capture.status !== 'COMPLETED') {
    console.warn('[ghost/paypal] capture not COMPLETED orderID=%s status=%s', orderID, capture.status);
    return res.status(400).json({ ok: false, message: `Payment not completed (status: ${capture.status}). No charge was made.` });
  }

  // ── Step 3: Extract and verify the capture unit ───────────────────────────
  const pu = (capture.purchase_units || [])[0];
  if (!pu) {
    console.error('[ghost/paypal] no purchase_units in capture orderID=%s', orderID);
    return res.status(500).json({ ok: false, message: 'Payment capture data invalid. Contact support.' });
  }

  const captureUnit = (pu.payments?.captures || [])[0];
  if (!captureUnit || captureUnit.status !== 'COMPLETED') {
    console.error('[ghost/paypal] capture unit not COMPLETED orderID=%s', orderID);
    return res.status(400).json({ ok: false, message: 'Capture unit is not complete. Contact support.' });
  }

  const captureId = captureUnit.id;
  const capturedAmount   = captureUnit.amount?.value;
  const capturedCurrency = captureUnit.amount?.currency_code;

  // ── Step 4: Verify amount and currency ───────────────────────────────────
  if (capturedCurrency !== 'USD') {
    console.error('[ghost/paypal] wrong currency orderID=%s currency=%s', orderID, capturedCurrency);
    return res.status(400).json({ ok: false, message: 'Unexpected payment currency. Contact support.' });
  }
  if (parseFloat(capturedAmount) !== parseFloat(plan.priceUsd)) {
    console.error('[ghost/paypal] amount mismatch orderID=%s expected=%s got=%s',
      orderID, plan.priceUsd, capturedAmount);
    return res.status(400).json({ ok: false, message: 'Payment amount mismatch. Contact support.' });
  }

  // ── Step 5: Resolve payer email ───────────────────────────────────────────
  // Use the PayPal-verified payer email preferentially; fall back to the
  // email supplied in the form (which we already have from create-order).
  const payerEmail = capture.payer?.email_address || email || '';
  const resolvedEmail = payerEmail.trim() || (email || '').trim();

  const resolvedDiscord = (discord || '').trim();

  console.log('[ghost/paypal] capture verified orderID=%s captureId=%s plan=%s amount=%s',
    orderID, captureId, planId, capturedAmount);

  // ── Step 6: Call the delivery backend ────────────────────────────────────
  // Use captureId as the dedup key so replay of the same PayPal capture
  // never generates a second license key.
  try {
    const deliveryRes = await _deliveryFetch('/api/payment/confirm', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        order_id:      captureId,          // unique PayPal capture ID = dedup key
        payment_token: `paypal:${captureId}`,
        plan:          planId,
        email:         resolvedEmail,
        discord:       resolvedDiscord,
        price_usd:     parseFloat(capturedAmount),
        paypal_order_id: orderID,
        paypal_capture_id: captureId,
      }),
    });

    const data = await deliveryRes.json();

    if (!data.ok) {
      console.error('[ghost/paypal] delivery failed orderID=%s captureId=%s error=%s',
        orderID, captureId, data.error);
      return res.status(500).json({
        ok:      false,
        message: 'Your payment was received but license delivery failed. ' +
                 'Please contact support with your Order ID: ' + captureId,
      });
    }

    console.log('[ghost/paypal] license delivered orderID=%s captureId=%s plan=%s',
      orderID, captureId, planId);

    return res.json({
      ok:       true,
      key:      data.key,
      tier:     data.tier,
      orderId:  captureId,
      plan:     planId,
      priceUsd: parseFloat(capturedAmount),
      email:    resolvedEmail,
      discord:  resolvedDiscord,
    });

  } catch (err) {
    console.error('[ghost/paypal] delivery exception orderID=%s: %s', orderID, err.message);
    return res.status(502).json({
      ok:      false,
      message: 'License delivery service unavailable. Your payment was received — ' +
               'contact support with Order ID: ' + captureId,
    });
  }
}


module.exports = { createOrder, captureOrder, PLAN_CATALOGUE };
