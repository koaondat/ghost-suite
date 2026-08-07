/**
 * admin.js — Ghost Admin Panel (Complete)
 * ========================================
 * Handles login, tab navigation, inventory, orders, customers,
 * downloads, activity log, settings, and dashboard.
 */

'use strict';

(function () {

  /* ── State ─────────────────────────────────────────────────────────── */
  let _token      = sessionStorage.getItem('ghost_admin_panel_token') || '';
  let _invPage    = 1;
  let _ordPage    = 1;
  let _custPage   = 1;
  let _actPage    = 1;
  const PAGE_SIZE = 50;

  let _allInventory  = [];
  let _allOrders     = [];
  let _allCustomers  = [];
  let _allActivity   = [];
  let _dashData      = null;
  let _extendKeyTarget = '';
  let _chartInstance = null;
  let _graphData     = null;

  /* ── DOM helpers ───────────────────────────────────────────────────── */
  const $   = id => document.getElementById(id);
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

  /* ── Toast notifications ───────────────────────────────────────────── */
  function toast (msg, type = 'success', dur = 3500) {
    const c   = $('toastContainer');
    if (!c) return;
    const el  = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.textContent = msg;
    c.appendChild(el);
    requestAnimationFrame(() => el.classList.add('toast--in'));
    setTimeout(() => {
      el.classList.remove('toast--in');
      el.addEventListener('transitionend', () => el.remove(), { once: true });
    }, dur);
  }

  /* ── Authenticated fetch with retry ───────────────────────────────── */
  async function apiFetch (path, opts = {}, retries = 2) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (_token) headers['X-Admin-Panel-Token'] = _token;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const res  = await fetch(path, { ...opts, headers });
        // If the session expired, clear token and redirect to login
        if (res.status === 401) {
          const data = await res.json().catch(() => ({}));
          if (_token && (data.error || '').toLowerCase().includes('session')) {
            _token = '';
            sessionStorage.removeItem('ghost_admin_panel_token');
            hide('adminShell');
            show('adminLogin');
            toast('Session expired. Please log in again.', 'error');
          }
          return { ok: false, status: 401, data };
        }
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
      } catch (networkErr) {
        if (attempt < retries) {
          await new Promise(r => setTimeout(r, 600 * (attempt + 1)));
        } else {
          throw networkErr;
        }
      }
    }
  }

  /* ── Alert banner ──────────────────────────────────────────────────── */
  function showAlert (id, type, msg) {
    const el = $(id);
    if (!el) return;
    el.className = `admin-alert admin-alert--${type}`;
    el.textContent = msg;
    el.style.display = '';
    setTimeout(() => { if (el) el.style.display = 'none'; }, 6000);
  }
  function hideAlert (id) { const el = $(id); if (el) el.style.display = 'none'; }

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
  window.switchTab = function (name) {
    document.querySelectorAll('.admin-tab').forEach(s => s.style.display = 'none');
    document.querySelectorAll('.admin-nav-link').forEach(a => a.classList.remove('active'));
    const tab = $(`tab-${name}`);
    if (tab) tab.style.display = '';
    document.querySelectorAll(`.admin-nav-link[data-tab="${name}"]`)
      .forEach(a => a.classList.add('active'));
    if (name === 'dashboard')  loadDashboard();
    if (name === 'inventory')  loadInventory();
    if (name === 'orders')     loadOrders();
    if (name === 'customers')  loadCustomers();
    if (name === 'downloads')  loadDownloads();
    if (name === 'activity')   loadActivity();
    if (name === 'settings')   loadSettings();
  };

  /* ── Dashboard ─────────────────────────────────────────────────────── */
  async function loadDashboard () {
    try {
      const { ok, data } = await apiFetch('/api/admin/dashboard');
      if (!ok) {
        toast(data.error || 'Dashboard failed to load. Check server logs.', 'error');
        return;
      }
      _dashData = data;
      const c = data.cards || {};
      setText('statRevToday',   `$${(c.revenue_today  || 0).toFixed(2)}`);
      setText('statRevMonth',   `$${(c.revenue_month  || 0).toFixed(2)}`);
      setText('statRevTotal',   `$${(c.revenue_total  || 0).toFixed(2)}`);
      setText('statTotalOrders', c.total_orders   || 0);
      setText('statActiveLic',   c.active_licenses|| 0);
      setText('statKeysLeft',    c.keys_remaining || 0);
      setText('statPending',     c.pending_orders || 0);
      setText('statFailed',      c.failed_payments|| 0);

      _graphData = data.graphs;
      renderChart('revenue');

      // Load recent orders for the table
      const ordRes = await apiFetch('/api/admin/orders');
      if (ordRes.ok) {
        renderRecentOrders((ordRes.data.orders || []).slice(-10).reverse());
      }
    } catch (err) {
      toast(`Dashboard error: ${err.message || 'Network failure'}`, 'error');
    }
  }

  function renderChart (type) {
    if (!_graphData) return;
    const canvas = $('dashChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const labels = _graphData.labels || [];
    const values = _graphData[type] || [];
    const shortLabels = labels.map(d => d.slice(5));  // MM-DD

    if (_chartInstance) { _chartInstance = null; }

    // Simple hand-drawn chart (no external lib needed)
    const W = canvas.clientWidth || 700;
    const H = canvas.clientHeight || 200;
    canvas.width  = W;
    canvas.height = H;

    const maxVal = Math.max(...values, 1);
    const pad    = { top: 20, right: 16, bottom: 40, left: 50 };
    const plotW  = W - pad.left - pad.right;
    const plotH  = H - pad.top  - pad.bottom;
    const step   = plotW / Math.max(labels.length - 1, 1);

    ctx.clearRect(0, 0, W, H);

    // Grid lines
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.lineWidth   = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (plotH / 4) * i;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    }

    // Y axis labels
    ctx.fillStyle  = '#57606a';
    ctx.font       = '11px Inter, sans-serif';
    ctx.textAlign  = 'right';
    for (let i = 0; i <= 4; i++) {
      const v = maxVal * (1 - i / 4);
      const y = pad.top + (plotH / 4) * i;
      const lbl = type === 'revenue' ? `$${v >= 1000 ? (v/1000).toFixed(1)+'k' : v.toFixed(0)}` : String(Math.round(v));
      ctx.fillText(lbl, pad.left - 6, y + 4);
    }

    // Line fill gradient
    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    const colors = { revenue: ['rgba(168,85,247,0.35)','rgba(168,85,247,0)'], orders: ['rgba(34,211,238,0.3)','rgba(34,211,238,0)'], customers: ['rgba(34,197,94,0.3)','rgba(34,197,94,0)'] };
    const lineC  = { revenue: '#a855f7', orders: '#22d3ee', customers: '#22c55e' };
    grad.addColorStop(0, (colors[type] || colors.revenue)[0]);
    grad.addColorStop(1, (colors[type] || colors.revenue)[1]);

    const points = values.map((v, i) => ({
      x: pad.left + i * step,
      y: pad.top  + plotH * (1 - v / maxVal),
    }));

    // Fill area
    ctx.beginPath();
    ctx.moveTo(points[0].x, pad.top + plotH);
    points.forEach(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(points[points.length-1].x, pad.top + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.strokeStyle = lineC[type] || '#a855f7';
    ctx.lineWidth   = 2.5;
    ctx.lineJoin    = 'round';
    points.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    ctx.stroke();

    // Dots
    ctx.fillStyle = lineC[type] || '#a855f7';
    points.forEach(p => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });

    // X labels (every 5th)
    ctx.fillStyle  = '#57606a';
    ctx.textAlign  = 'center';
    shortLabels.forEach((lbl, i) => {
      if (i % 5 === 0 || i === shortLabels.length - 1) {
        ctx.fillText(lbl, pad.left + i * step, H - 10);
      }
    });
  }

  function renderRecentOrders (orders) {
    const tb = $('recentOrdersTbody');
    if (!orders.length) {
      tb.innerHTML = '<tr><td colspan="7" class="admin-table-empty">No orders yet.</td></tr>';
      return;
    }
    tb.innerHTML = orders.map(o => `
      <tr>
        <td><span class="key-mono key-mono--sm copy-cell" onclick="copyText('${esc(o.order_id)}')">${esc((o.order_id||'').slice(0,16))}…</span></td>
        <td>${esc(o.email || o.discord || '—')}</td>
        <td>${planBadge(o.plan)}</td>
        <td>$${parseFloat(o.price_usd || 0).toFixed(2)}</td>
        <td>${deliveryBadge(o.delivery_status)}</td>
        <td>${fmtDate(o.created_at)}</td>
        <td>${o.license_key ? `<span class="key-mono key-mono--sm">${esc((o.license_key||'').slice(0,16))}…</span>` : '<span class="badge badge--muted">—</span>'}</td>
      </tr>
    `).join('');
  }

  /* ── Key Inventory ─────────────────────────────────────────────────── */
  async function loadInventory (filter = {}) {
    const tb = $('inventoryTbody');
    tb.innerHTML = '<tr><td colspan="11" class="admin-table-empty skeleton-row">Loading…</td></tr>';
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
      tb.innerHTML = `<tr><td colspan="11" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderInventory (keys, page) {
    _invPage = page;
    const tb    = $('inventoryTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = keys.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="11" class="admin-table-empty">No keys found.</td></tr>';
      $('invPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(k => `
      <tr>
        <td><input type="checkbox" class="inv-check" data-key="${esc(k.key)}" /></td>
        <td><span class="key-mono copy-cell" onclick="copyText('${esc(k.key)}')" title="Click to copy">${esc(k.key)}</span></td>
        <td>${planBadge(k.plan)}</td>
        <td>${statusBadge(k.status)}</td>
        <td class="cell-truncate">${esc(k.customer || '—')}</td>
        <td>${fmtDateShort(k.purchase_date)}</td>
        <td class="cell-mono-sm">${k.hwid ? `<span title="${esc(k.hwid)}">${esc(k.hwid.slice(0,12))}…</span>` : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${fmtDateShort(k.created_date || k.added_at)}</td>
        <td>${k.expiration ? fmtDateShort(k.expiration) : '<span style="color:var(--muted)">—</span>'}</td>
        <td class="cell-truncate" title="${esc(k.notes || '')}">${esc((k.notes||'').slice(0,20))||'<span style="color:var(--muted)">—</span>'}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="copyText('${esc(k.key)}')" title="Copy">Copy</button>
          <button class="btn-icon" onclick="openEditKey(${JSON.stringify(JSON.stringify(k))})" title="Edit">Edit</button>
          <button class="btn-icon" onclick="openExtendKey('${esc(k.key)}')" title="Extend">Extend</button>
          ${k.status !== 'revoked' ? `<button class="btn-icon btn-icon--danger" onclick="revokeKey('${esc(k.key)}')" title="Revoke">Revoke</button>` : ''}
          <button class="btn-icon btn-icon--danger" onclick="deleteKey('${esc(k.key)}')" title="Delete">Delete</button>
        </td>
      </tr>
    `).join('');

    renderPagination('invPagination', keys.length, page, p => renderInventory(keys, p));
    wireSelectAll();
  }

  function wireSelectAll () {
    const cb = $('selectAllKeys');
    if (!cb) return;
    cb.onchange = () => {
      document.querySelectorAll('.inv-check').forEach(c => c.checked = cb.checked);
      updateDeleteBtn();
    };
    document.querySelectorAll('.inv-check').forEach(c => { c.onchange = updateDeleteBtn; });
  }

  function updateDeleteBtn () {
    const btn   = $('deleteSelectedKeys');
    const count = document.querySelectorAll('.inv-check:checked').length;
    btn.disabled = count === 0;
    btn.textContent = count > 0 ? `Delete Selected (${count})` : 'Delete Selected';
  }

  function currentInvFilter () {
    return {
      status: $('invFilterStatus')?.value || '',
      plan:   $('invFilterPlan')?.value   || '',
      search: $('invSearch')?.value       || '',
    };
  }

  /* ── Edit Key ──────────────────────────────────────────────────────── */
  window.openEditKey = function (jsonStr) {
    const k = JSON.parse(jsonStr);
    $('editKeyValue').value        = k.key;
    $('editKeyStatus').value       = k.status || 'available';
    $('editKeyPlan').value         = k.plan   || 'pro';
    $('editKeyCustomer').value     = k.customer || '';
    $('editKeyHwid').value         = k.hwid    || '';
    $('editKeyNotes').value        = k.notes   || '';
    $('editKeyPurchaseDate').value = (k.purchase_date  || '').slice(0,10);
    $('editKeyExpiration').value   = (k.expiration     || '').slice(0,10);
    hideAlert('editKeyAlert');
    show('editKeyModal');
  };

  async function saveEditKey () {
    const key     = $('editKeyValue').value;
    const updates = {
      status:        $('editKeyStatus').value,
      plan:          $('editKeyPlan').value,
      customer:      $('editKeyCustomer').value.trim(),
      hwid:          $('editKeyHwid').value.trim(),
      notes:         $('editKeyNotes').value.trim(),
      purchase_date: $('editKeyPurchaseDate').value || '',
      expiration:    $('editKeyExpiration').value   || '',
    };
    setBusy('saveEditKey', true);
    try {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      });
      if (ok) {
        hide('editKeyModal');
        toast('Key updated successfully.', 'success');
        loadInventory(currentInvFilter());
      } else {
        showAlert('editKeyAlert', 'error', data.error || 'Update failed.');
      }
    } catch (_) {
      showAlert('editKeyAlert', 'error', 'Network error.');
    } finally {
      setBusy('saveEditKey', false);
    }
  }

  /* ── Extend Key ────────────────────────────────────────────────────── */
  window.openExtendKey = function (key) {
    _extendKeyTarget = key;
    $('extendKeyVal').textContent = key;
    $('extendDays').value = 30;
    show('extendKeyModal');
  };

  async function confirmExtend () {
    const days = parseInt($('extendDays').value) || 30;
    setBusy('confirmExtendKey', true);
    try {
      const { ok, data } = await apiFetch(
        `/api/admin/inventory/${encodeURIComponent(_extendKeyTarget)}/extend`,
        { method: 'POST', body: JSON.stringify({ days }) }
      );
      if (ok) {
        hide('extendKeyModal');
        toast(`Extended by ${days} days. New expiry: ${fmtDateShort(data.expiration)}`, 'success');
        loadInventory(currentInvFilter());
      } else {
        toast(data.error || 'Extend failed.', 'error');
      }
    } catch (_) {
      toast('Network error.', 'error');
    } finally {
      setBusy('confirmExtendKey', false);
    }
  }

  /* ── Import modal ──────────────────────────────────────────────────── */
  function openImportModal () {
    $('importPaste').value = '';
    $('importFile').value  = '';
    hideAlert('importAlert');
    hide('importResultBox');
    $('importResultBox').innerHTML = '';
    show('importModal');
    // Default to paste tab
    switchImportSource('paste');
  }
  function closeImportModal () { hide('importModal'); }

  function switchImportSource (source) {
    if (source === 'paste') {
      show('importSourcePaste');
      hide('importSourceFile');
      $('tabPaste').classList.add('active');
      $('tabFile').classList.remove('active');
    } else {
      hide('importSourcePaste');
      show('importSourceFile');
      $('tabFile').classList.add('active');
      $('tabPaste').classList.remove('active');
    }
  }

  async function submitImport () {
    hideAlert('importAlert');
    setBusy('submitImport', true);
    hide('importResultBox');

    let rawText = $('importPaste').value.trim();
    const file  = $('importFile').files[0];
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

    const plan  = $('importPlan').value;
    const notes = ($('importNotes')?.value || '').trim();
    const keys  = rawText
      .split(/[\r\n,;]+/)
      .map(l => l.trim())
      .filter(Boolean);

    if (!keys.length) {
      showAlert('importAlert', 'error', 'No valid lines found.');
      setBusy('submitImport', false);
      return;
    }

    try {
      const { ok, data } = await apiFetch('/api/admin/inventory/import', {
        method: 'POST',
        body: JSON.stringify({ keys, plan, notes }),
      });
      if (ok) {
        const rb = $('importResultBox');
        rb.style.display = '';
        rb.innerHTML = `
          <div class="import-stat import-stat--green">
            <span class="import-stat-num">${data.added}</span>
            <span class="import-stat-label">Imported</span>
          </div>
          <div class="import-stat import-stat--yellow">
            <span class="import-stat-num">${data.skipped}</span>
            <span class="import-stat-label">Duplicates</span>
          </div>
          <div class="import-stat import-stat--red">
            <span class="import-stat-num">${data.invalid ?? 0}</span>
            <span class="import-stat-label">Invalid</span>
          </div>
        `;
        if (data.added > 0) {
          toast(`✓ Imported ${data.added} key(s) successfully.`, 'success');
          loadInventory(currentInvFilter());
        } else if ((data.invalid ?? 0) > 0 && data.added === 0) {
          showAlert('importAlert', 'error', `No valid keys found. ${data.invalid} invalid format(s).`);
        } else {
          showAlert('importAlert', 'warning', `No new keys added. ${data.skipped} duplicate(s) skipped.`);
        }
      } else {
        showAlert('importAlert', 'error', data.error || 'Import failed.');
      }
    } catch (_) {
      showAlert('importAlert', 'error', 'Network error during import.');
    } finally {
      setBusy('submitImport', false);
    }
  }

  /* ── Export CSV ─────────────────────────────────────────────────────── */
  function exportInventoryCSV () {
    if (!_allInventory.length) { toast('No keys to export.', 'warning'); return; }
    const cols = ['key','plan','status','customer','purchase_date','hwid','created_date','expiration','notes','order_id'];
    const rows = [cols.join(',')].concat(_allInventory.map(k =>
      cols.map(c => `"${(k[c] || '').toString().replace(/"/g,'""')}"`).join(',')
    ));
    dlCSV('ghost_inventory.csv', rows.join('\n'));
  }

  function exportOrdersCSV () {
    if (!_allOrders.length) { toast('No orders to export.', 'warning'); return; }
    const cols = ['order_id','email','discord','plan','price_usd','payment_status','delivery_status','license_key','created_at'];
    const rows = [cols.join(',')].concat(_allOrders.map(o =>
      cols.map(c => `"${(o[c] || '').toString().replace(/"/g,'""')}"`).join(',')
    ));
    dlCSV('ghost_orders.csv', rows.join('\n'));
  }

  function dlCSV (name, csv) {
    const blob = new Blob([csv], { type: 'text/csv' });
    const a    = document.createElement('a');
    a.href     = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Exported ${name}`, 'success');
  }

  /* ── Orders ─────────────────────────────────────────────────────────── */
  async function loadOrders () {
    const tb = $('ordersTbody');
    tb.innerHTML = '<tr><td colspan="10" class="admin-table-empty">Loading…</td></tr>';
    try {
      const { ok, data } = await apiFetch('/api/admin/orders');
      if (!ok) throw new Error(data.error || 'Failed to load orders');
      let orders = (data.orders || []).reverse();
      _allOrders = orders;
      applyOrderFilter();
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="10" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function applyOrderFilter () {
    const search  = ($('orderSearch')?.value || '').toLowerCase();
    const delivery= $('orderFilterDelivery')?.value || '';
    let orders = _allOrders;
    if (search) {
      orders = orders.filter(o =>
        (o.order_id  || '').toLowerCase().includes(search) ||
        (o.email     || '').toLowerCase().includes(search) ||
        (o.discord   || '').toLowerCase().includes(search)
      );
    }
    if (delivery) {
      orders = orders.filter(o => (o.delivery_status || '') === delivery);
    }
    renderOrders(orders, 1);
  }

  function renderOrders (orders, page) {
    _ordPage = page;
    const tb    = $('ordersTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = orders.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="10" class="admin-table-empty">No orders found.</td></tr>';
      $('orderPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(o => `
      <tr>
        <td><span class="key-mono key-mono--sm copy-cell" onclick="copyText('${esc(o.order_id)}')" title="${esc(o.order_id)}">${esc((o.order_id||'').slice(0,16))}…</span></td>
        <td class="cell-truncate" title="${esc(o.email||'')}">${esc((o.email||'—').slice(0,24))}</td>
        <td class="cell-truncate">${esc(o.discord||'—')}</td>
        <td>${planBadge(o.plan)}</td>
        <td>$${parseFloat(o.price_usd || 0).toFixed(2)}</td>
        <td>${paymentBadge(o.payment_status)}</td>
        <td>${deliveryBadge(o.delivery_status)}</td>
        <td>${o.license_key ? `<span class="key-mono key-mono--sm copy-cell" onclick="copyText('${esc(o.license_key)}')">${esc((o.license_key||'').slice(0,16))}…</span>` : '<span class="badge badge--muted">—</span>'}</td>
        <td>${fmtDateShort(o.created_at)}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="openOrderDetail('${esc(o.order_id)}')">View</button>
          <button class="btn-icon" onclick="copyText('${esc(o.order_id)}')">Copy ID</button>
          ${o.delivery_status !== 'delivered' ? `<button class="btn-icon" onclick="reissueOrder('${esc(o.order_id)}')">Reissue</button>` : ''}
        </td>
      </tr>
    `).join('');

    renderPagination('orderPagination', orders.length, page, p => renderOrders(orders, p));
  }

  /* ── Order detail modal ─────────────────────────────────────────────── */
  window.openOrderDetail = async function (orderId) {
    $('orderDetailBody').innerHTML = '<p style="color:var(--muted)">Loading…</p>';
    $('reissueBtn').style.display = 'none';
    show('orderDetailModal');
    try {
      const { ok, data } = await apiFetch(`/api/admin/orders/${encodeURIComponent(orderId)}`);
      if (!ok) throw new Error(data.error || 'Order not found');
      const o = data.order || data;
      $('orderDetailBody').innerHTML = `
        <div class="order-detail-grid">
          <div class="order-detail-field"><span class="order-detail-label">Order ID</span><span class="order-detail-value copy-cell" onclick="copyText('${esc(o.order_id)}')">${esc(o.order_id)}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Plan</span><span class="order-detail-value">${esc(o.plan_label || o.plan || '—')}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Customer Email</span><span class="order-detail-value">${esc(o.email || '—')}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Discord</span><span class="order-detail-value">${esc(o.discord || '—')}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Amount</span><span class="order-detail-value">$${parseFloat(o.price_usd || 0).toFixed(2)} ${esc(o.currency || 'USD')}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Purchase Date</span><span class="order-detail-value">${fmtDate(o.created_at)}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Payment</span><span class="order-detail-value">${paymentBadge(o.payment_status)}</span></div>
          <div class="order-detail-field"><span class="order-detail-label">Delivery</span><span class="order-detail-value">${deliveryBadge(o.delivery_status)}</span></div>
          <div class="order-detail-key" style="grid-column:1/-1">
            <span class="order-detail-label">License Key</span>
            <span class="order-detail-key-val">${o.license_key ? `<span class="copy-cell" onclick="copyText('${esc(o.license_key)}')">${esc(o.license_key)}</span>` : '<span style="color:var(--muted)">Not yet assigned</span>'}</span>
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
  };

  window.reissueOrder = async function (orderId) {
    try {
      const { ok, data } = await apiFetch(`/api/paypal/retry-fulfillment`, {
        method: 'POST',
        body: JSON.stringify({ captureId: orderId }),
      });
      if (ok && data.licenseKey) {
        toast('License reissued successfully.', 'success');
        openOrderDetail(orderId);
        loadOrders();
      } else {
        toast(data.message || data.error || 'Reissue failed. Check inventory stock.', 'error');
      }
    } catch (_) {
      toast('Network error during reissue.', 'error');
    }
  };

  /* ── Key actions (global) ──────────────────────────────────────────── */
  window.revokeKey = function (key) {
    confirmAction('Revoke Key', `Revoke key ${key}? It will remain in the database marked as revoked.`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}/revoke`, { method: 'POST' });
      if (ok) { toast('Key revoked.', 'success'); loadInventory(currentInvFilter()); }
      else toast(data.error || 'Revoke failed.', 'error');
    });
  };

  window.deleteKey = function (key) {
    confirmAction('Delete Key', `Permanently delete key ${key}?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, { method: 'DELETE' });
      if (ok) { toast('Key deleted.', 'success'); loadInventory(currentInvFilter()); }
      else toast(data.error || 'Delete failed.', 'error');
    });
  };

  /* ── Customers ──────────────────────────────────────────────────────── */
  async function loadCustomers (search = '') {
    const tb = $('customersTbody');
    tb.innerHTML = '<tr><td colspan="8" class="admin-table-empty">Loading…</td></tr>';
    try {
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      const { ok, data } = await apiFetch(`/api/admin/customers?${params}`);
      if (!ok) throw new Error(data.error || 'Failed to load customers');
      _allCustomers = data.customers || [];
      renderCustomers(_allCustomers, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="8" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderCustomers (customers, page) {
    _custPage = page;
    const tb    = $('customersTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = customers.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="8" class="admin-table-empty">No customers found.</td></tr>';
      $('customerPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(c => `
      <tr>
        <td>${esc(c.email || '—')}</td>
        <td>${esc(c.discord || '—')}</td>
        <td>${c.total_orders}</td>
        <td>$${(c.total_spent||0).toFixed(2)}</td>
        <td>${c.active_licenses}</td>
        <td>${fmtDateShort(c.first_purchase)}</td>
        <td>${fmtDateShort(c.last_purchase)}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="openCustomerDetail(${JSON.stringify(JSON.stringify(c))})">View</button>
          <button class="btn-icon" onclick="resetCustomerHwid('${esc(c.email)}')">Reset HWID</button>
          <button class="btn-icon btn-icon--danger" onclick="revokeCustomer('${esc(c.email)}')">Revoke</button>
        </td>
      </tr>
    `).join('');

    renderPagination('customerPagination', customers.length, page, p => renderCustomers(customers, p));
  }

  window.openCustomerDetail = function (jsonStr) {
    const c = JSON.parse(jsonStr);
    const orders = (c.orders || []).map(o => `
      <tr>
        <td>${esc(o.order_id.slice(0,16))}…</td>
        <td>${planBadge(o.plan)}</td>
        <td>$${parseFloat(o.price_usd||0).toFixed(2)}</td>
        <td>${deliveryBadge(o.delivery_status)}</td>
        <td>${o.license_key ? esc(o.license_key.slice(0,20))+'…' : '—'}</td>
        <td>${fmtDateShort(o.created_at)}</td>
      </tr>
    `).join('');
    $('customerDetailBody').innerHTML = `
      <div class="customer-detail-header">
        <div><span class="order-detail-label">Email</span><div class="order-detail-value">${esc(c.email||'—')}</div></div>
        <div><span class="order-detail-label">Discord</span><div class="order-detail-value">${esc(c.discord||'—')}</div></div>
        <div><span class="order-detail-label">Orders</span><div class="order-detail-value">${c.total_orders}</div></div>
        <div><span class="order-detail-label">Total Spent</span><div class="order-detail-value">$${(c.total_spent||0).toFixed(2)}</div></div>
      </div>
      <h3 style="font-size:.85rem;font-weight:700;margin:18px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em">Purchase History</h3>
      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead><tr><th>Order ID</th><th>Plan</th><th>Amount</th><th>Delivery</th><th>License</th><th>Date</th></tr></thead>
          <tbody>${orders || '<tr><td colspan="6" class="admin-table-empty">No orders.</td></tr>'}</tbody>
        </table>
      </div>
    `;
    show('customerDetailModal');
  };

  window.resetCustomerHwid = function (email) {
    confirmAction('Reset HWID', `Clear HWID for all keys belonging to ${email}?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/customers/${encodeURIComponent(email)}/reset-hwid`, { method: 'POST' });
      if (ok) toast(`HWID reset for ${data.reset?.length || 0} key(s).`, 'success');
      else toast(data.error || 'Reset failed.', 'error');
    });
  };

  window.revokeCustomer = function (email) {
    confirmAction('Revoke All Keys', `Revoke all licenses for ${email}?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/customers/${encodeURIComponent(email)}/revoke`, { method: 'POST' });
      if (ok) { toast(`Revoked ${data.revoked?.length || 0} key(s).`, 'success'); loadCustomers(); }
      else toast(data.error || 'Revoke failed.', 'error');
    });
  };

  /* ── Downloads ──────────────────────────────────────────────────────── */
  async function loadDownloads () {
    try {
      const { ok, data } = await apiFetch('/api/admin/downloads');
      if (!ok) {
        toast(data.error || 'Failed to load downloads info.', 'error');
        return;
      }
      const v = data.current_version || '';
      setText('dlVersion',     v || 'Not set');
      setText('dlReleaseDate', data.release_date || '—');
      setText('dlFilename',    data.filename     || '—');
      setText('dlUrl',         data.download_url || '—');
      setText('dlCount',       data.download_count ?? 0);
      $('dlChangelog').value = data.changelog || '';
      $('dlVersionBadge').textContent = v || 'None';

      // History
      const hist = $('dlHistory');
      const history = data.history || [];
      if (!history.length) {
        hist.innerHTML = '<p class="admin-table-empty">No previous versions.</p>';
      } else {
        hist.innerHTML = history.reverse().map(h => `
          <div class="dl-hist-item">
            <div class="dl-hist-ver">${esc(h.version)}</div>
            <div class="dl-hist-date">${fmtDateShort(h.release_date || h.archived_at)}</div>
            <button class="btn-icon btn--sm" onclick="rollbackVersion('${esc(h.version)}')">Rollback</button>
          </div>
        `).join('');
      }
    } catch (err) {
      toast(`Downloads error: ${err.message || 'Network failure'}`, 'error');
    }
  }

  window.rollbackVersion = function (version) {
    confirmAction('Rollback Version', `Roll back to version ${version}?`, async () => {
      const { ok, data } = await apiFetch('/api/admin/downloads/rollback', {
        method: 'POST',
        body: JSON.stringify({ version }),
      });
      if (ok) { toast(`Rolled back to ${version}.`, 'success'); loadDownloads(); }
      else toast(data.error || 'Rollback failed.', 'error');
    });
  };

  async function saveDownload () {
    setBusy('confirmUpdateDl', true);
    const payload = {
      current_version: $('dlNewVersion').value.trim(),
      release_date:    $('dlNewReleaseDate').value,
      download_url:    $('dlNewUrl').value.trim(),
      filename:        $('dlNewFilename').value.trim(),
      changelog:       $('dlNewChangelog').value.trim(),
    };
    if (!payload.current_version) {
      showAlert('dlModalAlert', 'error', 'Version is required.');
      setBusy('confirmUpdateDl', false);
      return;
    }
    try {
      const { ok, data } = await apiFetch('/api/admin/downloads', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (ok) {
        hide('updateDlModal');
        toast(`Version ${payload.current_version} saved.`, 'success');
        loadDownloads();
      } else {
        showAlert('dlModalAlert', 'error', data.error || 'Save failed.');
      }
    } catch (_) {
      showAlert('dlModalAlert', 'error', 'Network error.');
    } finally {
      setBusy('confirmUpdateDl', false);
    }
  }

  /* ── Activity Log ───────────────────────────────────────────────────── */
  async function loadActivity () {
    const tb = $('activityTbody');
    tb.innerHTML = '<tr><td colspan="7" class="admin-table-empty">Loading…</td></tr>';
    try {
      const level = $('activityLevelFilter')?.value || '';
      const params = new URLSearchParams({ limit: 500 });
      if (level) params.set('level', level);
      const { ok, data } = await apiFetch(`/api/admin/activity?${params}`);
      if (!ok) throw new Error(data.error || 'Failed');
      let log = data.log || [];
      const search = ($('activitySearch')?.value || '').toLowerCase();
      if (search) {
        log = log.filter(r =>
          (r.action||'').toLowerCase().includes(search) ||
          (r.actor||'').toLowerCase().includes(search)  ||
          (r.target||'').toLowerCase().includes(search) ||
          (r.details||'').toLowerCase().includes(search)
        );
      }
      _allActivity = log;
      renderActivity(log, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="7" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderActivity (log, page) {
    _actPage = page;
    const tb    = $('activityTbody');
    const start = (page - 1) * PAGE_SIZE;
    const slice = log.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="7" class="admin-table-empty">No activity records.</td></tr>';
      $('activityPagination').innerHTML = '';
      return;
    }

    tb.innerHTML = slice.map(r => `
      <tr>
        <td class="cell-mono-sm">${fmtDate(r.timestamp)}</td>
        <td>${levelBadge(r.level)}</td>
        <td class="cell-truncate">${esc(r.action || '—')}</td>
        <td>${esc(r.actor || '—')}</td>
        <td class="cell-truncate">${esc(r.target || '—')}</td>
        <td class="cell-truncate" title="${esc(r.details||'')}">${esc((r.details||'').slice(0,60))||'—'}</td>
        <td class="cell-mono-sm">${esc(r.ip||'—')}</td>
      </tr>
    `).join('');
    renderPagination('activityPagination', log.length, page, p => renderActivity(log, p));
  }

  /* ── Settings ───────────────────────────────────────────────────────── */
  async function loadSettings () {
    try {
      const { ok, data } = await apiFetch('/api/admin/settings');
      if (!ok) {
        toast(data.error || 'Failed to load settings.', 'error');
        return;
      }
      const s = data.settings || {};
      $('settingSiteName').value        = s.site_name           || '';
      $('settingLogoUrl').value         = s.logo_url            || '';
      $('settingDiscordInvite').value   = s.discord_invite      || '';
      $('settingDownloadUrl').value     = s.download_url        || '';
      $('settingBanner').value          = s.announcement_banner || '';
      $('settingMaintenance').checked   = !!s.maintenance_mode;
      $('settingPaypalClientId').value  = s.paypal_client_id    || '';
      $('settingPaypalEnv').value       = s.paypal_environment  || 'sandbox';
    } catch (err) {
      toast(`Settings error: ${err.message || 'Network failure'}`, 'error');
    }
  }

  async function saveSiteSettings () {
    setBusy('saveSiteSettings', true);
    hideAlert('settingsAlert');
    const payload = {
      site_name:           $('settingSiteName').value.trim(),
      logo_url:            $('settingLogoUrl').value.trim(),
      discord_invite:      $('settingDiscordInvite').value.trim(),
      download_url:        $('settingDownloadUrl').value.trim(),
      announcement_banner: $('settingBanner').value.trim(),
      maintenance_mode:    $('settingMaintenance').checked,
    };
    try {
      const { ok, data } = await apiFetch('/api/admin/settings', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (ok) {
        toast('Site settings saved.', 'success');
      } else {
        showAlert('settingsAlert', 'error', data.error || 'Save failed.');
      }
    } catch (_) {
      showAlert('settingsAlert', 'error', 'Network error.');
    } finally {
      setBusy('saveSiteSettings', false);
    }
  }

  async function savePaypalSettings () {
    setBusy('savePaypalSettings', true);
    const payload = {
      paypal_client_id:   $('settingPaypalClientId').value.trim(),
      paypal_environment: $('settingPaypalEnv').value,
    };
    try {
      const { ok, data } = await apiFetch('/api/admin/settings', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (ok) toast('PayPal settings saved.', 'success');
      else toast(data.error || 'Save failed.', 'error');
    } catch (_) {
      toast('Network error.', 'error');
    } finally {
      setBusy('savePaypalSettings', false);
    }
  }

  async function changePassword () {
    hideAlert('pwAlert');
    const pw1 = $('settingNewPw').value;
    const pw2 = $('settingConfirmPw').value;
    if (!pw1 || pw1.length < 8) { showAlert('pwAlert', 'error', 'Password must be at least 8 characters.'); return; }
    if (pw1 !== pw2) { showAlert('pwAlert', 'error', 'Passwords do not match.'); return; }
    setBusy('changePasswordBtn', true);
    try {
      const { ok, data } = await apiFetch('/api/admin/settings/password', {
        method: 'POST',
        body: JSON.stringify({ new_password: pw1 }),
      });
      if (ok) {
        toast('Password updated. Session remains active.', 'success');
        $('settingNewPw').value    = '';
        $('settingConfirmPw').value = '';
      } else {
        showAlert('pwAlert', 'error', data.error || 'Failed.');
      }
    } catch (_) {
      showAlert('pwAlert', 'error', 'Network error.');
    } finally {
      setBusy('changePasswordBtn', false);
    }
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
    if (!el) return;
    if (pages <= 1) { el.innerHTML = `<span>${total} item(s)</span>`; return; }
    let html = `<span>${total} item(s) &nbsp;|&nbsp; Page ${current}/${pages} &nbsp; </span>`;
    const start = Math.max(1, current - 2);
    const end   = Math.min(pages, current + 2);
    if (start > 1) html += `<button class="btn btn-ghost btn--sm" onclick="(${onPage.toString()})(1)">1</button> … `;
    for (let i = start; i <= end; i++) {
      html += `<button class="btn btn-ghost btn--sm${i === current ? ' btn-page-active' : ''}" onclick="(${onPage.toString()})(${i})">${i}</button> `;
    }
    if (end < pages) html += ` … <button class="btn btn-ghost btn--sm" onclick="(${onPage.toString()})(${pages})">${pages}</button>`;
    el.innerHTML = html;
  }

  /* ── Badge helpers ──────────────────────────────────────────────────── */
  function statusBadge (status) {
    const map = {
      available: ['green',  'Available'],
      reserved:  ['yellow', 'Reserved'],
      sold:      ['purple', 'Sold'],
      activated: ['cyan',   'Activated'],
      revoked:   ['red',    'Revoked'],
      expired:   ['muted',  'Expired'],
      unused:    ['green',  'Available'],   // legacy
      assigned:  ['purple', 'Sold'],        // legacy
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
      completed:      ['green',  'Completed'],
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
      delivered:        ['green',  'Delivered'],
      out_of_stock:     ['yellow', 'Out of Stock'],
      delivery_pending: ['yellow', 'Pending'],
      pending:          ['muted',  'Pending'],
    };
    const [cls, label] = map[status] || ['muted', status || '—'];
    return `<span class="badge badge--${cls}">${label}</span>`;
  }

  function levelBadge (level) {
    const map = { info: ['cyan','info'], warn: ['yellow','warn'], warning: ['yellow','warn'], error: ['red','error'] };
    const [cls, lbl] = map[(level||'info').toLowerCase()] || ['muted', level || '—'];
    return `<span class="badge badge--${cls}">${lbl}</span>`;
  }

  /* ── Date formatters ────────────────────────────────────────────────── */
  function fmtDate (iso) {
    if (!iso) return '<span style="color:var(--muted)">—</span>';
    try {
      return new Date(iso).toLocaleString('en-US', {
        month: 'short', day: 'numeric', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { return iso; }
  }

  function fmtDateShort (iso) {
    if (!iso) return '<span style="color:var(--muted)">—</span>';
    try {
      return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch (_) { return iso; }
  }

  /* ── Clipboard ──────────────────────────────────────────────────────── */
  window.copyText = function (text) {
    navigator.clipboard.writeText(text).then(() => toast('Copied to clipboard!', 'success', 1500));
  };
  window.copyToClipboard = window.copyText;  // legacy compat

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
    } catch (_) {}
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

    // Dashboard
    $('refreshDashboard').addEventListener('click', loadDashboard);
    document.querySelectorAll('.graph-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.graph-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderChart(btn.dataset.graph);
      });
    });

    // Inventory filters
    $('applyInvFilter').addEventListener('click', () => loadInventory(currentInvFilter()));
    $('invSearch').addEventListener('keydown', e => { if (e.key === 'Enter') loadInventory(currentInvFilter()); });

    // Import modal
    $('openImportModal').addEventListener('click', openImportModal);
    $('closeImportModal').addEventListener('click', closeImportModal);
    $('cancelImport').addEventListener('click', closeImportModal);
    $('submitImport').addEventListener('click', submitImport);
    $('tabPaste').addEventListener('click', () => switchImportSource('paste'));
    $('tabFile').addEventListener('click',  () => switchImportSource('file'));
    $('importModal').addEventListener('click', e => { if (e.target === $('importModal')) closeImportModal(); });

    // Export
    $('exportInvBtn').addEventListener('click', exportInventoryCSV);
    $('exportOrdersBtn')?.addEventListener('click', exportOrdersCSV);

    // Bulk delete
    $('deleteSelectedKeys').addEventListener('click', () => {
      const keys = [...document.querySelectorAll('.inv-check:checked')].map(c => c.dataset.key);
      if (!keys.length) return;
      confirmAction(`Delete ${keys.length} key(s)?`, `Permanently remove ${keys.length} key(s) from inventory.`, async () => {
        const { ok, data } = await apiFetch('/api/admin/inventory/bulk-delete', {
          method: 'POST',
          body: JSON.stringify({ keys }),
        });
        if (ok) { toast(`Deleted ${data.deleted?.length || 0} key(s).`, 'success'); loadInventory(currentInvFilter()); }
        else toast(data.error || 'Bulk delete failed.', 'error');
      });
    });

    // Edit key modal
    $('closeEditKeyModal').addEventListener('click', () => hide('editKeyModal'));
    $('cancelEditKey').addEventListener('click',     () => hide('editKeyModal'));
    $('saveEditKey').addEventListener('click',       saveEditKey);
    $('editKeyModal').addEventListener('click', e => { if (e.target === $('editKeyModal')) hide('editKeyModal'); });

    // Extend key modal
    $('confirmExtendKey').addEventListener('click', confirmExtend);
    $('extendKeyModal').addEventListener('click', e => { if (e.target === $('extendKeyModal')) hide('extendKeyModal'); });

    // Orders
    $('refreshOrders').addEventListener('click', loadOrders);
    $('applyOrderFilter').addEventListener('click', applyOrderFilter);
    $('orderSearch').addEventListener('keydown', e => { if (e.key === 'Enter') applyOrderFilter(); });
    $('orderFilterDelivery').addEventListener('change', applyOrderFilter);
    $('closeOrderDetail').addEventListener('click', () => hide('orderDetailModal'));
    $('closeOrderDetailBtn').addEventListener('click', () => hide('orderDetailModal'));
    $('orderDetailModal').addEventListener('click', e => { if (e.target === $('orderDetailModal')) hide('orderDetailModal'); });

    // Customers
    $('refreshCustomers').addEventListener('click', () => loadCustomers());
    $('applyCustomerFilter').addEventListener('click', () => loadCustomers($('customerSearch').value));
    $('customerSearch').addEventListener('keydown', e => { if (e.key === 'Enter') loadCustomers($('customerSearch').value); });
    $('closeCustomerDetail').addEventListener('click', () => hide('customerDetailModal'));
    $('closeCustomerDetailBtn').addEventListener('click', () => hide('customerDetailModal'));
    $('customerDetailModal').addEventListener('click', e => { if (e.target === $('customerDetailModal')) hide('customerDetailModal'); });

    // Downloads
    $('openUpdateDlModal').addEventListener('click', () => {
      hideAlert('dlModalAlert');
      $('dlNewVersion').value    = '';
      $('dlNewReleaseDate').value= '';
      $('dlNewUrl').value        = '';
      $('dlNewFilename').value   = '';
      $('dlNewChangelog').value  = '';
      show('updateDlModal');
    });
    $('closeUpdateDlModal').addEventListener('click', () => hide('updateDlModal'));
    $('cancelUpdateDl').addEventListener('click',     () => hide('updateDlModal'));
    $('confirmUpdateDl').addEventListener('click',    saveDownload);
    $('updateDlModal').addEventListener('click', e => { if (e.target === $('updateDlModal')) hide('updateDlModal'); });

    // Activity log
    $('refreshActivity').addEventListener('click', loadActivity);
    $('activityLevelFilter').addEventListener('change', loadActivity);
    $('activitySearch').addEventListener('keydown', e => { if (e.key === 'Enter') loadActivity(); });
    $('clearActivityBtn').addEventListener('click', () => {
      confirmAction('Clear Activity Log', 'Permanently delete all activity log entries?', async () => {
        const { ok } = await apiFetch('/api/admin/activity', { method: 'DELETE' });
        if (ok) { toast('Activity log cleared.', 'success'); loadActivity(); }
        else toast('Clear failed.', 'error');
      });
    });

    // Settings
    $('saveSiteSettings').addEventListener('click',  saveSiteSettings);
    $('savePaypalSettings').addEventListener('click', savePaypalSettings);
    $('changePasswordBtn').addEventListener('click',  changePassword);

    // Confirm modal
    $('closeConfirm').addEventListener('click',  () => hide('confirmModal'));
    $('confirmCancel').addEventListener('click', () => hide('confirmModal'));
    $('confirmOk').addEventListener('click', async () => {
      hide('confirmModal');
      if (_confirmCb) { await _confirmCb(); _confirmCb = null; }
    });
    $('confirmModal').addEventListener('click', e => { if (e.target === $('confirmModal')) hide('confirmModal'); });

    // Resize: re-render chart
    window.addEventListener('resize', () => {
      if (_graphData) {
        const active = document.querySelector('.graph-btn.active');
        renderChart(active?.dataset.graph || 'revenue');
      }
    });
  }

  /* ── Init ───────────────────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    wireAll();
    checkExistingSession();
  });

}());
