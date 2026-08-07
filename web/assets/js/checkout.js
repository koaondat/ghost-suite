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

  /* ── Coupon state ────────────────────────────────────────────── */
  let _appliedCoupon = null;   // null | { couponCode, discount, finalPrice, originalPrice, isFree, label }

  /* ── State machine ──────────────────────────────────────────
     All state panel IDs.  Only one is visible at a time.
  ──────────────────────────────────────────────────────────── */
  const STATES = ['idle', 'loading', 'success', 'delivery_pending', 'cancelled', 'failed'];

  function showState (name) {
    STATES.forEach(s => {
      const el = document.getElementById('state-' + s);
      if (el) el.hidden = (s !== name);
    });
  }


  /* ── Order summary renderer ─────────────────────────────────── */

  function renderSummary (plan, coupon) {
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

    const subtotalEl   = document.getElementById('co-price-subtotal');
    const totalEl      = document.getElementById('co-price-total');
    const discountRow  = document.getElementById('co-discount-row');
    const discountLabel= document.getElementById('co-discount-label');
    const discountVal  = document.getElementById('co-price-discount');
    const durationEl   = document.getElementById('co-license-duration-text');
    const cardPrice    = document.getElementById('co-card-price');

    if (subtotalEl) subtotalEl.textContent = plan.symbol + plan.price.toFixed(2);

    const activeCoupon = coupon || _appliedCoupon;
    if (activeCoupon) {
      const finalPrice = activeCoupon.finalPrice;
      if (totalEl)       totalEl.textContent     = plan.symbol + finalPrice.toFixed(2);
      if (discountRow)   discountRow.hidden       = false;
      if (discountLabel) discountLabel.textContent = activeCoupon.couponCode + '  ' + activeCoupon.label;
      if (discountVal)   discountVal.textContent  = '−' + plan.symbol + activeCoupon.discount.toFixed(2);
      if (cardPrice)     cardPrice.textContent    = finalPrice.toFixed(2);
    } else {
      if (totalEl)      totalEl.textContent    = plan.symbol + plan.price.toFixed(2);
      if (discountRow)  discountRow.hidden     = true;
      if (cardPrice)    cardPrice.textContent  = plan.price.toFixed(2);
    }
    if (durationEl) durationEl.textContent = 'License duration: ' + plan.duration;
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

  /* ── _formatDate — format ISO timestamp for display ──────────────── */
  function _formatDate (iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) {
      return iso;
    }
  }

  /* ── _showDeliveredKey — populate and show the full success panel ─── */
  function _showDeliveredKey (result) {
    const {
      licenseKey, orderId, instructions,
    } = result;

    // ── License section ──────────────────────────────────────────────
    _show('co-license-section');

    if (licenseKey) {
      // Populate and reveal the large key box
      _setText('success-license-key', licenseKey);
      _show('co-key-delivery');

      const inlineCopyBtn = document.getElementById('success-copy-btn');
      if (inlineCopyBtn) {
        inlineCopyBtn.onclick = () => _copyKey(licenseKey, [inlineCopyBtn]);
      }

      // "Copy License" action button
      const copyBtn = document.getElementById('success-copy-key-btn');
      if (copyBtn) {
        copyBtn.hidden = false;
        copyBtn.onclick = () => _copyKey(licenseKey, [copyBtn, inlineCopyBtn].filter(Boolean));
      }

      _show('co-key-warning');
    }

    // ── Download section (revealed only after verified payment) ──────
    _show('co-download-section');

    const dlBtn = document.getElementById('success-download-btn');
    if (dlBtn) {
      dlBtn.disabled = false;
      dlBtn.onclick = async function () {
        dlBtn.disabled    = true;
        dlBtn.textContent = 'Preparing download\u2026';
        try {
          // Ask server for the current configured download URL
          const cfgRes = await fetch('/api/download/current');
          const cfg    = await cfgRes.json().catch(() => ({}));
          const dlUrl  = (cfg.ok && cfg.url)      ? cfg.url      : '/dl/GhostConfig.exe';
          const dlName = (cfg.ok && cfg.filename) ? cfg.filename : 'GhostConfig.exe';
          const a   = document.createElement('a');
          a.href     = dlUrl;
          a.download = dlName;
          document.body.appendChild(a);
          a.click();
          a.remove();
        } catch (err) {
          // Hard fallback
          const a   = document.createElement('a');
          a.href     = '/dl/GhostConfig.exe';
          a.download = 'GhostConfig.exe';
          document.body.appendChild(a);
          a.click();
          a.remove();
        } finally {
          dlBtn.disabled = false;
          dlBtn.innerHTML =
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Download Ghost';
        }
      };
    }

    // ── Setup guide ──────────────────────────────────────────────────
    _show('co-setup-guide');
    // If backend returned custom steps, replace the static fallback
    if (Array.isArray(instructions) && instructions.length) {
      const stepsEl = document.getElementById('co-setup-steps');
      if (stepsEl) {
        stepsEl.innerHTML = instructions.map((step, i) => `
          <li class="co-setup-step">
            <span class="co-setup-step-num">${i + 1}</span>
            <div><span>${_escHtml(step)}</span></div>
          </li>`).join('');
      }
    }

    // ── Dashboard button ─────────────────────────────────────────────
    _show('success-dashboard-btn');
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
      // Show inline copy confirmation
      const confirm = document.getElementById('co-key-copy-confirm');
      if (confirm) {
        confirm.hidden = false;
        setTimeout(() => { confirm.hidden = true; }, 2500);
      }
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
    // "Try again" on failed / cancelled — resets to checkout form
    ['failed-retry-btn', 'cancelled-retry-btn'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', () => {
        showState('idle');
        _hide('co-payment-section');
        _hide('co-free-checkout-section');
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

    // 18-second client-side timeout (backend has 15s)
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 18_000);

    let res, data;
    try {
      res  = await fetch('/api/paypal/create-order', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          plan:       ACTIVE_PLAN.id,
          email:      vals.email,
          discord:    vals.discord,
          couponCode: _appliedCoupon ? _appliedCoupon.couponCode : undefined,
        }),
        signal:  controller.signal,
      });
      data = await res.json().catch(() => ({}));
    } catch (err) {
      clearTimeout(timer);
      const isAbort = err.name === 'AbortError';
      console.error('[ghost/checkout] create-order %s', isAbort ? 'timeout' : 'network-error');
      throw new Error(isAbort
        ? 'PayPal did not respond in time. Please try again.'
        : 'Network error connecting to payment service. Please try again.');
    } finally {
      clearTimeout(timer);
    }

    if (!res.ok || !data.ok) {
      const msg = data.message || 'Could not create PayPal order. Please try again.';
      console.error('[ghost/checkout] create-order failed stage=%s: %s', data.stage || '', msg);
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
  /* ── Loading steps helper ───────────────────────────────────────── */
  function _advanceStep (n) {
    for (let i = 1; i <= 3; i++) {
      const el = document.getElementById('lstep-' + i);
      if (!el) continue;
      el.classList.remove('active', 'done');
      if (i < n)        el.classList.add('done');
      else if (i === n) el.classList.add('active');
    }
  }

  /* ── _redirectToSuccess — obtain an access token then navigate ─────────
     Called once capture succeeds (or is known to be in-flight).
     Issues a server-side token for the orderId, then redirects to
     /order-success.html?order=<orderId>&token=<token>
  ───────────────────────────────────────────────────────────────────── */
  async function _redirectToSuccess (orderId) {
    try {
      const tr  = await fetch(`/api/order/${encodeURIComponent(orderId)}/issue-token`, { method: 'POST' });
      const td  = await tr.json().catch(() => ({}));
      const tok = td.ok && td.token ? `&token=${encodeURIComponent(td.token)}` : '';
      window.location.href = `/order-success.html?order=${encodeURIComponent(orderId)}${tok}`;
    } catch (_) {
      // Even if token issuance fails, send the user to the success page — it
      // will show a "complete" view once delivery_status becomes 'delivered'.
      window.location.href = `/order-success.html?order=${encodeURIComponent(orderId)}`;
    }
  }

  async function _capturePayPalOrder (orderID) {
    const vals = _capturedVals;
    console.log('[ghost/checkout] capturing order orderID=%s', orderID);
    _advanceStep(1);
    showState('loading');

    // 35-second client-side timeout (backend itself has 30s)
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35_000);

    let res, result;
    try {
      _advanceStep(2);
      res = await fetch('/api/paypal/capture-order', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          orderID,
          plan:    ACTIVE_PLAN.id,
          email:   vals.email,
          discord: vals.discord,
        }),
        signal: controller.signal,
      });
      _advanceStep(3);
      result = await res.json().catch(() => ({}));
    } catch (err) {
      clearTimeout(timer);
      const isAbort = err.name === 'AbortError';
      console.error('[ghost/checkout] capture fetch %s orderID=%s',
        isAbort ? 'timeout' : 'network-error', orderID);
      // Payment may have been captured — show the processing screen which will
      // poll the backend and redirect once delivery_status is confirmed.
      _showProcessingScreen(orderID, result && result.paymentStatus === 'COMPLETED', result && result.amount);
      return;
    } finally {
      clearTimeout(timer);
    }

    if (!res.ok || !result.ok) {
      // Only show "Payment failed" when the PayPal capture itself failed.
      // (delivery failures return ok:true with deliveryStatus:'delivery_pending')
      const msg   = result.message || 'Payment could not be processed. No charge was made.';
      const stage = result.stage   || '';
      console.error('[ghost/checkout] capture-order failed orderID=%s stage=%s: %s',
        orderID, stage, msg);
      showState('failed');
      _setText('failed-reason', msg);
      return;
    }

    const orderId = result.orderId || result.captureId || orderID;

    console.log('[ghost/checkout] capture response orderID=%s orderId=%s paymentStatus=%s deliveryStatus=%s',
      orderID, orderId, result.paymentStatus, result.deliveryStatus);

    // ── Payment captured and order exists — redirect immediately.
    //    The success page handles license polling; checkout is done here.
    if (result.paymentStatus === 'COMPLETED' && orderId) {
      await _redirectToSuccess(orderId);
      return;
    }

    // ── Out of stock: redirect to success page — it will show the OOS state ──
    if (result.deliveryStatus === 'out_of_stock') {
      await _redirectToSuccess(orderId);
      return;
    }

    // ── Fallback: payment confirmed but orderId somehow missing — should not
    //    happen, but show a brief processing screen rather than a blank page.
    _showProcessingScreen(orderId, result.paymentStatus === 'COMPLETED', result.amount);
  }

  /* ── _showProcessingScreen — compact "Processing your order" card ──────
     Shown briefly while delivery is being confirmed.
     Polls GET /api/order/:id every 1.5 s; redirects when delivered.
  ─────────────────────────────────────────────────────────────────────── */
  function _showProcessingScreen (orderId, paymentCaptured, amount, specialStatus) {
    // Fill the processing card info fields
    _setText('proc-order-id',  orderId || '—');
    const amtNum = parseFloat(amount);
    _setText('proc-amount',    (!isNaN(amtNum) ? 'USD ' + amtNum.toFixed(2) : (amount || '—')));
    _setText('proc-pay-status', paymentCaptured ? 'Captured ✓' : 'Confirming…');

    // Out-of-stock variant
    if (specialStatus === 'out_of_stock') {
      const stepsEl = document.getElementById('proc-steps');
      if (stepsEl) stepsEl.innerHTML =
        '<li class="co-pstep co-pstep--done"><span class="co-pstep-icon">✓</span> Payment captured</li>' +
        '<li class="co-pstep co-pstep--done"><span class="co-pstep-icon">✓</span> Order created</li>' +
        '<li class="co-pstep co-pstep--warn"><span class="co-pstep-icon">⚠</span> Temporarily out of stock</li>';
      const noteEl = document.getElementById('proc-oos-note');
      if (noteEl) { noteEl.hidden = false; }
    }

    showState('delivery_pending');

    if (!orderId || specialStatus === 'out_of_stock') return;

    // ── Poll the backend every 1.5 s ─────────────────────────────────────
    let _pollCount = 0;
    const MAX_POLL  = 40;   // ~60 s total

    const _poll = async () => {
      _pollCount++;
      try {
        const pr   = await fetch('/api/order/' + encodeURIComponent(orderId));
        const data = await pr.json().catch(() => ({}));

        if (data.ok) {
          // Update live status dots
          // NOTE: license_key is not present without a token — check delivery_status only
          const isPaid = data.payment_status === 'completed' || data.payment_status === 'verified';
          const hasOrder = !!(data.order_id);
          const isDelivered = data.delivery_status === 'delivered';

          _updateProcSteps(isPaid, hasOrder, isDelivered ? 'done' : (data.delivery_status === 'out_of_stock' ? 'warn' : 'spin'));

          if (isDelivered) {
            // Stop polling and redirect
            await _redirectToSuccess(orderId);
            return;
          }

          if (data.delivery_status === 'out_of_stock') {
            _updateProcSteps(true, true, 'warn');
            const noteEl = document.getElementById('proc-oos-note');
            if (noteEl) noteEl.hidden = false;
            return;  // Stop polling for OOS — admin must fulfill manually
          }
        }
      } catch (_) {}

      if (_pollCount < MAX_POLL) {
        setTimeout(_poll, 1500);
      } else {
        // Timed out — update note to suggest retrying later
        const noteEl = document.getElementById('proc-timeout-note');
        if (noteEl) noteEl.hidden = false;
      }
    };

    setTimeout(_poll, 1500);
  }

  function _updateProcSteps (payOk, orderOk, licenseState) {
    const el = document.getElementById('proc-steps');
    if (!el) return;
    const licIcon = licenseState === 'done' ? '✓' :
                    licenseState === 'warn' ? '⚠' : '⟳';
    const licClass = licenseState === 'done' ? 'co-pstep--done' :
                     licenseState === 'warn' ? 'co-pstep--warn' : 'co-pstep--spin';
    el.innerHTML =
      `<li class="co-pstep ${payOk   ? 'co-pstep--done' : 'co-pstep--spin'}"><span class="co-pstep-icon">${payOk ? '✓' : '⟳'}</span> Payment captured</li>` +
      `<li class="co-pstep ${orderOk ? 'co-pstep--done' : 'co-pstep--spin'}"><span class="co-pstep-icon">${orderOk ? '✓' : '⟳'}</span> Order created</li>` +
      `<li class="co-pstep ${licClass}"><span class="co-pstep-icon">${licIcon}</span> Assigning license</li>`;
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

    // ── Free Redemption Mode: coupon covers 100% — skip PayPal entirely ─────
    if (_appliedCoupon && _appliedCoupon.isFree) {
      _hide('co-submit-btn-wrap');
      _hide('co-payment-section');
      _show('co-free-checkout-section');
      return;
    }

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


  /* ── Coupon wiring ────────────────────────────────────────────── */
  function wireCoupon () {
    // Toggle coupon body visibility
    const toggleBtn = document.getElementById('co-coupon-toggle');
    const couponBody = document.getElementById('co-coupon-body');
    if (toggleBtn && couponBody) {
      toggleBtn.addEventListener('click', () => {
        const open = couponBody.style.display === 'none' || !couponBody.style.display;
        couponBody.style.display = open ? '' : 'none';
        toggleBtn.classList.toggle('active', open);
      });
    }

    const applyBtn  = document.getElementById('co-coupon-apply');
    const removeBtn = document.getElementById('co-coupon-remove');
    const input     = document.getElementById('co-coupon-input');
    const msgEl     = document.getElementById('co-coupon-msg');

    if (!applyBtn || !input) return;

    async function applyCode () {
      const code = (input.value || '').trim().toUpperCase();
      if (!code) { if (msgEl) { msgEl.textContent = 'Enter a coupon code.'; msgEl.className = 'co-coupon-msg co-coupon-msg--error'; } return; }
      if (msgEl) { msgEl.textContent = 'Checking…'; msgEl.className = 'co-coupon-msg co-coupon-msg--info'; }
      applyBtn.disabled = true;
      try {
        const res  = await fetch('/api/coupons/validate', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ code, plan: ACTIVE_PLAN.id }),
        });
        const data = await res.json().catch(() => ({}));
        if (data.ok) {
          _appliedCoupon = data;
          renderSummary(ACTIVE_PLAN);
          input.disabled = true;
          applyBtn.style.display = 'none';
          if (removeBtn) removeBtn.style.display = '';
          if (msgEl) {
            msgEl.textContent = data.isFree
              ? '🎉 100% off! Fill in your details above and click "Continue to Payment" to claim your free license.'
              : `✓ ${data.label} applied — you save $${data.discount.toFixed(2)}`;
            msgEl.className = 'co-coupon-msg co-coupon-msg--success';
          }
        } else {
          _appliedCoupon = null;
          renderSummary(ACTIVE_PLAN);
          if (msgEl) { msgEl.textContent = data.message || 'Invalid coupon.'; msgEl.className = 'co-coupon-msg co-coupon-msg--error'; }
        }
      } catch (_) {
        if (msgEl) { msgEl.textContent = 'Network error. Please try again.'; msgEl.className = 'co-coupon-msg co-coupon-msg--error'; }
      } finally {
        applyBtn.disabled = false;
      }
    }

    applyBtn.addEventListener('click', applyCode);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyCode(); } });

    if (removeBtn) {
      removeBtn.addEventListener('click', () => {
        _appliedCoupon = null;
        input.value    = '';
        input.disabled = false;
        applyBtn.style.display = '';
        removeBtn.style.display = 'none';
        if (msgEl) { msgEl.textContent = ''; msgEl.className = 'co-coupon-msg'; }
        renderSummary(ACTIVE_PLAN);
        // If free section was revealed, hide it and restore the submit button
        _hide('co-free-checkout-section');
        if (document.getElementById('co-payment-section')?.hidden !== false) {
          _show('co-submit-btn-wrap');
        }
      });
    }

    // Free coupon redemption — skips PayPal entirely
    const freeBtn = document.getElementById('co-free-coupon-btn');
    if (freeBtn) {
      freeBtn.addEventListener('click', async () => {
        if (!_appliedCoupon || !_appliedCoupon.isFree) return;
        const vals = _capturedVals;
        if (!vals || !vals.email) {
          // Form not yet submitted — validate first
          showAlert('error', 'Please fill in your details and click "Continue to Payment" first.');
          return;
        }
        freeBtn.disabled = true;
        freeBtn.innerHTML =
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="co-spinner-icon" aria-hidden="true"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg> Processing\u2026';
        showState('loading');
        try {
          const res  = await fetch('/api/coupons/redeem-free', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({
              code:    _appliedCoupon.couponCode,
              plan:    ACTIVE_PLAN.id,
              email:   vals.email,
              discord: vals.discord,
            }),
          });
          const data = await res.json().catch(() => ({}));
          if (data.ok && data.orderId) {
            await _redirectToSuccess(data.orderId);
          } else {
            showState('idle');
            showAlert('error', data.message || 'Free redemption failed. Please try again.');
            freeBtn.disabled = false;
            freeBtn.innerHTML =
              '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> Claim Free License';
          }
        } catch (_) {
          showState('idle');
          showAlert('error', 'Network error. Please try again.');
          freeBtn.disabled = false;
          freeBtn.innerHTML =
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> Claim Free License';
        }
      });
    }
  }

  /* ── Init ─────────────────────────────────────────────────────── */

  (function init () {
    renderSummary(ACTIVE_PLAN);
    applyPlanTheme();
    wireForm();
    wireRetryButtons();
    wireCoupon();

    const btnText = document.getElementById('co-btn-text');
    if (btnText) {
      btnText.textContent = 'Continue to Payment — ' + ACTIVE_PLAN.formatted;
    }
  })();

})();
