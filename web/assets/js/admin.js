/**
 * admin.js — Ghost Admin Panel (Complete)
 * ========================================
 * Handles login, tab navigation, inventory, orders, customers,
 * downloads, activity log, settings, and dashboard.
 */

'use strict';

(function () {

  /* ── State ─────────────────────────────────────────────────────────── */
  // Session is managed by a server-side HttpOnly cookie (__Host-ghost_admin_session).
  // The browser sends it automatically with every same-origin request because every
  // admin fetch uses credentials: "include".
  // We track login state client-side only to control UI visibility — the cookie
  // is the real authority; the server validates it on every authenticated request.
  //
  // _sessionVerified: true once /api/admin/session has returned a definitive answer.
  // Before it is true, 401 responses from background requests must NOT trigger
  // session-expiry UI — the initial check hasn't finished yet.
  let _loggedIn        = false;
  let _sessionVerified = false;
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
  // credentials: "include" ensures the HttpOnly session cookie is sent with
  // every same-origin request.  The browser manages the cookie automatically;
  // we never read or set it from JavaScript.
  //
  // 401 deduplication: only the FIRST 401 received while _loggedIn=true AND
  // _sessionVerified=true triggers a UI transition + toast.
  // Before _sessionVerified is set, 401 responses from background requests
  // are returned to callers but do NOT affect the UI — the initial session
  // check may still be in-flight.
  let _handlingSessionExpiry = false;

  // Front-end request timeout — prevents the button from spinning forever if
  // the server hangs.  Import can be slow (Redis write), so we allow 15 s.
  const _FETCH_TIMEOUT_MS = 15000;

  async function apiFetch (path, opts = {}, retries = 2) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    for (let attempt = 0; attempt <= retries; attempt++) {
      const ctrl  = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), _FETCH_TIMEOUT_MS);
      try {
        const res  = await fetch(path, { ...opts, headers, credentials: 'include', signal: ctrl.signal });
        clearTimeout(timer);
        // If the server returns 401, the session cookie is missing or expired.
        // Do NOT retry 401 responses — retrying would just produce more alerts.
        if (res.status === 401) {
          const data = await res.json().catch(() => ({}));
          // Guard 1: _sessionVerified must be true — we only react to 401s that
          //   happen AFTER the initial /api/admin/session check completed and
          //   confirmed the user was authenticated.
          // Guard 2: _loggedIn must be true — prevents duplicate handling.
          // Guard 3: _handlingSessionExpiry prevents multiple simultaneous 401s
          //   (e.g. dashboard + inventory in parallel) from each showing a toast.
          if (_sessionVerified && _loggedIn && !_handlingSessionExpiry) {
            _handlingSessionExpiry = true;
            _loggedIn = false;
            hide('adminShell');
            hide('adminLoading');
            show('adminLogin');
            toast('Session expired. Please log in again.', 'error');
            setTimeout(() => { _handlingSessionExpiry = false; }, 4000);
          }
          return { ok: false, status: 401, data };
        }
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
      } catch (networkErr) {
        clearTimeout(timer);
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
    const key = $('adminApiKey').value.trim();
    if (!key) return;
    setBusy('loginBtn', true);
    hideAlert('loginAlert');
    try {
      // POST admin API key → server verifies against GHOST_ADMIN_API_KEY,
      // sets HttpOnly __Host-ghost_admin_session cookie on success.
      const { ok: authOk, data: authData } = await apiFetch('/api/admin/login', {
        method: 'POST',
        body:   JSON.stringify({ key }),
      });
      if (!authOk) {
        showAlert('loginAlert', 'error', authData.error || 'Invalid admin API key.');
        return;
      }

      // Verify the cookie was set before rendering the dashboard.
      // Prevents flash+logout if the cookie round-trip fails.
      const verifyRes  = await fetch('/api/admin/session', { credentials: 'include' });
      const verifyData = await verifyRes.json().catch(() => ({}));
      if (!verifyRes.ok || !verifyData.authenticated) {
        showAlert('loginAlert', 'error', 'Session could not be established. Please try again.');
        return;
      }

      // Session confirmed — safe to render.
      _loggedIn        = true;
      _sessionVerified = true;
      hide('adminLogin');
      hide('adminLoading');
      show('adminShell');
      loadDashboard();
    } catch (_) {
      showAlert('loginAlert', 'error', 'Network error. Please try again.');
    } finally {
      setBusy('loginBtn', false);
    }
  }

  async function doLogout () {
    _loggedIn = false;
    _sessionVerified = false;
    // POST to /api/admin/logout so the server can clear the HttpOnly cookie.
    // JS cannot delete an HttpOnly cookie directly.
    await fetch('/api/admin/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
    // Redirect to homepage after logout.
    window.location.href = '/';
  }

  /* ── Tab navigation ─────────────────────────────────────────────────── */
  window.switchTab = function (name) {
    document.querySelectorAll('.admin-tab').forEach(s => s.style.display = 'none');
    document.querySelectorAll('.admin-nav-link').forEach(a => a.classList.remove('active'));
    const tab = $(`tab-${name}`);
    if (tab) tab.style.display = '';
    document.querySelectorAll(`.admin-nav-link[data-tab="${name}"]`)
      .forEach(a => a.classList.add('active'));
    if (name === 'dashboard')          loadDashboard();
    if (name === 'inventory')          loadInventory();
    if (name === 'generate')           loadGenStats();
    if (name === 'orders')             loadOrders();
    if (name === 'customer-licenses')  loadCustomerLicenses();
    if (name === 'customers')          loadCustomers();
    if (name === 'downloads')          loadDownloads();
    if (name === 'activity')           loadActivity();
    if (name === 'fulfillment-diag')   loadFulfillmentDiag();
    if (name === 'settings')           loadSettings();
    if (name === 'coupons')            loadCoupons();
    if (name === 'releases')           loadReleases();
  };

  /* ── Dashboard ─────────────────────────────────────────────────────── */
  async function loadDashboard () {
    try {
      const [dashRes, ordRes, couponRes] = await Promise.all([
        apiFetch('/api/admin/dashboard'),
        apiFetch('/api/admin/orders'),
        apiFetch('/api/admin/coupons'),
      ]);
      if (!dashRes.ok) {
        toast(dashRes.data.error || 'Dashboard failed to load. Check server logs.', 'error');
        return;
      }
      const data = dashRes.data;
      _dashData = data;
      const c       = data.cards || {};
      const graphs  = data.graphs || {};
      const ordToday = (graphs.orders || []).slice(-1)[0] || 0;

      setText('statRevToday',    `$${(c.revenue_today  || 0).toFixed(2)}`);
      setText('statRevMonth',    `$${(c.revenue_month  || 0).toFixed(2)}`);
      setText('statRevTotal',    `$${(c.revenue_total  || 0).toFixed(2)}`);
      setText('statTotalOrders',  c.total_orders    || 0);
      setText('statActiveLic',    c.active_licenses || 0);
      setText('statKeysLeft',     c.keys_remaining  || 0);
      setText('statPending',      c.pending_orders  || 0);
      setText('statFailed',       c.failed_payments || 0);
      setText('statOrdersToday',  ordToday);

      // Coupon count
      if (couponRes.ok) {
        const coupons = couponRes.data.coupons || [];
        const activeCoupons = coupons.filter(cp => !cp.disabled && (cp.uses == null || cp.remaining == null || cp.remaining > 0));
        setText('statCoupons', activeCoupons.length);
      }

      _graphData = data.graphs;
      renderChart('revenue');

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
    const safeOrders = Array.isArray(orders) ? orders : [];
    const tb = $('recentOrdersTbody');
    if (!safeOrders.length) {
      tb.innerHTML = '<tr><td colspan="7" class="admin-table-empty">No orders yet.</td></tr>';
      return;
    }
    tb.innerHTML = safeOrders.map(o => `
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

  /* ── Inventory status tab wiring ───────────────────────────────────── */
  let _invStatusFilter = 'available';   // default: show only available keys

  function wireInvStatusTabs () {
    const tabs = document.querySelectorAll('.admin-status-tab');
    tabs.forEach(btn => {
      btn.addEventListener('click', function () {
        tabs.forEach(t => t.classList.remove('active'));
        this.classList.add('active');
        _invStatusFilter = this.dataset.status || 'available';
        loadInventory(currentInvFilter());
      });
    });
  }

  /* ── Key Inventory ─────────────────────────────────────────────────── */
  async function loadInventory (filter = {}) {
    const tb = $('inventoryTbody');
    // Skeleton loader
    tb.innerHTML = Array(5).fill('<tr>' + Array(12).fill('<td><div class="skel-cell"></div></td>').join('') + '</tr>').join('');
    try {
      const params = new URLSearchParams();
      // Active tab sets the status. '__all__' = no filter. '' or 'available' = status=available.
      const statusToLoad = (filter.status !== undefined) ? filter.status : _invStatusFilter;
      if (statusToLoad && statusToLoad !== '__all__') {
        params.set('status', statusToLoad);
      } else if (!statusToLoad) {
        params.set('status', 'available');
      }
      // __all__ → no status param (server returns everything)
      if (filter.plan)   params.set('plan',   filter.plan);
      if (filter.search) params.set('search', filter.search);
      const { ok, data } = await apiFetch(`/api/admin/inventory?${params}`);
      if (!ok) throw new Error(data.error || 'Failed to load inventory');

      // Normalize: always guarantee an array regardless of what the server sent
      const items = Array.isArray(data.items) ? data.items : [];

      _allInventory = items;
      renderInventory(_allInventory, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="12" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderInventory (keys, page) {
    _invPage = page;
    const tb    = $('inventoryTbody');
    // Defensive: keys must be an array before we slice/map
    const safeKeys = Array.isArray(keys) ? keys : [];
    const start = (page - 1) * PAGE_SIZE;
    const slice = safeKeys.slice(start, start + PAGE_SIZE);

    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="12" class="admin-table-empty">No keys found.</td></tr>';
      $('invPagination').innerHTML = '';
      return;
    }

    // Show discord/order-id columns only for sold/activated/revoked views
    const showExtended = ['sold','activated','revoked','expired'].includes(_invStatusFilter);
    document.querySelectorAll('.inv-col-discord, .inv-col-orderid').forEach(el => {
      el.style.display = showExtended ? '' : 'none';
    });

    tb.innerHTML = slice.map(k => `
      <tr>
        <td><input type="checkbox" class="inv-check" data-key="${esc(k.key)}" /></td>
        <td><span class="key-mono copy-cell" onclick="copyText('${esc(k.key)}')" title="Click to copy">${esc(k.key)}</span></td>
        <td>${planBadge(k.plan)}</td>
        <td>${statusBadge(k.status)}</td>
        <td class="cell-truncate">${esc(k.customer_email || k.customer || '—')}</td>
        <td class="inv-col-discord cell-truncate" style="${showExtended ? '' : 'display:none'}">${esc(k.discord_username || k.assigned_user || '—')}</td>
        <td class="inv-col-orderid cell-mono-sm" style="${showExtended ? '' : 'display:none'}">${k.order_id ? `<span class="copy-cell" onclick="copyText('${esc(k.order_id)}')" title="${esc(k.order_id)}">${esc((k.order_id||'').slice(0,14))}…</span>` : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${fmtDateShort(k.purchase_date)}</td>
        <td class="cell-mono-sm">${k.hwid ? `<span title="${esc(k.hwid)}">${esc(k.hwid.slice(0,12))}…</span>` : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${k.expiration ? fmtDateShort(k.expiration) : '<span style="color:var(--muted)">—</span>'}</td>
        <td class="cell-truncate" title="${esc(k.notes || '')}">${esc((k.notes||'').slice(0,20))||'<span style="color:var(--muted)">—</span>'}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="copyText('${esc(k.key)}')" title="Copy">Copy</button>
          ${k.order_id ? `<button class="btn-icon" onclick="openOrderDetail('${esc(k.order_id)}')" title="View Order">View Order</button>` : ''}
          <button class="btn-icon" onclick="openEditKey(${JSON.stringify(JSON.stringify(k))})" title="Edit">Edit</button>
          <button class="btn-icon" onclick="openExtendKey('${esc(k.key)}')" title="Extend">Extend</button>
          ${(k.status === 'sold' || k.status === 'available') ? `<button class="btn-icon" onclick="activateKey('${esc(k.key)}')" title="Activate">Activate</button>` : ''}
          ${k.status !== 'revoked' ? `<button class="btn-icon btn-icon--danger" onclick="revokeKey('${esc(k.key)}')" title="Revoke">Revoke</button>` : ''}
          ${k.hwid ? `<button class="btn-icon" onclick="resetHwidForKey('${esc(k.key)}')" title="Reset HWID">Reset HWID</button>` : ''}
          <button class="btn-icon btn-icon--danger" onclick="deleteKey('${esc(k.key)}')" title="Delete">Delete</button>
        </td>
      </tr>
    `).join('');

    renderPagination('invPagination', safeKeys.length, page, p => renderInventory(safeKeys, p));
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
      // Status comes from the active tab, not the hidden select
      status: _invStatusFilter,
      plan:   $('invFilterPlan')?.value   || '',
      search: $('invSearch')?.value       || '',
    };
  }

  /* ── Edit Key ──────────────────────────────────────────────────────── */
  window.openEditKey = function (jsonStr) {
    const k = JSON.parse(jsonStr);
    $('editKeyValue').value        = k.key;
    $('editKeyStatus').value       = k.status || 'available';
    $('editKeyPlan').value         = k.plan   || 'month';
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

      const importedCount  = data.imported_count  ?? data.added   ?? 0;
      const duplicateCount = data.duplicate_count ?? data.skipped ?? 0;
      const invalidCount   = data.invalid_count   ?? data.invalid ?? 0;

      if (ok) {
        const rb = $('importResultBox');
        rb.style.display = '';
        rb.innerHTML = `
          <div class="import-stat import-stat--green">
            <span class="import-stat-num">${importedCount}</span>
            <span class="import-stat-label">Imported</span>
          </div>
          <div class="import-stat import-stat--yellow">
            <span class="import-stat-num">${duplicateCount}</span>
            <span class="import-stat-label">Duplicates</span>
          </div>
          <div class="import-stat import-stat--red">
            <span class="import-stat-num">${invalidCount}</span>
            <span class="import-stat-label">Invalid</span>
          </div>
        `;

        if (importedCount > 0) {
          toast(`✓ Imported ${importedCount} key(s) successfully.`, 'success');
          // Reset to "Available" tab so newly-imported keys are visible
          _invStatusFilter = 'available';
          document.querySelectorAll('.admin-status-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.status === '' || t.dataset.status === 'available');
          });
          const planEl   = $('invFilterPlan');
          const searchEl = $('invSearch');
          if (planEl)   planEl.value   = '';
          if (searchEl) searchEl.value = '';
          // Always re-fetch from the server — never rely on stale cached data
          await loadInventory({});
        } else if (invalidCount > 0 && importedCount === 0) {
          showAlert('importAlert', 'error', `No valid keys found. ${invalidCount} invalid format(s).`);
        } else {
          showAlert('importAlert', 'warning', `No new keys added. ${duplicateCount} duplicate(s) skipped.`);
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
    // Skeleton loader
    tb.innerHTML = Array(5).fill('<tr>' + Array(10).fill('<td><div class="skel-cell"></div></td>').join('') + '</tr>').join('');
    try {
      const { ok, data } = await apiFetch('/api/admin/orders');
      if (!ok) throw new Error(data.error || 'Failed to load orders');
      // Normalize: orders must always be an array
        const raw = data.orders;
        let orders = (Array.isArray(raw) ? raw : []).reverse();
      _allOrders = orders;
      applyOrderFilter();
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="10" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function applyOrderFilter () {
    const search     = ($('orderSearch')?.value || '').toLowerCase();
    const statusFilter = $('orderStatusFilter')?.value || '';
    let orders = _allOrders;

    if (search) {
      orders = orders.filter(o =>
        (o.order_id  || '').toLowerCase().includes(search) ||
        (o.email     || '').toLowerCase().includes(search) ||
        (o.discord   || '').toLowerCase().includes(search)
      );
    }

    if (statusFilter) {
      orders = orders.filter(o => {
        const ps = (o.payment_status  || '').toLowerCase();
        const ds = (o.delivery_status || '').toLowerCase();
        switch (statusFilter) {
          case 'completed':
            // Payment complete and key delivered
            return (ps === 'completed' || ps === 'verified') && ds === 'delivered';
          case 'pending_delivery':
            // Paid but key not yet assigned
            return (ps === 'completed' || ps === 'verified') &&
                   (ds === 'pending' || ds === 'delivery_pending' || ds === 'out_of_stock');
          case 'failed_delivery':
            return ds === 'failed' || ds === 'failed_delivery';
          case 'refunded':
            return ps === 'refunded';
          default:
            return true;
        }
      });
    }

    renderOrders(orders, 1);
  }

  function renderOrders (orders, page) {
    _ordPage = page;
    const tb    = $('ordersTbody');
    // Defensive: orders must be an array before slice/map
    const safeOrders = Array.isArray(orders) ? orders : [];
    const start = (page - 1) * PAGE_SIZE;
    const slice = safeOrders.slice(start, start + PAGE_SIZE);

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

    renderPagination('orderPagination', safeOrders.length, page, p => renderOrders(safeOrders, p));
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

  /* ── Fulfill all pending orders in one click ────────────────────────── */
  async function fulfillPendingOrders () {
    setBusy('fulfillPendingBtn', true);
    try {
      const { ok, data } = await apiFetch('/api/admin/orders/fulfill-pending', { method: 'POST' });
      if (ok) {
        const fulfilled = data.fulfilled || 0;
        const failed    = data.failed    || 0;
        const skipped   = data.skipped   || 0;
        if (fulfilled > 0) {
          toast(`Fulfilled ${fulfilled} order(s). Failed: ${failed}. Already done: ${skipped}.`, 'success');
        } else if (failed > 0) {
          toast(`No orders fulfilled. ${failed} failed (check inventory stock). Skipped: ${skipped}.`, 'error');
        } else {
          toast(`No pending orders found. All ${skipped} order(s) already delivered.`, 'info');
        }
        loadOrders();
      } else {
        toast(data.error || 'Fulfill-pending failed.', 'error');
      }
    } catch (_) {
      toast('Network error during fulfill-pending.', 'error');
    } finally {
      setBusy('fulfillPendingBtn', false);
    }
  }

  /* ── Customer Licenses ──────────────────────────────────────────────── */
  let _allCustomerLicenses = [];
  let _clPage = 1;

  async function loadCustomerLicenses () {
    const tb = $('clTbody');
    if (!tb) return;
    tb.innerHTML = '<tr><td colspan="10" class="admin-table-empty">Loading…</td></tr>';
    try {
      const { ok, data } = await apiFetch('/api/admin/customer-licenses');
      if (!ok) throw new Error(data.error || 'Failed to load customer licenses');
      _allCustomerLicenses = Array.isArray(data.licenses) ? data.licenses : [];
      applyClFilter();
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="10" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function applyClFilter () {
    const search   = ($('clSearch')?.value    || '').toLowerCase();
    const plan     = $('clFilterPlan')?.value  || '';
    const statusF  = $('clFilterStatus')?.value || '';
    let licenses   = _allCustomerLicenses;
    if (search) {
      licenses = licenses.filter(k =>
        (k.key             || '').toLowerCase().includes(search) ||
        (k.customer_email  || '').toLowerCase().includes(search) ||
        (k.customer        || '').toLowerCase().includes(search) ||
        (k.order_id        || '').toLowerCase().includes(search) ||
        (k.assigned_user   || '').toLowerCase().includes(search)
      );
    }
    if (plan)    licenses = licenses.filter(k => (k.plan || '') === plan);
    if (statusF) licenses = licenses.filter(k => (k.status || '') === statusF);
    renderCustomerLicenses(licenses, 1);
  }

  function renderCustomerLicenses (licenses, page) {
    _clPage = page;
    const tb        = $('clTbody');
    const safeLic   = Array.isArray(licenses) ? licenses : [];
    const start     = (page - 1) * PAGE_SIZE;
    const slice     = safeLic.slice(start, start + PAGE_SIZE);
    if (!slice.length) {
      tb.innerHTML = '<tr><td colspan="10" class="admin-table-empty">No sold licenses found.</td></tr>';
      $('clPagination').innerHTML = '';
      return;
    }
    tb.innerHTML = slice.map(k => `
      <tr>
        <td><span class="key-mono copy-cell" onclick="copyText('${esc(k.key)}')" title="Click to copy">${esc(k.key)}</span></td>
        <td>${planBadge(k.plan)}</td>
        <td>${statusBadge(k.status)}</td>
        <td class="cell-truncate">${esc(k.customer_email || k.customer || '—')}</td>
        <td class="cell-truncate">${esc(k.assigned_user || '—')}</td>
        <td><span class="key-mono key-mono--sm copy-cell" onclick="copyText('${esc(k.order_id || '')}')">${esc((k.order_id || '').slice(0, 16))}${k.order_id && k.order_id.length > 16 ? '…' : ''}</span></td>
        <td>${fmtDateShort(k.purchase_date)}</td>
        <td class="cell-mono-sm">${k.hwid ? `<span title="${esc(k.hwid)}">${esc(k.hwid.slice(0, 12))}…</span>` : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${k.expiration ? fmtDateShort(k.expiration) : '<span style="color:var(--muted)">—</span>'}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="copyText('${esc(k.key)}')" title="Copy Key">Copy</button>
          ${k.order_id ? `<button class="btn-icon" onclick="openOrderDetail('${esc(k.order_id)}')">View Order</button>` : ''}
          ${k.customer_email || k.customer ? `<button class="btn-icon" onclick="viewCustomerByEmail('${esc(k.customer_email || k.customer)}')">View Customer</button>` : ''}
          <button class="btn-icon" onclick="openExtendKey('${esc(k.key)}')">Extend</button>
          <button class="btn-icon" onclick="resetHwidForKey('${esc(k.key)}')">Reset HWID</button>
          ${k.status === 'revoked' ? `<button class="btn-icon btn-icon--green" onclick="reactivateKey('${esc(k.key)}')">Reactivate</button>` : `<button class="btn-icon btn-icon--danger" onclick="revokeKey('${esc(k.key)}')">Revoke</button>`}
          ${k.order_id ? `<button class="btn-icon" onclick="downloadInvoice('${esc(k.order_id)}')">Invoice</button>` : ''}
        </td>
      </tr>
    `).join('');
    renderPagination('clPagination', safeLic.length, page, p => renderCustomerLicenses(safeLic, p));
  }

  window.resetHwidForKey = function (key) {
    confirmAction('Reset HWID', `Clear HWID for key ${key}?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, {
        method: 'PATCH',
        body: JSON.stringify({ hwid: '' }),
      });
      if (ok) { toast('HWID cleared.', 'success'); loadCustomerLicenses(); }
      else toast(data.error || 'Reset failed.', 'error');
    });
  };

  function exportClCSV () {
    if (!_allCustomerLicenses.length) { toast('No licenses to export.', 'warning'); return; }
    const cols = ['key','plan','status','customer_email','assigned_user','order_id','purchase_date','hwid','expiration'];
    const rows = [cols.join(',')].concat(_allCustomerLicenses.map(k =>
      cols.map(c => `"${(k[c] || '').toString().replace(/"/g, '""')}"`).join(',')
    ));
    dlCSV('ghost_customer_licenses.csv', rows.join('\n'));
  }

  /* ── Key actions (global) ──────────────────────────────────────────── */
  window.activateKey = function (key) {
    confirmAction('Activate Key', `Mark key ${key} as activated?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'activated' }),
      });
      if (ok) { toast('Key marked as activated.', 'success'); loadInventory(currentInvFilter()); }
      else toast(data.error || 'Activate failed.', 'error');
    });
  };

  window.revokeKey = function (key) {
    confirmAction('Revoke Key', `Revoke key ${key}? It will remain in the database marked as revoked.`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}/revoke`, { method: 'POST' });
      if (ok) { toast('Key revoked.', 'success'); loadInventory(currentInvFilter()); }
      else toast(data.error || 'Revoke failed.', 'error');
    });
  };

  window.reactivateKey = function (key) {
    confirmAction('Reactivate Key', `Reactivate key ${key}? Status will be changed back to activated.`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/inventory/${encodeURIComponent(key)}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'activated' }),
      });
      if (ok) { toast('Key reactivated.', 'success'); loadCustomerLicenses(); }
      else toast(data.error || 'Reactivate failed.', 'error');
    });
  };

  window.viewCustomerByEmail = function (email) {
    switchTab('customers');
    const searchEl = $('customerSearch');
    if (searchEl) { searchEl.value = email; }
    loadCustomers(email);
  };

  window.downloadInvoice = async function (orderId) {
    try {
      const { ok, data } = await apiFetch(`/api/admin/orders/${encodeURIComponent(orderId)}`);
      if (!ok) { toast('Could not load order for invoice.', 'error'); return; }
      const o = data.order || data;
      const lines = [
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        '              GHOST — INVOICE',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        `Invoice ID  : ${o.order_id}`,
        `Date        : ${fmtDate(o.created_at)}`,
        `Customer    : ${o.email || '—'}`,
        `Discord     : ${o.discord || '—'}`,
        `Plan        : ${o.plan_label || o.plan || '—'}`,
        `Amount      : $${parseFloat(o.price_usd || 0).toFixed(2)} ${o.currency || 'USD'}`,
        `Payment     : ${o.payment_status || '—'}`,
        `Delivery    : ${o.delivery_status || '—'}`,
        `License Key : ${o.license_key || 'Not assigned'}`,
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        'ghost.gg — Thank you for your purchase.',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
      ];
      const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href     = url;
      a.download = `ghost-invoice-${o.order_id}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast('Invoice downloaded.', 'success');
    } catch (_) {
      toast('Invoice download failed.', 'error');
    }
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
      // Normalize: customers must always be an array
      const rawCust = data.customers;
      _allCustomers = Array.isArray(rawCust) ? rawCust : [];
      renderCustomers(_allCustomers, 1);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="8" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderCustomers (customers, page) {
    _custPage = page;
    const tb    = $('customersTbody');
    // Defensive: customers must be an array before slice/map
    const safeCust = Array.isArray(customers) ? customers : [];
    const start = (page - 1) * PAGE_SIZE;
    const slice = safeCust.slice(start, start + PAGE_SIZE);

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

    renderPagination('customerPagination', safeCust.length, page, p => renderCustomers(safeCust, p));
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
      const v = data.current_version || data.version || '';
      setText('dlVersion',     v || 'Not set');
      setText('dlReleaseDate', data.release_date || '—');
      setText('dlFilename',    data.filename     || 'GhostConfig.exe');
      setText('dlPlatform',    data.platform     || 'Windows x64');
      setText('dlUrl',         data.url || data.download_url || '/downloads/GhostConfig-v2.exe');
      setText('dlCount',       data.download_count ?? 0);
      $('dlChangelog').value = data.changelog || '';
      $('dlVersionBadge').textContent = v || 'None';

      // History
      const hist = $('dlHistory');
      // Normalize: history must be an array before .reverse().map()
      const history = Array.isArray(data.history) ? data.history : [];
      if (!history.length) {
        hist.innerHTML = '<p class="admin-table-empty">No previous versions.</p>';
      } else {
        hist.innerHTML = history.slice().reverse().map(h => `
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
      url:             ($('dlNewUrl').value.trim()) || '/downloads/GhostConfig-v2.exe',
      filename:        ($('dlNewFilename').value.trim()) || 'GhostConfig.exe',
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

  /* ── Fulfillment Diagnostics ────────────────────────────────────────── */
  async function loadFulfillmentDiag () {
    const tb    = $('fulfillmentDiagTbody');
    const alert = $('fulfillmentDiagAlert');
    if (tb) tb.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">Loading…</td></tr>';
    if (alert) alert.style.display = 'none';
    try {
      const { ok, data } = await apiFetch('/api/admin/fulfillment-log?limit=20');
      if (!ok) throw new Error(data.error || 'Failed to load fulfillment log');

      const attempts = (data.attempts || []).slice().reverse(); // newest first

      if (data.note && alert) {
        alert.textContent = data.note;
        alert.className   = 'admin-alert admin-alert--warn';
        alert.style.display = '';
      }

      if (!attempts.length) {
        if (tb) tb.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No fulfillment attempts recorded yet.</td></tr>';
        return;
      }

      const OUTCOME_COLORS = {
        delivered:   'var(--green)',
        idempotent:  '#6366f1',
        out_of_stock:'#f59e0b',
        failed:      '#f87171',
      };

      if (tb) tb.innerHTML = attempts.map(a => {
        const color  = OUTCOME_COLORS[a.outcome] || 'var(--muted)';
        const ts     = a.timestamp ? fmtDate(a.timestamp) : '—';
        const orderId = (a.order_id || '').slice(-12) || '—'; // last 12 chars for readability
        return `<tr>
          <td class="cell-mono-sm" style="white-space:nowrap">${esc(ts)}</td>
          <td class="cell-mono-sm" title="${esc(a.order_id||'')}">${esc(orderId)}</td>
          <td>${esc(a.plan || '—')}</td>
          <td style="text-align:center">${a.available_keys ?? '—'}</td>
          <td style="text-align:center">${a.selected_key ? '✓' : '✗'}</td>
          <td><span style="color:${color};font-weight:600">${esc(a.outcome||'—')}</span></td>
          <td class="cell-truncate" title="${esc(a.error||'')}" style="color:#f87171;font-size:12px">${esc((a.error||'').slice(0,80))||'—'}</td>
        </tr>`;
      }).join('');

    } catch (err) {
      if (tb)  tb.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#f87171;padding:24px">${esc(err.message)}</td></tr>`;
    }
  }

  /* ── Settings ───────────────────────────────────────────────────────── */
  async function loadSettings () {
    try {
      const { ok, data } = await apiFetch('/api/admin/settings');
      if (!ok) {
        toast(data.error || 'Failed to load settings.', 'error');
        return;
      }
      const s = data.settings || data || {};
      $('settingSiteName').value        = s.site_name           || '';
      $('settingLogoUrl').value         = s.logo_url            || '';
      $('settingDiscordInvite').value   = s.discord_invite      || '';
      $('settingDownloadUrl').value     = s.download_url        || '';
      $('settingBanner').value          = s.announcement_banner || '';
      $('settingMaintenance').checked   = !!s.maintenance_mode;

      // PayPal config: load from /api/paypal/config (env-based, authoritative) first,
      // then fall back to stored settings only when env vars are absent.
      try {
        const cfgRes  = await fetch('/api/paypal/config');
        const cfgData = await cfgRes.json().catch(() => ({}));
        if (cfgData.configured) {
          // Env var values — show these as the live/active values
          $('settingPaypalClientId').value = cfgData.clientId || '';
          $('settingPaypalEnv').value      = cfgData.environment || 'sandbox';
          // Mark the fields as env-controlled (read-only display)
          const note = $('paypalEnvNote');
          if (note) note.textContent = 'Active: ' + (cfgData.environment === 'live' ? 'Live (Production)' : 'Sandbox (Testing)');
        } else {
          // Env var not set — show whatever is stored in Redis settings
          $('settingPaypalClientId').value = s.paypal_client_id  || '';
          $('settingPaypalEnv').value      = s.paypal_environment || 'sandbox';
        }
      } catch (_) {
        $('settingPaypalClientId').value = s.paypal_client_id  || '';
        $('settingPaypalEnv').value      = s.paypal_environment || 'sandbox';
      }
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
    const clientId = $('settingPaypalClientId').value.trim();
    const env      = $('settingPaypalEnv').value;
    // Save to Redis settings for reference.
    // NOTE: These values only affect checkout when PAYPAL_CLIENT_ID and PAYPAL_ENVIRONMENT
    // are NOT set in Vercel environment variables. When env vars are present, they always
    // take precedence. PAYPAL_CLIENT_SECRET must always be a Vercel env var — never store it here.
    const payload = { paypal_client_id: clientId, paypal_environment: env };
    try {
      const { ok, data } = await apiFetch('/api/admin/settings', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (ok) {
        toast('PayPal settings saved. Note: PAYPAL_CLIENT_ID env var takes precedence if set on the server.', 'success', 5000);
      } else {
        toast(data.error || 'Save failed.', 'error');
      }
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
      day:      ['muted',  '1 Day'],
      '3days':  ['muted',  '3 Days'],
      week:     ['cyan',   '1 Week'],
      month:    ['cyan',   '1 Month'],
      '3months':['purple', '3 Months'],
      // legacy
      pro:      ['cyan',   '1 Month'],
      lifetime: ['purple', '3 Months'],
      trial:    ['muted',  '1 Day'],
    };
    const key = (plan || '').toLowerCase();
    const [cls, label] = map[key] || ['muted', plan || '—'];
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
    try {
      const res  = await fetch('/api/admin/session', { credentials: 'include' });
      const data = await res.json().catch(() => ({}));

      if (res.ok && data.authenticated) {
        _loggedIn        = true;
        _sessionVerified = true;
        hide('adminLoading');
        hide('adminLogin');
        show('adminShell');
        loadDashboard();
      } else {
        _sessionVerified = true;
        hide('adminLoading');
        show('adminLogin');
        const keyField = $('adminApiKey');
        if (keyField) keyField.focus();
      }
    } catch (_) {
      _sessionVerified = true;
      hide('adminLoading');
      show('adminLogin');
    }
  }

  /* ── Wire events ─────────────────────────────────────────────────────── */
  /* ── Generate Keys ──────────────────────────────────────────────────── */

  // State for the current generation session
  let _genLastKeys   = [];  // all generated keys from last successful run
  let _genFilteredKeys = [];
  let _genPage       = 1;

  // Plan label lookup
  const _GEN_PLAN_LABELS = {
    day:      '1 Day',
    '3days':  '3 Days',
    week:     '1 Week',
    month:    '1 Month',
    '3months':'3 Months',
    custom:   'Custom',
  };

  function _genInputs () {
    return {
      plan:       $('genPlan').value,
      quantity:   Math.max(1, Math.min(10000, parseInt($('genQty').value, 10) || 100)),
      prefix:     $('genPrefix').value.trim().toUpperCase() || 'GHOST',
      format:     $('genFormat').value,
      charTypes:  {
        upper:   $('genCharUpper').checked,
        numbers: $('genCharNum').checked,
        symbols: $('genCharSym').checked,
      },
      expiration: $('genExpiry').value,
      notes:      $('genNotes').value.trim(),
    };
  }

  function _genFormatLabel (fmt, prefix) {
    const p = prefix || 'GHOST';
    const map = {
      seg4x4:  `${p}-XXXX-XXXX-XXXX-XXXX`,
      seg3x5:  `${p}-XXXXX-XXXXX-XXXXX`,
      seg1x12: `${p}-XXXXXXXXXXXX`,
      custom:  `${p}-XXXX-XXXX-XXXX-XXXX`,
    };
    return map[fmt] || map['seg4x4'];
  }

  function _genUpdatePreview () {
    const { format, prefix } = _genInputs();
    const el = $('genPreviewKey');
    if (el) el.textContent = _genFormatLabel(format, prefix);
  }

  function _genExpiryLabel (val) {
    const map = { never: 'Never', '1': '1 Day', '7': '7 Days', '30': '30 Days', '90': '90 Days', '365': '365 Days', custom: 'Custom' };
    return map[val] || val;
  }

  function _genSetFormDisabled (disabled) {
    ['genPlan','genQty','genPrefix','genFormat','genExpiry','genNotes'].forEach(id => {
      const el = $(id);
      if (el) el.disabled = disabled;
    });
    ['genCharUpper','genCharNum','genCharSym'].forEach(id => {
      const el = $(id);
      if (el) el.disabled = disabled;
    });
  }

  async function loadGenStats () {
    try {
      const { ok, data } = await apiFetch('/api/admin/inventory/stats');
      if (!ok) return;
      setText('genStatAvailable', (data.available  ?? 0).toLocaleString());
      setText('genStatSold',      ((data.sold ?? 0) + (data.activated ?? 0)).toLocaleString());
      setText('genStatTotal',     (data.total ?? 0).toLocaleString());
    } catch (_) { /* silent */ }
    // Today / Month counters: count from _genLastKeys dates
    _updateGenDateStats();
  }

  function _updateGenDateStats () {
    if (!_genLastKeys.length) {
      // Try existing inventory table data for today/month
      const now = new Date();
      const todayStr  = now.toISOString().slice(0, 10);
      const monthStr  = now.toISOString().slice(0, 7);
      let today = 0, month = 0;
      _allInventory.forEach(k => {
        const d = (k.created || '').slice(0, 10);
        if (d === todayStr) today++;
        if (d.slice(0, 7) === monthStr) month++;
      });
      setText('genStatToday', today.toLocaleString());
      setText('genStatMonth', month.toLocaleString());
      return;
    }
    // Count from last generation batch
    const now = new Date();
    const todayStr = now.toISOString().slice(0, 10);
    const monthStr = now.toISOString().slice(0, 7);
    let today = 0, month = 0;
    _genLastKeys.forEach(k => {
      // generated keys all have today's date
      today++;
      month++;
    });
    setText('genStatToday', today.toLocaleString());
    setText('genStatMonth', month.toLocaleString());
  }

  async function doGenerate () {
    const inputs = _genInputs();

    // Validate char type selection
    if (!inputs.charTypes.upper && !inputs.charTypes.numbers && !inputs.charTypes.symbols) {
      toast('Select at least one character type.', 'error');
      return;
    }
    if (inputs.quantity < 1 || inputs.quantity > 10000) {
      toast('Quantity must be between 1 and 10,000.', 'error');
      return;
    }

    setBusy('genBtn', true);
    _genSetFormDisabled(true);

    try {
      const { ok, data } = await apiFetch('/api/admin/inventory/generate', {
        method: 'POST',
        body: JSON.stringify(inputs),
      }, 1);

      if (!ok) {
        const err = data?.error || 'Generation failed. Check server logs.';
        toast(err, 'error');
        return;
      }

      // Store for download/display
      _genLastKeys = data.keys || [];

      // Re-render the table with the new keys
      _genFilteredKeys = _genLastKeys.map(k => ({
        key:        k,
        plan:       inputs.plan,
        status:     'available',
        created:    new Date().toISOString(),
        expiration: data.expiration || null,
        notes:      inputs.notes || '',
      }));
      _genPage = 1;
      _renderGenTable(_genFilteredKeys, 1);

      // Also refresh global inventory so "Go To Inventory" shows fresh data
      loadInventory();

      // Update stats
      await loadGenStats();

      // Show success modal
      _showGenSuccessModal(data, inputs);

      toast(`${data.generated.toLocaleString()} keys generated and saved.`, 'success');

    } catch (err) {
      toast('Network error during generation. Please try again.', 'error');
    } finally {
      setBusy('genBtn', false);
      _genSetFormDisabled(false);
    }
  }

  function _showGenSuccessModal (data, inputs) {
    setText('genResGenerated',  (data.generated  ?? 0).toLocaleString());
    setText('genResDuplicates', (data.duplicates  ?? 0).toLocaleString());
    setText('genResInventory',  (data.availableInventory ?? '—').toLocaleString());
    setText('genResPlan',       _GEN_PLAN_LABELS[inputs.plan] || inputs.plan);
    setText('genResExpiry',     _genExpiryLabel(inputs.expiration));
    setText('genResPrefix',     data.prefix || inputs.prefix);

    // Key preview list (first 20)
    const list = $('genResKeyPreview');
    if (list) {
      list.innerHTML = '';
      const sample = _genLastKeys.slice(0, 20);
      sample.forEach(k => {
        const code = document.createElement('code');
        code.textContent = k;
        list.appendChild(code);
      });
      if (_genLastKeys.length > 20) {
        const more = document.createElement('p');
        more.className = 'gen-key-preview-more';
        more.textContent = `… and ${(_genLastKeys.length - 20).toLocaleString()} more`;
        list.appendChild(more);
      }
    }

    show('genSuccessModal');
  }

  function _genDownloadTXT () {
    if (!_genLastKeys.length) { toast('No keys to download.', 'warning'); return; }
    dlCSV('ghost-keys.txt', _genLastKeys.join('\n'));
  }

  function _genDownloadCSV () {
    if (!_genLastKeys.length) { toast('No keys to download.', 'warning'); return; }
    const rows = ['License Key,Plan,Status,Created'];
    const plan = $('genPlan')?.value || 'month';
    const now  = new Date().toISOString();
    _genLastKeys.forEach(k => {
      rows.push(`${k},${plan},available,${now}`);
    });
    dlCSV('ghost-keys.csv', rows.join('\n'));
  }

  function _genCopyAll () {
    if (!_genLastKeys.length) { toast('No keys to copy.', 'warning'); return; }
    copyText(_genLastKeys.join('\n'));
    toast(`${_genLastKeys.length.toLocaleString()} keys copied to clipboard.`, 'success');
  }

  function _renderGenTable (keys, page) {
    const tbody = $('genTableTbody');
    const footer = $('genTableFooter');
    if (!tbody) return;

    if (!keys.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="admin-table-empty">No matching keys found.</td></tr>';
      if (footer) footer.innerHTML = '';
      return;
    }

    const start = (page - 1) * PAGE_SIZE;
    const slice = keys.slice(start, start + PAGE_SIZE);

    tbody.innerHTML = slice.map(k => `
      <tr>
        <td><span class="key-mono key-mono--sm">${esc(k.key || '')}</span></td>
        <td>${esc(_GEN_PLAN_LABELS[k.plan] || k.plan || '—')}</td>
        <td>${statusBadge(k.status || 'available')}</td>
        <td class="cell-mono-sm">${fmtDateShort(k.created)}</td>
        <td class="cell-mono-sm">${k.expiration ? fmtDateShort(k.expiration) : 'Never'}</td>
        <td class="cell-truncate">${esc(k.notes || '—')}</td>
      </tr>
    `).join('');

    renderPagination('genTableFooter', keys.length, page, p => {
      _genPage = p;
      _renderGenTable(_genFilteredKeys, p);
    });
  }

  function _applyGenSearch () {
    const q = ($('genSearch')?.value || '').trim().toLowerCase();
    if (!q) {
      _genFilteredKeys = _genLastKeys.map(k => ({
        key: k, plan: $('genPlan')?.value || '', status: 'available',
        created: new Date().toISOString(), expiration: null, notes: $('genNotes')?.value || '',
      }));
    } else {
      _genFilteredKeys = _genFilteredKeys.filter(k =>
        (k.key     || '').toLowerCase().includes(q) ||
        (k.plan    || '').toLowerCase().includes(q) ||
        (k.status  || '').toLowerCase().includes(q) ||
        (k.notes   || '').toLowerCase().includes(q) ||
        (k.created || '').toLowerCase().includes(q)
      );
    }
    _genPage = 1;
    _renderGenTable(_genFilteredKeys, 1);
  }

  function wireGenerate () {
    $('genBtn').addEventListener('click', doGenerate);

    // Live preview on any form change
    ['genPlan','genQty','genPrefix','genFormat','genExpiry'].forEach(id => {
      const el = $(id);
      if (el) el.addEventListener('change', _genUpdatePreview);
    });
    $('genPrefix')?.addEventListener('input', _genUpdatePreview);

    // Search
    $('genSearchBtn')?.addEventListener('click', _applyGenSearch);
    $('genSearch')?.addEventListener('keydown', e => { if (e.key === 'Enter') _applyGenSearch(); });

    // Success modal buttons
    $('closeGenSuccessModal')?.addEventListener('click', () => hide('genSuccessModal'));
    $('closeGenSuccessBtn')?.addEventListener('click',   () => hide('genSuccessModal'));
    $('genSuccessModal')?.addEventListener('click', e => { if (e.target === $('genSuccessModal')) hide('genSuccessModal'); });
    $('genCopyAllBtn')?.addEventListener('click',   _genCopyAll);
    $('genDlTxtBtn')?.addEventListener('click',     _genDownloadTXT);
    $('genDlCsvBtn')?.addEventListener('click',     _genDownloadCSV);
    $('genGoInventoryBtn')?.addEventListener('click', () => {
      hide('genSuccessModal');
      switchTab('inventory');
    });
  }

  /* ── Coupon Management ─────────────────────────────────────────────────── */
  let _allCoupons = [];

  async function loadCoupons () {
    const tb = $('couponsTbody');
    if (!tb) return;
    // Skeleton loader while fetching
    tb.innerHTML = Array(3).fill('<tr>' + Array(9).fill('<td><div class="skel-cell"></div></td>').join('') + '</tr>').join('');
    try {
      const { ok, data } = await apiFetch('/api/admin/coupons');
      if (!ok) throw new Error(data.error || 'Failed to load coupons');
      _allCoupons = Array.isArray(data.coupons) ? data.coupons : [];
      renderCoupons(_allCoupons);
    } catch (err) {
      tb.innerHTML = `<tr><td colspan="9" class="admin-table-empty" style="color:#f87171">${esc(err.message)}</td></tr>`;
    }
  }

  function renderCoupons (coupons) {
    const tb = $('couponsTbody');
    if (!tb) return;
    if (!coupons.length) {
      tb.innerHTML = '<tr><td colspan="9" class="admin-table-empty">No coupons. Create one above.</td></tr>';
      return;
    }
    tb.innerHTML = coupons.map(c => {
      const discLabel = c.discount_type === 'free'
        ? '100% Off'
        : c.discount_type === 'percentage'
          ? `${c.discount_value}%`
          : `$${parseFloat(c.discount_value).toFixed(2)}`;
      const _planMap = { day:'1 Day','3days':'3 Days',week:'1 Week',month:'1 Month','3months':'3 Months',
                         pro:'1 Month',lifetime:'3 Months',trial:'1 Day' };
      const planLabel = c.applies_to === 'all' ? 'All Plans'
        : (_planMap[c.applies_to] || esc(c.applies_to));
      const uses      = c.uses || 0;
      const limit     = c.usage_limit != null ? c.usage_limit : null;
      const remaining = limit != null ? (limit - uses) : '∞';
      const remColor  = (remaining !== '∞' && remaining <= 0) ? 'var(--red)' : (remaining !== '∞' && remaining <= 5) ? 'var(--yellow)' : '';
      const statusBadgeHtml = c.active
        ? '<span class="badge badge--green">Active</span>'
        : '<span class="badge badge--muted">Disabled</span>';
      const expiredSoon = c.expiration_date && new Date(c.expiration_date) < new Date(Date.now() + 3*24*60*60*1000);
      return `<tr>
        <td><span class="key-mono copy-cell" onclick="copyText('${esc(c.code)}')" title="Copy">${esc(c.code)}</span></td>
        <td><span style="font-weight:600;color:${c.discount_type==='free'?'var(--green)':c.discount_type==='percentage'?'var(--cyan)':'var(--gold)'}">${esc(discLabel)}</span></td>
        <td>${planLabel}</td>
        <td>${uses}</td>
        <td style="${remColor?'color:'+remColor:''}">${remaining}</td>
        <td>${c.expiration_date ? `<span ${expiredSoon?'style="color:var(--red)"':''}>${fmtDateShort(c.expiration_date)}</span>` : '<span style="color:var(--muted)">Never</span>'}</td>
        <td>${statusBadgeHtml}</td>
        <td>${c.created_at ? fmtDateShort(c.created_at) : '<span style="color:var(--muted)">—</span>'}</td>
        <td class="actions-cell">
          <button class="btn-icon" onclick="copyText('${esc(c.code)}')" title="Copy">Copy</button>
          <button class="btn-icon" onclick="openEditCoupon(${JSON.stringify(JSON.stringify(c))})" title="Edit">Edit</button>
          <button class="btn-icon" onclick="duplicateCoupon(${JSON.stringify(JSON.stringify(c))})" title="Duplicate">Dupe</button>
          ${c.active
            ? `<button class="btn-icon btn-icon--danger" onclick="setCouponActive('${esc(c.code)}',false)">Disable</button>`
            : `<button class="btn-icon" onclick="setCouponActive('${esc(c.code)}',true)">Enable</button>`}
          <button class="btn-icon btn-icon--danger" onclick="deleteCoupon('${esc(c.code)}')">Delete</button>
        </td>
      </tr>`;
    }).join('');
  }

  /* Quick-create coupon with preset discount */
  async function quickCreateCoupon (pct) {
    const code = 'GHOST' + pct + 'OFF' + Math.random().toString(36).slice(2,6).toUpperCase();
    const payload = {
      code,
      discount_type:  pct === 100 ? 'free' : 'percentage',
      discount_value: pct,
      applies_to:     'all',
      usage_limit:    null,
      active:         true,
      notes:          `Quick-created ${pct}% coupon`,
    };
    try {
      const { ok, data } = await apiFetch('/api/admin/coupons', { method: 'POST', body: JSON.stringify(payload) });
      if (ok) {
        toast(`✔ Coupon ${code} created (${pct === 100 ? 'FREE' : pct + '% off'})`, 'success');
        loadCoupons();
      } else {
        toast(data.error || 'Create failed.', 'error');
      }
    } catch (_) {
      toast('Network error.', 'error');
    }
  }

  window.duplicateCoupon = function (jsonStr) {
    const c = JSON.parse(jsonStr);
    _couponModalClear();
    $('couponModalTitle').textContent = 'Duplicate Coupon';
    $('couponCode').value            = c.code + '_COPY';
    $('couponCode').disabled         = false;
    $('couponDiscountType').value    = c.discount_type || 'percentage';
    $('couponDiscountValue').value   = c.discount_type === 'free' ? '' : (c.discount_value || '');
    $('couponAppliesTo').value       = c.applies_to || 'all';
    $('couponUsageLimit').value      = c.usage_limit != null ? c.usage_limit : '';
    $('couponNotes').value           = 'Copy of ' + c.code;
    $('couponActive').checked        = true;
    window._couponTypeChange();
    show('couponModal');
  };

  function _couponModalClear () {
    $('couponEditCode').value        = '';
    $('couponCode').value            = '';
    $('couponCode').disabled         = false;
    $('couponDiscountType').value    = 'percentage';
    $('couponDiscountValue').value   = '';
    $('couponAppliesTo').value       = 'all';
    $('couponUsageLimit').value      = '';
    $('couponUsagePerCustomer').value= '';
    $('couponStartDate').value       = '';
    $('couponExpirationDate').value  = '';
    $('couponNotes').value           = '';
    $('couponActive').checked        = true;
    window._couponTypeChange();
    hideAlert('couponModalAlert');
  }

  window._couponTypeChange = function () {
    const type  = $('couponDiscountType')?.value;
    const group = $('couponValueGroup');
    const label = $('couponValueLabel');
    const input = $('couponDiscountValue');
    if (!type) return;
    if (type === 'free') {
      if (group) group.style.display = 'none';
    } else {
      if (group) group.style.display = '';
      if (label) label.firstChild.textContent = type === 'percentage' ? 'Percentage (0–100) ' : 'Fixed Amount ($) ';
      if (input) { input.placeholder = type === 'percentage' ? '20' : '5.00'; }
    }
  };

  function openCreateCoupon () {
    _couponModalClear();
    $('couponModalTitle').textContent = 'Create Coupon';
    show('couponModal');
  }

  window.openEditCoupon = function (jsonStr) {
    const c = JSON.parse(jsonStr);
    _couponModalClear();
    $('couponModalTitle').textContent   = 'Edit Coupon';
    $('couponEditCode').value           = c.code;
    $('couponCode').value               = c.code;
    $('couponCode').disabled            = true;   // code is the primary key
    $('couponDiscountType').value       = c.discount_type || 'percentage';
    $('couponDiscountValue').value      = c.discount_type === 'free' ? '' : (c.discount_value || '');
    $('couponAppliesTo').value          = c.applies_to || 'all';
    $('couponUsageLimit').value         = c.usage_limit != null ? c.usage_limit : '';
    $('couponUsagePerCustomer').value   = c.usage_per_customer != null ? c.usage_per_customer : '';
    $('couponStartDate').value          = (c.start_date      || '').slice(0, 10);
    $('couponExpirationDate').value     = (c.expiration_date || '').slice(0, 10);
    $('couponNotes').value              = c.notes || '';
    $('couponActive').checked           = Boolean(c.active);
    window._couponTypeChange();
    show('couponModal');
  };

  async function saveCoupon () {
    hideAlert('couponModalAlert');
    const editCode    = $('couponEditCode').value;
    const isEdit      = Boolean(editCode);
    const code        = $('couponCode').value.trim().toUpperCase().replace(/[^A-Z0-9_-]/g, '');
    const discountType  = $('couponDiscountType').value;
    const discountValue = $('couponDiscountValue').value;
    const appliesTo     = $('couponAppliesTo').value;
    const usageLimit    = $('couponUsageLimit').value   ? parseInt($('couponUsageLimit').value, 10)   : null;
    const usagePerCust  = $('couponUsagePerCustomer').value ? parseInt($('couponUsagePerCustomer').value, 10) : null;
    const startDate     = $('couponStartDate').value    || null;
    const expirationDate= $('couponExpirationDate').value || null;
    const notes         = $('couponNotes').value.trim();
    const active        = $('couponActive').checked;

    if (!code) { showAlert('couponModalAlert', 'error', 'Code is required.'); return; }
    if (discountType !== 'free' && (!discountValue || parseFloat(discountValue) <= 0)) {
      showAlert('couponModalAlert', 'error', 'Discount value must be greater than zero.'); return;
    }

    setBusy('saveCouponBtn', true);
    try {
      const payload = {
        code, discount_type: discountType,
        discount_value: discountType === 'free' ? 100 : parseFloat(discountValue),
        applies_to: appliesTo, usage_limit: usageLimit,
        usage_per_customer: usagePerCust, start_date: startDate,
        expiration_date: expirationDate, active, notes,
      };
      const endpoint = isEdit
        ? `/api/admin/coupons/${encodeURIComponent(editCode)}`
        : '/api/admin/coupons';
      const method = isEdit ? 'PATCH' : 'POST';
      const { ok, data } = await apiFetch(endpoint, { method, body: JSON.stringify(payload) });
      if (ok) {
        hide('couponModal');
        toast(isEdit ? 'Coupon updated.' : 'Coupon created.', 'success');
        loadCoupons();
      } else {
        showAlert('couponModalAlert', 'error', data.error || 'Save failed.');
      }
    } catch (_) {
      showAlert('couponModalAlert', 'error', 'Network error.');
    } finally {
      setBusy('saveCouponBtn', false);
    }
  }

  window.setCouponActive = function (code, active) {
    const msg = active ? `Enable coupon ${code}?` : `Disable coupon ${code}?`;
    confirmAction(active ? 'Enable Coupon' : 'Disable Coupon', msg, async () => {
      const { ok, data } = await apiFetch(`/api/admin/coupons/${encodeURIComponent(code)}`, {
        method: 'PATCH', body: JSON.stringify({ active }),
      });
      if (ok) { toast(`Coupon ${active ? 'enabled' : 'disabled'}.`, 'success'); loadCoupons(); }
      else toast(data.error || 'Update failed.', 'error');
    });
  };

  window.deleteCoupon = function (code) {
    confirmAction('Delete Coupon', `Permanently delete coupon ${code}?`, async () => {
      const { ok, data } = await apiFetch(`/api/admin/coupons/${encodeURIComponent(code)}`, { method: 'DELETE' });
      if (ok) { toast('Coupon deleted.', 'success'); loadCoupons(); }
      else toast(data.error || 'Delete failed.', 'error');
    });
  };

  function wireCoupons () {
    $('openCreateCouponBtn')?.addEventListener('click', openCreateCoupon);
    $('closeCouponModal')?.addEventListener('click', () => hide('couponModal'));
    $('cancelCouponModal')?.addEventListener('click', () => hide('couponModal'));
    $('saveCouponBtn')?.addEventListener('click', saveCoupon);
    $('couponModal')?.addEventListener('click', e => { if (e.target === $('couponModal')) hide('couponModal'); });
    // Quick-create preset buttons
    document.querySelectorAll('[data-quick-coupon]').forEach(btn => {
      btn.addEventListener('click', () => quickCreateCoupon(parseInt(btn.dataset.quickCoupon, 10)));
    });
  }

  /* ── Global Search ──────────────────────────────────────────────────── */
  let _globalSearchTimer = null;

  function wireGlobalSearch () {
    const input = $('globalSearch');
    if (!input) return;

    input.addEventListener('input', () => {
      clearTimeout(_globalSearchTimer);
      _globalSearchTimer = setTimeout(() => runGlobalSearch(input.value.trim()), 220);
    });

    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { input.value = ''; hideGlobalResults(); }
    });

    document.addEventListener('click', e => {
      if (!e.target.closest('.global-search-wrap')) hideGlobalResults();
    });
  }

  function hideGlobalResults () {
    const box = $('globalSearchResults');
    if (box) box.style.display = 'none';
  }

  async function runGlobalSearch (q) {
    const box = $('globalSearchResults');
    if (!box) return;
    if (!q || q.length < 2) { hideGlobalResults(); return; }

    box.style.display = '';
    box.innerHTML = '<div class="gs-loading">Searching…</div>';

    try {
      // Search local data first, then fetch orders as fallback
      let orders = _allOrders.length ? _allOrders : [];
      if (!orders.length) {
        const { ok, data } = await apiFetch('/api/admin/orders');
        if (ok) orders = Array.isArray(data.orders) ? data.orders : [];
      }

      const ql = q.toLowerCase();
      const matches = orders.filter(o =>
        (o.order_id    || '').toLowerCase().includes(ql) ||
        (o.email       || '').toLowerCase().includes(ql) ||
        (o.discord     || '').toLowerCase().includes(ql) ||
        (o.license_key || '').toLowerCase().includes(ql)
      ).slice(0, 8);

      // Also search coupons
      const couponMatches = _allCoupons.filter(c =>
        (c.code || '').toLowerCase().includes(ql)
      ).slice(0, 3);

      if (!matches.length && !couponMatches.length) {
        box.innerHTML = '<div class="gs-empty">No results for "' + esc(q) + '"</div>';
        return;
      }

      const orderHtml = matches.map(o => `
        <div class="gs-item" onclick="switchTab('orders');openOrderDetail('${esc(o.order_id)}');hideGlobalResults()">
          <div class="gs-item-main">${esc(o.email || o.discord || '—')}</div>
          <div class="gs-item-sub">${esc((o.order_id||'').slice(0,20))} · ${planBadge(o.plan)}</div>
        </div>`).join('');

      const couponHtml = couponMatches.map(c => `
        <div class="gs-item" onclick="switchTab('coupons');hideGlobalResults()">
          <div class="gs-item-main">${esc(c.code)}</div>
          <div class="gs-item-sub">Coupon · ${esc(c.discount_type === 'free' ? '100% off' : (c.discount_value + (c.discount_type==='percentage'?'%':'$')))}</div>
        </div>`).join('');

      box.innerHTML = (matches.length ? '<div class="gs-section-title">Orders</div>' + orderHtml : '') +
        (couponMatches.length ? '<div class="gs-section-title">Coupons</div>' + couponHtml : '');

    } catch (_) {
      box.innerHTML = '<div class="gs-empty">Search error</div>';
    }
  }

  window.hideGlobalResults = hideGlobalResults;

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

    wireGenerate();
    wireInvStatusTabs();
    wireCoupons();
    wireGlobalSearch();
    wireReleases();

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

    // Customer Licenses
    $('refreshCustomerLicenses')?.addEventListener('click', loadCustomerLicenses);
    $('applyClFilter')?.addEventListener('click', applyClFilter);
    $('clSearch')?.addEventListener('keydown', e => { if (e.key === 'Enter') applyClFilter(); });
    $('clFilterPlan')?.addEventListener('change', applyClFilter);
    $('clFilterStatus')?.addEventListener('change', applyClFilter);
    $('exportClBtn')?.addEventListener('click', exportClCSV);

    // Orders
    $('refreshOrders').addEventListener('click', loadOrders);
    $('fulfillPendingBtn')?.addEventListener('click', fulfillPendingOrders);
    $('applyOrderFilter').addEventListener('click', applyOrderFilter);
    $('orderSearch').addEventListener('keydown', e => { if (e.key === 'Enter') applyOrderFilter(); });
    $('orderStatusFilter').addEventListener('change', applyOrderFilter);
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

    // Fulfillment Diagnostics
    if ($('refreshFulfillmentDiag')) {
      $('refreshFulfillmentDiag').addEventListener('click', loadFulfillmentDiag);
    }

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

  /* ── Release Manager ────────────────────────────────────────────────── */
  let _allReleases   = [];
  let _relFilterMode = 'all';
  let _uploadedRelUrl    = '';
  let _uploadedRelSha256 = '';
  let _uploadedRelSize   = 0;

  // Utility: human-readable file size
  function _fmtBytes (b) {
    if (!b) return '—';
    if (b >= 1_048_576) return (b / 1_048_576).toFixed(1) + ' MB';
    if (b >= 1024)      return (b / 1024).toFixed(0) + ' KB';
    return b + ' B';
  }

  // Utility: channel colour
  function _chColor (ch) {
    if (ch === 'beta')        return 'var(--cyan)';
    if (ch === 'development') return 'var(--purple)';
    return 'var(--green)';
  }

  async function loadReleases () {
    const [relRes, statsRes] = await Promise.all([
      apiFetch('/api/admin/releases'),
      apiFetch('/api/admin/releases/stats'),
    ]);
    if (!relRes.ok) { showAlert('releasesAlert', 'error', relRes.data.error || 'Failed to load releases.'); return; }
    _allReleases = relRes.data.releases || [];
    renderReleasesTable(_allReleases);
    updateReleaseSummary(_allReleases, relRes.data);
    if (statsRes.ok) renderVersionDistribution(statsRes.data);
    updateCurrentReleaseCard(_allReleases);
    loadSilentUpdateSetting();
  }

  function updateReleaseSummary (releases) {
    const stableCurrent = releases.find(r => r.channel === 'stable'      && r.current && !r.disabled);
    const betaCurrent   = releases.find(r => r.channel === 'beta'        && r.current && !r.disabled);
    const devCurrent    = releases.find(r => r.channel === 'development' && r.current && !r.disabled);
    const totalDl       = releases.reduce((s, r) => s + (r.downloads || 0), 0);
    setText('relStableVer', stableCurrent ? stableCurrent.version : '—');
    setText('relBetaVer',   betaCurrent   ? betaCurrent.version   : '—');
    setText('relDevVer',    devCurrent    ? devCurrent.version    : '—');
    setText('relTotalDl',   totalDl.toLocaleString());
  }

  function updateCurrentReleaseCard (releases) {
    // Show info for the stable current release (highest priority)
    const current = releases.find(r => r.channel === 'stable' && r.current && !r.disabled)
                 || releases.find(r => r.current && !r.disabled);
    if (!current) return;
    setText('relCurVersion',   current.version || '—');
    setText('relCurPublished', current.released_at ? current.released_at.slice(0, 10) : '—');
    setText('relCurDownloads', (current.downloads || 0).toLocaleString());
    setText('relCurSize',      _fmtBytes(current.fileSize));
    setText('relCurMinVer',    current.minVersion || 'Any');
    setText('relCurSha',       current.sha256 || '—');
    const ch = $('relCurrentChannel');
    if (ch) { ch.textContent = (current.channel || 'stable').toUpperCase(); ch.style.color = _chColor(current.channel); }
    // Release notes
    const ul = $('relCurNotes');
    if (ul) {
      const notes = current.releaseNotes || [];
      ul.innerHTML = notes.length
        ? notes.map(n => `<li>${esc(n)}</li>`).join('')
        : '<li style="color:var(--muted)">No release notes.</li>';
    }
  }

  function renderVersionDistribution (stats) {
    const wrap = $('relDistribution');
    if (!wrap) return;
    const dist = stats.distribution || [];
    if (!dist.length) { wrap.innerHTML = '<p style="color:var(--muted);font-size:13px">No download data yet.</p>'; return; }
    const total = stats.total_downloads || 0;
    wrap.innerHTML = dist.slice(0, 10).map(d => {
      const pct = d.percent || 0;
      const bar = Math.round(pct);
      return `
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          <code style="width:70px;font-size:12px;flex-shrink:0">${esc(d.version)}</code>
          <span style="width:68px;font-size:11px;color:${_chColor(d.channel)};flex-shrink:0">${esc(d.channel)}</span>
          <div style="flex:1;height:12px;background:var(--surface-3);border-radius:6px;overflow:hidden">
            <div style="height:100%;width:${bar}%;background:${_chColor(d.channel)};border-radius:6px;transition:width .4s"></div>
          </div>
          <span style="width:54px;text-align:right;font-size:12px;color:var(--muted)">${pct}%</span>
          <span style="width:64px;text-align:right;font-size:12px;color:var(--muted)">${(d.downloads||0).toLocaleString()} dl</span>
          ${d.current ? '<span style="font-size:10px;color:var(--green);font-weight:700">● current</span>' : '<span style="width:52px"></span>'}
        </div>`;
    }).join('');
  }

  function renderReleasesTable (releases) {
    const tbody = $('releasesTbody');
    if (!tbody) return;
    let filtered = releases;
    if (_relFilterMode !== 'all') filtered = filtered.filter(r => r.channel === _relFilterMode);
    // Sort newest released_at first
    filtered = [...filtered].sort((a, b) => (b.released_at || '').localeCompare(a.released_at || ''));

    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="admin-table-empty">No releases found.</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(r => {
      const chColor = _chColor(r.channel);
      const statusBadge = r.disabled
        ? `<span style="color:var(--red);font-weight:600">Disabled</span>`
        : r.current
          ? `<span style="color:var(--green);font-weight:600">● Current</span>`
          : `<span style="color:var(--muted)">Inactive</span>`;
      const mandBadge = r.mandatory
        ? `<span style="color:var(--red);font-size:11px;font-weight:700">REQUIRED</span>`
        : `<span style="color:var(--muted);font-size:11px">—</span>`;
      const relDate = r.released_at ? r.released_at.slice(0, 10) : '—';
      const sizeStr = _fmtBytes(r.fileSize);

      return `<tr>
        <td><code style="font-size:12px">${esc(r.version)}</code>${r.minVersion ? `<br><span style="font-size:10px;color:var(--muted)">min: ${esc(r.minVersion)}</span>` : ''}</td>
        <td><span style="color:${chColor};font-weight:600;text-transform:capitalize">${esc(r.channel)}</span></td>
        <td>${relDate}</td>
        <td>${(r.downloads||0).toLocaleString()}</td>
        <td style="font-size:11px;color:var(--muted)">${sizeStr}</td>
        <td>${mandBadge}</td>
        <td>${statusBadge}</td>
        <td style="white-space:nowrap;display:flex;gap:6px;flex-wrap:wrap">
          ${!r.current && !r.disabled ? `<button class="btn btn-ghost btn--sm" onclick="relSetCurrent(${r.id})">Set Current</button>` : ''}
          <button class="btn btn-ghost btn--sm" onclick="relEditNotes(${r.id})">Edit Notes</button>
          ${!r.disabled
            ? `<button class="btn btn-ghost btn--sm" style="color:var(--red)" onclick="relDisable(${r.id})">Disable</button>`
            : `<button class="btn btn-ghost btn--sm" onclick="relRollback(${r.id})">Roll Back</button>`}
        </td>
      </tr>`;
    }).join('');
  }

  window._relFilter = function (mode) {
    _relFilterMode = mode;
    // Update button active states
    ['relFilterAll','relFilterStable','relFilterBeta','relFilterDev'].forEach(id => {
      const btn = $(id);
      if (!btn) return;
      const modeMap = { relFilterAll:'all', relFilterStable:'stable', relFilterBeta:'beta', relFilterDev:'development' };
      btn.className = modeMap[id] === mode ? 'btn btn-primary btn--sm' : 'btn btn-ghost btn--sm';
    });
    renderReleasesTable(_allReleases);
  };

  /* ── Upload + auto-publish flow ────────────────────────────────────── */
  //
  // Production upload architecture (no HTTP 413, no file size limits):
  //
  //   Step 1 — Hash (browser, no network):
  //     Compute SHA-256 of the EXE using SubtleCrypto while showing progress.
  //
  //   Step 2 — Get presigned URL (tiny JSON request):
  //     GET /api/admin/releases/upload-url?version=…&channel=…&filename=…
  //     Returns { uploadUrl, downloadUrl, filename }.
  //     uploadUrl is a short-lived R2/S3 presigned PUT URL.
  //     The EXE never passes through Vercel or the API server.
  //
  //   Step 3 — Upload directly to R2 (browser → R2, no proxy):
  //     PUT uploadUrl  Content-Type: application/octet-stream
  //     XHR progress events drive the progress bar.
  //     No Vercel. No 4.5 MB limit. No HTTP 413.
  //
  //   Step 4 — Finalize (tiny JSON request):
  //     POST /api/admin/releases/finalize-upload { version, channel, sha256, fileSize, … }
  //     API creates the release record pointing at the CDN URL.
  //     /download/latest now redirects to the CDN. Auto-updater picks it up.

  function wireUploadZone () {
    const input    = $('relFileInput');
    const dropZone = $('relDropZone');
    const label    = $('relDropLabel');
    if (!input) return;

    function handleFile (file) {
      if (!file) return;
      if (!file.name.toLowerCase().endsWith('.exe')) {
        toast('Only .exe files are accepted.', 'error'); return;
      }
      if (label) label.textContent = `✓  ${file.name}  (${_fmtBytes(file.size)})`;
      if (dropZone) dropZone.style.borderColor = 'var(--accent)';
      // Clear any old SHA-256 from a previous file
      if ($('relUploadSha256')) $('relUploadSha256').value = '';
    }

    input.addEventListener('change', () => handleFile(input.files?.[0]));
    if (dropZone) {
      dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
      dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = 'var(--border-2)'; });
      dropZone.addEventListener('drop', e => {
        e.preventDefault();
        const file = e.dataTransfer.files[0];
        if (file) { input.files = e.dataTransfer.files; handleFile(file); }
      });
    }
  }

  /* Compute SHA-256 of a File using SubtleCrypto (streams through 1 MB chunks
     to keep the UI responsive on large files).  Returns lowercase hex string. */
  async function _computeSha256 (file, onProgress) {
    const chunkSize = 1024 * 1024; // 1 MB
    const totalChunks = Math.ceil(file.size / chunkSize);
    // Use streaming incremental hashing via SubtleCrypto digest on chunks
    // (SubtleCrypto doesn't expose incremental API, so we collect all ArrayBuffers
    //  then digest — this is still non-blocking because we yield between chunks)
    const buffers = [];
    let loaded = 0;
    for (let i = 0; i < totalChunks; i++) {
      const start = i * chunkSize;
      const end   = Math.min(start + chunkSize, file.size);
      const buf   = await file.slice(start, end).arrayBuffer();
      buffers.push(buf);
      loaded += (end - start);
      if (onProgress) onProgress(loaded, file.size);
      // Yield to keep UI responsive
      await new Promise(r => setTimeout(r, 0));
    }
    // Concatenate all chunks
    const total  = buffers.reduce((s, b) => s + b.byteLength, 0);
    const merged = new Uint8Array(total);
    let offset   = 0;
    for (const b of buffers) { merged.set(new Uint8Array(b), offset); offset += b.byteLength; }
    const hash = await crypto.subtle.digest('SHA-256', merged.buffer);
    return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  async function relUploadAndPublish () {
    const file    = $('relFileInput')?.files?.[0];
    const version = $('relUploadVersion')?.value.trim();
    if (!file)    { showAlert('relUploadAlert', 'error', 'Select a GhostConfig.exe first.'); return; }
    if (!version) { showAlert('relUploadAlert', 'error', 'Enter a version number (e.g. 2.5.0).'); return; }

    const channel    = $('relUploadChannel')?.value || 'stable';
    const mandatory  = !!$('relUploadMandatory')?.checked;
    const setCurrent = $('relUploadSetCurrent')?.checked !== false;
    const minVersion = $('relUploadMinVer')?.value.trim() || '';
    const notes = ($('relUploadNotes')?.value || '')
      .split('\n').map(s => s.replace(/^[•·\-]\s*/, '').trim()).filter(Boolean);

    const progressWrap = $('relUploadProgress');
    const progressBar  = $('relUploadProgressBar');
    const progressLbl  = $('relUploadProgressLabel');

    // Helper: update progress UI.  pct is 0–100 for the overall bar.
    function setProgress (label, pct) {
      if (progressWrap) progressWrap.style.display = '';
      if (progressLbl)  progressLbl.textContent = label;
      if (progressBar)  progressBar.style.width = (pct || 0) + '%';
    }

    // Helper: reset UI and surface a stage-specific failure message.
    function _fail (stage, detail) {
      const stageLabel = {
        auth:     'Authentication failed',
        hash:     'SHA-256 computation failed',
        urlFetch: 'Storage upload failed',
        upload:   'Storage upload failed',
        publish:  'Release metadata creation failed',
      }[stage] || 'Upload failed';
      const msg = detail ? `${stageLabel}: ${detail}` : stageLabel;
      showAlert('relUploadAlert', 'error', msg);
      setBusy('relUploadPublishBtn', false);
      if (progressWrap) progressWrap.style.display = 'none';
    }

    setBusy('relUploadPublishBtn', true);
    hideAlert('relUploadAlert');

    // ── Step 1: Compute SHA-256 in the browser ──────────────────────────
    // The EXE binary NEVER leaves the browser during this step — pure Web Crypto.
    // No network request is made here.
    let sha256hex = '';
    try {
      setProgress('Preparing upload…', 0);
      sha256hex = await _computeSha256(file, (loaded, total) => {
        const pct = Math.round(loaded * 100 / total);
        setProgress(`Preparing upload… ${pct}%`, pct * 0.2); // 0–20% of bar
      });
      if ($('relUploadSha256')) $('relUploadSha256').value = sha256hex;
      setProgress('Getting upload URL…', 22);
    } catch (err) {
      _fail('hash', err.message);
      return;
    }

    // ── Step 2: Authenticate + get presigned upload URL from API ────────
    // This request is a tiny GET with query params — no binary data sent to Vercel.
    let uploadUrl, downloadUrl, remoteFilename;
    try {
      const params = new URLSearchParams({ version, channel, filename: file.name });
      const { ok, status, data } = await apiFetch(`/api/admin/releases/upload-url?${params}`);
      if (status === 401 || status === 403) {
        _fail('auth', data.error || 'Admin session expired. Please log in again.');
        return;
      }
      if (!ok) {
        // Surface the exact backend message (e.g. "Object storage is not configured on this server …")
        _fail('urlFetch', data.error || `Server returned HTTP ${status}. Check that GHOST_STORAGE_BUCKET, GHOST_STORAGE_ACCESS_KEY, and GHOST_STORAGE_SECRET_KEY are set on the API server.`);
        return;
      }
      uploadUrl      = data.uploadUrl;
      downloadUrl    = data.downloadUrl;
      remoteFilename = data.filename || file.name;
    } catch (err) {
      _fail('urlFetch', err.message);
      return;
    }

    // ── Step 3: PUT directly to object storage (browser → R2, no proxy) ─
    //
    // THE EXE IS UPLOADED DIRECTLY TO CLOUDFLARE R2 HERE.
    // It never touches the Vercel/Express API server — no HTTP 413 possible.
    // XHR is used so we get upload progress events.
    try {
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', uploadUrl);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.upload.addEventListener('progress', e => {
          if (!e.lengthComputable) return;
          const pct    = Math.round(e.loaded * 100 / e.total);
          // Map 0–100% upload progress to 25–88% of the overall bar
          const barPct = 25 + Math.round(pct * 0.63);
          setProgress(
            `Uploading ${file.name}… ${pct}%`,
            barPct,
          );
        });
        xhr.addEventListener('load', () => {
          // R2 returns 200; many S3-compatible stores return 204
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            reject(new Error(
              `HTTP ${xhr.status} from storage provider. ` +
              'Check R2 CORS policy: allow PUT from your admin origin.'
            ));
          }
        });
        xhr.addEventListener('error', () => reject(new Error('Network error — check CORS policy on the storage bucket')));
        xhr.addEventListener('abort', () => reject(new Error('Upload cancelled')));
        xhr.send(file);
      });
    } catch (err) {
      _fail('upload', err.message);
      return;
    }

    setProgress('Verifying…', 90);
    // Brief pause so "Verifying…" is visible before "Publishing…"
    await new Promise(r => setTimeout(r, 500));
    setProgress('Publishing release…', 93);

    // ── Step 4: Finalize — send ONLY small JSON metadata to the API ─────
    // The EXE is already in R2. This request is a few hundred bytes of JSON.
    // sha256 was computed locally before upload — it is correct for the file.
    try {
      const { ok, status, data } = await apiFetch('/api/admin/releases/finalize-upload', {
        method: 'POST',
        body: JSON.stringify({
          version,
          channel,
          filename:     remoteFilename,
          downloadUrl,
          sha256:       sha256hex,
          fileSize:     file.size,
          releaseNotes: notes,
          mandatory,
          set_current:  setCurrent,
          minVersion,
        }),
      });
      if (status === 401 || status === 403) {
        _fail('auth', data.error || 'Admin session expired. Please log in again.');
        return;
      }
      if (!ok) {
        // File is in R2 but the record was not created — surface the exact reason.
        _fail('publish', data.error ||
          `Server returned HTTP ${status}. The file was uploaded to storage but the release record was not created. ` +
          'You can retry by publishing manually with the "Publish New Release" button.');
        return;
      }
    } catch (err) {
      _fail('publish', err.message);
      return;
    }

    setProgress(`Update v${version} published successfully`, 100);
    setTimeout(() => { if (progressWrap) progressWrap.style.display = 'none'; }, 2500);
    setBusy('relUploadPublishBtn', false);
    toast(`✓  Update v${version} published successfully`, 'success', 5000);

    // Reset form
    if ($('relFileInput'))        $('relFileInput').value = '';
    if ($('relUploadVersion'))    $('relUploadVersion').value = '';
    if ($('relUploadNotes'))      $('relUploadNotes').value = '';
    if ($('relUploadSha256'))     $('relUploadSha256').value = '';
    if ($('relUploadMinVer'))     $('relUploadMinVer').value = '';
    if ($('relUploadMandatory'))  $('relUploadMandatory').checked = false;
    if ($('relUploadSetCurrent')) $('relUploadSetCurrent').checked = true;
    if ($('relDropLabel'))        $('relDropLabel').textContent = 'Drop GhostConfig.exe here or click to browse';
    if ($('relDropZone'))         $('relDropZone').style.borderColor = 'var(--border-2)';
    loadReleases();
  }

  /* ── Silent updates setting ────────────────────────────────────────── */
  async function loadSilentUpdateSetting () {
    const { ok, data } = await apiFetch('/api/admin/settings');
    if (!ok) return;
    const s = data.settings || {};
    const cb = $('relSilentUpdates');
    if (cb) cb.checked = !!s.silent_updates;
  }

  async function saveSilentUpdateSetting () {
    const val = !!$('relSilentUpdates')?.checked;
    const { ok, data } = await apiFetch('/api/admin/settings', {
      method: 'POST',
      body: JSON.stringify({ silent_updates: val }),
    });
    if (ok) toast(`Silent updates ${val ? 'enabled' : 'disabled'}.`, 'success');
    else toast(data.error || 'Save failed.', 'error');
  }

  window.relSetCurrent = async function (id) {
    confirmAction('Set as Current', 'Promote this release to the active version for its channel?', async () => {
      const { ok, data } = await apiFetch(`/api/admin/releases/${id}/set-current`, { method: 'POST' });
      if (ok) { toast('Release set as current.', 'success'); loadReleases(); }
      else toast(data.error || 'Failed.', 'error');
    });
  };

  window.relDisable = async function (id) {
    confirmAction('Disable Release', 'Hide this release from the desktop app update check?', async () => {
      const { ok, data } = await apiFetch(`/api/admin/releases/${id}/disable`, { method: 'POST' });
      if (ok) { toast('Release disabled.', 'success'); loadReleases(); }
      else toast(data.error || 'Failed.', 'error');
    });
  };

  window.relRollback = async function (id) {
    confirmAction('Roll Back', 'Re-enable this release and make it current for clients?', async () => {
      const { ok, data } = await apiFetch('/api/admin/releases/rollback', {
        method: 'POST',
        body: JSON.stringify({ release_id: id }),
      });
      if (ok) { toast('Rolled back successfully.', 'success'); loadReleases(); }
      else toast(data.error || 'Failed.', 'error');
    });
  };

  window.relEditNotes = function (id) {
    const r = _allReleases.find(r => r.id === id);
    if (!r) return;
    $('editRelNotesId').value     = id;
    $('editRelNotesText').value   = (r.releaseNotes || []).join('\n');
    $('editRelMandatory').checked = !!r.mandatory;
    show('editRelNotesModal');
  };

  async function saveEditRelNotes () {
    const id    = parseInt($('editRelNotesId').value, 10);
    const notes = $('editRelNotesText').value.trim().split('\n').map(s => s.trim()).filter(Boolean);
    const mand  = $('editRelMandatory').checked;
    setBusy('saveEditRelNotesBtn', true);
    try {
      const { ok, data } = await apiFetch(`/api/admin/releases/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ releaseNotes: notes, mandatory: mand }),
      });
      if (ok) { toast('Notes updated.', 'success'); hide('editRelNotesModal'); loadReleases(); }
      else showAlert('editRelNotesModal', 'error', data.error || 'Save failed.');
    } catch (_) {
      toast('Network error.', 'error');
    } finally {
      setBusy('saveEditRelNotesBtn', false);
    }
  }

  function openPublishRelease () {
    $('editRelId').value        = '';
    $('relVersion').value       = '';
    $('relChannel').value       = 'stable';
    $('relDownloadUrl').value   = '';
    $('relFilename').value      = 'GhostConfig.exe';
    $('relSha256').value        = '';
    $('relNotes').value         = '';
    $('relMandatory').checked   = false;
    $('relSetCurrent').checked  = true;
    if ($('relMinVersion')) $('relMinVersion').value = '';
    $('publishRelModalTitle').textContent = 'Publish New Release';
    hide('publishRelAlert');
    show('publishReleaseModal');
  }

  async function savePublishRelease () {
    const version = $('relVersion').value.trim();
    const url     = $('relDownloadUrl').value.trim();
    if (!version || !url) {
      showAlert('publishRelAlert', 'error', 'Version and Download URL are required.');
      return;
    }
    const notes = $('relNotes').value.trim().split('\n').map(s => s.trim()).filter(Boolean);
    const payload = {
      version,
      downloadUrl:  url,
      filename:     $('relFilename').value.trim() || 'GhostConfig.exe',
      sha256:       $('relSha256').value.trim().toLowerCase(),
      releaseNotes: notes,
      mandatory:    $('relMandatory').checked,
      channel:      $('relChannel').value,
      set_current:  $('relSetCurrent').checked,
      minVersion:   ($('relMinVersion')?.value || '').trim(),
    };
    setBusy('savePublishReleaseBtn', true);
    hideAlert('publishRelAlert');
    try {
      const { ok, data } = await apiFetch('/api/admin/releases', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (ok) {
        toast(`Release ${version} published.`, 'success');
        hide('publishReleaseModal');
        loadReleases();
      } else {
        showAlert('publishRelAlert', 'error', data.error || 'Publish failed.');
      }
    } catch (_) {
      showAlert('publishRelAlert', 'error', 'Network error.');
    } finally {
      setBusy('savePublishReleaseBtn', false);
    }
  }

  function wireReleases () {
    $('openPublishReleaseBtn')?.addEventListener('click', openPublishRelease);
    $('refreshReleasesBtn')?.addEventListener('click', loadReleases);
    $('closePublishReleaseModal')?.addEventListener('click', () => hide('publishReleaseModal'));
    $('cancelPublishReleaseBtn')?.addEventListener('click', () => hide('publishReleaseModal'));
    $('savePublishReleaseBtn')?.addEventListener('click', savePublishRelease);
    $('publishReleaseModal')?.addEventListener('click', e => {
      if (e.target === $('publishReleaseModal')) hide('publishReleaseModal');
    });
    $('closeEditRelNotesModal')?.addEventListener('click', () => hide('editRelNotesModal'));
    $('cancelEditRelNotes')?.addEventListener('click',    () => hide('editRelNotesModal'));
    $('saveEditRelNotesBtn')?.addEventListener('click', saveEditRelNotes);
    $('editRelNotesModal')?.addEventListener('click', e => {
      if (e.target === $('editRelNotesModal')) hide('editRelNotesModal');
    });
    $('relUploadPublishBtn')?.addEventListener('click', relUploadAndPublish);
    $('saveSilentUpdatesBtn')?.addEventListener('click', saveSilentUpdateSetting);
    wireUploadZone();
  }

  /* ── Init ───────────────────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    wireAll();
    checkExistingSession();
  });

}());
