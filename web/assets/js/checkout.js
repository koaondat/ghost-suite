/* ============================================================
   checkout.js — Ghost checkout page controller (PayPal)
   ============================================================
   Flow
   ----
   Step 1  — Customer fills email + Discord, agrees to terms.
             "Continue to Payment" validates the form.
             If valid, reveals the Step 2 payment section.

   Step 2  — GET /api/paypal/config returns { configured, clientId, environment }.
             If not configured, a clear error is shown and the payment section
             is disabled.
             The PayPal JS SDK is loaded with:
               components=buttons,card-fields
               currency=USD
               intent=capture
             The official PayPal Buttons are always rendered.
             cardFields.isEligible() is checked:
               • true  → CardFields fields (name, number, expiry, CVV) are
                         rendered and the card form is shown.
               • false → #co-card-ineligible notice is shown; PayPal button
                         remains the only payment path.

   Payment — createOrder calls POST /api/paypal/create-order (backend creates
             the PayPal order and returns { orderID }).
             onApprove / card form submit calls
             POST /api/paypal/capture-order (backend captures, verifies amount +
             currency + status, then calls license_delivery).
             Success is only shown when capture status is COMPLETED and the
             backend returns { ok: true, key }.

   Security
   --------
   • No payment card data is ever handled here.
   • No license keys are generated here.
   • Prices, plan verification, and capture all happen server-side.
   • PAYPAL_CLIENT_SECRET never reaches the browser.
   • PAYPAL_CLIENT_ID comes from /api/paypal/config, not from static HTML.
   ============================================================ */

