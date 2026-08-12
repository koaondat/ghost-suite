/* ============================================================
   order-success.js — Phantom Order Success Page Controller
   ============================================================
   Flow
   ----
   1. Read ?order=<orderId>&token=<token> from URL.
   2. Fetch GET /api/order/:orderId?token=<token>
   3. If license_key present → render delivered state.
   4. If order paid but no key → auto-attempt recovery once, then poll.
   5. Render confetti + animations on success.
   ============================================================ */

(function () {
  'use strict';

  var _params  = new URLSearchParams(window.location.search);
  var _orderId = (_params.get('order') || '').trim();
  var _token   = (_params.get('token') || '').trim();

  /* ── Polling state ──────────────────────────────────────────── */
  var _pollTimer    = null;
  var _pollCount    = 0;
  var MAX_POLL      = 120;       // 120 × 2s = 4 minutes max
  var FALLBACK_POLL = 5;         // show retry button after 5 failed polls
  var _recoveryAttempted = false;
  var _confettiFired     = false;

  /* ── Toast ──────────────────────────────────────────────────── */
  function toast(msg, type, dur) {
    type = type || 'success';
    dur  = dur  || 3000;
    var c = document.getElementById('os-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'os-toast-container';
      c.style.cssText = 'position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column;gap:10px;z-index:9999;pointer-events:none;';
      document.body.appendChild(c);
    }
    var el = document.createElement('div');
    var colors = {
      success: 'background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.35);color:#86efac;',
      error:   'background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.35);color:#fca5a5;',
      info:    'background:rgba(192,21,42,0.1);border:1px solid rgba(192,21,42,0.3);color:#f0a0aa;',
    };
    el.style.cssText = 'padding:12px 18px;border-radius:10px;font-size:.875rem;font-weight:500;' +
      (colors[type] || colors.success) +
      'opacity:0;transform:translateY(10px);transition:opacity .22s,transform .22s;max-width:340px;pointer-events:all;backdrop-filter:blur(10px);';
    el.textContent = msg;
    c.appendChild(el);
    requestAnimationFrame(function () {
      el.style.opacity = '1'; el.style.transform = 'translateY(0)';
    });
    setTimeout(function () {
      el.style.opacity = '0'; el.style.transform = 'translateY(10px)';
      el.addEventListener('transitionend', function () { el.remove(); }, { once: true });
    }, dur);
  }

  /* ── Confetti ───────────────────────────────────────────────── */
  function launchConfetti() {
    if (_confettiFired) return;
    _confettiFired = true;
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:9000;';
    document.body.appendChild(canvas);
    var ctx = canvas.getContext('2d');
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    var pieces = [];
    var colors = ['#c0152a','#e01830','#f0a0aa','#f0f0f2','#6e6e7a'];
    for (var i = 0; i < 100; i++) {
      pieces.push({
        x: Math.random() * canvas.width, y: -10 - Math.random() * 200,
        w: 5 + Math.random() * 8, h: 7 + Math.random() * 6,
        color: colors[Math.floor(Math.random() * colors.length)],
        vx: (Math.random() - 0.5) * 3.5, vy: 2 + Math.random() * 3.5,
        rot: Math.random() * 360, rotV: (Math.random() - 0.5) * 7, opacity: 1,
      });
    }
    var start = Date.now(), dur = 2200;
    (function draw() {
      var elapsed = Date.now() - start;
      if (elapsed > dur + 400) { canvas.remove(); return; }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      pieces.forEach(function (p) {
        p.x += p.vx; p.y += p.vy; p.rot += p.rotV;
        if (elapsed > dur * 0.6) p.opacity = Math.max(0, 1 - (elapsed - dur * 0.6) / (dur * 0.4));
        ctx.save(); ctx.globalAlpha = p.opacity;
        ctx.translate(p.x + p.w / 2, p.y + p.h / 2);
        ctx.rotate(p.rot * Math.PI / 180);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        ctx.restore();
      });
      requestAnimationFrame(draw);
    })();
  }

  /* ── Helpers ────────────────────────────────────────────────── */
  function _show(id) { var el = document.getElementById(id); if (el) el.hidden = false; }
  function _hide(id) { var el = document.getElementById(id); if (el) el.hidden = true; }
  function _setText(id, text) { var el = document.getElementById(id); if (el) el.textContent = (text != null && text !== '') ? text : '—'; }

  function _formatDate(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { return iso; }
  }

  function _buildApiUrl(orderId) {
    var base = '/api/order/' + encodeURIComponent(orderId);
    return _token ? base + '?token=' + encodeURIComponent(_token) : base;
  }

  /* ── Error display ──────────────────────────────────────────── */
  function _showError(title, msg, orderNote) {
    _hide('os-loading');
    _hide('os-content');
    _setText('os-error-title', title);
    _setText('os-error-msg', msg);
    if (orderNote) _setText('os-error-order-note', 'Order ID: ' + orderNote);
    _show('os-error');
  }

  /* ── Plan label ─────────────────────────────────────────────── */
  var _PLAN_LABELS = {
    day:      'Phantom 1 Day',
    '3days':  'Phantom 3 Days',
    week:     'Phantom 1 Week',
    month:    'Phantom 1 Month',
    '3months':'Phantom 3 Months',
    // Legacy aliases
    pro:      'Phantom Pro (Monthly)',
    lifetime: 'Phantom 3 Months',
    trial:    'Phantom 1 Day',
  };
  function _planLabel(planId, storedLabel) {
    if (storedLabel) return storedLabel;
    return _PLAN_LABELS[(planId || '').toLowerCase()] || planId || '—';
  }

  /* ── Render order details ───────────────────────────────────── */
  function _renderOrderDetails(order) {
    var orderId   = order.order_id || _orderId;
    var planLabel = _planLabel(order.plan, order.plan_label);
    var amountRaw = order.price_usd != null ? Number(order.price_usd) : null;
    var amountStr = amountRaw != null ? 'USD\u00a0' + amountRaw.toFixed(2) : '—';
    var dateStr   = _formatDate(order.created_at);
    var payStatus = (order.payment_status || 'completed').replace(/^\w/, function (c) { return c.toUpperCase(); });
    var isFree    = amountRaw === 0;

    // Header
    var titleEl = document.getElementById('os-title');
    if (titleEl) titleEl.textContent = isFree ? '✓ Free License Redeemed' : '✓ Purchase Complete';
    var subEl = document.getElementById('os-subtitle');
    if (subEl) {
      subEl.textContent = isFree
        ? 'Your coupon covered 100% of this purchase.'
        : 'Thank you for purchasing Phantom. Your payment was successful.';
    }

    _setText('os-order-id',  orderId);
    _setText('os-plan',      planLabel);
    _setText('os-amount',    isFree ? 'FREE' : amountStr);
    _setText('os-date',      dateStr);
    _setText('os-pay-status', isFree ? 'Free (Coupon)' : payStatus);

    // Hide payment method row for free orders
    var pmRow = document.getElementById('os-payment-method-row');
    if (pmRow) pmRow.hidden = isFree;
  }

  /* ── Discord card — shown when user has no linked Discord account ─────────── */
  function _checkAndShowDiscordCard() {
    var token = typeof localStorage !== 'undefined' ? localStorage.getItem('ghost_token') : null;
    if (!token) {
      _show('os-discord-card');
      return;
    }
    // Query the status endpoint; show the card if not linked
    fetch('/api/account/discord/status', {
      headers: { 'Authorization': 'Bearer ' + token }
    }).then(function(r) { return r.json().catch(function() { return {}; }); })
      .then(function(d) {
        if (!d.linked) {
          _show('os-discord-card');
        }
      }).catch(function() {
        // On error, show the card as a safe fallback (user can dismiss by navigating)
        _show('os-discord-card');
      });
  }

  /* ── License states ─────────────────────────────────────────── */
  function _renderLicenseDelivered(licenseKey) {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }

    _hide('os-license-pending');
    _hide('os-license-fallback');
    _hide('os-license-error');
    _show('os-license-delivered');

    _setText('os-license-key', licenseKey);
    _wireCopyButtons(licenseKey);

    // Animate checkmark icon
    var icon = document.getElementById('os-check-icon');
    if (icon) icon.style.animation = 'osCheckPop .5s cubic-bezier(.34,1.56,.64,1) both';

    // Show Discord card if the account has not yet linked Discord
    _checkAndShowDiscordCard();

    setTimeout(launchConfetti, 200);
    toast('✔ License key ready', 'success', 4000);
  }

  function _renderLicensePending() {
    _hide('os-license-delivered');
    _hide('os-license-fallback');
    _hide('os-license-error');
    _show('os-license-pending');
  }

  function _renderLicenseFallback() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
    _hide('os-license-pending');
    _hide('os-license-delivered');
    _hide('os-license-error');
    _show('os-license-fallback');

    var btn = document.getElementById('os-retry-btn');
    if (!btn) return;
    // Replace to remove stale listeners
    var fresh = btn.cloneNode(true);
    btn.parentNode.replaceChild(fresh, btn);
    fresh.addEventListener('click', function () {
      fresh.disabled = true;
      fresh.textContent = 'Checking…';
      _renderLicensePending();
      _pollCount = 0;
      _schedulePoll();
    });
  }

  /* ── Copy buttons ───────────────────────────────────────────── */
  function _wireCopyButtons(licenseKey) {
    ['os-copy-btn', 'os-copy-btn-2'].forEach(function (id) {
      var btn = document.getElementById(id);
      if (!btn) return;
      var fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', async function () {
        try {
          await navigator.clipboard.writeText(licenseKey);
        } catch (_) {
          var ta = document.createElement('textarea');
          ta.value = licenseKey;
          ta.style.cssText = 'position:fixed;opacity:0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        var confirm = document.getElementById('os-copy-confirm');
        if (confirm) { confirm.hidden = false; setTimeout(function () { confirm.hidden = true; }, 2500); }
        toast('✔ License copied', 'success', 2000);
      });
    });
  }

  /* ── Download button ────────────────────────────────────────── */
  function _wireDownloadButton() {
    var btn = document.getElementById('os-download-btn');
    if (!btn) return;
    var fresh = btn.cloneNode(true);
    btn.parentNode.replaceChild(fresh, btn);
    fresh.addEventListener('click', function () {
      var a = document.createElement('a');
      a.href = '/download/latest';
      a.download = 'Phantom.exe';
      a.click();
      toast('✔ Download started', 'success', 2000);
    });
  }

  /* ── Recovery: try to generate the missing license ─────────── */
  // Calls POST /api/order/:orderId/recover-license with the user's token.
  // Idempotent — safe to call multiple times.
  async function _attemptRecovery() {
    if (!_token) return false;   // no token → cannot authorize recovery
    _recoveryAttempted = true;
    try {
      console.log('[order-success] attempting license recovery for orderId=%s', _orderId);
      var r = await fetch(
        '/api/order/' + encodeURIComponent(_orderId) + '/recover-license',
        {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ token: _token }),
        }
      );
      var data = await r.json().catch(function () { return {}; });
      if (r.ok && data.ok && data.licenseKey) {
        console.log('[order-success] recovery succeeded, licenseKey=[present]');
        return data.licenseKey;
      }
      console.warn('[order-success] recovery failed status=%d reason=%s', r.status, data.error || data.reason || 'unknown');
    } catch (err) {
      console.error('[order-success] recovery error: %s', err.message);
    }
    return false;
  }

  /* ── Full render ────────────────────────────────────────────── */
  function _render(order) {
    _renderOrderDetails(order);
    _wireDownloadButton();

    var licenseKey = order.license_key || null;

    if (licenseKey && order.delivery_status === 'delivered') {
      _renderLicenseDelivered(licenseKey);
    } else {
      // Payment verified but license not yet delivered — start recovery + poll
      _renderLicensePending();
      _scheduleRecoveryAndPoll(order);
    }

    _hide('os-loading');
    _show('os-content');
  }

  /* ── Recovery + poll flow ───────────────────────────────────── */
  async function _scheduleRecoveryAndPoll(order) {
    // Only attempt auto-recovery for verified, paid orders
    var ps = (order.payment_status || '').toLowerCase();
    var isPaid = ps === 'completed' || ps === 'captured' || ps === 'verified';

    if (isPaid && !_recoveryAttempted) {
      // Give the server 1s to complete any in-flight fulfillment first
      await new Promise(function (r) { setTimeout(r, 1000); });
      var recoveredKey = await _attemptRecovery();
      if (recoveredKey) {
        _renderLicenseDelivered(recoveredKey);
        return;
      }
    }

    // If recovery didn't produce a key immediately, start polling
    _schedulePoll();
  }

  /* ── Polling ────────────────────────────────────────────────── */
  function _schedulePoll() {
    if (_pollTimer) clearTimeout(_pollTimer);
    _pollTimer = setTimeout(_poll, 2000);
  }

  async function _poll() {
    _pollCount++;
    try {
      var r    = await fetch(_buildApiUrl(_orderId));
      var data = await r.json().catch(function () { return {}; });

      if (r.ok && data.ok) {
        var licenseKey  = data.license_key || null;
        var isDelivered = data.delivery_status === 'delivered';

        if (isDelivered && licenseKey) {
          _renderLicenseDelivered(licenseKey);
          return;
        }

        // Delivered but key missing from response (token expired/invalid) →
        // try one recovery attempt to get the key
        if (isDelivered && !licenseKey && !_recoveryAttempted) {
          var recoveredKey = await _attemptRecovery();
          if (recoveredKey) {
            _renderLicenseDelivered(recoveredKey);
            return;
          }
        }
      }

      if (r.status === 403) {
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
        _hide('os-license-pending');
        _hide('os-license-fallback');
        _show('os-license-error');
        _setText('os-license-error-msg', 'Your access link has expired. Please contact support with your Order ID.');
        return;
      }
    } catch (_) {}

    if (_pollCount < MAX_POLL) {
      if (_pollCount === FALLBACK_POLL) _renderLicenseFallback();
      _schedulePoll();
    } else {
      _renderLicenseFallback();
    }
  }

  /* ── Initial fetch ──────────────────────────────────────────── */
  async function _initialFetch() {
    var attempts = 0;
    var MAX_INIT = 5;

    var tryFetch = async function () {
      attempts++;
      try {
        var r    = await fetch(_buildApiUrl(_orderId));
        var data = await r.json().catch(function () { return {}; });

        if (r.ok && data.ok) { _render(data); return; }

        if (r.status === 403) {
          _showError('Access link expired', 'Your order access link has expired. Please contact support with your Order ID.', _orderId);
          return;
        }

        if (r.status === 404 || !data.ok) {
          if (attempts < MAX_INIT) { setTimeout(tryFetch, 1500); return; }
          _showError('Order not found', 'We could not find your order. Please contact support.', _orderId);
          return;
        }

        _showError('Load failed', 'Could not load your order. Please refresh the page.', _orderId);
      } catch (_) {
        if (attempts < MAX_INIT) { setTimeout(tryFetch, 1500); return; }
        _showError('Network error', 'Could not reach the server. Please refresh the page.', _orderId);
      }
    };

    tryFetch();
  }

  /* ── Init ───────────────────────────────────────────────────── */
  (function init() {
    if (!_orderId) {
      _showError('No order specified', 'This page requires an order ID. Please return to checkout.', null);
      return;
    }
    _initialFetch();
  })();

})();
