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
 *   RESEND_API_KEY          — Resend API key for receipt emails
 *   RECEIPT_FROM_EMAIL      — From address e.g. Ghost <receipts@yourdomain.com>
 *   SUPPORT_EMAIL           — shown in receipt and error messages
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
const RESEND_API_KEY       = process.env.RESEND_API_KEY       || '';
const RECEIPT_FROM_EMAIL   = process.env.RECEIPT_FROM_EMAIL   || 'Ghost <noreply@example.com>';
const SUPPORT_EMAIL        = process.env.SUPPORT_EMAIL        || '';

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
if (!RESEND_API_KEY) {
  console.warn('[ghost/paypal] WARNING: RESEND_API_KEY is not set — receipt emails will be skipped.');
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


/* ── Resend receipt email ────────────────────────────────────────────────── */
async function _sendReceiptEmail ({
  toEmail, plan, planLabel, amount, currency,
  purchaseDate, paypalOrderId, captureId, licenseKey,
}) {
  if (!RESEND_API_KEY) {
    console.warn('[ghost/paypal] Receipt email skipped — RESEND_API_KEY not configured');
    return { ok: false, reason: 'no_api_key' };
  }

  const { default: fetch } = await import('node-fetch');

  const formattedDate = new Date(purchaseDate).toLocaleString('en-US', {
    year: 'numeric', month: 'long', day: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  });

  const supportLine = SUPPORT_EMAIL
    ? `<p style="margin:0 0 8px">Support email: <a href="mailto:${SUPPORT_EMAIL}" style="color:#a855f7">${SUPPORT_EMAIL}</a></p>`
    : '<p style="margin:0 0 8px">Get help on our Discord server.</p>';

  const html = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ghost Purchase Receipt</title></head>
<body style="background:#0f1117;margin:0;padding:0;font-family:-apple-system,'Segoe UI',system-ui,sans-serif;">
<div style="max-width:560px;margin:40px auto;background:#1a1d2e;border-radius:12px;overflow:hidden;border:1px solid #2d2f45;">
  <div style="background:linear-gradient(135deg,#a855f7 0%,#7c3aed 100%);padding:32px 32px 24px;text-align:center;">
    <div style="display:inline-block;background:rgba(255,255,255,0.15);border-radius:10px;padding:8px 16px;margin-bottom:12px;">
      <span style="color:#fff;font-size:20px;font-weight:700;letter-spacing:.5px;">👻 Ghost</span>
    </div>
    <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700;">Payment Completed</h1>
    <p style="color:rgba(255,255,255,0.8);margin:8px 0 0;font-size:14px;">Thank you for your purchase.</p>
  </div>
  <div style="padding:32px;">
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <tr style="border-bottom:1px solid #2d2f45;">
        <td style="padding:10px 0;color:#888;font-size:13px;">Plan</td>
        <td style="padding:10px 0;color:#e0e0e0;font-size:13px;text-align:right;font-weight:600;">${planLabel}</td>
      </tr>
      <tr style="border-bottom:1px solid #2d2f45;">
        <td style="padding:10px 0;color:#888;font-size:13px;">Amount</td>
        <td style="padding:10px 0;color:#e0e0e0;font-size:13px;text-align:right;font-weight:600;">${currency} ${amount}</td>
      </tr>
      <tr style="border-bottom:1px solid #2d2f45;">
        <td style="padding:10px 0;color:#888;font-size:13px;">Date</td>
        <td style="padding:10px 0;color:#e0e0e0;font-size:13px;text-align:right;">${formattedDate}</td>
      </tr>
      <tr style="border-bottom:1px solid #2d2f45;">
        <td style="padding:10px 0;color:#888;font-size:13px;">PayPal Order ID</td>
        <td style="padding:10px 0;color:#e0e0e0;font-size:13px;text-align:right;font-family:monospace;font-size:12px;">${paypalOrderId}</td>
      </tr>
      <tr>
        <td style="padding:10px 0;color:#888;font-size:13px;">Capture ID</td>
        <td style="padding:10px 0;color:#e0e0e0;font-size:13px;text-align:right;font-family:monospace;font-size:12px;">${captureId}</td>
      </tr>
    </table>

    <div style="background:#0f1117;border:1px solid #a855f7;border-radius:8px;padding:20px;margin-bottom:24px;text-align:center;">
      <p style="color:#888;font-size:12px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.5px;">Your License Key</p>
      <code style="color:#a855f7;font-size:16px;font-family:monospace;font-weight:600;letter-spacing:1px;word-break:break-all;">${licenseKey}</code>
    </div>

    <div style="background:#1e2035;border-radius:8px;padding:20px;margin-bottom:24px;">
      <h3 style="color:#e0e0e0;margin:0 0 12px;font-size:14px;font-weight:600;">Setup Instructions</h3>
      <ol style="color:#aaa;font-size:13px;margin:0;padding-left:20px;line-height:1.8;">
        <li>Download the application using the button below.</li>
        <li>Extract the ZIP archive if required.</li>
        <li>Launch the installer or application.</li>
        <li>Enter the license key above when prompted.</li>
        <li>Contact support if activation fails.</li>
      </ol>
    </div>

    <div style="background:#2d1a1a;border:1px solid #7f1d1d;border-radius:8px;padding:14px 16px;margin-bottom:24px;">
      <p style="color:#fca5a5;font-size:13px;margin:0;font-weight:600;">⚠️ Do not share your license key. It is tied to your account and sharing it may result in deactivation.</p>
    </div>

    <div style="text-align:center;margin-bottom:24px;">
      <p style="color:#888;font-size:13px;margin-bottom:12px;">Need help?</p>
      ${supportLine}
    </div>
  </div>
  <div style="background:#0f1117;padding:16px 32px;text-align:center;border-top:1px solid #2d2f45;">
    <p style="color:#555;font-size:12px;margin:0;">© ${new Date().getFullYear()} Ghost. All rights reserved.</p>
  </div>
</div>
</body></html>`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10_000);

  try {
    const emailRes = await fetch('https://api.resend.com/emails', {
      method:  'POST',
      headers: {
        Authorization:  `Bearer ${RESEND_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from:    RECEIPT_FROM_EMAIL,
        to:      [toEmail],
        subject: `Your Ghost ${planLabel} receipt — ${captureId}`,
        html,
      }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    const emailData = await emailRes.json().catch(() => ({}));

    if (!emailRes.ok) {
      console.error('[ghost/paypal] Resend email failed status=%d', emailRes.status);
      return { ok: false, reason: 'send_failed', status: emailRes.status };
    }

    console.log('[ghost/paypal] Receipt email sent id=%s to=[redacted] captureId=%s',
      emailData.id, captureId);
    return { ok: true, messageId: emailData.id };

  } catch (err) {
    clearTimeout(timer);
    const reason = err.name === 'AbortError' ? 'timeout' : 'network_error';
    console.error('[ghost/paypal] Receipt email exception reason=%s', reason);
    return { ok: false, reason };
  }
}


