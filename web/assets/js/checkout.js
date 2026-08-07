/* ============================================================
   checkout.js — Ghost checkout page controller (PayPal)
   ============================================================
   Architecture
   ------------
   PayPal orders are created by the Node.js backend
   (api/paypal.js → POST /api/paypal/create-order).  The
   frontend renders the official PayPal JS SDK button.

   Flow
   ----
   1. User fills in email + discord, agrees to terms.
   2. On "Purchase" click, client-validates the form.
   3. PayPal SDK calls our createOrder callback →
      POST /api/paypal/create-order (plan + email + discord).
      Backend returns { orderID }.
   4. Customer logs into PayPal and approves.
   5. PayPal SDK calls our onApprove callback →
      POST /api/paypal/capture-order (orderID + plan + email + discord).
      Backend captures, verifies amount/currency/status, then calls
      license_delivery to generate and return the key.
   6. Success state shown with the delivered key.

   If PayPal is cancelled:  onCancel → cancelled state.
   If PayPal errors:        onError  → failed state.

   Security notes
   --------------
   • No payment card data is ever handled here.
   • No license keys are generated here.
   • Prices, plan amounts, and capture verification all happen server-side.
   • PAYPAL_CLIENT_ID is the only PayPal value that appears in frontend
     code; PAYPAL_CLIENT_SECRET never leaves the server.
   ============================================================ */

