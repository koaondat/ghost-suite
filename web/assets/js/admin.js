/**
 * admin.js — Ghost Admin Panel
 * ============================
 * Handles login, tab navigation, inventory management,
 * key import/delete, order listing, and order actions.
 *
 * All API calls go through /api/admin/* which is proxied to the
 * Python backend (api.py) that requires X-Admin-Key authentication.
 * The admin panel itself is protected by ADMIN_PANEL_PASSWORD set on
 * the server (validated by the /api/admin/panel/auth endpoint).
 */

'use strict';

(function () {

  /* ── State ─────────────────────────────────────────────────────────── */
  let _token      = sessionStorage.getItem('ghost_admin_panel_token') || '';
  let _invPage    = 1;
  let _ordPage    = 1;
  const PAGE_SIZE = 50;

  let _allInventory = [];
  let _allOrders    = [];

  /* ── DOM helpers ───────────────────────────────────────────────────── */
  const $  = id => document.getElementById(id);
  const esc = s  => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  function setText (id, v)  { const el = $(id); if (el) el.textContent = v ?? '—'; }
  function show (id)        { const el = $(id); if (el) el.style.display = ''; }
  function hide (id)        { const el = $(id); if (el) el.style.display = 'none'; }
  function setBusy (btnId, busy) {
    const btn  = $(btnId);
    if (!btn) return;
    const text = btn.querySelector('.btn-text');
    const spin = btn.querySelector('.btn-spinner');
    btn.disabled = busy;
    if (text) text.style.display = busy ? 'none' : '';
    if (spin) spin.style.display = busy ? '' : 'none';
  }

  /* ── Authenticated fetch ───────────────────────────────────────────── */
  async function apiFetch (path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (_token) headers['X-Admin-Panel-Token'] = _token;
    const res  = await fetch(path, { ...opts, headers });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  /* ── Alert banner ──────────────────────────────────────────────────── */
  function showAlert (id, type, msg) {
    const el = $(id);
    if (!el) return;
    el.className = `admin-alert admin-alert--${type}`;
    el.textContent = msg;
    el.style.display = '';
  }
  function hideAlert (id) {
    const el = $(id);
    if (el) el.style.display = 'none';
  }

  /* ── Login ─────────────────────────────────────────────────────────── */
  async function doLogin (e) {
    e.preventDefault();
    const pw = $('adminPassword').value.trim();
    if (!pw) return;
    setBusy('loginBtn', true);
    hideAlert('loginAlert');

    try {
      const { ok, data } = await apiFetch('/api/admin/panel/auth', {
        method: 'POST',
        body: JSON.stringify({ password: pw }),
      });
      if (ok && data.token) {
        _token = data.token;
        sessionStorage.setItem('ghost_admin_panel_token', _token);
        hide('adminLogin');
        show('adminShell');
        loadDashboard();
      } else {
        showAlert('loginAlert', 'error', data.error || 'Invalid password.');
      }
    } catch (_) {
      showAlert('loginAlert', 'error', 'Network error. Please try again.');
    } finally {
      setBusy('loginBtn', false);
    }
  }

  function doLogout () {
    _token = '';
    sessionStorage.removeItem('ghost_admin_panel_token');
    hide('adminShell');
    show('adminLogin');
    $('adminPassword').value = '';
  }

  /* ── Tab navigation ─────────────────────────────────────────────────── */
  function switchTab (name) {
    document.querySelectorAll('.admin-tab').forEach(s => s.style.display = 'none');
    document.querySelectorAll('.admin-nav-link').forEach(a => a.classList.remove('active'));
    const tab = $(`tab-${name}`);
    if (tab) tab.style.display = '';
    document.querySelectorAll(`.admin-nav-link[data-tab="${name}"]`)
      .forEach(a => a.classList.add('active'));

    if (name === 'dashboard')  loadDashboard();
    if (name === 'inventory')  loadInventory();
    if (name === 'orders')     loadOrders();
  }

  /* ── Dashboard ─────────────────────────────────────────────────────── */
  async function loadDashboard () {
    try {
      const [invRes, ordRes] = await Promise.all([
        apiFetch('/api/admin/inventory/stats'),
        apiFetch('/api/admin/orders'),
      ]);

      if (invRes.ok) {
        const s = invRes.data;
        setText('statTotalKeys',  s.total    ?? 0);
        setText('statUnusedKeys', s.unused   ?? 0);
        setText('statSoldKeys',   s.assigned ?? 0);
        setText('statRevokedKeys',s.revoked  ?? 0);
      }

      if (ordRes.ok) {
        const orders = ordRes.data.orders || [];
        setText('statTotalOrders', orders.length);
        const rev = orders.reduce((acc, o) => acc + (parseFloat(o.price_usd) || 0), 0);
        setText('statRevenue', `$${rev.toFixed(2)}`);
        renderRecentOrders(orders.slice(-10).reverse());
      }
    } catch (_) {
      setText('statTotalKeys', 'err');
    }
  }

  function renderRecentOrders (orders) {
    const tb = $('recentOrdersTbody');
    if (!orders.length) {
      tb.innerHTML = '<tr><td colspan="7" class="admin-table-empty">No orders yet.</td></tr>';
      return;
    }
    tb.innerHTML = orders.map(o => `
      <tr>
        <td><span class="key-mono key-mono--sm" style="cursor:pointer" onclick="navigator.clipboard.writeText('${esc(o.order_id)}')">${esc(o.order_id.slice(0,16))}…</span></td>
        <td>${esc(o.email || o.discord || '—')}</td>
        <td>${planBadge(o.plan)}</td>
        <td>$${parseFloat(o.price_usd || 0).toFixed(2)}</td>
        <td>${deliveryBadge(o.delivery_status)}</td>
        <td>${fmtDate(o.created_at)}</td>
        <td>${o.license_key ? `<span class="key-mono key-mono--sm">${esc(o.license_key.slice(0,16))}…</span>` : '<span class="badge badge--muted">—</span>'}</td>
      </tr>
    `).join('');
  }

  /* ── Key Inventory ─────────────────────────────────────────────────── */
  async function loadInventory (filter = {}) {
    const tb = $('inventoryTbody');
    tb.innerHTML = '<tr><td colspan="8" class="admin-table-empty">Loading…</td></tr>';
    try {
      const params = new URLSearchParams();
      if (filter.status) params.set('status', filter.status);
      if (filter.plan)   params.set('plan',   filter.plan);
      if (filter.search) params.set('search', filter.search);
      const { ok, data } = await apiFetch(`/api/admin/inventory?${params}`);
      if (!ok) throw new Error(data.error || 'Failed to load inventory');
      _allInventory = data.keys || [];
      renderInventory(_allInventory, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="8" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderInventory (keys, page) {
    _invPage = page;
    const tb    = $('inventoryTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = keys.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="8" class="admin-table-empty">No keys found.</td></tr>';
      $('invPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(k => `
      <tr>
        <td><input type="checkbox" class="inv-check" data-key="${esc(k.key)}" /></td>
        <td><span class="key-mono">${esc(k.key)}</span></td>
        <td>${planBadge(k.plan)}</td>
        <td>${statusBadge(k.status)}</td>
        <td>${k.order_id ? `<span class="key-mono key-mono--sm" title="${esc(k.order_id)}">${esc(k.order_id.slice(0,14))}…</span>` : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${fmtDate(k.purchase_date)}</td>
        <td>${esc(k.customer_email || '—')}</td>
        <td>
          <button class="btn-icon" onclick="copyToClipboard('${esc(k.key)}')" title="Copy key">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Copy
          </button>
          ${k.status === 'assigned' ? `<button class="btn-icon btn-icon--danger" onclick="revokeKey('${esc(k.key)}')" title="Revoke">Revoke</button>` : ''}
          <button class="btn-icon btn-icon--danger" onclick="deleteKey('${esc(k.key)}')" title="Delete">Delete</button>
        </td>
      </tr>
    `).join('');

    renderPagination('invPagination', keys.length, page, p => renderInventory(keys, p));
    wireSelectAll();
    wireDeleteSelected();
  }

  function wireSelectAll () {
    const cb = $('selectAllKeys');
    if (!cb) return;
    cb.onchange = () => {
      document.querySelectorAll('.inv-check').forEach(c => c.checked = cb.checked);
      updateDeleteBtn();
    };
    document.querySelectorAll('.inv-check').forEach(c => {
      c.onchange = updateDeleteBtn;
    });
  }

  function updateDeleteBtn () {
    const btn   = $('deleteSelectedKeys');
    const count = document.querySelectorAll('.inv-check:checked').length;
    btn.disabled = count === 0;
    btn.textContent = count > 0 ? `Delete Selected (${count})` : 'Delete Selected';
  }

  function wireDeleteSelected () {
    const btn = $('deleteSelectedKeys');
    btn.onclick = () => {
      const keys = [...document.querySelectorAll('.inv-check:checked')].map(c => c.dataset.key);
      if (!keys.length) return;
      confirmAction(`Delete ${keys.length} key(s)?`, `This will permanently remove ${keys.length} key(s) from inventory.`, async () => {
        const { ok, data } = await apiFetch('/api/admin/inventory/bulk-delete', {
          method: 'POST',
          body: JSON.stringify({ keys }),
        });
        if (ok) {
          loadInventory(currentInvFilter());
        }
      });
    };
  }

  function currentInvFilter () {
    return {
      status: $('invFilterStatus')?.value || '',
      plan:   $('invFilterPlan')?.value   || '',
      search: $('invSearch')?.value       || '',
    };
  }

  /* ── Import modal ──────────────────────────────────────────────────── */
  function openImportModal () {
    $('importPaste').value = '';
    $('importFile').value  = '';
    hideAlert('importAlert');
    show('importModal');
  }
  function closeImportModal () { hide('importModal'); }

  async function submitImport () {
    hideAlert('importAlert');
    setBusy('submitImport', true);

    let rawText = $('importPaste').value.trim();

    // If a file was selected, read it
    const file = $('importFile').files[0];
    if (file && !rawText) {
      try {
        rawText = await file.text();
      } catch (_) {
        showAlert('importAlert', 'error', 'Could not read file.');
        setBusy('submitImport', false);
        return;
      }
    }

    if (!rawText) {
      showAlert('importAlert', 'error', 'No keys provided. Paste keys or select a file.');
      setBusy('submitImport', false);
      return;
    }

    const plan = $('importPlan').value;
    const keys = rawText.split(/\r?\n/).map(l => l.trim()).filter(Boolean);

    try {
      const { ok, data } = await apiFetch('/api/admin/inventory/import', {
        method: 'POST',
        body: JSON.stringify({ keys, plan }),
      });
      if (ok) {
        showAlert('importAlert', 'success', `Imported ${data.added} key(s). Skipped ${data.skipped} duplicate(s).`);
        loadInventory(currentInvFilter());
      } else {
        showAlert('importAlert', 'error', data.error || 'Import failed.');
      }
    } catch (_) {
      showAlert('importAlert', 'error', 'Network error during import.');
    } finally {
      setBusy('submitImport', false);
    }
  }

  /* ── Orders ─────────────────────────────────────────────────────────── */
  async function loadOrders (search = '') {
    const tb = $('ordersTbody');
    tb.innerHTML = '<tr><td colspan="9" class="admin-table-empty">Loading…</td></tr>';
    try {
      const { ok, data } = await apiFetch('/api/admin/orders');
      if (!ok) throw new Error(data.error || 'Failed to load orders');
      let orders = (data.orders || []).reverse();
      if (search) {
        const q = search.toLowerCase();
        orders = orders.filter(o =>
          (o.order_id || '').toLowerCase().includes(q) ||
          (o.email    || '').toLowerCase().includes(q) ||
          (o.discord  || '').toLowerCase().includes(q)
        );
      }
      _allOrders = orders;
      renderOrders(orders, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="9" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderOrders (orders, page) {
    _ordPage = page;
    const tb    = $('ordersTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = orders.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="9" class="admin-table-empty">No orders found.</td></tr>';
      $('orderPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(o => `
      <tr>
        <td><span class="key-mono key-mono--sm" style="cursor:pointer" onclick="copyToClipboard('${esc(o.order_id)}')" title="${esc(o.order_id)}">${esc(o.order_id.slice(0,16))}…</span></td>
        <td><span title="${esc(o.email || '')}">${esc((o.email || o.discord || '—').slice(0,24))}</span></td>
        <td>${planBadge(o.plan)}</td>
        <td>$${parseFloat(o.price_usd || 0).toFixed(2)}</td>
        <td>${paymentBadge(o.payment_status)}</td>
        <td>${deliveryBadge(o.delivery_status)}</td>
        <td>${o.license_key ? `<span class="key-mono key-mono--sm">${esc(o.license_key.slice(0,16))}…</span>` : '<span class="badge badge--muted">—</span>'}</td>
        <td>${fmtDate(o.created_at)}</td>
        <td>
          <button class="btn-icon" onclick="openOrderDetail('${esc(o.order_id)}')">View</button>
          <button class="btn-icon" onclick="copyToClipboard('${esc(o.order_id)}')">Copy ID</button>
          ${o.delivery_status !== 'delivered' ? `<button class="btn-icon" onclick="reissueOrder('${esc(o.order_id)}')">Reissue</button>` : ''}
        </td>
      </tr>
    `).join('');

    renderPagination('orderPagination', orders.length, page, p => renderOrders(orders, p));
  }

  /* ── Order detail modal ─────────────────────────────────────────────── */
  async function openOrderDetail (orderId) {
    $('orderDetailBody').innerHTML = '<p style="color:var(--muted)">Loading…</p>';
    $('reissueBtn').style.display = 'none';
    show('orderDetailModal');

    try {
      const { ok, data } = await apiFetch(`/api/admin/orders/${encodeURIComponent(orderId)}`);
      if (!ok) throw new Error(data.error || 'Order not found');
      const o = data.order || data;

      $('orderDetailBody').innerHTML = `
        <div class="order-detail-grid">
          <div class="order-detail-field">
            <span class="order-detail-label">Order ID</span>
            <span class="order-detail-value" style="cursor:pointer" onclick="copyToClipboard('${esc(o.order_id)}')">${esc(o.order_id)}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Plan</span>
            <span class="order-detail-value">${esc(o.plan_label || o.plan || '—')}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Customer Email</span>
            <span class="order-detail-value">${esc(o.email || '—')}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Discord</span>
            <span class="order-detail-value">${esc(o.discord || '—')}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Amount</span>
            <span class="order-detail-value">$${parseFloat(o.price_usd || 0).toFixed(2)} ${esc(o.currency || 'USD')}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Purchase Date</span>
            <span class="order-detail-value">${fmtDate(o.created_at)}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Payment Status</span>
            <span class="order-detail-value">${paymentBadge(o.payment_status)}</span>
          </div>
          <div class="order-detail-field">
            <span class="order-detail-label">Delivery Status</span>
            <span class="order-detail-value">${deliveryBadge(o.delivery_status)}</span>
          </div>
          <div class="order-detail-key">
            <span class="order-detail-label">License Key</span>
            <span class="order-detail-key-val">${o.license_key ? esc(o.license_key) : '<span style="color:var(--muted)">Not yet assigned</span>'}</span>
          </div>
        </div>
      `;

      if (o.delivery_status !== 'delivered') {
        const rb = $('reissueBtn');
        rb.style.display = '';
        rb.onclick = () => reissueOrder(o.order_id);
      }
    } catch (err) {
      $('orderDetailBody').innerHTML = `<p style="color:#f87171">${esc(err.message)}</p>`;
    }
  }

  async function reissueOrder (orderId) {
    try {
      const { ok, data } = await apiFetch(`/api/paypal/retry-fulfillment`, {
        method: 'POST',
        body: JSON.stringify({ captureId: orderId }),
      });
      if (ok && data.licenseKey) {
        openOrderDetail(orderId);  // refresh
        loadOrders($('orderSearch')?.value || '');
      } else {
        alert(data.message || data.error || 'Reissue failed. Check inventory stock.');
      }
    } catch (_) {
      alert('Network error during reissue.');
    }
  }

  /* ── Key actions ───────────────────────────────────────────────────── */
  function revokeKey (key) {
    confirmAction('Revoke Key', `Revoke key ${key}? It will remain in the database but marked as revoked.`, async () => {
      await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}/revoke`, { method: 'POST' });
      loadInventory(currentInvFilter());
    });
  }

  function deleteKey (key) {
    confirmAction('Delete Key', `Permanently delete key ${key}?`, async () => {
      await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, { method: 'DELETE' });
      loadInventory(currentInvFilter());
    });
  }

  /* ── Confirm modal ─────────────────────────────────────────────────── */
  let _confirmCb = null;
  function confirmAction (title, msg, cb) {
    $('confirmTitle').textContent   = title;
    $('confirmMessage').textContent = msg;
    _confirmCb = cb;
    show('confirmModal');
  }

  /* ── Pagination ─────────────────────────────────────────────────────── */
  function renderPagination (id, total, current, onPage) {
    const pages = Math.ceil(total / PAGE_SIZE);
    const el    = $(id);
    if (!el || pages <= 1) { if (el) el.innerHTML = `<span>${total} item(s)</span>`; return; }

    let html = `<span>${total} item(s) &nbsp;|&nbsp; </span>`;
    for (let i = 1; i <= pages; i++) {
      html += `<button class="btn btn-ghost btn--sm${i === current ? ' active' : ''}" onclick="(${onPage.toString()})(${i})">${i}</button> `;
    }
    el.innerHTML = html;
  }

  /* ── Badge helpers ──────────────────────────────────────────────────── */
  function statusBadge (status) {
    const map = {
      unused:   ['green',  'Unused'],
      assigned: ['purple', 'Sold'],
      revoked:  ['red',    'Revoked'],
    };
    const [cls, label] = map[status] || ['muted', status || '—'];
    return `<span class="badge badge--${cls}">${label}</span>`;
  }

  function planBadge (plan) {
    const map = {
      pro:      ['cyan',   'Pro'],
      lifetime: ['purple', 'Lifetime'],
      trial:    ['muted',  'Trial'],
    };
    const [cls, label] = map[(plan || '').toLowerCase()] || ['muted', plan || '—'];
    return `<span class="badge badge--${cls}">${label}</span>`;
  }

  function paymentBadge (status) {
    const map = {
      verified:       ['green',  'Verified'],
      payment_failed: ['red',    'Failed'],
      refunded:       ['yellow', 'Refunded'],
      cancelled:      ['red',    'Cancelled'],
      expired:        ['muted',  'Expired'],
    };
    const [cls, label] = map[status] || ['muted', status || '—'];
    return `<span class="badge badge--${cls}">${label}</span>`;
  }

  function deliveryBadge (status) {
    const map = {
      delivered:       ['green',  'Delivered'],
      out_of_stock:    ['yellow', 'Out of Stock'],
      delivery_pending:['yellow', 'Pending'],
      pending:         ['muted',  'Pending'],
    };
    const [cls, label] = map[status] || ['muted', status || '—'];
    return `<span class="badge badge--${cls}">${label}</span>`;
  }

  /* ── Date formatter ─────────────────────────────────────────────────── */
  function fmtDate (iso) {
    if (!iso) return '<span style="color:var(--muted)">—</span>';
    try {
      return new Date(iso).toLocaleString('en-US', {
        month: 'short', day: 'numeric', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { return iso; }
  }

  /* ── Clipboard ──────────────────────────────────────────────────────── */
  window.copyToClipboard = function (text) {
    navigator.clipboard.writeText(text).then(() => {
      // brief visual feedback via button
    });
  };

  /* ── Expose tab actions for onclick ────────────────────────────────── */
  window.openOrderDetail = openOrderDetail;
  window.reissueOrder    = reissueOrder;
  window.revokeKey       = revokeKey;
  window.deleteKey       = deleteKey;

  /* ── Auto-login check ───────────────────────────────────────────────── */
  async function checkExistingSession () {
    if (!_token) return;
    try {
      const { ok } = await apiFetch('/api/admin/panel/verify');
      if (ok) {
        hide('adminLogin');
        show('adminShell');
        loadDashboard();
      } else {
        _token = '';
        sessionStorage.removeItem('ghost_admin_panel_token');
      }
    } catch (_) {
      // Network error — stay on login page
    }
  }

  /* ── Wire events ─────────────────────────────────────────────────────── */
  function wireAll () {
    // Login
    $('loginForm').addEventListener('submit', doLogin);
    $('logoutBtn').addEventListener('click', doLogout);

    // Tab nav
    document.querySelectorAll('.admin-nav-link').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        switchTab(a.dataset.tab);
      });
    });

    // Dashboard refresh
    $('refreshDashboard').addEventListener('click', loadDashboard);

    // Inventory filters
    $('applyInvFilter').addEventListener('click', () => loadInventory(currentInvFilter()));
    $('invSearch').addEventListener('keypress', e => { if (e.key === 'Enter') loadInventory(currentInvFilter()); });

    // Import modal
    $('openImportModal').addEventListener('click', openImportModal);
    $('closeImportModal').addEventListener('click', closeImportModal);
    $('cancelImport').addEventListener('click', closeImportModal);
    $('submitImport').addEventListener('click', submitImport);

    // Orders
    $('refreshOrders').addEventListener('click', () => loadOrders());
    $('applyOrderFilter').addEventListener('click', () => loadOrders($('orderSearch').value));
    $('orderSearch').addEventListener('keypress', e => { if (e.key === 'Enter') loadOrders($('orderSearch').value); });

    // Order detail modal
    $('closeOrderDetail').addEventListener('click', () => hide('orderDetailModal'));
    $('closeOrderDetailBtn').addEventListener('click', () => hide('orderDetailModal'));

    // Confirm modal
    $('closeConfirm').addEventListener('click', () => hide('confirmModal'));
    $('confirmCancel').addEventListener('click', () => hide('confirmModal'));
    $('confirmOk').addEventListener('click', async () => {
      hide('confirmModal');
      if (_confirmCb) { await _confirmCb(); _confirmCb = null; }
    });

    // Close modals on overlay click
    $('importModal').addEventListener('click', e => { if (e.target === $('importModal')) closeImportModal(); });
    $('orderDetailModal').addEventListener('click', e => { if (e.target === $('orderDetailModal')) hide('orderDetailModal'); });
    $('confirmModal').addEventListener('click', e => { if (e.target === $('confirmModal')) hide('confirmModal'); });
  }

  /* ── Init ───────────────────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    wireAll();
    checkExistingSession();
  });

}());