/* ── Mark receipt status on the delivery backend ────────────────────────── */
async function _markReceiptStatus (captureId, receiptResult) {
  try {
    await _deliveryFetch(`/api/order/${encodeURIComponent(captureId)}/receipt`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        receipt_sent:       receiptResult.ok === true,
        receipt_message_id: receiptResult.messageId || null,
        receipt_status:     receiptResult.ok ? 'sent' : 'receipt_pending',
      }),
    }, 8_000);
  } catch (_) {
    // Non-fatal — order record may not have this endpoint yet; just log
    console.warn('[ghost/paypal] Could not update receipt status for captureId=%s', captureId);
  }
}


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

  // ── Step 6: Call the delivery backend ────────────────────────────────────
  let data;
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
    return {
      ok:      false,
      message: 'License delivery service unavailable. Your payment was received — ' +
               'contact support with Capture ID: ' + captureId,
      stage:   'delivery',
      orderId: orderID,
      captureId,
      _status: 502,
    };
  }

  if (!data.ok) {
    console.error('[ghost/paypal] delivery failed orderID=%s captureId=%s error=%s',
      orderID, captureId, data.error);
    return {
      ok:      false,
      message: 'Your payment was received but license delivery failed. ' +
               'Please contact support with Capture ID: ' + captureId,
      stage:   'delivery',
      orderId: orderID,
      captureId,
      _status: 500,
    };
  }

  console.log('[ghost/paypal] license delivered orderID=%s captureId=%s plan=%s',
    orderID, captureId, planId);

  const purchaseDate = data.created_at || new Date().toISOString();

  // ── Step 7: Send receipt email (non-blocking — never blocks fulfillment) ──
  if (RESEND_API_KEY && resolvedEmail) {
    _sendReceiptEmail({
      toEmail:      resolvedEmail,
      plan:         planId,
      planLabel:    plan.label,
      amount:       capturedAmount,
      currency:     capturedCurrency,
      purchaseDate,
      paypalOrderId: orderID,
      captureId,
      licenseKey:   data.key,
    }).then(receiptResult => {
      if (!receiptResult.ok) {
        console.warn('[ghost/paypal] Receipt email not sent captureId=%s reason=%s',
          captureId, receiptResult.reason);
      }
      // Update receipt status on delivery backend asynchronously
      _markReceiptStatus(captureId, receiptResult).catch(() => {});
    }).catch(err => {
      console.error('[ghost/paypal] Receipt email unhandled error captureId=%s', captureId);
    });
  }

  return {
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
    licenseKey:     data.key,
    licenseStatus:  data.key ? 'active' : 'pending',
    purchaseDate,
    downloadUrl:    `/api/order/${encodeURIComponent(captureId)}/download`,
    tier:           data.tier,
    instructions: [
      'Download the application using the Download App button below.',
      'Extract the ZIP archive if required.',
      'Launch the installer or application.',
      'Sign in or enter the provided license key when prompted.',
      'Contact Discord support if activation fails.',
    ],
  };
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
      // Send receipt if not already sent (delivery returns existing key on duplicate)
      if (RESEND_API_KEY && email && data.key) {
        _sendReceiptEmail({
          toEmail:      email,
          plan,
          planLabel:    planMeta.label,
          amount,
          currency,
          purchaseDate: data.created_at || new Date().toISOString(),
          paypalOrderId: orderId,
          captureId,
          licenseKey:   data.key,
        }).then(r => {
          _markReceiptStatus(captureId, r).catch(() => {});
        }).catch(() => {});
      }
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


module.exports = { createOrder, captureOrder, handleWebhook, PLAN_CATALOGUE };
