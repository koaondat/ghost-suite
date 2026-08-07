/* ============================================================
   order-success.js — Ghost Order Success Page Controller
   ============================================================
   This page NEVER trusts URL parameters beyond the order ID.
   All order data is fetched from GET /api/order/:orderId?token=<token>
   using the server-issued access token embedded in the URL.

   Flow
   ----
   1. Read ?order=<orderId>&token=<token> from the URL.
   2. Fetch GET /api/order/:orderId?token=<token>
   3. Render order details immediately (payment + order info).
   4. If delivery_status !== 'delivered', show license-pending spinner.
   5. Poll every 2 s until delivery_status === 'delivered' + licenseKey.
   6. On delivery: replace spinner with key + copy/download buttons.
   7. Stop polling after successful delivery.
   ============================================================ */

(function () {
  'use strict';

  /* ── URL params ───────────────────────────────────────────────────────────── */
  const _params  = new URLSearchParams(window.location.search);
  const _orderId = (_params.get('order') || '').trim();
  const _token   = (_params.get('token') || '').trim();

  /* ── Polling state ────────────────────────────────────────────────────────── */
  let _pollTimer      = null;
  let _pollCount      = 0;
  const MAX_POLL      = 150;   // ~5 min at 2 s intervals — kept for long-tail
  const FALLBACK_POLL = 3;     // after 3 polls (~6 s) show fallback UI if still pending

  /* ── Helpers ─────────────────────────────────────────────────────────────── */
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
    if (el) el.textContent = text || '—';
  }

  function _formatDate (iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { return iso; }
  }

  function _buildApiUrl (orderId) {
    const base = `/api/order/${encodeURIComponent(orderId)}`;
    return _token ? `${base}?token=${encodeURIComponent(_token)}` : base;
  }

  /* ── Error display ───────────────────────────────────────────────────────── */
  function _showError (title, msg, orderNote) {
    _hide('os-loading');
    _hide('os-content');
    _setText('os-error-title', title);
    _setText('os-error-msg', msg);
    if (orderNote) _setText('os-error-order-note', 'Order ID: ' + orderNote);
    _show('os-error');
  }

  /* ── Plan label helper ───────────────────────────────────────────────────── */
  const _PLAN_LABELS = { pro: 'Ghost Pro (monthly)', lifetime: 'Ghost Lifetime', trial: 'Ghost Trial (free)' };
  function _planLabel (planId, storedLabel) {
    if (storedLabel) return storedLabel;
    return _PLAN_LABELS[(planId || '').toLowerCase()] || planId || '—';
  }

  function _deriveInvoiceId (captureId) {
    if (!captureId) return '—';
    const raw  = captureId.replace(/[^A-Z0-9]/gi, '').toUpperCase();
    const sufx = raw.slice(-8).padStart(8, '0');
    return `GHOST-INV-${sufx}`;
  }

  /* ── Render order details (always called immediately) ────────────────────── */
  function _renderOrderDetails (order) {
    const orderId   = order.order_id   || _orderId;
    const captureId = order.paypal_capture_id || order.captureId || orderId;
    const invoiceId = order.invoice_id || _deriveInvoiceId(captureId);
    const planLabel = _planLabel(order.plan, order.plan_label);
    const amountRaw = order.price_usd != null ? Number(order.price_usd) : null;
    const amountStr = amountRaw != null ? `USD ${amountRaw.toFixed(2)}` : '—';
    const dateStr   = _formatDate(order.created_at);
    const payStatus = (order.payment_status || 'completed').replace(/^\w/, c => c.toUpperCase());

    _setText('os-order-id',   orderId);
    _setText('os-invoice-id', invoiceId);
    _setText('os-capture-id', captureId);
    _setText('os-plan',       planLabel);
    _setText('os-amount',     amountStr);
    _setText('os-date',       dateStr);
    _setText('os-pay-status', payStatus);
    _setText('os-inv-id-badge', invoiceId);

    _wireDownloadButton();
    _wireInvoiceButtons(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus });
  }

  /* ── Show license key (called once delivery_status = delivered) ──────────── */
  function _renderLicenseDelivered (licenseKey) {
    // Stop any outstanding poll first
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }

    // Hide all pending/fallback states, show key section
    _hide('os-license-pending');
    _hide('os-license-fallback');
    _show('os-license-delivered');

    _setText('os-license-key', licenseKey);
    _wireCopyButtons(licenseKey);

    // Enable the inline copy+download action row
    _show('os-actions');
  }

  /* ── Show license pending spinner ───────────────────────────────────────── */
  function _renderLicensePending () {
    _hide('os-license-delivered');
    _hide('os-license-fallback');
    _show('os-license-pending');
    _hide('os-actions');
  }

  /* ── Show fallback message after 5 s with no key ────────────────────────── */
  function _renderLicenseFallback () {
    _hide('os-license-pending');
    _hide('os-license-delivered');
    _show('os-license-fallback');
    _hide('os-actions');
    // Wire the refresh button
    const btn = document.getElementById('os-refresh-license-btn');
    if (btn) {
      const fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', async () => {
        fresh.disabled = true;
        fresh.textContent = 'Checking…';
        try {
          const r    = await fetch(_buildApiUrl(_orderId));
          const data = await r.json().catch(() => ({}));
          if (r.ok && data.ok) {
            const licenseKey = data.license_key || data.key || null;
            if (licenseKey && data.delivery_status === 'delivered') {
              _renderLicenseDelivered(licenseKey);
              return;
            }
          }
        } catch (_) {}
        // Not yet — re-show fallback and reset button
        fresh.disabled = false;
        fresh.textContent = 'Refresh License';
      });
    }
  }

  /* ── Full render on first load ───────────────────────────────────────────── */
  function _render (order) {
    _renderOrderDetails(order);

    const licenseKey = order.license_key || order.key || null;

    if (licenseKey && order.delivery_status === 'delivered') {
      _renderLicenseDelivered(licenseKey);
    } else {
      _renderLicensePending();
      // Start polling for the key
      _schedulePoll();
    }

    // Populate invoice body now that we have the data
    const orderId   = order.order_id   || _orderId;
    const captureId = order.paypal_capture_id || order.captureId || orderId;
    const invoiceId = order.invoice_id || _deriveInvoiceId(captureId);
    const planLabel = _planLabel(order.plan, order.plan_label);
    const amountRaw = order.price_usd != null ? Number(order.price_usd) : null;
    const amountStr = amountRaw != null ? `USD ${amountRaw.toFixed(2)}` : '—';
    const dateStr   = _formatDate(order.created_at);
    const payStatus = (order.payment_status || 'completed').replace(/^\w/, c => c.toUpperCase());
    _renderInvoice(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });

    _hide('os-loading');
    _show('os-content');
  }

  /* ── Render invoice table ────────────────────────────────────────────────── */
  function _renderInvoice (order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey }) {
    const email   = order.email   || '—';
    const discord = order.discord || '—';
    const rows = [
      ['Invoice ID',       invoiceId,  true],
      ['Order ID',         orderId,    true],
      ['Customer Email',   email,      false],
      ['Discord Username', discord,    false],
      ['Plan',             planLabel,  false],
      ['Amount Paid',      amountStr,  false],
      ['Purchase Date',    dateStr,    false],
      ['Payment Status',   payStatus,  false],
      ['License Key',      licenseKey || '—', true],
    ];

    const body = document.getElementById('os-invoice-body');
    if (!body) return;
    body.innerHTML = rows.map(([label, value, mono]) =>
      `<div class="os-invoice-row">
         <label>${_escHtml(label)}</label>
         <span class="${mono ? 'mono' : ''}">${_escHtml(String(value))}</span>
       </div>`
    ).join('');
  }

  function _escHtml (str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ── Copy license button wiring ─────────────────────────────────────────── */
  function _wireCopyButtons (licenseKey) {
    ['os-copy-btn', 'os-copy-btn-2'].forEach(id => {
      const btn = document.getElementById(id);
      if (!btn) return;
      // Remove any old listeners by cloning
      const fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(licenseKey);
          _showCopied();
        } catch (_) {
          const ta = document.createElement('textarea');
          ta.value = licenseKey;
          ta.style.position = 'fixed';
          ta.style.opacity  = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
          _showCopied();
        }
      });
    });
  }

  function _showCopied () {
    const confirm = document.getElementById('os-copy-confirm');
    if (!confirm) return;
    confirm.hidden = false;
    setTimeout(() => { confirm.hidden = true; }, 2500);
  }

  /* ── Download button ─────────────────────────────────────────────────────── */
  async function _wireDownloadButton () {
    const btn = document.getElementById('os-download-btn');
    if (!btn) return;

    let downloadUrl = '/dl/GhostConfig.exe';
    try {
      const r = await fetch('/api/download/current');
      const d = await r.json().catch(() => ({}));
      if (d.ok && d.url) downloadUrl = d.url;
    } catch (_) { /* use fallback */ }

    const fresh = btn.cloneNode(true);
    btn.parentNode.replaceChild(fresh, btn);
    fresh.addEventListener('click', () => {
      const a = document.createElement('a');
      a.href     = downloadUrl;
      a.download = 'GhostConfig.exe';
      a.click();
    });
    fresh.disabled = false;
  }

  /* ── Invoice action buttons ─────────────────────────────────────────────── */
  function _wireInvoiceButtons (order, invoiceData) {
    document.getElementById('os-view-receipt-btn')?.addEventListener('click', () => {
      const el = document.getElementById('os-invoice');
      if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });
    document.getElementById('os-download-invoice-btn')?.addEventListener('click', () => {
      _downloadInvoiceText(order, invoiceData);
    });
  }

  function _downloadInvoiceText (order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus }) {
    const email      = order.email   || '—';
    const discord    = order.discord || '—';
    const licenseKey = order.license_key || order.key || '—';

    const lines = [
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      '                  GHOST — INVOICE',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      '',
      `Invoice ID       : ${invoiceId}`,
      `Order ID         : ${orderId}`,
      `PayPal Capture ID: ${captureId}`,
      '',
      `Customer Email   : ${email}`,
      `Discord Username : ${discord}`,
      '',
      `Plan             : ${planLabel}`,
      `Amount Paid      : ${amountStr}`,
      `Purchase Date    : ${dateStr}`,
      `Payment Status   : ${payStatus}`,
      '',
      '── License ──────────────────────────────────────',
      `License Key      : ${licenseKey}`,
      '',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      'Ghost — Windows QA Environment Configuration Utility',
      'Support: https://discord.gg/your-invite',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
    ];

    const captureId2 = invoiceData.captureId;
    const invoiceId2 = invoiceData.invoiceId;
    const text = lines.join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `ghost-invoice-${invoiceId2 || invoiceId}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  /* ── Polling loop — only for license key, not for page display ───────────── */
  function _schedulePoll () {
    if (_pollTimer) clearTimeout(_pollTimer);
    _pollTimer = setTimeout(_poll, 2000);
  }

  async function _poll () {
    _pollCount++;
    try {
      const r    = await fetch(_buildApiUrl(_orderId));
      const data = await r.json().catch(() => ({}));

      if (r.ok && data.ok) {
        const licenseKey  = data.license_key || data.key || null;
        const isDelivered = data.delivery_status === 'delivered';

        if (isDelivered && licenseKey) {
          // Update invoice body with the key now that it's available
          const orderId   = data.order_id   || _orderId;
          const captureId = data.paypal_capture_id || data.captureId || orderId;
          const invoiceId = data.invoice_id || _deriveInvoiceId(captureId);
          const planLabel = _planLabel(data.plan, data.plan_label);
          const amountRaw = data.price_usd != null ? Number(data.price_usd) : null;
          const amountStr = amountRaw != null ? `USD ${amountRaw.toFixed(2)}` : '—';
          const dateStr   = _formatDate(data.created_at);
          const payStatus = (data.payment_status || 'completed').replace(/^\w/, c => c.toUpperCase());
          _renderInvoice(data, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });

          _renderLicenseDelivered(licenseKey);
          return; // Stop polling — timer cleared inside _renderLicenseDelivered
        }
      }

      // Expired token — stop polling, show error
      if (r.status === 403) {
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
        _hide('os-license-pending');
        _hide('os-license-fallback');
        _show('os-license-error');
        _setText('os-license-error-msg', 'Your access link has expired. Please contact support with your Order ID.');
        return;
      }
    } catch (_) {
      // Network error — keep trying
    }

    if (_pollCount < MAX_POLL) {
      // After ~5 seconds (FALLBACK_POLL polls × 2 s), replace the spinner with
      // a calm fallback message + [Refresh License] button.  Polling continues
      // in the background every 2 s so the key still appears automatically.
      if (_pollCount === FALLBACK_POLL) {
        _renderLicenseFallback();
      }
      _schedulePoll();
    } else {
      // Hard timeout — show fallback if not already shown
      if (document.getElementById('os-license-fallback')?.hidden !== false) {
        _renderLicenseFallback();
      }
    }
  }

  /* ── Initial fetch ───────────────────────────────────────────────────────── */
  async function _initialFetch () {
    let attempts = 0;
    const MAX_INIT = 5;

    const tryFetch = async () => {
      attempts++;
      try {
        const r    = await fetch(_buildApiUrl(_orderId));
        const data = await r.json().catch(() => ({}));

        if (r.ok && data.ok) {
          _render(data);
          return;
        }

        if (r.status === 403) {
          _showError(
            'Access link expired',
            'Your order access link has expired. Please contact support with your Order ID.',
            _orderId
          );
          return;
        }

        if (r.status === 404 || !data.ok) {
          if (attempts < MAX_INIT) {
            // Order may not be written yet — retry briefly
            setTimeout(tryFetch, 1500);
            return;
          }
          _showError('Order not found', 'We could not find your order. Please contact support.', _orderId);
          return;
        }

        _showError('Load failed', 'Could not load your order. Please refresh the page.', _orderId);
      } catch (_) {
        if (attempts < MAX_INIT) {
          setTimeout(tryFetch, 1500);
          return;
        }
        _showError('Network error', 'Could not reach the server. Please refresh the page.', _orderId);
      }
    };

    tryFetch();
  }

  /* ── Init ────────────────────────────────────────────────────────────────── */
  (function init () {
    if (!_orderId) {
      _showError(
        'No order specified',
        'This page requires an order ID. Please return to checkout.',
        null
      );
      return;
    }
    _initialFetch();
  })();

})();
