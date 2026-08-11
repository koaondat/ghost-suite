/* ============================================================
   order-success.js — Ghost Order Success Page Controller
   ============================================================
   Flow
   ----
   1. Read ?order=<orderId>&token=<token> from the URL.
   2. Fetch GET /api/order/:orderId?token=<token>
   3. Render order details + confetti + animations.
   4. Poll for license delivery if not yet delivered.
   ============================================================ */

(function () {
  'use strict';

  /* ── URL params ─────────────────────────────────────────────────────── */
  const _params  = new URLSearchParams(window.location.search);
  const _orderId = (_params.get('order') || '').trim();
  const _token   = (_params.get('token') || '').trim();

  /* ── Polling state ──────────────────────────────────────────────────── */
  let _pollTimer  = null;
  let _pollCount  = 0;
  const MAX_POLL  = 150;
  const FALLBACK_POLL = 3;

  /* ── Toast system ───────────────────────────────────────────────────── */
  function toast(msg, type, dur) {
    type = type || 'success';
    dur  = dur  || 3500;
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
      info:    'background:rgba(34,211,238,0.08);border:1px solid rgba(34,211,238,0.3);color:#67e8f9;'
    };
    el.style.cssText = 'padding:12px 18px;border-radius:10px;font-size:.875rem;font-weight:500;' +
      (colors[type] || colors.success) +
      'opacity:0;transform:translateY(10px);transition:opacity .22s,transform .22s;max-width:340px;pointer-events:all;backdrop-filter:blur(10px);';
    el.textContent = msg;
    c.appendChild(el);
    requestAnimationFrame(function() {
      el.style.opacity = '1';
      el.style.transform = 'translateY(0)';
    });
    setTimeout(function() {
      el.style.opacity = '0';
      el.style.transform = 'translateY(10px)';
      el.addEventListener('transitionend', function() { el.remove(); }, { once: true });
    }, dur);
  }

  /* ── Confetti ───────────────────────────────────────────────────────── */
  function launchConfetti() {
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:9000;';
    document.body.appendChild(canvas);
    var ctx = canvas.getContext('2d');
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;
    var pieces = [];
    var colors = ['#a855f7','#22d3ee','#22c55e','#f59e0b','#ec4899','#6366f1'];
    for (var i = 0; i < 120; i++) {
      pieces.push({
        x: Math.random() * canvas.width,
        y: -10 - Math.random() * 200,
        w: 6 + Math.random() * 8,
        h: 8 + Math.random() * 6,
        color: colors[Math.floor(Math.random() * colors.length)],
        vx: (Math.random() - 0.5) * 4,
        vy: 2 + Math.random() * 4,
        rot: Math.random() * 360,
        rotV: (Math.random() - 0.5) * 8,
        opacity: 1
      });
    }
    var start = Date.now();
    var dur = 2000;
    function draw() {
      var elapsed = Date.now() - start;
      if (elapsed > dur + 500) { canvas.remove(); return; }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      pieces.forEach(function(p) {
        p.x  += p.vx;
        p.y  += p.vy;
        p.rot += p.rotV;
        if (elapsed > dur * 0.6) p.opacity = Math.max(0, 1 - (elapsed - dur * 0.6) / (dur * 0.4));
        ctx.save();
        ctx.globalAlpha = p.opacity;
        ctx.translate(p.x + p.w/2, p.y + p.h/2);
        ctx.rotate(p.rot * Math.PI / 180);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.w/2, -p.h/2, p.w, p.h);
        ctx.restore();
      });
      requestAnimationFrame(draw);
    }
    draw();
  }

  /* ── Animated checkmark ─────────────────────────────────────────────── */
  function animateCheckmark() {
    var icon = document.querySelector('.os-success-icon');
    if (!icon) return;
    icon.style.cssText += 'animation:osCheckPop .5s cubic-bezier(.34,1.56,.64,1) both;';
    var style = document.createElement('style');
    style.textContent = '@keyframes osCheckPop{from{opacity:0;transform:scale(.4)}to{opacity:1;transform:scale(1)}}' +
      '@keyframes osFadeIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}' +
      '.os-fade-in{animation:osFadeIn .4s ease both}' +
      '.os-fade-in-1{animation-delay:.1s}' +
      '.os-fade-in-2{animation-delay:.22s}' +
      '.os-fade-in-3{animation-delay:.34s}' +
      '.os-fade-in-4{animation-delay:.46s}' +
      '.os-fade-in-5{animation-delay:.58s}';
    document.head.appendChild(style);
  }

  /* ── Helpers ────────────────────────────────────────────────────────── */
  function _show(id) { var el = document.getElementById(id); if (el) el.hidden = false; }
  function _hide(id) { var el = document.getElementById(id); if (el) el.hidden = true; }
  function _setText(id, text) { var el = document.getElementById(id); if (el) el.textContent = text || '—'; }

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

  function _escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* ── Error display ──────────────────────────────────────────────────── */
  function _showError(title, msg, orderNote) {
    _hide('os-loading');
    _hide('os-content');
    _setText('os-error-title', title);
    _setText('os-error-msg', msg);
    if (orderNote) _setText('os-error-order-note', 'Order ID: ' + orderNote);
    _show('os-error');
  }

  /* ── Plan label ─────────────────────────────────────────────────────── */
  var _PLAN_LABELS = {
    pro:      'Pro (monthly)',
    lifetime: 'Phantom Lifetime',
    trial:    'Phantom Trial (free)',
    '1day':   'Phantom — 1 Day',
    '7day':   'Phantom — 7 Days',
    '30day':  'Phantom — 30 Days',
    '90day':  'Phantom — 90 Days',
  };
  function _planLabel(planId, storedLabel) {
    if (storedLabel) return storedLabel;
    return _PLAN_LABELS[(planId || '').toLowerCase()] || planId || '—';
  }

  function _deriveInvoiceId(captureId) {
    if (!captureId) return '—';
    var raw  = captureId.replace(/[^A-Z0-9]/gi, '').toUpperCase();
    var sufx = raw.slice(-8).padStart(8, '0');
    return 'PHANTOM-INV-' + sufx;
  }

  /* ── Render order details ───────────────────────────────────────────── */
  function _renderOrderDetails(order) {
    var orderId   = order.order_id   || _orderId;
    var captureId = order.paypal_capture_id || order.captureId || orderId;
    var invoiceId = order.invoice_id || _deriveInvoiceId(captureId);
    var planLabel = _planLabel(order.plan, order.plan_label);
    var amountRaw = order.price_usd != null ? Number(order.price_usd) : null;
    var amountStr = amountRaw != null ? 'USD ' + amountRaw.toFixed(2) : '—';
    var dateStr   = _formatDate(order.created_at);
    var payStatus = (order.payment_status || 'completed').replace(/^\w/, function(c) { return c.toUpperCase(); });
    var isFree    = amountRaw === 0;

    /* Update header */
    var titleEl = document.getElementById('os-title');
    if (titleEl) {
      if (isFree) {
        titleEl.textContent = '✓ Free License Redeemed';
      } else {
        titleEl.textContent = '✓ Purchase Complete';
      }
    }
    var subEl = document.getElementById('os-subtitle');
    if (subEl) {
      subEl.textContent = isFree
        ? 'Your coupon covered 100% of this purchase.'
        : 'Thank you for purchasing Phantom.';
    }

    _setText('os-order-id',   orderId);
    _setText('os-invoice-id', invoiceId);
    _setText('os-plan',       planLabel);
    _setText('os-amount',     amountStr);
    _setText('os-date',       dateStr);
    _setText('os-pay-status', isFree ? 'Free (Coupon)' : payStatus);
    _setText('os-inv-id-badge', invoiceId);

    /* Payment method — hide PayPal row for free orders */
    var pmRow = document.getElementById('os-payment-method-row');
    if (pmRow) pmRow.hidden = isFree;

    /* Capture ID row — hide for free */
    var capRow = document.getElementById('os-capture-row');
    if (capRow) capRow.hidden = isFree;
    else _setText('os-capture-id', captureId);

    _wireDownloadButton();
    _wireInvoiceButtons(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus });
  }

  /* ── Show license key ───────────────────────────────────────────────── */
  function _renderLicenseDelivered(licenseKey) {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }

    _hide('os-license-pending');
    _hide('os-license-fallback');
    _show('os-license-delivered');

    _setText('os-license-key', licenseKey);
    _wireCopyButtons(licenseKey);
    _show('os-actions');

    toast('✔ License key ready — saved to your dashboard', 'success');
  }

  function _renderLicensePending() {
    _hide('os-license-delivered');
    _hide('os-license-fallback');
    _show('os-license-pending');
    _hide('os-actions');
  }

  function _renderLicenseFallback() {
    _hide('os-license-pending');
    _hide('os-license-delivered');
    _show('os-license-fallback');
    _hide('os-actions');
    var btn = document.getElementById('os-refresh-license-btn');
    if (btn) {
      var fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', async function() {
        fresh.disabled = true;
        fresh.textContent = 'Checking…';
        try {
          var r    = await fetch(_buildApiUrl(_orderId));
          var data = await r.json().catch(function() { return {}; });
          if (r.ok && data.ok) {
            var licenseKey = data.license_key || data.key || null;
            if (licenseKey && data.delivery_status === 'delivered') {
              _renderLicenseDelivered(licenseKey);
              return;
            }
          }
        } catch (_) {}
        fresh.disabled = false;
        fresh.textContent = 'Refresh License';
      });
    }
  }

  /* ── Full render ────────────────────────────────────────────────────── */
  function _render(order) {
    _renderOrderDetails(order);

    var licenseKey = order.license_key || order.key || null;

    if (licenseKey && order.delivery_status === 'delivered') {
      _renderLicenseDelivered(licenseKey);
    } else {
      _renderLicensePending();
      _schedulePoll();
    }

    var orderId   = order.order_id   || _orderId;
    var captureId = order.paypal_capture_id || order.captureId || orderId;
    var invoiceId = order.invoice_id || _deriveInvoiceId(captureId);
    var planLabel = _planLabel(order.plan, order.plan_label);
    var amountRaw = order.price_usd != null ? Number(order.price_usd) : null;
    var amountStr = amountRaw != null ? 'USD ' + amountRaw.toFixed(2) : '—';
    var dateStr   = _formatDate(order.created_at);
    var payStatus = (order.payment_status || 'completed').replace(/^\w/, function(c) { return c.toUpperCase(); });
    _renderInvoice(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });

    _hide('os-loading');
    _show('os-content');

    /* Animate sections */
    animateCheckmark();
    document.querySelectorAll('.os-card, .os-actions, .os-invoice').forEach(function(el, i) {
      el.classList.add('os-fade-in', 'os-fade-in-' + (i + 1));
    });

    /* Confetti on first load */
    setTimeout(launchConfetti, 300);
  }

  /* ── Invoice ────────────────────────────────────────────────────────── */
  function _renderInvoice(order, data) {
    var orderId   = data.orderId;
    var invoiceId = data.invoiceId;
    var captureId = data.captureId;
    var planLabel = data.planLabel;
    var amountStr = data.amountStr;
    var dateStr   = data.dateStr;
    var payStatus = data.payStatus;
    var licenseKey = data.licenseKey;
    var email   = order.email   || '—';
    var discord = order.discord || '—';
    var isFree  = amountStr === 'USD 0.00';

    var rows = [
      ['Invoice ID',       invoiceId,  true],
      ['Order ID',         orderId,    true],
    ];
    if (!isFree) rows.push(['PayPal Capture ID', captureId, true]);
    rows = rows.concat([
      ['Customer Email',   email,      false],
      ['Discord Username', discord,    false],
      ['Plan',             planLabel,  false],
      ['Amount Paid',      isFree ? 'FREE (Coupon Redeemed)' : amountStr, false],
      ['Purchase Date',    dateStr,    false],
      ['Payment Status',   isFree ? 'Free Redemption' : payStatus, false],
      ['License Key',      licenseKey || '—', true],
    ]);

    var body = document.getElementById('os-invoice-body');
    if (!body) return;
    body.innerHTML = rows.map(function(r) {
      return '<div class="os-invoice-row">' +
        '<label>' + _escHtml(r[0]) + '</label>' +
        '<span class="' + (r[2] ? 'mono' : '') + '">' + _escHtml(String(r[1])) + '</span>' +
        '</div>';
    }).join('');
  }

  /* ── Copy buttons ───────────────────────────────────────────────────── */
  function _wireCopyButtons(licenseKey) {
    ['os-copy-btn', 'os-copy-btn-2'].forEach(function(id) {
      var btn = document.getElementById(id);
      if (!btn) return;
      var fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', async function() {
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
        _showCopied();
        toast('✔ License copied to clipboard', 'success', 2000);
      });
    });
  }

  function _showCopied() {
    var confirm = document.getElementById('os-copy-confirm');
    if (!confirm) return;
    confirm.hidden = false;
    setTimeout(function() { confirm.hidden = true; }, 2500);
  }

  /* ── Download button ────────────────────────────────────────────────── */
  async function _wireDownloadButton() {
    var btn = document.getElementById('os-download-btn');
    if (!btn) return;

    var downloadUrl = '/download/latest';
    try {
      var r = await fetch('/api/downloads/current');
      var d = await r.json().catch(function() { return {}; });
      if (d.ok && d.url) downloadUrl = d.url;
    } catch (_) {}

    var fresh = btn.cloneNode(true);
    btn.parentNode.replaceChild(fresh, btn);
    fresh.addEventListener('click', function() {
      var a = document.createElement('a');
      a.href     = '/download/latest';
      a.download = 'Phantom.exe';
      a.click();
      toast('✔ Download started', 'success', 2000);
    });
    fresh.disabled = false;
  }

  /* ── Invoice buttons ────────────────────────────────────────────────── */
  function _wireInvoiceButtons(order, invoiceData) {
    var viewBtn = document.getElementById('os-view-receipt-btn');
    if (viewBtn) {
      viewBtn.addEventListener('click', function() {
        var el = document.getElementById('os-invoice');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
    var dlBtn = document.getElementById('os-download-invoice-btn');
    if (dlBtn) {
      dlBtn.addEventListener('click', function() {
        _downloadInvoiceText(order, invoiceData);
        toast('✔ Invoice downloading…', 'info', 2000);
      });
    }
  }

  function _downloadInvoiceText(order, invData) {
    var orderId    = invData.orderId;
    var invoiceId  = invData.invoiceId;
    var captureId  = invData.captureId;
    var planLabel  = invData.planLabel;
    var amountStr  = invData.amountStr;
    var dateStr    = invData.dateStr;
    var payStatus  = invData.payStatus;
    var email      = order.email   || '—';
    var discord    = order.discord || '—';
    var licenseKey = order.license_key || order.key || '—';

    var lines = [
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      '                 PHANTOM — INVOICE',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      '',
      'Invoice ID       : ' + invoiceId,
      'Order ID         : ' + orderId,
      'PayPal Capture ID: ' + captureId,
      '',
      'Customer Email   : ' + email,
      'Discord Username : ' + discord,
      '',
      'Plan             : ' + planLabel,
      'Amount Paid      : ' + amountStr,
      'Purchase Date    : ' + dateStr,
      'Payment Status   : ' + payStatus,
      '',
      '── License ──────────────────────────────────────',
      'License Key      : ' + licenseKey,
      '',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      'Phantom — Premium Windows Utility',
      'Support: https://discord.gg/your-invite',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
    ];

    var text = lines.join('\n');
    var blob = new Blob([text], { type: 'text/plain' });
    var url  = URL.createObjectURL(blob);
    var a    = document.createElement('a');
    a.href     = url;
    a.download = 'phantom-invoice-' + (invoiceId || orderId) + '.txt';
    a.click();
    setTimeout(function() { URL.revokeObjectURL(url); }, 60000);
  }

  /* ── Polling ────────────────────────────────────────────────────────── */
  function _schedulePoll() {
    if (_pollTimer) clearTimeout(_pollTimer);
    _pollTimer = setTimeout(_poll, 2000);
  }

  async function _poll() {
    _pollCount++;
    try {
      var r    = await fetch(_buildApiUrl(_orderId));
      var data = await r.json().catch(function() { return {}; });

      if (r.ok && data.ok) {
        var licenseKey  = data.license_key || data.key || null;
        var isDelivered = data.delivery_status === 'delivered';

        if (isDelivered && licenseKey) {
          var orderId   = data.order_id   || _orderId;
          var captureId = data.paypal_capture_id || data.captureId || orderId;
          var invoiceId = data.invoice_id || _deriveInvoiceId(captureId);
          var planLabel = _planLabel(data.plan, data.plan_label);
          var amountRaw = data.price_usd != null ? Number(data.price_usd) : null;
          var amountStr = amountRaw != null ? 'USD ' + amountRaw.toFixed(2) : '—';
          var dateStr   = _formatDate(data.created_at);
          var payStatus = (data.payment_status || 'completed').replace(/^\w/, function(c) { return c.toUpperCase(); });
          _renderInvoice(data, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });
          _renderLicenseDelivered(licenseKey);
          return;
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
      if (document.getElementById('os-license-fallback')?.hidden !== false) {
        _renderLicenseFallback();
      }
    }
  }

  /* ── Initial fetch ──────────────────────────────────────────────────── */
  async function _initialFetch() {
    var attempts = 0;
    var MAX_INIT = 5;

    var tryFetch = async function() {
      attempts++;
      try {
        var r    = await fetch(_buildApiUrl(_orderId));
        var data = await r.json().catch(function() { return {}; });

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

  /* ── Init ───────────────────────────────────────────────────────────── */
  (function init() {
    if (!_orderId) {
      _showError('No order specified', 'This page requires an order ID. Please return to checkout.', null);
      return;
    }
    _initialFetch();
  })();

})();
