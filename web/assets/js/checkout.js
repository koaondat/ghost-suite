/* ============================================================
   checkout.js — Ghost checkout page controller
   ============================================================
   Architecture
   ------------
   Stripe Checkout Sessions are created by the Node.js backend
   (api/checkout.js → POST /api/checkout/create-session).  The
   frontend collects email / discord / coupon, sends them to the
   backend, and redirects to the Stripe-hosted checkout page.

   On return from Stripe:
   • ?state=success&session_id=cs_…  → success state
     The session_id is sent to the backend to retrieve the
     generated license key via GET /api/order/<session_id>.
   • ?state=cancelled                → cancelled state

   Coupon validation:
     POST /api/checkout/validate-coupon
     Calls Stripe promo-code lookup on the backend; discount
     amounts are never calculated client-side.

   Security notes
   --------------
   • No payment card data is ever handled here.
   • No license keys are generated here.
   • No prices, plan names, or payment status are trusted from
     this file — everything is verified on the backend.
   • STRIPE_PUBLISHABLE_KEY is the only Stripe value that may
     appear in frontend code; all secret keys stay server-side.
   ============================================================ */

(function () {
  'use strict';

  /* ── Plan catalogue ─────────────────────────────────────────
     Single source of truth for plan metadata used across the
     order summary, button label, and success screen.
     To add a plan, add a key here — nothing else needs changing.
  ─────────────────────────────────────────────────────────── */
  const PLANS = {
    trial: {
      id:          'trial',
      name:        'Trial',
      tagline:     'Free 7-day trial',
      price:       0,
      currency:    'USD',
      symbol:      '$',
      formatted:   'Free',
      duration:    '7 days',
      color:       'accent',
      features: [
        'Windows only',
        'Core features',
        '7-day access',
        'Upgrade to Pro anytime',
      ],
    },
    pro: {
      id:          'pro',
      name:        'Pro',
      tagline:     'Monthly subscription',
      price:       7,
      currency:    'USD',
      symbol:      '$',
      formatted:   '$7 / month',
      duration:    'Active while subscription is live',
      color:       'accent',   // 'accent' = purple, 'cyan' = cyan
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
      id:          'lifetime',
      name:        'Lifetime',
      tagline:     'One-time payment — never pay again',
      price:       79,
      currency:    'USD',
      symbol:      '$',
      formatted:   '$79 one-time',
      duration:    'Permanent — no expiry',
      color:       'cyan',
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

  /* Default to 'pro' if no valid ?plan= param is present */
  const params      = new URLSearchParams(window.location.search);
  const planKey     = (params.get('plan') || 'pro').toLowerCase();
  const ACTIVE_PLAN = PLANS[planKey] || PLANS.pro;

  /* Track applied coupon so we can include it in the payload */
  let appliedCoupon = null;  // { code, discountPct, label } | null


  /* ══════════════════════════════════════════════════════════
     API STUB LAYER
     Replace stub bodies to connect real payment provider.
  ═════════════════════════════════════════════════════════ */
  const GhostCheckout = {

    /**
     * Create a Stripe Checkout Session on the backend and redirect to it.
     * The backend resolves price, plan, and mode — nothing from this
     * function is trusted for billing.
     *
     * For the free trial plan the backend returns { ok, free, key, tier }
     * and no redirect happens — the success state is shown inline.
     *
     * @param {{
     *   plan:    string,
     *   email:   string,
     *   discord: string,
     *   coupon?: string,
     * }} payload
     *
     * @returns {Promise<{
     *   ok:           boolean,
     *   redirect?:    boolean,  // true when the browser is about to navigate to Stripe
     *   free?:        boolean,  // true for trial plan (no redirect)
     *   orderId?:     string,   // set for free trial
     *   key?:         string,   // set for free trial
     *   tier?:        string,   // set for free trial
     *   message?:     string,   // error reason
     * }>}
     */
    createSession: async function (payload) {
      const res = await fetch('/api/checkout/create-session', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        return { ok: false, message: data.message || 'Order creation failed.' };
      }

      // Free trial: key returned directly — no Stripe redirect
      if (data.free) {
        return { ok: true, free: true, orderId: data.orderId, key: data.key, tier: data.tier };
      }

      // Paid plan: redirect to Stripe Checkout
      if (data.checkoutUrl) {
        window.location.href = data.checkoutUrl;
        return { ok: true, redirect: true };
      }

      return { ok: false, message: 'No checkout URL returned by server.' };
    },

    /**
     * Fetch a completed order record from the backend after a Stripe redirect.
     * Used on the success return URL (?state=success&session_id=cs_…).
     *
     * @param {string} sessionId  Stripe Checkout Session ID from the URL.
     * @returns {Promise<{ok, key?, plan?, tier?, email?, discord?, price_usd?, order_id?, error?}>}
     */
    fetchOrder: async function (sessionId) {
      try {
        const res  = await fetch(`/api/order/${encodeURIComponent(sessionId)}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          return { ok: false, error: data.error || 'Order not found.' };
        }
        return { ok: true, ...data };
      } catch (_) {
        return { ok: false, error: 'Could not reach the server. Contact support with your Session ID.' };
      }
    },

    /**
     * Validate a coupon code server-side via POST /api/checkout/validate-coupon.
     * The backend checks Stripe's promotion code API — discount amounts are
     * never calculated client-side.
     *
     * @param {string} code  Raw coupon text entered by user.
     * @param {string} plan  Plan ID ('pro' | 'lifetime').
     *
     * @returns {Promise<{
     *   ok:           boolean,
     *   discountPct?: number,
     *   label?:       string,
     *   message?:     string,
     * }>}
     */
    validateCoupon: async function (code, plan) {
      try {
        const res = await fetch('/api/checkout/validate-coupon', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ code, plan }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) return { ok: false, message: data.message || 'Invalid coupon.' };
        return data;
      } catch (_) {
        return { ok: false, message: 'Could not validate coupon. Please try again.' };
      }
    },
  };


  /* ══════════════════════════════════════════════════════════
     STATE MACHINE
  ═════════════════════════════════════════════════════════ */

  /** All state panel IDs in order. */
  const STATES = ['idle', 'loading', 'success', 'cancelled', 'failed'];

  /**
   * Switch the visible state panel.
   * @param {'idle'|'loading'|'success'|'cancelled'|'failed'} name
   */
  function showState (name) {
    STATES.forEach(s => {
      const el = document.getElementById('state-' + s);
      if (el) el.hidden = (s !== name);
    });
  }


  /* ══════════════════════════════════════════════════════════
     ORDER SUMMARY RENDERER
  ═════════════════════════════════════════════════════════ */

  function renderSummary (plan, coupon) {
    /* Plan header */
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

    /* Features */
    const featList = document.getElementById('co-summary-features');
    if (featList) {
      const isCyan = plan.color === 'cyan';
      featList.innerHTML = plan.features.map(f => `
        <li class="co-summary-feat">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="${isCyan ? 'feat-cyan' : 'feat-yes'}" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
          ${_escHtml(f)}
        </li>`).join('');
    }

    /* Price lines */
    const subtotalEl  = document.getElementById('co-price-subtotal');
    const totalEl     = document.getElementById('co-price-total');
    const discountRow = document.getElementById('co-discount-row');
    const discountLbl = document.getElementById('co-discount-label');
    const discountVal = document.getElementById('co-price-discount');
    const durationEl  = document.getElementById('co-license-duration-text');

    const subtotal = plan.price;
    let total      = subtotal;

    if (subtotalEl) subtotalEl.textContent = plan.symbol + subtotal.toFixed(2);

    if (coupon && coupon.discountPct) {
      const saving = subtotal * coupon.discountPct / 100;
      total        = subtotal - saving;
      if (discountRow) discountRow.hidden = false;
      if (discountLbl) discountLbl.textContent = 'Coupon (' + coupon.label + ')';
      if (discountVal) discountVal.textContent  = '−' + plan.symbol + saving.toFixed(2);
    } else {
      if (discountRow) discountRow.hidden = true;
    }

    if (totalEl)   totalEl.textContent  = plan.symbol + total.toFixed(2);
    if (durationEl) durationEl.textContent = 'License duration: ' + plan.duration;
  }


  /* ══════════════════════════════════════════════════════════
     LOADING STEP ANIMATOR
  ═════════════════════════════════════════════════════════ */

  function animateLoadingSteps () {
    const steps = ['lstep-1', 'lstep-2', 'lstep-3'];
    let i = 0;
    const tick = () => {
      if (i > 0) {
        const prev = document.getElementById(steps[i - 1]);
        if (prev) { prev.classList.remove('active'); prev.classList.add('done'); }
      }
      const cur = document.getElementById(steps[i]);
      if (cur) cur.classList.add('active');
      i++;
      if (i < steps.length) setTimeout(tick, 900);
    };
    // reset
    steps.forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.classList.remove('active', 'done'); }
    });
    tick();
  }


  /* ══════════════════════════════════════════════════════════
     FIELD HELPERS  (same pattern as auth.js)
  ═════════════════════════════════════════════════════════ */

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

  function setSubmitLoading (loading, label) {
    const btn     = document.getElementById('co-submit-btn');
    const text    = document.getElementById('co-btn-text');
    const spinner = btn?.querySelector('.btn-spinner');
    const lbl     = document.getElementById('co-btn-label');
    if (!btn) return;

    btn.disabled = loading;
    if (loading) {
      btn.setAttribute('disabled', '');
    } else {
      btn.removeAttribute('disabled');
    }
    if (text)    text.style.opacity = loading ? '0.7' : '1';
    if (spinner) spinner.hidden     = !loading;
    if (lbl)     lbl.hidden         = !loading;
    if (lbl && label) lbl.textContent = '— ' + label;
  }


  /* ══════════════════════════════════════════════════════════
     VALIDATION
  ═════════════════════════════════════════════════════════ */

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


  /* ══════════════════════════════════════════════════════════
     COUPON HANDLER
  ═════════════════════════════════════════════════════════ */

  function wireCouponButton () {
    const btn    = document.getElementById('co-coupon-btn');
    const input  = document.getElementById('co-coupon');
    const errEl  = document.getElementById('co-coupon-err');
    const okEl   = document.getElementById('co-coupon-status');
    const msgEl  = document.getElementById('co-coupon-msg');

    if (!btn || !input) return;

    btn.addEventListener('click', async () => {
      const code = input.value.trim();

      /* Clear previous state */
      if (errEl) errEl.textContent = '';
      if (okEl)  okEl.hidden = true;

      if (!code) {
        /* Reset any applied coupon */
        appliedCoupon = null;
        renderSummary(ACTIVE_PLAN, null);
        return;
      }

      btn.disabled = true;
      btn.textContent = '…';

      const result = await GhostCheckout.validateCoupon(code, ACTIVE_PLAN.id);

      btn.disabled = false;
      btn.textContent = 'Apply';

      if (result.ok) {
        appliedCoupon = { code, discountPct: result.discountPct, label: result.label };
        renderSummary(ACTIVE_PLAN, appliedCoupon);
        if (okEl)  okEl.hidden  = false;
        if (msgEl) msgEl.textContent = result.label + ' applied!';
        if (errEl) errEl.textContent = '';
        /* Mark field valid */
        const fg = document.getElementById('fg-co-coupon');
        if (fg) { fg.classList.remove('is-invalid'); fg.classList.add('is-valid'); }
      } else {
        appliedCoupon = null;
        renderSummary(ACTIVE_PLAN, null);
        if (errEl) errEl.textContent = result.message || 'Invalid coupon.';
        if (okEl)  okEl.hidden = true;
        const fg = document.getElementById('fg-co-coupon');
        if (fg) { fg.classList.add('is-invalid'); fg.classList.remove('is-valid'); }
      }
    });

    /* Allow Enter key inside coupon input to trigger Apply */
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); btn.click(); }
    });

    /* Clear coupon if input is emptied */
    input.addEventListener('input', () => {
      if (!input.value.trim() && appliedCoupon) {
        appliedCoupon = null;
        renderSummary(ACTIVE_PLAN, null);
        const okEl2 = document.getElementById('co-coupon-status');
        if (okEl2) okEl2.hidden = true;
        const fg = document.getElementById('fg-co-coupon');
        if (fg) { fg.classList.remove('is-invalid', 'is-valid'); }
      }
    });
  }


  /* ══════════════════════════════════════════════════════════
     FORM SUBMIT
  ═════════════════════════════════════════════════════════ */

  function wireForm () {
    const form = document.getElementById('checkout-form');
    if (!form) return;

    /* Inline validation on blur */
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

      const email   = form.querySelector('#co-email').value.trim();
      const discord = form.querySelector('#co-discord').value.trim();
      const terms   = form.querySelector('#co-terms').checked;

      /* Client-side validation */
      const errors = validateForm(email, discord, terms);
      fieldState('fg-co-email',    'co-email-err',   errors.email   || '');
      fieldState('fg-co-discord',  'co-discord-err', errors.discord || '');
      fieldState('fg-co-terms',    'co-terms-err',   errors.terms   || '');

      if (Object.keys(errors).length) return;

      /* ── Start loading state ── */
      setSubmitLoading(true, 'processing');
      showState('loading');
      animateLoadingSteps();

      const payload = {
        plan:    ACTIVE_PLAN.id,
        email,
        discord,
        coupon:  appliedCoupon ? appliedCoupon.code : undefined,
      };

      try {
        const result = await GhostCheckout.createSession(payload);

        if (result.redirect) {
          /* Browser is navigating to Stripe — keep the loading state visible */
          return;
        }

        if (result.ok && result.free) {
          /* Free trial — key returned directly without redirect */
          _setText('success-email',    email);
          _setText('success-plan',     ACTIVE_PLAN.name);
          _setText('success-order-id', result.orderId || '—');
          _setText('success-duration', ACTIVE_PLAN.duration);
          _setText('success-amount',   '$0.00');
          showState('success');
          setSubmitLoading(false);
          _showDeliveredKey(result.key, result.tier, result.orderId, email, discord, 0);
          return;
        }

        if (!result.ok) {
          showState('failed');
          _setText('failed-reason', result.message || 'Your payment could not be processed. No charge was made.');
          setSubmitLoading(false);
        }

      } catch (_) {
        showState('failed');
        _setText('failed-reason', 'A network error occurred. Please check your connection and try again. No charge was made.');
        setSubmitLoading(false);
      }
    });
  }


  /* ══════════════════════════════════════════════════════════
     RETRY / BACK BUTTONS
  ═════════════════════════════════════════════════════════ */

  function wireRetryButtons () {
    ['failed-retry-btn', 'cancelled-retry-btn'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', () => {
        showState('idle');
        setSubmitLoading(false);
        hideAlert();
      });
    });
  }


  /* ══════════════════════════════════════════════════════════
     PLAN BADGE — style the summary card border for lifetime
  ═════════════════════════════════════════════════════════ */

  function applyPlanTheme () {
    const card = document.getElementById('co-summary-card');
    if (!card) return;
    if (ACTIVE_PLAN.color === 'cyan') {
      card.classList.add('co-summary-card--cyan');
    }

    /* Also theme the submit button */
    const btn = document.getElementById('co-submit-btn');
    if (btn && ACTIVE_PLAN.color === 'cyan') {
      btn.classList.remove('btn-primary');
      btn.classList.add('btn-secondary');
    }
  }


  /* ══════════════════════════════════════════════════════════
     LICENSE KEY DELIVERY
     Called after createOrder succeeds.  Calls the backend,
     then updates the success state UI with the returned key.
     Never runs before payment is confirmed; never exposes secrets.
   ═════════════════════════════════════════════════════════ */

  /**
   * Fetch the order from the backend using the Stripe session ID, then
   * update the success UI with the license key.
   * Called when the user returns from Stripe Checkout (?state=success).
   *
   * @param {string} sessionId  Stripe Checkout Session ID from the URL
   */
  async function _fetchAndShowOrder (sessionId) {
    _show('co-key-pending');
    _hide('co-key-delivery');
    _hide('co-key-error');

    // Poll up to ~20 s — the webhook may arrive a few seconds after redirect
    const MAX_ATTEMPTS = 10;
    const POLL_MS      = 2000;
    let result;

    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      result = await GhostCheckout.fetchOrder(sessionId);
      if (result.ok && result.license_key) break;
      // Order not yet delivered — wait before retrying
      if (i < MAX_ATTEMPTS - 1) await _delay(POLL_MS);
    }

    _hide('co-key-pending');

    if (result && result.ok && result.license_key) {
      const plan = PLANS[(result.plan || '').toLowerCase()] || ACTIVE_PLAN;

      /* Populate meta fields from backend-verified data */
      _setText('success-email',    result.email    || '—');
      _setText('success-plan',     plan.name       || result.plan || '—');
      _setText('success-order-id', result.order_id || sessionId);
      _setText('success-duration', plan.duration   || '—');
      _setText('success-amount',   result.price_usd != null
        ? '$' + Number(result.price_usd).toFixed(2) : '—');

      /* Persist for the dashboard */
      try {
        sessionStorage.setItem('ghost_last_order', JSON.stringify({
          orderId:   result.order_id || sessionId,
          key:       result.license_key,
          tier:      result.tier    || '',
          plan:      result.plan    || '',
          email:     result.email   || '',
          discord:   result.discord || '',
          priceUsd:  result.price_usd,
          sessionId,
        }));
      } catch (_) { /* non-fatal */ }

      _showDeliveredKey(result.license_key, result.tier, result.order_id, result.email, result.discord, result.price_usd);

    } else {
      const errMsg = (result && result.error) || 'License key delivery failed or is still processing.';
      _setText('co-key-error-msg', errMsg + ' Your payment was received — please contact support with your Session ID: ' + sessionId);
      _show('co-key-error');
      const dashBtn = document.getElementById('success-dashboard-btn');
      if (dashBtn) dashBtn.hidden = false;
    }
  }

  /**
   * Render a delivered key onto the success UI and wire copy buttons.
   * Used both for free-trial (inline) and post-redirect Stripe purchases.
   */
  function _showDeliveredKey (key, tier, orderId, email, discord, priceUsd) {
    _show('co-key-delivery');
    _setText('success-license-key', key);

    /* Wire inline copy button */
    const inlineCopyBtn = document.getElementById('success-copy-btn');
    if (inlineCopyBtn) inlineCopyBtn.onclick = () => _copyKey(key, [inlineCopyBtn]);

    /* Show action buttons */
    const copyBtn = document.getElementById('success-copy-key-btn');
    const dashBtn = document.getElementById('success-dashboard-btn');
    const dlBtn   = document.getElementById('success-download-btn');

    if (copyBtn) { copyBtn.hidden = false; copyBtn.onclick = () => _copyKey(key, [copyBtn]); }
    if (dashBtn) dashBtn.hidden = false;
    if (dlBtn)   dlBtn.hidden   = false;
  }

  /** Copy text to clipboard with brief visual feedback on the button(s). */
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

  /** Show an element by removing the hidden attribute. */
  function _show (id) {
    const el = document.getElementById(id);
    if (el) el.hidden = false;
  }

  /** Hide an element by setting the hidden attribute. */
  function _hide (id) {
    const el = document.getElementById(id);
    if (el) el.hidden = true;
  }


  /* ══════════════════════════════════════════════════════════
     UTILITY
   ═════════════════════════════════════════════════════════ */

  function _delay (ms) { return new Promise(r => setTimeout(r, ms)); }

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


  /* ══════════════════════════════════════════════════════════
     INIT
  ═════════════════════════════════════════════════════════ */

  /**
   * Handle the Stripe return URL.
   * Called when the page loads with ?state=success&session_id=cs_… or ?state=cancelled.
   * Returns true if a return state was detected (form should not be shown).
   */
  function handleStripeReturn () {
    const state     = params.get('state');
    const sessionId = params.get('session_id');

    if (state === 'cancelled') {
      showState('cancelled');
      return true;
    }

    if (state === 'success' && sessionId) {
      /* Show success skeleton immediately, then fetch the order */
      showState('success');
      _show('co-key-pending');

      /* Derive plan from the URL if present (for display only — key data comes from backend) */
      const urlPlan = params.get('plan');
      if (urlPlan && PLANS[urlPlan]) {
        _setText('success-plan', PLANS[urlPlan].name);
        _setText('success-duration', PLANS[urlPlan].duration);
      }

      /* Fetch the real order data (with key) from the backend */
      _fetchAndShowOrder(sessionId);
      return true;
    }

    return false;
  }


  (function init () {
    /* If we're returning from Stripe, handle that first */
    if (handleStripeReturn()) return;

    /* Normal checkout form flow */
    renderSummary(ACTIVE_PLAN, null);
    applyPlanTheme();
    wireCouponButton();
    wireForm();
    wireRetryButtons();

    /* Update button text to reflect plan */
    const btnText = document.getElementById('co-btn-text');
    if (btnText) {
      btnText.textContent = ACTIVE_PLAN.price === 0
        ? 'Claim Free Access'
        : 'Complete Purchase — ' + ACTIVE_PLAN.formatted;
    }
  })();

})();
