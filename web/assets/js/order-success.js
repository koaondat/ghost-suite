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
   3. If delivery_status !== 'delivered', poll every 1.5 s until it is.
   4. Render all fields from the fetched order record.
   5. Wire Copy License, Download GhostConfig.exe, invoice buttons.
   ============================================================ */

(function () {
  'use strict';

  /* ── URL params ───────────────────────────────────────────────────────────── */
  const _params  = new URLSearchParams(window.location.search);
  const _orderId = (_params.get('order') || '').trim();
  const _token   = (_params.get('token') || '').trim();

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

  /* ── Render the full order page ──────────────────────────────────────────── */
  function _render (order) {
    const licenseKey = order.license_key || order.key || null;
    const orderId    = order.order_id   || _orderId;
    const captureId  = order.paypal_capture_id || order.captureId || orderId;
    const invoiceId  = order.invoice_id || _deriveInvoiceId(captureId);
    const planLabel  = _planLabel(order.plan, order.plan_label);
    const amountRaw  = order.price_usd != null ? Number(order.price_usd) : null;
    const amountStr  = amountRaw != null ? `USD ${amountRaw.toFixed(2)}` : '—';
    const dateStr    = _formatDate(order.created_at);
    const payStatus  = (order.payment_status || 'completed').replace(/^\w/, c => c.toUpperCase());

    // ── Main fields ──
    _setText('os-order-id',   orderId);
    _setText('os-invoice-id', invoiceId);
    _setText('os-capture-id', captureId);
    _setText('os-plan',       planLabel);
    _setText('os-amount',     amountStr);
    _setText('os-date',       dateStr);
    _setText('os-pay-status', payStatus);

    // ── License key ──
    if (licenseKey) {
      _setText('os-license-key', licenseKey);
      _wireCopyButtons(licenseKey);
    } else {
      _setText('os-license-key', 'Key pending — refresh this page in a moment.');
    }

    // ── Invoice badge ──
    _setText('os-inv-id-badge', invoiceId);

    // ── Populate invoice body ──
    _renderInvoice(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });

    // ── Download button ──
    _wireDownloadButton();

    // ── Invoice action buttons ──
    _wireInvoiceButtons(order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey });

    // ── Show the content ──
    _hide('os-loading');
    _show('os-content');
  }

  function _deriveInvoiceId (captureId) {
    if (!captureId) return '—';
    const raw  = captureId.replace(/[^A-Z0-9]/gi, '').toUpperCase();
    const sufx = raw.slice(-8).padStart(8, '0');
    return `GHOST-INV-${sufx}`;
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
      btn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(licenseKey);
          _showCopied();
        } catch (_) {
          // Fallback for older browsers
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

    // Fetch current download URL from the backend (respects admin-configured URL)
    let downloadUrl = '/dl/GhostConfig.exe';
    try {
      const r = await fetch('/api/download/current');
      const d = await r.json().catch(() => ({}));
      if (d.ok && d.url) downloadUrl = d.url;
    } catch (_) { /* use fallback */ }

    btn.addEventListener('click', () => {
      const a = document.createElement('a');
      a.href     = downloadUrl;
      a.download = 'GhostConfig.exe';
      a.click();
    });

    btn.disabled = false;
  }

  /* ── Invoice action buttons ─────────────────────────────────────────────── */
  function _wireInvoiceButtons (order, invoiceData) {
    // View Receipt — scroll to invoice section and highlight it
    document.getElementById('os-view-receipt-btn')?.addEventListener('click', () => {
      const el = document.getElementById('os-invoice');
      if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });

    // Download Invoice — generate a plain-text invoice and trigger download
    document.getElementById('os-download-invoice-btn')?.addEventListener('click', () => {
      _downloadInvoiceText(order, invoiceData);
    });
  }

  function _downloadInvoiceText (order, { orderId, invoiceId, captureId, planLabel, amountStr, dateStr, payStatus, licenseKey }) {
    const email   = order.email   || '—';
    const discord = order.discord || '—';

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
      `License Key      : ${licenseKey || '—'}`,
      '',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      'Ghost — Windows QA Environment Configuration Utility',
      'Support: https://discord.gg/your-invite',
      '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
    ];

    const text = lines.join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `ghost-invoice-${invoiceId}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  /* ── Polling loop ────────────────────────────────────────────────────────── */
  let _pollCount = 0;
  const MAX_POLL  = 40;  // ~60 s

  async function _poll () {
    _pollCount++;
    try {
      const r    = await fetch(_buildApiUrl(_orderId));
      const data = await r.json().catch(() => ({}));

      if (!r.ok || !data.ok) {
        // Still 403 (no token / expired) — show partial info
        if (r.status === 403) {
          _showError(
            'Access link expired',
            'Your order access link has expired. Please contact support with your Order ID.',
            _orderId
          );
          return;
        }
        if (r.status === 404) {
          if (_pollCount < 5) {
            // Order may not be saved yet — keep polling briefly
            setTimeout(_poll, 1500);
            return;
          }
          _showError('Order not found', 'We could not find your order. Please contact support.', _orderId);
          return;
        }
        if (_pollCount < MAX_POLL) { setTimeout(_poll, 1500); return; }
        _showError('Load failed', 'Could not load your order. Please refresh the page.', _orderId);
        return;
      }

      // If delivered, render it
      if (data.delivery_status === 'delivered') {
        _render(data);
        return;
      }

      // Keep polling if still pending
      if (_pollCount < MAX_POLL) {
        setTimeout(_poll, 1500);
        return;
      }

      // Timed out — render what we have (may have no license key yet)
      _render(data);
    } catch (err) {
      if (_pollCount < MAX_POLL) { setTimeout(_poll, 1500); return; }
      _showError('Network error', 'Could not reach the server. Please refresh the page.', _orderId);
    }
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
    _poll();
  })();

})();