(function () {
  'use strict';

  /* ── Plan catalogue ─────────────────────────────────────────
     Used ONLY for display (labels, features, price strings).
     The backend is the authoritative source for all billing.
  ─────────────────────────────────────────────────────────── */
  const PLANS = {
    pro: {
      id:        'pro',
      name:      'Pro',
      tagline:   'Monthly subscription',
      price:     7,
      currency:  'USD',
      symbol:    '$',
      formatted: '$7 / month',
      duration:  'Active while subscription is live',
      color:     'accent',
      features: [
        'Everything in Trial',
        'MAC address spoofing',
        'Discord bot integration',
        'Key generation & revocation',
        'Audit log access',
        'Priority Discord support',
        'All future updates',
      ],
    },
    lifetime: {
      id:        'lifetime',
      name:      'Lifetime',
      tagline:   'One-time payment — never pay again',
      price:     79,
      currency:  'USD',
      symbol:    '$',
      formatted: '$79 one-time',
      duration:  'Permanent — no expiry',
      color:     'cyan',
      features: [
        'Everything in Pro',
        'Permanent license key',
        'All future updates included',
        'No subscription, zero recurring cost',
        'Priority Discord support',
        'Early access to new features',
      ],
    },
  };

  const params      = new URLSearchParams(window.location.search);
  const planKey     = (params.get('plan') || 'pro').toLowerCase();
  const ACTIVE_PLAN = PLANS[planKey] || PLANS.pro;

  /* ── State machine ──────────────────────────────────────────
     All state panel IDs.  Only one is visible at a time.
  ──────────────────────────────────────────────────────────── */
  const STATES = ['idle', 'loading', 'success', 'cancelled', 'failed'];

  function showState (name) {
    STATES.forEach(s => {
      const el = document.getElementById('state-' + s);
      if (el) el.hidden = (s !== name);
    });
  }


  /* ── Order summary renderer ─────────────────────────────────── */

  function renderSummary (plan) {
    const iconEl    = document.getElementById('co-summary-icon');
    const nameEl    = document.getElementById('co-summary-plan-name');
    const taglineEl = document.getElementById('co-summary-plan-tagline');

    if (nameEl)    nameEl.textContent    = plan.name;
    if (taglineEl) taglineEl.textContent = plan.tagline;

    if (iconEl) {
      const isCyan = plan.color === 'cyan';
      iconEl.innerHTML = `
        <div class="co-plan-icon ${isCyan ? 'co-plan-icon--cyan' : ''}">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
        </div>`;
    }

    const featList = document.getElementById('co-summary-features');
    if (featList) {
      const isCyan = plan.color === 'cyan';
      featList.innerHTML = plan.features.map(f => `
        <li class="co-summary-feat">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="${isCyan ? 'feat-cyan' : 'feat-yes'}" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
          ${_escHtml(f)}
        </li>`).join('');
    }

    const subtotalEl  = document.getElementById('co-price-subtotal');
    const totalEl     = document.getElementById('co-price-total');
    const discountRow = document.getElementById('co-discount-row');
    const durationEl  = document.getElementById('co-license-duration-text');
    const cardPrice   = document.getElementById('co-card-price');

    if (subtotalEl) subtotalEl.textContent = plan.symbol + plan.price.toFixed(2);
    if (totalEl)    totalEl.textContent    = plan.symbol + plan.price.toFixed(2);
    if (discountRow) discountRow.hidden    = true;
    if (durationEl)  durationEl.textContent = 'License duration: ' + plan.duration;
    if (cardPrice)   cardPrice.textContent  = plan.price.toFixed(2);
  }


  /* ── Field helpers ──────────────────────────────────────────── */

  function fieldState (groupId, errorId, message) {
    const group = document.getElementById(groupId);
    const errEl = document.getElementById(errorId);
    if (!group || !errEl) return;
    if (message) {
      errEl.textContent = message;
      group.classList.add('is-invalid');
      group.classList.remove('is-valid');
    } else {
      errEl.textContent = '';
      group.classList.remove('is-invalid');
      if (group.querySelector('.form-input')?.value) group.classList.add('is-valid');
    }
  }

  function showAlert (type, message) {
    const el = document.getElementById('co-alert');
    if (!el) return;
    const icons = {
      error:   '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
      success: '<polyline points="20 6 9 17 4 12"/>',
      info:    '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    };
    el.querySelector('.alert-icon').innerHTML = icons[type] || icons.info;
    el.querySelector('.alert-msg').textContent = message;
    el.className = 'auth-alert auth-alert--' + type;
    el.hidden = false;
  }

  function showCardAlert (type, message) {
    const el = document.getElementById('co-card-alert');
    if (!el) return;
    const icons = {
      error:   '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
      success: '<polyline points="20 6 9 17 4 12"/>',
      info:    '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    };
    el.querySelector('.alert-icon').innerHTML = icons[type] || icons.info;
    el.querySelector('.alert-msg').textContent = message;
    el.className = 'auth-alert auth-alert--' + type;
    el.hidden = false;
  }

  function hideCardAlert () {
    const el = document.getElementById('co-card-alert');
    if (el) el.hidden = true;
  }

  function hideAlert () {
    const el = document.getElementById('co-alert');
    if (el) el.hidden = true;
  }


  /* ── Form validation ────────────────────────────────────────── */

  function validateForm (email, discord, terms) {
    const errors = {};
    if (!email.trim()) {
      errors.email = 'Email address is required.';
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      errors.email = 'Please enter a valid email address.';
    }
    if (!discord.trim()) {
      errors.discord = 'Discord username is required.';
    } else if (discord.trim().length < 2) {
      errors.discord = 'Please enter your full Discord username.';
    }
    if (!terms) {
      errors.terms = 'You must agree to the Terms of Service to continue.';
    }
    return errors;
  }


  /* ── Read current form values ───────────────────────────────── */

  function _formValues () {
    const form = document.getElementById('checkout-form');
    if (!form) return {};
    return {
      email:   (form.querySelector('#co-email')?.value   || '').trim(),
      discord: (form.querySelector('#co-discord')?.value || '').trim(),
      terms:   form.querySelector('#co-terms')?.checked  || false,
    };
  }


  /* ── License key display ────────────────────────────────────── */

  function _showDeliveredKey (key, tier, orderId, email, discord, priceUsd) {
    _show('co-key-delivery');
    _setText('success-license-key', key);

    const inlineCopyBtn = document.getElementById('success-copy-btn');
    if (inlineCopyBtn) inlineCopyBtn.onclick = () => _copyKey(key, [inlineCopyBtn]);

    const copyBtn = document.getElementById('success-copy-key-btn');
    const dashBtn = document.getElementById('success-dashboard-btn');
    const dlBtn   = document.getElementById('success-download-btn');

    if (copyBtn) { copyBtn.hidden = false; copyBtn.onclick = () => _copyKey(key, [copyBtn]); }
    if (dashBtn) dashBtn.hidden = false;
    if (dlBtn)   dlBtn.hidden   = false;
  }

  function _copyKey (text, btns) {
    navigator.clipboard.writeText(text).then(() => {
      btns.forEach(btn => {
        if (!btn) return;
        const ic = btn.querySelector('.icon-copy');
        const ik = btn.querySelector('.icon-check');
        if (ic) ic.style.display = 'none';
        if (ik) ik.style.display = '';
        btn.classList.add('copied');
        setTimeout(() => {
          if (ic) ic.style.display = '';
          if (ik) ik.style.display = 'none';
          btn.classList.remove('copied');
        }, 2000);
      });
    }).catch(() => {
      alert('Could not copy automatically — please select and copy the key manually.');
    });
  }


  /* ── Utility ────────────────────────────────────────────────── */

  function _show (id) {
    const el = document.getElementById(id);
    if (el) el.hidden = false;
  }

  function _hide (id) {
    const el = document.getElementById(id);
    if (el) el.hidden = true;
  }

  function _setText (id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function _escHtml (str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _setSubmitBusy (id, busy) {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = busy;
    if (busy) btn.classList.add('is-loading');
    else      btn.classList.remove('is-loading');
  }

  function applyPlanTheme () {
    const card = document.getElementById('co-summary-card');
    if (card && ACTIVE_PLAN.color === 'cyan') card.classList.add('co-summary-card--cyan');
  }

  function wireRetryButtons () {
    ['failed-retry-btn', 'cancelled-retry-btn'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', () => {
        showState('idle');
        _hide('co-payment-section');
        _paypalRendered = false;
        _paypalSdkPromise = null;
        hideAlert();
        _show('co-submit-btn-wrap');
        const form = document.getElementById('checkout-form');
        if (form) form.reset();
      });
    });
  }


  /* ═══════════════════════════════════════════════════════════════
     STEP 2 — PayPal integration
     All PayPal logic lives in the functions below.  It is only
     executed after Step 1 validation passes.
  ═══════════════════════════════════════════════════════════════ */

  let _paypalRendered   = false;
  let _paypalSdkPromise = null;   // cache to avoid double-loading
  let _capturedVals     = {};     // email/discord snapped at validation time

  /* ── Fetch PayPal configuration from the backend ────────────────
     Uses GET /api/paypal/config which returns:
       { configured: bool, clientId: string|null, environment: string }
     The client secret is NEVER returned by this endpoint.
  ─────────────────────────────────────────────────────────────── */
  async function _fetchPayPalConfig () {
    const res  = await fetch('/api/paypal/config');
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.configured) {
      console.error('[ghost/checkout] /api/paypal/config returned not-configured:', data.error || res.status);
      return null;
    }
    console.log('[ghost/checkout] PayPal config loaded env=%s', data.environment);
    return data;
  }

  /* ── Load the PayPal JS SDK ─────────────────────────────────────
     Uses components=buttons,card-fields so both payment paths are
     available in a single SDK request.
  ─────────────────────────────────────────────────────────────── */
  function _loadPayPalSDK (clientId) {
    if (_paypalSdkPromise) return _paypalSdkPromise;

    _paypalSdkPromise = new Promise((resolve, reject) => {
      if (window.paypal) {
        console.log('[ghost/checkout] PayPal SDK already present');
        resolve(window.paypal);
        return;
      }

      const s   = document.createElement('script');
      const url = new URL('https://www.paypal.com/sdk/js');
      url.searchParams.set('client-id',  clientId);
      url.searchParams.set('currency',   'USD');
      url.searchParams.set('intent',     'capture');
      url.searchParams.set('components', 'buttons,card-fields');

      s.src = url.toString();
      s.onload  = () => {
        console.log('[ghost/checkout] PayPal SDK loaded successfully');
        resolve(window.paypal);
      };
      s.onerror = () => {
        console.error('[ghost/checkout] PayPal SDK failed to load — check network or CSP');
        _paypalSdkPromise = null;   // allow retry
        reject(new Error('PayPal SDK failed to load.'));
      };
      document.head.appendChild(s);
    });

    return _paypalSdkPromise;
  }

  /* ── createOrder — shared by both Buttons and CardFields ────────
     Calls our backend so prices and plan verification happen
     entirely server-side.
  ─────────────────────────────────────────────────────────────── */
  async function _createPayPalOrder () {
    const vals = _capturedVals;
    console.log('[ghost/checkout] creating PayPal order plan=%s', ACTIVE_PLAN.id);

    const res  = await fetch('/api/paypal/create-order', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ plan: ACTIVE_PLAN.id, email: vals.email, discord: vals.discord }),
    });
    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data.ok) {
      const msg = data.message || 'Could not create PayPal order. Please try again.';
      console.error('[ghost/checkout] create-order failed:', msg);
      throw new Error(msg);
    }

    console.log('[ghost/checkout] order created orderID=%s', data.orderID);
    return data.orderID;
  }

  /* ── capturePayPalOrder — shared by both Buttons and CardFields ──
     Calls our backend which:
       1. Calls PayPal capture API
       2. Verifies status === COMPLETED
       3. Verifies amount + currency
       4. Calls license_delivery
     No success state is shown until the backend confirms COMPLETED.
  ─────────────────────────────────────────────────────────────── */
  async function _capturePayPalOrder (orderID) {
    const vals = _capturedVals;
    console.log('[ghost/checkout] capturing order orderID=%s', orderID);
    showState('loading');

    const res = await fetch('/api/paypal/capture-order', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        orderID,
        plan:    ACTIVE_PLAN.id,
        email:   vals.email,
        discord: vals.discord,
      }),
    });
    const result = await res.json().catch(() => ({}));

    if (!res.ok || !result.ok) {
      const msg = result.message || 'Payment capture failed. Please contact support.';
      console.error('[ghost/checkout] capture-order failed orderID=%s: %s', orderID, msg);
      showState('failed');
      _setText('failed-reason', msg);
      return;
    }

    console.log('[ghost/checkout] capture confirmed orderID=%s plan=%s amount=%s',
      orderID, result.plan, result.priceUsd);

    // ── Populate success state ────────────────────────────────────────
    const plan = PLANS[(result.plan || '').toLowerCase()] || ACTIVE_PLAN;

    _setText('success-email',    result.email   || vals.email || '—');
    _setText('success-plan',     plan.name      || result.plan || '—');
    _setText('success-order-id', result.orderId || orderID || '—');
    _setText('success-duration', plan.duration  || '—');
    _setText('success-amount',   result.priceUsd != null
      ? '$' + Number(result.priceUsd).toFixed(2) : '—');

    showState('success');
    _hide('co-key-pending');

    if (result.key) {
      console.log('[ghost/checkout] license key delivered orderID=%s', orderID);
      _showDeliveredKey(result.key, result.tier, result.orderId, result.email, result.discord, result.priceUsd);
    } else {
      console.error('[ghost/checkout] capture succeeded but no key returned orderID=%s', orderID);
      _setText('co-key-error-msg',
        'Your payment was received but license delivery failed. ' +
        'Contact support with Order ID: ' + (result.orderId || orderID));
      _show('co-key-error');
    }
  }

  /* ── Render the PayPal Buttons ─────────────────────────────────── */
  function _renderPayPalButtons (paypalSdk) {
    paypalSdk.Buttons({
      style: {
        layout: 'vertical',
        color:  'gold',
        shape:  'rect',
        label:  'pay',
        height: 48,
      },

      /* Called by PayPal SDK when the popup opens — we create the order here */
      createOrder: async () => {
        try {
          return await _createPayPalOrder();
        } catch (err) {
          showAlert('error', err.message);
          throw err;   // propagates to PayPal SDK to abort the flow
        }
      },

      /* Called by PayPal SDK after the customer approves */
      onApprove: async (data) => {
        try {
          await _capturePayPalOrder(data.orderID);
        } catch (err) {
          console.error('[ghost/checkout] onApprove capture exception:', err.message);
          showState('failed');
          _setText('failed-reason',
            'A network error occurred after payment. Your payment may have been processed — ' +
            'please contact support with PayPal Order ID: ' + data.orderID);
        }
      },

      /* Customer closed the PayPal popup without paying */
      onCancel: () => {
        console.log('[ghost/checkout] PayPal flow cancelled by user');
        showState('cancelled');
      },

      /* SDK-level error (not a payment failure) */
      onError: (err) => {
        console.error('[ghost/checkout] PayPal SDK error:', err);
        showState('failed');
        _setText('failed-reason',
          'A PayPal error occurred. No charge was made. Please try again or contact support.');
      },

    }).render('#paypal-button-container')
      .then(() => console.log('[ghost/checkout] PayPal Buttons rendered'))
      .catch(err => console.error('[ghost/checkout] Buttons.render failed:', err));
  }

  /* ── Render PayPal CardFields ──────────────────────────────────── */
  function _renderCardFields (paypalSdk) {
    const cf = paypalSdk.CardFields({
      /* createOrder is shared — same backend endpoint */
      createOrder: async () => {
        try {
          return await _createPayPalOrder();
        } catch (err) {
          showCardAlert('error', err.message);
          throw err;
        }
      },

      onApprove: async (data) => {
        try {
          await _capturePayPalOrder(data.orderID);
        } catch (err) {
          console.error('[ghost/checkout] CardFields onApprove exception:', err.message);
          showState('failed');
          _setText('failed-reason',
            'A network error occurred after card payment. Your payment may have been processed — ' +
            'please contact support with Order ID: ' + (data.orderID || '—'));
        }
      },

      onError: (err) => {
        console.error('[ghost/checkout] CardFields SDK error:', err);
        showCardAlert('error', 'A payment error occurred. Please try again or use the PayPal button above.');
        _setSubmitBusy('co-card-submit-btn', false);
      },
    });

    console.log('[ghost/checkout] CardFields isEligible=%s', cf.isEligible());

    if (!cf.isEligible()) {
      // CardFields not supported in this context — show info notice
      console.log('[ghost/checkout] CardFields not eligible — showing fallback notice');
      _show('co-card-ineligible');
      return;
    }

    // Render each field into its container div
    cf.NameField().render('#card-name-field')
      .catch(err => console.error('[ghost/checkout] CardFields NameField render error:', err));
    cf.NumberField().render('#card-number-field')
      .catch(err => console.error('[ghost/checkout] CardFields NumberField render error:', err));
    cf.ExpiryField().render('#card-expiry-field')
      .catch(err => console.error('[ghost/checkout] CardFields ExpiryField render error:', err));
    cf.CVVField().render('#card-cvv-field')
      .catch(err => console.error('[ghost/checkout] CardFields CVVField render error:', err));

    // Show the card form and divider
    _show('co-card-divider');
    _show('co-card-form');

    // Wire the card submit button
    const cardForm = document.getElementById('co-card-form');
    if (cardForm) {
      cardForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        hideCardAlert();
        _setSubmitBusy('co-card-submit-btn', true);

        try {
          await cf.submit();
          // onApprove above handles the rest
        } catch (err) {
          console.error('[ghost/checkout] CardFields submit error:', err);
          const msg = (err && err.message) ? err.message : 'Card payment failed. Please check your details and try again.';
          showCardAlert('error', msg);
          _setSubmitBusy('co-card-submit-btn', false);
        }
      });
    }
  }

  /* ── Reveal Step 2 and initialise PayPal ─────────────────────────
     Called once, after Step 1 validation passes.
  ─────────────────────────────────────────────────────────────── */
  async function _revealPaymentSection (email, discord) {
    if (_paypalRendered) return;

    // Snap the validated values so createOrder/capture always uses them
    _capturedVals = { email, discord };

    // Reveal the payment section immediately so the user sees feedback
    _hide('co-submit-btn-wrap');
    _show('co-payment-section');
    _show('co-sdk-loading');

    // ── Fetch PayPal configuration from the backend ───────────────────
    let config;
    try {
      config = await _fetchPayPalConfig();
    } catch (err) {
      console.error('[ghost/checkout] Failed to fetch /api/paypal/config:', err.message);
      config = null;
    }

    _hide('co-sdk-loading');

    if (!config) {
      // Show the pre-built unconfigured banner and disable the payment section
      _show('co-payment-unconfigured');
      _hide('co-payment-section');
      _show('co-submit-btn-wrap');
      return;
    }

    // ── Load the SDK ──────────────────────────────────────────────────
    let paypalSdk;
    try {
      paypalSdk = await _loadPayPalSDK(config.clientId);
    } catch (err) {
      console.error('[ghost/checkout] SDK load failed:', err.message);
      showAlert('error', 'Could not load the PayPal payment interface. Please refresh and try again.');
      _hide('co-payment-section');
      _show('co-submit-btn-wrap');
      return;
    }

    _paypalRendered = true;

    // ── Render official PayPal Buttons ────────────────────────────────
    _renderPayPalButtons(paypalSdk);

    // ── Render CardFields (if eligible) ──────────────────────────────
    _renderCardFields(paypalSdk);
  }


  /* ── "Continue to Payment" button — Step 1 form ─────────────────
     Submitting the form validates Step 1.  If valid, Step 2 is
     revealed and PayPal is initialised.
  ─────────────────────────────────────────────────────────────── */

  function wireForm () {
    const form = document.getElementById('checkout-form');
    if (!form) return;

    form.querySelector('#co-email')?.addEventListener('blur', function () {
      const msg = !this.value.trim()
        ? 'Email address is required.'
        : !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(this.value)
          ? 'Please enter a valid email address.'
          : '';
      fieldState('fg-co-email', 'co-email-err', msg);
    });

    form.querySelector('#co-discord')?.addEventListener('blur', function () {
      const msg = !this.value.trim() ? 'Discord username is required.' : '';
      fieldState('fg-co-discord', 'co-discord-err', msg);
    });

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      hideAlert();

      const vals   = _formValues();
      const errors = validateForm(vals.email, vals.discord, vals.terms);
      fieldState('fg-co-email',   'co-email-err',   errors.email   || '');
      fieldState('fg-co-discord', 'co-discord-err', errors.discord || '');
      fieldState('fg-co-terms',   'co-terms-err',   errors.terms   || '');

      if (Object.keys(errors).length) return;

      // Validation passed — reveal Step 2 and initialise PayPal
      await _revealPaymentSection(vals.email, vals.discord);
    });
  }


  /* ── Init ─────────────────────────────────────────────────────── */

  (function init () {
    renderSummary(ACTIVE_PLAN);
    applyPlanTheme();
    wireForm();
    wireRetryButtons();

    const btnText = document.getElementById('co-btn-text');
    if (btnText) {
      btnText.textContent = 'Continue to Payment — ' + ACTIVE_PLAN.formatted;
    }
  })();

})();