(function () {
  'use strict';

  /* ── Plan catalogue ─────────────────────────────────────────
     Used only for display (labels, features, price strings).
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

  /* ── PayPal Client ID (injected by server or set at build time) ─────────
     The meta tag approach lets the Node server inject the sandbox/live
     client ID without baking it into the static JS file.
     The <meta name="paypal-client-id"> tag is added in checkout.html.
  ──────────────────────────────────────────────────────────────────────── */
  function _getPayPalClientId () {
    const meta = document.querySelector('meta[name="paypal-client-id"]');
    return meta ? meta.content : '';
  }

  /* ── State machine ──────────────────────────────────────────────────────
     All state panel IDs.  Only one is visible at a time.
  ──────────────────────────────────────────────────────────────────────── */
  const STATES = ['idle', 'loading', 'success', 'cancelled', 'failed'];

  function showState (name) {
    STATES.forEach(s => {
      const el = document.getElementById('state-' + s);
      if (el) el.hidden = (s !== name);
    });
  }


  /* ── Order summary renderer ─────────────────────────────────────────── */

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

    if (subtotalEl) subtotalEl.textContent = plan.symbol + plan.price.toFixed(2);
    if (totalEl)    totalEl.textContent    = plan.symbol + plan.price.toFixed(2);
    if (discountRow) discountRow.hidden    = true;
    if (durationEl)  durationEl.textContent = 'License duration: ' + plan.duration;
  }


  /* ── Field helpers ──────────────────────────────────────────────────── */

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

  function hideAlert () {
    const el = document.getElementById('co-alert');
    if (el) el.hidden = true;
  }


  /* ── Form validation ────────────────────────────────────────────────── */

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


  /* ── Read current form values ───────────────────────────────────────── */

  function _formValues () {
    const form = document.getElementById('checkout-form');
    if (!form) return {};
    return {
      email:   (form.querySelector('#co-email')?.value   || '').trim(),
      discord: (form.querySelector('#co-discord')?.value || '').trim(),
      terms:   form.querySelector('#co-terms')?.checked  || false,
    };
  }


  /* ── License key display ────────────────────────────────────────────── */

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


  /* ── Utility ────────────────────────────────────────────────────────── */

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

  function applyPlanTheme () {
    const card = document.getElementById('co-summary-card');
    if (card && ACTIVE_PLAN.color === 'cyan') card.classList.add('co-summary-card--cyan');
  }

  function wireRetryButtons () {
    ['failed-retry-btn', 'cancelled-retry-btn'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', () => {
        showState('idle');
        hideAlert();
      });
    });
  }


  /* ── PayPal JS SDK button rendering ─────────────────────────────────────
     The PayPal SDK is loaded dynamically once the user fills the form
     and clicks "Pay with PayPal".  This avoids loading it on page open
     and also lets us pass form validation before the SDK initialises.

     The Purchase button in the form is replaced by a "Pay with PayPal"
     button that triggers form validation, then renders the PayPal button
     inside #paypal-button-container.  The SDK button handles the rest.
  ──────────────────────────────────────────────────────────────────────── */

  let _paypalRendered = false;

  function _loadPayPalSDK (clientId) {
    return new Promise((resolve, reject) => {
      if (window.paypal) { resolve(window.paypal); return; }
      const s = document.createElement('script');
      // currency=USD, intent=capture — no subscription support needed for sandbox test
      s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture`;
      s.onload  = () => resolve(window.paypal);
      s.onerror = () => reject(new Error('PayPal SDK failed to load.'));
      document.head.appendChild(s);
    });
  }

  async function _renderPayPalButton (email, discord) {
    if (_paypalRendered) return;

    const clientId = _getPayPalClientId();
    if (!clientId) {
      showAlert('error', 'Payment is not configured. Please contact support.');
      return;
    }

    // Show the PayPal button container, hide the native submit button
    _hide('co-submit-btn-wrap');
    _show('paypal-button-container');

    let paypalSdk;
    try {
      paypalSdk = await _loadPayPalSDK(clientId);
    } catch (err) {
      _show('co-submit-btn-wrap');
      _hide('paypal-button-container');
      showAlert('error', 'Could not load the PayPal payment interface. Please refresh and try again.');
      return;
    }

    _paypalRendered = true;

    paypalSdk.Buttons({
      style: {
        layout: 'vertical',
        color:  'gold',
        shape:  'rect',
        label:  'pay',
        height: 48,
      },

      /* Called by PayPal SDK to create the order on our server */
      createOrder: async () => {
        // Re-read form in case values changed while the SDK was loading
        const vals = _formValues();
        const errs = validateForm(vals.email, vals.discord, vals.terms);
        if (Object.keys(errs).length) {
          fieldState('fg-co-email',   'co-email-err',   errs.email   || '');
          fieldState('fg-co-discord', 'co-discord-err', errs.discord || '');
          fieldState('fg-co-terms',   'co-terms-err',   errs.terms   || '');
          // Returning a rejected promise tells PayPal to stop and show an error
          throw new Error('Please complete the form before paying.');
        }

        const res  = await fetch('/api/paypal/create-order', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ plan: ACTIVE_PLAN.id, email: vals.email, discord: vals.discord }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          throw new Error(data.message || 'Could not create PayPal order.');
        }
        // Show loading state once we have an order ID and the customer is in PayPal
        showState('loading');
        return data.orderID;
      },

      /* Called by PayPal SDK after the customer approves the payment */
      onApprove: async (data) => {
        showState('loading');
        hideAlert();

        const vals = _formValues();

        try {
          const res = await fetch('/api/paypal/capture-order', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({
              orderID: data.orderID,
              plan:    ACTIVE_PLAN.id,
              email:   vals.email,
              discord: vals.discord,
            }),
          });
          const result = await res.json().catch(() => ({}));

          if (!res.ok || !result.ok) {
            showState('failed');
            _setText('failed-reason', result.message || 'Payment capture failed. Please contact support.');
            return;
          }

          // ── Payment confirmed and license delivered ──────────────────────
          const plan = PLANS[(result.plan || '').toLowerCase()] || ACTIVE_PLAN;

          _setText('success-email',    result.email   || vals.email || '—');
          _setText('success-plan',     plan.name      || result.plan || '—');
          _setText('success-order-id', result.orderId || data.orderID || '—');
          _setText('success-duration', plan.duration  || '—');
          _setText('success-amount',   result.priceUsd != null
            ? '$' + Number(result.priceUsd).toFixed(2) : '—');

          showState('success');
          _hide('co-key-pending');

          if (result.key) {
            _showDeliveredKey(result.key, result.tier, result.orderId, result.email, result.discord, result.priceUsd);
          } else {
            // Key not yet ready — show error + contact info
            _setText('co-key-error-msg',
              'Your payment was received but license delivery failed. ' +
              'Contact support with Order ID: ' + (result.orderId || data.orderID));
            _show('co-key-error');
          }

        } catch (err) {
          showState('failed');
          _setText('failed-reason',
            'A network error occurred after payment. Your payment may have been processed — ' +
            'please contact support with PayPal Order ID: ' + data.orderID);
        }
      },

      /* Customer cancelled in the PayPal popup */
      onCancel: () => {
        showState('cancelled');
      },

      /* PayPal SDK-level error (not a payment failure) */
      onError: (err) => {
        console.error('[ghost/paypal-sdk] error:', err);
        showState('failed');
        _setText('failed-reason',
          'A PayPal error occurred. No charge was made. Please try again or contact support.');
      },

    }).render('#paypal-button-container');
  }


  /* ── "Pay with PayPal" trigger button ───────────────────────────────────
     The form's submit button now triggers validation only.  If validation
     passes, the PayPal buttons are rendered into #paypal-button-container
     and the form submit button is hidden.
  ──────────────────────────────────────────────────────────────────────── */

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

      const vals = _formValues();
      const errors = validateForm(vals.email, vals.discord, vals.terms);
      fieldState('fg-co-email',   'co-email-err',   errors.email   || '');
      fieldState('fg-co-discord', 'co-discord-err', errors.discord || '');
      fieldState('fg-co-terms',   'co-terms-err',   errors.terms   || '');

      if (Object.keys(errors).length) return;

      // Validation passed — render PayPal button
      await _renderPayPalButton(vals.email, vals.discord);
    });
  }


  /* ── Init ────────────────────────────────────────────────────────────── */

  (function init () {
    renderSummary(ACTIVE_PLAN);
    applyPlanTheme();
    wireForm();
    wireRetryButtons();

    const btnText = document.getElementById('co-btn-text');
    if (btnText) {
      btnText.textContent = 'Pay with PayPal — ' + ACTIVE_PLAN.formatted;
    }
  })();

})();
