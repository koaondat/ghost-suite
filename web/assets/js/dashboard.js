/* ============================================================
   dashboard.js — Ghost Customer Dashboard Controller
   ============================================================
   Architecture notes
   ------------------
   • All customer data is loaded through GhostDashboard.loadAccount().
     To connect a real API, replace the stub body of that function.
     Every renderer below consumes the resolved { account } object
     and will work unchanged once a real endpoint is wired in.

   • Auth guard: if no session token is found the user is
     redirected to login.html. Replace _isAuthenticated() with
     a real session / JWT check.

   • Sections are shown/hidden purely via CSS `hidden` attribute.
     No frameworks — vanilla ES6.
   ============================================================ */

(function () {
  'use strict';

  /* ── Placeholder data ──────────────────────────────────────
     Replace this with a real API call in GhostDashboard.loadAccount().
     Structure matches what the renderers expect.
  ─────────────────────────────────────────────────────────── */
  const PLACEHOLDER_ACCOUNT = {
    username:    'phantomUser',
    email:       'phantom@ghost.gg',
    memberSince: '2025-07-12',
    license: {
      key:          'GHOST-K7X2P-M4NR8-QJ6WL-T9YBV',
      tier:         'Pro',               // 'Trial' | 'Basic' | 'Pro' | 'Lifetime'
      status:       'active',            // 'active' | 'expired' | 'trial' | 'banned' | 'revoked' | 'pending'
      activatedAt:  '2025-07-12',
      expiresAt:    '2026-01-12',
      seatsUsed:    1,
      seatsTotal:   3,
      hwid:         'a3f9c1d8-7b2e-4f01-9e56-3c8a0b12d4f7',
    },
    activity: [
      { color: 'green',  title: 'License activated',        desc: 'Ghost Pro activated on this device',       time: 'Jul 12, 2025 · 14:32' },
      { color: 'purple', title: 'Account registered',       desc: 'Welcome to Ghost! Account created.',       time: 'Jul 12, 2025 · 14:28' },
      { color: 'cyan',   title: 'License purchased',        desc: 'Ghost Pro — 6-month plan',                 time: 'Jul 12, 2025 · 14:15' },
      { color: 'yellow', title: 'Download completed',       desc: 'Ghost v2.4.1 — Windows x64',               time: 'Jul 12, 2025 · 14:35' },
      { color: 'green',  title: 'Key copied to clipboard',  desc: 'License key copied from dashboard',        time: 'Jul 15, 2025 · 09:11' },
    ],

    /* ── Release catalogue ─────────────────────────────────────
       The actual binary URLs are NEVER stored here.
       When a real backend is connected, the frontend will call
       POST /api/download  { version, platform, token }
       and receive a short-lived signed redirect URL from the
       server. The href values below are placeholder route tokens
       that trigger a client-side handler — no S3/CDN paths or
       secrets are ever present in this file.
    ──────────────────────────────────────────────────────────── */
    releases: {
      /* Minimum version the server requires for continued use */
      minimumVersion: 'v2.3.0',

      /* Latest release */
      latest: {
        version:     'v2.4.1',
        releaseDate: '2025-07-01',
        fileSize:    '42 MB',           // representative size shown in UI
        status:      'stable',          // 'stable' | 'beta' | 'rc'
        updateRequired: false,          // true → show update-required warning
        notes: [
          { text: 'Performance improvements across all game modes', type: 'green' },
          { text: 'Fixed intermittent crash on Windows 11 24H2',    type: '' },
          { text: 'Reduced memory footprint by ~18%',              type: 'cyan' },
          { text: 'Improved anti-cheat bypass compatibility',       type: '' },
          { text: 'New stealth module for ranked queues',           type: 'cyan' },
        ],
        /* Per-platform sizes; token is sent to backend, never a real URL */
        platforms: [
          { id: 'win',   name: 'Windows',  os: 'Windows 10/11',   arch: 'x64', size: '42 MB', icon: 'windows', token: 'dl:win:v2.4.1' },
          { id: 'mac',   name: 'macOS',    os: 'macOS 12+',       arch: 'Universal', size: '38 MB', icon: 'mac', token: 'dl:mac:v2.4.1', cyan: true },
          { id: 'linux', name: 'Linux',    os: 'Ubuntu 20.04+',   arch: 'x64', size: '36 MB', icon: 'linux', token: 'dl:linux:v2.4.1' },
        ],
      },

      /* Plan access matrix — which platforms each tier may download */
      planAccess: {
        Trial:    ['win'],               // trial: Windows only
        Basic:    ['win', 'mac'],
        Pro:      ['win', 'mac', 'linux'],
        Lifetime: ['win', 'mac', 'linux'],
        TRIAL:    ['win'],
        PRO:      ['win', 'mac', 'linux'],
        LIFETIME: ['win', 'mac', 'linux'],
        ADMIN:    ['win', 'mac', 'linux'],
      },

      /* Previous versions (shown to Pro + Lifetime only) */
      previous: [
        { version: 'v2.4.0', releaseDate: '2025-06-10', size: '41 MB', status: 'stable',  token: 'dl:win:v2.4.0' },
        { version: 'v2.3.2', releaseDate: '2025-05-02', size: '39 MB', status: 'stable',  token: 'dl:win:v2.3.2' },
        { version: 'v2.3.1', releaseDate: '2025-04-14', size: '38 MB', status: 'stable',  token: 'dl:win:v2.3.1' },
        { version: 'v2.3.0', releaseDate: '2025-03-22', size: '37 MB', status: 'stable',  token: 'dl:win:v2.3.0' },
        { version: 'v2.2.9', releaseDate: '2025-02-08', size: '35 MB', status: 'legacy',  token: 'dl:win:v2.2.9' },
      ],
    },

    /* ── Purchase history ──────────────────────────────────────
       In production each row comes from the backend.
       licenseKey values here are placeholder demo strings only.
       Receipt URLs are opaque server-side tokens — no payment
       processor secrets are stored client-side.
    ──────────────────────────────────────────────────────────── */
    purchases: [
      {
        orderId:        '#GH-10042',
        purchaseDate:   '2025-07-12',
        plan:           'Ghost Pro',
        planTier:       'pro',
        billingPeriod:  '6 months',
        amount:         34.99,
        paymentStatus:  'paid',
        licenseKey:     'GHOST-K7X2P-M4NR8-QJ6WL-T9YBV',
        licenseStatus:  'active',
        receiptToken:   'rcpt:GH-10042',  // opaque token — backend resolves to receipt URL
      },
      {
        orderId:        '#GH-10019',
        purchaseDate:   '2025-05-03',
        plan:           'Ghost Basic',
        planTier:       'basic',
        billingPeriod:  '1 month',
        amount:         9.99,
        paymentStatus:  'paid',
        licenseKey:     'GHOST-B2R4T-N8QX3-VW7KL-P5HMF',
        licenseStatus:  'expired',
        receiptToken:   'rcpt:GH-10019',
      },
      {
        orderId:        '#GH-10003',
        purchaseDate:   '2025-04-20',
        plan:           'Ghost Trial',
        planTier:       'trial',
        billingPeriod:  '7 days',
        amount:         0,
        paymentStatus:  'expired',
        licenseKey:     'GHOST-TR14L-XXXXX-XXXXX-XXXXX',
        licenseStatus:  'expired',
        receiptToken:   null,
      },
    ],
  };

  /* ── Plan label helper ─────────────────────────────────────── */
  function _planLabel (planSlug) {
    const s   = (planSlug || '').toLowerCase();
    // Normalise compound slugs like "ghost_pro_lifetime", "ghost_pro", "ghost_basic"
    if (s.includes('lifetime')) return 'Lifetime';
    if (s.includes('trial'))    return 'Trial';
    if (s.includes('basic'))    return 'Basic';
    if (s.includes('admin'))    return 'Admin';
    if (s.includes('pro'))      return 'Pro';
    const map = { trial: 'Trial', pro: 'Pro', lifetime: 'Lifetime', admin: 'Admin', basic: 'Basic' };
    return map[s] || planSlug || 'Pro';
  }

  /* ── Build an account object from a verified backend order record ─── */
  function _accountFromOrder (order) {
    const planLabel = _planLabel(order.plan);
    const now       = new Date().toISOString().slice(0, 10);
    const expires   = order.plan === 'lifetime'
      ? null
      : order.plan === 'trial'
        ? new Date(Date.now() + 7  * 86_400_000).toISOString().slice(0, 10)
        : new Date(Date.now() + 30 * 86_400_000).toISOString().slice(0, 10);

    const orderId = order.order_id || order.stripe_session_id || '#GH-NEW';
    return {
      username:    order.discord    || 'customer',
      email:       order.email      || '',
      memberSince: (order.created_at || now).slice(0, 10),
      license: {
        key:         order.license_key,
        tier:        planLabel,
        status:      order.payment_status === 'verified' ? 'active' : order.payment_status || 'pending',
        activatedAt: (order.created_at || now).slice(0, 10),
        expiresAt:   expires,
        seatsUsed:   1,
        seatsTotal:  3,
        hwid:        '—',
      },
      activity: [
        {
          color: 'green',
          title: 'License purchased & activated',
          desc:  planLabel + ' plan · key delivered automatically',
          time:  new Date(order.created_at || now).toLocaleString('en-US', {
            month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit',
          }),
        },
      ],
      releases: PLACEHOLDER_ACCOUNT.releases,
      purchases: [
        {
          orderId:       orderId,
          purchaseDate:  (order.created_at || now).slice(0, 10),
          plan:          planLabel,
          planTier:      order.plan || 'pro',
          billingPeriod: order.plan === 'lifetime' ? 'Once' : order.plan === 'trial' ? '7 days' : 'Monthly',
          amount:        order.price_usd != null ? Number(order.price_usd) : 0,
          paymentStatus: order.payment_status === 'verified' ? 'paid' : order.payment_status || 'pending',
          licenseKey:    order.license_key,
          licenseStatus: order.payment_status === 'verified' ? 'active' : 'pending',
          receiptToken:  `rcpt:${orderId.replace(/^#/, '')}`,
        },
      ],
    };
  }

  /* ── API layer ─────────────────────────────────────────────── */
  const GhostDashboard = {

    /**
     * Load account data.
     *
     * Priority order:
     * 1. sessionStorage.ghost_last_order  — set by checkout.js on success page;
     *    if the session_id is present the order is re-fetched from the backend
     *    to get the authoritative license key (not trusting sessionStorage alone).
     * 2. URL ?session_id= param            — direct link from Stripe success URL.
     * 3. GET /api/license/info + /api/purchases — authenticated account endpoint.
     */
    loadAccount: async function () {
      const token = localStorage.getItem('ghost_token');

      /* ── 1. Check for a just-delivered order in sessionStorage ─────── */
      let freshOrder = null;
      try {
        const raw = sessionStorage.getItem('ghost_last_order');
        if (raw) {
          freshOrder = JSON.parse(raw);
          sessionStorage.removeItem('ghost_last_order');
        }
      } catch (_) { /* sessionStorage unavailable — ignore */ }

      /* ── 2. Also check URL for session_id (e.g. direct link) ───────── */
      const urlParams  = new URLSearchParams(window.location.search);
      const urlSession = urlParams.get('session_id');
      const sessionId  = (freshOrder && freshOrder.sessionId) || urlSession || null;

      /* ── 3. If we have a session ID, fetch the authoritative order from backend ── */
      if (sessionId) {
        try {
          const MAX_ATTEMPTS = 6;
          let order = null;
          for (let i = 0; i < MAX_ATTEMPTS; i++) {
            const res = await fetch(`/api/order/${encodeURIComponent(sessionId)}`);
            const data = await res.json().catch(() => ({}));
            if (data.ok && data.license_key) { order = data; break; }
            if (i < MAX_ATTEMPTS - 1) await _delay(2000);
          }
          if (order) return _accountFromOrder(order);
        } catch (_) { /* fall through */ }
      }

      /* ── 4. sessionStorage has the key already (e.g. free trial) ─── */
      if (freshOrder && freshOrder.key) {
        return _accountFromOrder({
          order_id:       freshOrder.orderId,
          plan:           freshOrder.plan,
          email:          freshOrder.email,
          discord:        freshOrder.discord,
          price_usd:      freshOrder.priceUsd,
          license_key:    freshOrder.key,
          tier:           freshOrder.tier,
          payment_status: 'verified',
          created_at:     new Date().toISOString(),
        });
      }

      /* ── 5. Real authenticated call — single dashboard endpoint ─────── */
      if (!token) throw new Error('auth');

      const headers = {
        'Content-Type':  'application/json',
        'Authorization': `Bearer ${token}`,
      };

      const res  = await fetch('/api/customer/dashboard', { credentials: 'include', headers });

      if (res.status === 401) throw new Error('auth');

      const data = await res.json().catch(() => ({}));
      console.log("Dashboard JSON:", data);

      if (!data.ok) throw new Error('load_failed');

      const lic       = data.license  || {};
      const purchases = data.purchases || [];
      const settings  = data.settings  || {};
      const downloads = data.downloads || [];
      const counts    = data.counts    || {};

      // Normalise tier slug from either the top-level or the license object
      const rawTier    = lic.tier || data.tier || 'Pro';
      const normTier   = _planLabel(rawTier);

      return {
        username:    data.username    || localStorage.getItem('ghost_username') || 'customer',
        email:       data.email       || '',
        memberSince: data.memberSince || new Date().toISOString().slice(0, 10),
        license: {
          key:         lic.key         || '',
          tier:        normTier,
          status:      lic.banned      ? 'banned'
                     : lic.expired     ? 'expired'
                     : lic.valid       ? 'active' : (lic.status || 'none'),
          activatedAt: lic.activatedAt || '',
          expiresAt:   lic.expiresAt   || null,   // null = never expires
          seatsUsed:   1,
          seatsTotal:  3,
          hwid:        '—',
        },
        activity: [],
        releases:  Object.assign({}, PLACEHOLDER_ACCOUNT.releases,
                    settings.version
                      ? { latest: Object.assign({}, PLACEHOLDER_ACCOUNT.releases.latest,
                            { version: settings.version }) }
                      : {}),
        // Use server purchases; fall back to empty array (never show fake data for real users)
        purchases: purchases,
        downloads: downloads,
        counts:    counts,
      };
    },

    /** POST /api/license/reset */
    resetActivation: async function () {
      const token = localStorage.getItem('ghost_token');
      const res = await fetch('/api/license/reset', {
        method:      'POST',
        credentials: 'include',
        headers: {
          'Content-Type':  'application/json',
          'Authorization': token ? `Bearer ${token}` : '',
        },
      });
      if (!res.ok) throw new Error('reset_failed');
      return { ok: true };
    },
  };

  /* ── Auth guard ─────────────────────────────────────────────
     Replace with a real session / JWT check.
     Called before any data is loaded.
  ─────────────────────────────────────────────────────────── */
  function _isAuthenticated () {
    // Real check: require ghost_token in localStorage (set by auth.js on login)
    return !!localStorage.getItem('ghost_token');
  }

  /* ── Utilities ──────────────────────────────────────────── */
  function _delay (ms) { return new Promise(r => setTimeout(r, ms)); }

  function _el (id)  { return document.getElementById(id); }
  function _qs (sel) { return document.querySelector(sel); }

  /** Compute days remaining from an ISO date string. */
  function _daysRemaining (isoDate) {
    const diff = new Date(isoDate) - new Date();
    return Math.max(0, Math.ceil(diff / 86_400_000));
  }

  /** Format an ISO date string as "Month D, YYYY". */
  function _fmt (isoDate) {
    return new Date(isoDate).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  }

  /** Short format "Mon D, YYYY" */
  function _fmtShort (isoDate) {
    return new Date(isoDate).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  }

  /** Status badge modifier class from a raw status string. */
  const STATUS_CLASS = {
    active:  'db-status-badge--active',
    expired: 'db-status-badge--expired',
    trial:   'db-status-badge--trial',
    pending: 'db-status-badge--pending',
    banned:  'db-status-badge--expired',
    revoked: 'db-status-badge--expired',
  };

  /** Status label display text. */
  const STATUS_LABEL = {
    active:  'Active',
    expired: 'Expired',
    trial:   'Trial',
    pending: 'Pending',
    banned:  'Banned',
    revoked: 'Revoked',
  };

  /* ── Toast ───────────────────────────────────────────────── */
  let _toastTimer = null;

  function toast (message, type = 'success') {
    const el = _el('db-toast');
    if (!el) return;
    el.textContent = message;
    el.className   = `db-toast db-toast--${type}`;
    el.hidden      = false;
    // force reflow so transition fires
    void el.offsetWidth;
    el.classList.add('visible');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
      el.classList.remove('visible');
      setTimeout(() => { el.hidden = true; }, 280);
    }, 3000);
  }

  /* ── Copy to clipboard ───────────────────────────────────── */
  function copyText (text, feedbackBtns) {
    navigator.clipboard.writeText(text).then(() => {
      toast('License key copied to clipboard!', 'success');
      feedbackBtns.forEach(btn => {
        if (!btn) return;
        const iconCopy  = btn.querySelector('.icon-copy');
        const iconCheck = btn.querySelector('.icon-check');
        btn.classList.add('copied');
        if (iconCopy)  iconCopy.style.display  = 'none';
        if (iconCheck) iconCheck.style.display = '';
        setTimeout(() => {
          btn.classList.remove('copied');
          if (iconCopy)  iconCopy.style.display  = '';
          if (iconCheck) iconCheck.style.display = 'none';
        }, 2000);
      });
    }).catch(() => {
      toast('Could not copy — please copy the key manually.', 'error');
    });
  }

  /* ── Sidebar / mobile nav ────────────────────────────────── */
  function initSidebar () {
    const sidebar = _el('db-sidebar');
    const overlay = _el('db-overlay');
    const toggle  = _el('db-sidebar-toggle');

    function openSidebar () {
      sidebar.classList.add('open');
      overlay.classList.add('active');
      toggle && toggle.setAttribute('aria-expanded', 'true');
    }
    function closeSidebar () {
      sidebar.classList.remove('open');
      overlay.classList.remove('active');
      toggle && toggle.setAttribute('aria-expanded', 'false');
    }

    toggle  && toggle.addEventListener('click', () => {
      sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
    });
    overlay && overlay.addEventListener('click', closeSidebar);

    // Keyboard escape
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && sidebar.classList.contains('open')) closeSidebar();
    });
  }

  /* ── Section routing (single-page panel switching) ──────── */
  function initNav () {
    const links    = document.querySelectorAll('.db-nav-link[data-section]');
    const sections = document.querySelectorAll('.db-section');

    function showSection (id) {
      sections.forEach(s => {
        s.hidden = (s.id !== 'section-' + id);
        s.classList.toggle('active', s.id === 'section-' + id);
      });
      links.forEach(l => l.classList.toggle('active', l.dataset.section === id));
      // Update URL hash without a page jump
      history.replaceState(null, '', '#' + id);
      // Close mobile sidebar after navigation
      _el('db-sidebar')?.classList.remove('open');
      _el('db-overlay')?.classList.remove('active');
    }

    links.forEach(link => {
      link.addEventListener('click', e => {
        e.preventDefault();
        showSection(link.dataset.section);
      });
    });

    // Respect hash on page load
    const initial = (window.location.hash || '#dashboard').replace('#', '');
    showSection(initial);

    // Support-button in sidebar footer → jump to support section
    _el('db-support-btn')?.addEventListener('click', () => showSection('support'));
  }

  /* ── Logout ───────────────────────────────────────────────── */
  function initLogout () {
    _el('db-logout-btn')?.addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
      localStorage.removeItem('ghost_token');
      localStorage.removeItem('ghost_username');
      window.location.href = 'login.html';
    });
  }

  /* ── Reset-activation modal ─────────────────────────────── */
  function initResetModal (licenseKey) {
    const modal   = _el('db-modal-overlay');
    const cancel  = _el('db-modal-cancel');
    const confirm = _el('db-modal-confirm');

    const resetBtns = [
      _el('db-reset-btn'),
      _el('lic-reset-btn'),
    ].filter(Boolean);

    function openModal ()  { modal && (modal.hidden = false); }
    function closeModal () { modal && (modal.hidden = true);  }

    resetBtns.forEach(btn => btn.addEventListener('click', openModal));
    cancel?.addEventListener('click', closeModal);
    modal?.addEventListener('click', e => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

    confirm?.addEventListener('click', async () => {
      confirm.disabled = true;
      confirm.textContent = 'Resetting…';
      try {
        const result = await GhostDashboard.resetActivation();
        closeModal();
        if (result.ok) {
          toast('License activation reset. Re-enter your key to use Ghost.', 'success');
        } else {
          toast('Reset failed. Please try again or contact support.', 'error');
        }
      } catch (_) {
        closeModal();
        toast('Network error. Please try again.', 'error');
      } finally {
        confirm.disabled    = false;
        confirm.textContent = 'Reset Activation';
      }
    });
  }

  /* ── Download button wiring ─────────────────────────────── */
  function initDownloadBtns () {
    // Top-of-page download button navigates to the Downloads section
    const topBtn = _el('db-download-btn-top');
    const navDl  = document.querySelector('.db-nav-link[data-section="downloads"]');
    topBtn?.addEventListener('click', () => navDl && navDl.click());

    // License-card download button also navigates to downloads
    _el('db-download-btn')?.addEventListener('click', () => navDl && navDl.click());
    _el('lic-download-btn')?.addEventListener('click', () => navDl && navDl.click());
  }

  /* ─────────────────────────────────────────────────────────────
     RENDERERS — each function accepts the account data object
     and hydrates one section of the dashboard.
  ───────────────────────────────────────────────────────────── */

  /** Hydrate the user pill / avatar in sidebar + topbar. */
  function renderUserInfo (account) {
    const initial = (account.username || 'U')[0].toUpperCase();

    // Sidebar user pill
    const sidebarUser = _el('db-sidebar-user-pill');
    if (sidebarUser) {
      sidebarUser.querySelector('.db-avatar').textContent       = initial;
      sidebarUser.querySelector('.db-sidebar-username').textContent = account.username;
      sidebarUser.querySelector('.db-sidebar-email').textContent    = account.email;
    }

    // Topbar avatar
    const topAvatar = _el('db-topbar-user')?.querySelector('.db-avatar');
    if (topAvatar) topAvatar.textContent = initial;

    // Welcome heading
    const welcomeName = _el('db-welcome-name');
    if (welcomeName) welcomeName.textContent = account.username;
  }

  /** Hydrate the stat cards on the Dashboard overview. */
  function renderStatCards (account) {
    const lic      = account.license;
    const isLife   = lic.tier === 'Lifetime' || lic.expiresAt == null;
    const days     = isLife ? Infinity : _daysRemaining(lic.expiresAt);

    // Tier + status badge
    const tierEl   = _el('stat-tier');
    const badgeEl  = _el('stat-status-badge');
    if (tierEl)  tierEl.textContent = lic.tier;
    if (badgeEl) {
      badgeEl.className   = `db-status-badge ${STATUS_CLASS[lic.status] || ''}`;
      badgeEl.textContent = STATUS_LABEL[lic.status] || lic.status;
    }

    // Days remaining
    const daysEl  = _el('stat-days');
    if (daysEl) {
      daysEl.textContent = isLife ? '∞' : days;
      if (!isLife && days <= 30 && days > 0) daysEl.style.color = '#fbbf24';
      if (!isLife && days === 0)              daysEl.style.color = '#f87171';
    }

    // Expiry hint — find it via parent
    const daysCard = daysEl?.closest('.db-stat-card');
    const daysHint = daysCard?.querySelector('.db-stat-hint');
    if (daysHint) daysHint.textContent = isLife ? 'Never expires' : `Expires ${_fmtShort(lic.expiresAt)}`;

    // Activations
    const actEl = _el('stat-activations');
    if (actEl) {
      actEl.innerHTML = `${lic.seatsUsed} <span class="db-stat-of">/ ${lic.seatsTotal}</span>`;
    }

    // License since
    const sinceEl = _el('stat-activated');
    if (sinceEl) sinceEl.textContent = lic.activatedAt ? _fmtShort(lic.activatedAt) : '—';
  }

  /** Hydrate the key card on the dashboard overview. */
  function renderKeyCard (account) {
    const lic = account.license;
    const key = lic.key;

    // Key display
    const keyEl = _el('db-license-key');
    if (keyEl) keyEl.textContent = key;

    // Badge on key card header
    const cardBadge = _el('dash-key-card')?.querySelector('.db-status-badge');
    if (cardBadge) {
      cardBadge.className   = `db-status-badge ${STATUS_CLASS[lic.status] || ''}`;
      cardBadge.textContent = STATUS_LABEL[lic.status] || lic.status;
    }

    // Meta row
    const tierEl      = _el('db-key-tier');
    const activatedEl = _el('db-key-activated');
    const expiresEl   = _el('db-key-expires');
    const isLife = lic.tier === 'Lifetime' || lic.expiresAt == null;
    if (tierEl)      tierEl.textContent      = lic.tier;
    if (activatedEl) activatedEl.textContent = lic.activatedAt ? _fmtShort(lic.activatedAt) : '—';
    if (expiresEl)   expiresEl.textContent   = isLife ? 'Never' : _fmtShort(lic.expiresAt);

    // Copy buttons
    const copyBtn1 = _el('db-copy-key-btn');
    const copyBtn2 = _el('db-copy-key-btn-2');
    copyBtn1?.addEventListener('click', () => copyText(key, [copyBtn1]));
    copyBtn2?.addEventListener('click', () => copyText(key, [copyBtn1]));
  }

  /** Hydrate the License Details section. */
  function renderLicenseDetails (account) {
    const lic    = account.license;
    const isLife = lic.tier === 'Lifetime' || lic.expiresAt == null;
    const days   = isLife ? Infinity : _daysRemaining(lic.expiresAt);

    const set = (id, val) => { const el = _el(id); if (el) el.textContent = val; };

    set('detail-key',       lic.key);
    set('detail-tier',      lic.tier);
    set('detail-activated', lic.activatedAt ? _fmt(lic.activatedAt) : '—');
    set('detail-expires',   isLife ? 'Never' : _fmt(lic.expiresAt));
    set('detail-days',      isLife ? 'Lifetime' : `${days} day${days !== 1 ? 's' : ''}`);
    set('detail-seats',     `${lic.seatsUsed} of ${lic.seatsTotal} seat${lic.seatsTotal !== 1 ? 's' : ''}`);
    set('detail-hwid',      lic.hwid);

    const statusEl = _el('detail-status');
    if (statusEl) {
      statusEl.className   = `db-status-badge ${STATUS_CLASS[lic.status] || ''}`;
      statusEl.textContent = STATUS_LABEL[lic.status] || lic.status;
    }

    // Warn on expiry in detail days
    const daysEl = _el('detail-days');
    if (daysEl) {
      if (!isLife && days <= 30 && days > 0) daysEl.style.color = '#fbbf24';
      if (!isLife && days === 0)              daysEl.style.color = '#f87171';
    }

    // Copy button in license section
    _el('lic-copy-btn')?.addEventListener('click', () => copyText(lic.key, []));
  }

  /** Hydrate the Settings section. */
  function renderSettings (account) {
    const set = (id, val) => { const el = _el(id); if (el) el.textContent = val; };
    set('settings-username', account.username);
    set('settings-email',    account.email);
    set('settings-since',    _fmt(account.memberSince));
  }

  /* ── Activity list ────────────────────────────────────────── */
  function renderActivity (account) {
    const list  = _el('db-activity-list');
    const empty = _el('db-activity-empty');
    if (!list) return;

    const items = account.activity || [];

    if (!items.length) {
      list.innerHTML = '';
      if (empty) empty.hidden = false;
      return;
    }

    if (empty) empty.hidden = true;
    list.innerHTML = items.map((item, i) => `
      <li class="db-activity-item" style="animation-delay:${i * 0.06}s">
        <div class="db-activity-dot db-activity-dot--${item.color}" aria-hidden="true"></div>
        <div class="db-activity-body">
          <div class="db-activity-title">${_esc(item.title)}</div>
          <div class="db-activity-desc">${_esc(item.desc)}</div>
        </div>
        <time class="db-activity-time">${_esc(item.time)}</time>
      </li>
    `).join('');
  }

  /* ═══════════════════════════════════════════════════════════════
     DOWNLOADS — Full implementation
  ═══════════════════════════════════════════════════════════════ */

  /* Platform SVG icons (inline, no external assets) */
  const DL_ICONS = {
    windows: `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M0 3.449L9.75 2.1v9.451H0m10.949-9.602L24 0v11.4H10.949M0 12.6h9.75v9.451L0 20.699M10.949 12.6H24V24l-12.9-1.801"/></svg>`,
    mac:     `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/></svg>`,
    linux:   `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12.504 0c-.155 0-.315.008-.48.021C7.576.328 3.830 3.498 3.830 8.29c0 1.908.506 3.567 1.385 4.927C4.28 14.6 4 15.96 4 17.4c0 3.594 2.773 6.6 8.504 6.6 5.73 0 8.504-3.006 8.504-6.6 0-1.44-.28-2.8-1.215-4.183.879-1.36 1.385-3.019 1.385-4.927 0-4.793-3.746-7.963-8.194-8.269A6.494 6.494 0 0 0 12.504 0zm0 2.095c.297 0 .594.016.893.047 3.67.335 6.607 3.069 6.607 6.148 0 1.62-.49 3.082-1.326 4.23L18 12.73l.285.24c.845 1.127 1.22 2.257 1.22 3.43 0 2.828-2.125 4.505-7.001 4.505-4.875 0-7-1.677-7-4.505 0-1.173.375-2.303 1.22-3.43L7 12.52l-.678-.199C5.487 11.17 5 9.71 5 8.09c0-3.079 2.937-5.813 6.607-6.148.3-.031.597-.047.897-.047z"/></svg>`,
  };

  /** Maps raw version status to a display badge. */
  const DL_STATUS_BADGE = {
    stable: `<span class="dl-badge dl-badge--stable">Stable</span>`,
    beta:   `<span class="dl-badge dl-badge--beta">Beta</span>`,
    rc:     `<span class="dl-badge dl-badge--beta">RC</span>`,
    legacy: `<span class="dl-badge" style="background:rgba(136,150,176,0.1);color:var(--muted);border:1px solid var(--border)">Legacy</span>`,
  };

  /**
   * Determine if the license status blocks all downloads.
   * Returns an object { blocked: bool, reason: string, desc: string }.
   */
  function _dlAccessCheck (license) {
    const s = license.status;
    if (s === 'banned')   return { blocked: true, reason: 'Account Banned',    desc: 'Your account has been permanently banned. Downloads are disabled.' };
    if (s === 'revoked')  return { blocked: true, reason: 'License Revoked',   desc: 'Your license has been revoked. Please contact support.' };
    if (s === 'expired')  return { blocked: true, reason: 'License Expired',   desc: 'Your license has expired. Renew to re-enable downloads.' };
    if (s === 'pending')  return { blocked: true, reason: 'Payment Pending',   desc: 'Your payment is still processing. Downloads will unlock once confirmed.' };
    // trial and active allow access (limited by plan)
    return { blocked: false };
  }

  /**
   * Stub that simulates requesting a download from the backend.
   * In production: POST /api/download { token, licenseKey }
   * The server validates the license, logs the download, and
   * returns a short-lived signed URL. The CDN/storage path is
   * never exposed to the frontend.
   */
  async function _requestDownload (token, licenseKey) {
    // Fetch the current production download URL from server config.
    // Falls back to /dl/GhostConfig.exe if not configured.
    try {
      const cfgRes  = await fetch('/api/download/current');
      const cfg     = await cfgRes.json().catch(() => ({}));
      const dlUrl   = (cfg.ok && cfg.url) ? cfg.url : '/dl/GhostConfig.exe';
      const dlName  = (cfg.ok && cfg.filename) ? cfg.filename : 'GhostConfig.exe';

      const a   = document.createElement('a');
      a.href    = dlUrl;
      a.download = dlName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast('Download started — GhostConfig.exe', 'success');
    } catch (_) {
      // Fallback: direct link
      const a   = document.createElement('a');
      a.href    = '/dl/GhostConfig.exe';
      a.download = 'GhostConfig.exe';
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  }

  function renderDownloads (account) {
    const lic     = account.license;
    const releases = account.releases;

    // ── Evaluate access ─────────────────────────────────────
    const access = _dlAccessCheck(lic);

    // Hide skeleton
    const skel = _el('dl-skeleton');
    if (skel) skel.hidden = true;

    // Always hide all panels first, then selectively show
    ['dl-blocked', 'dl-update-warning', 'dl-plan-notice',
     'dl-latest-card', 'dl-prev-section', 'db-downloads-empty'
    ].forEach(id => { const el = _el(id); if (el) el.hidden = true; });

    // ── Blocked state ────────────────────────────────────────
    if (access.blocked) {
      const blocked = _el('dl-blocked');
      const reason  = _el('dl-blocked-reason');
      const desc    = _el('dl-blocked-desc');
      if (reason) reason.textContent = access.reason;
      if (desc)   desc.textContent   = access.desc;
      if (blocked) blocked.hidden    = false;
      return;
    }

    // ── No releases data ─────────────────────────────────────
    if (!releases || !releases.latest) {
      const empty = _el('db-downloads-empty');
      if (empty) empty.hidden = false;
      return;
    }

    const latest = releases.latest;
    const tier   = lic.tier;   // 'Trial' | 'Basic' | 'Pro' | 'Lifetime'
    const allowedPlatforms = (releases.planAccess || {})[tier] || [];
    const isTrial    = tier === 'Trial';
    const isPremium  = tier === 'Pro' || tier === 'Lifetime';

    // ── Update-required warning ──────────────────────────────
    if (latest.updateRequired) {
      const warn = _el('dl-update-warning');
      const desc = _el('dl-update-warning-desc');
      if (desc) desc.textContent =
        `Your installed version is below the required minimum (${releases.minimumVersion}). ` +
        `Please download v${latest.version} to continue using Ghost.`;
      if (warn) warn.hidden = false;
    }

    // ── Plan notice for Trial / Basic ────────────────────────
    if (isTrial || tier === 'Basic') {
      const notice = _el('dl-plan-notice');
      const text   = _el('dl-plan-notice-text');
      if (text) text.innerHTML =
        `Your <strong>${_esc(tier)}</strong> plan includes limited platform access. ` +
        `<a href="pricing.html" style="color:var(--cyan)">Upgrade to Pro</a> to unlock all platforms and previous versions.`;
      if (notice) notice.hidden = false;
    }

    // ── Latest release card ──────────────────────────────────
    const latestCard = _el('dl-latest-card');
    if (latestCard) latestCard.hidden = false;

    const setTxt = (id, v) => { const e = _el(id); if (e) e.textContent = v; };
    setTxt('dl-latest-name',    'Ghost ' + latest.version);
    setTxt('dl-latest-version', latest.version);
    setTxt('dl-release-date',   _fmtShort(latest.releaseDate));
    setTxt('dl-version-tag',    latest.version);
    setTxt('dl-version-status', latest.status.charAt(0).toUpperCase() + latest.status.slice(1));

    // Badges
    const badges = _el('dl-latest-badges');
    if (badges) {
      badges.innerHTML =
        `<span class="dl-badge dl-badge--latest">Latest</span>` +
        (DL_STATUS_BADGE[latest.status] || '');
    }

    // Release notes
    if (latest.notes && latest.notes.length) {
      const notesWrap = _el('dl-notes');
      const notesList = _el('dl-notes-list');
      if (notesList) {
        notesList.innerHTML = latest.notes.map(n =>
          `<li class="${_esc(n.type)}">${_esc(n.text)}</li>`
        ).join('');
      }
      if (notesWrap) notesWrap.hidden = false;
    }

    // Platform buttons
    const platformsEl = _el('dl-platforms');
    if (platformsEl) {
      platformsEl.innerHTML = latest.platforms.map(p => {
        const allowed  = allowedPlatforms.includes(p.id);
        const iconHtml = DL_ICONS[p.icon] || DL_ICONS.windows;
        const disabled = !allowed;
        const ariaLbl  = disabled
          ? `${p.name} — not available on your plan`
          : `Download Ghost ${latest.version} for ${p.name}`;
        return `
          <button
            class="dl-platform-btn${disabled ? ' dl-platform-btn--disabled' : ''}"
            data-token="${_esc(p.token)}"
            ${disabled ? 'disabled' : ''}
            aria-label="${_esc(ariaLbl)}"
            title="${disabled ? 'Upgrade your plan to access this platform' : ''}"
          >
            <div class="dl-platform-icon${p.cyan ? ' dl-platform-icon--cyan' : ''}" aria-hidden="true">${iconHtml}</div>
            <div class="dl-platform-info">
              <div class="dl-platform-name">${_esc(p.name)}</div>
              <div class="dl-platform-size">${_esc(p.os)} · ${_esc(p.arch)} · ${_esc(p.size)}</div>
            </div>
            <svg class="dl-platform-arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
        `;
      }).join('');

      // Wire download buttons
      platformsEl.querySelectorAll('.dl-platform-btn:not([disabled])').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (btn.dataset._busy) return;
          btn.dataset._busy = '1';
          const origHtml = btn.innerHTML;
          btn.innerHTML += `<span class="btn-spinner" style="margin-left:auto" aria-hidden="true"></span>`;
          btn.disabled = true;
          try {
            await _requestDownload(btn.dataset.token, lic.key);
          } catch (_) {
            toast('Download failed. Please try again or contact support.', 'error');
          } finally {
            btn.innerHTML = origHtml;
            btn.disabled  = false;
            delete btn.dataset._busy;
          }
        });
      });
    }

    // ── Previous versions accordion (Pro + Lifetime only) ────
    if (isPremium && releases.previous && releases.previous.length) {
      const prevSection = _el('dl-prev-section');
      const prevTbody   = _el('dl-prev-tbody');
      const prevToggle  = _el('dl-prev-toggle');
      const prevBody    = _el('dl-prev-body');

      if (prevTbody) {
        prevTbody.innerHTML = releases.previous.map(v => {
          const badge = DL_STATUS_BADGE[v.status] || '';
          return `
            <tr>
              <td class="dl-ver-mono">${_esc(v.version)}</td>
              <td class="dl-ver-date">${_fmtShort(v.releaseDate)}</td>
              <td>${_esc(v.size)}</td>
              <td>${badge}</td>
              <td>
                <button class="dl-prev-dl-btn" data-token="${_esc(v.token)}" aria-label="Download ${_esc(v.version)}">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  Download
                </button>
              </td>
            </tr>
          `;
        }).join('');

        // Wire previous version download buttons
        prevTbody.querySelectorAll('.dl-prev-dl-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            if (btn.dataset._busy) return;
            btn.dataset._busy = '1';
            const orig = btn.textContent;
            btn.textContent = 'Requesting…';
            btn.disabled = true;
            try {
              await _requestDownload(btn.dataset.token, lic.key);
            } catch (_) {
              toast('Download failed. Please try again.', 'error');
            } finally {
              btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Download`;
              btn.disabled = false;
              delete btn.dataset._busy;
            }
          });
        });
      }

      // Accordion toggle
      if (prevToggle && prevSection && prevBody) {
        prevSection.hidden = false;
        const toggleAccordion = () => {
          const isOpen = prevSection.classList.toggle('open');
          prevToggle.setAttribute('aria-expanded', String(isOpen));
        };
        prevToggle.addEventListener('click', toggleAccordion);
        prevToggle.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleAccordion(); }
        });
      }
    }
  }


  /* ═══════════════════════════════════════════════════════════════
     PURCHASE HISTORY — Full implementation with search/filter/pagination
  ═══════════════════════════════════════════════════════════════ */

  /** Status metadata for both payment and license status. */
  const PUR_STATUS = {
    paid:      { cls: 'db-status-badge--active',  label: 'Paid'      },
    completed: { cls: 'db-status-badge--active',  label: 'Paid'      },  // PayPal/Stripe "completed"
    active:    { cls: 'db-status-badge--active',  label: 'Active'    },
    expired:   { cls: 'db-status-badge--expired', label: 'Expired'   },
    pending:   { cls: 'db-status-badge--pending', label: 'Pending'   },
    refunded:  { cls: 'db-status-badge--expired', label: 'Refunded'  },
    revoked:   { cls: 'db-status-badge--expired', label: 'Revoked'   },
    trial:     { cls: 'db-status-badge--trial',   label: 'Trial'     },
    banned:    { cls: 'db-status-badge--expired', label: 'Banned'    },
  };

  const PUR_PAGE_SIZE = 5;

  function renderPurchases (account) {
    const allPurchases = account.purchases || [];
    let   page         = 1;
    let   searchQuery  = '';
    let   statusFilter = '';
    let   planFilter   = '';

    const tbody       = _el('pur-tbody');
    const tableWrap   = _el('pur-table-wrap');
    const loadingWrap = _el('pur-loading');
    const skelTbody   = _el('pur-skeleton-tbody');
    const empty       = _el('db-purchases-empty');
    const emptyMsg    = _el('pur-empty-msg');
    const pagination  = _el('pur-pagination');
    const pageInfo    = _el('pur-page-info');
    const pageCtrl    = _el('pur-page-controls');
    const errEl       = _el('pur-error');
    const purCard     = _el('pur-card');

    if (!tbody) return;

    // ── Summary stats ───────────────────────────────────────
    const _isPaid = s => s === 'paid' || s === 'completed';
    const paidOrders  = allPurchases.filter(p => _isPaid(p.paymentStatus));
    const totalSpent  = paidOrders.reduce((sum, p) => sum + (Number(p.amount) || 0), 0);
    const activeLics  = allPurchases.filter(p => p.licenseStatus === 'active').length;
    const latestPlan  = allPurchases.length ? allPurchases[0].plan : '—';

    const setTxt = (id, v) => { const e = _el(id); if (e) e.textContent = v; };
    setTxt('pur-stat-total',  allPurchases.length.toString());
    setTxt('pur-stat-spent',  totalSpent > 0 ? `$${totalSpent.toFixed(2)}` : '$0.00');
    setTxt('pur-stat-active', activeLics.toString());
    setTxt('pur-stat-plan',   latestPlan);

    // ── Skeleton loading (simulated async) ──────────────────
    if (skelTbody) {
      skelTbody.innerHTML = Array(4).fill(0).map(() => `
        <tr class="pur-skeleton-row">
          ${Array(8).fill(0).map((_, i) => {
            const w = [60, 50, 55, 35, 40, 80, 40, 30][i] || 50;
            return `<td><div class="pur-skel-bar" style="width:${w}%"></div></td>`;
          }).join('')}
        </tr>
      `).join('');
    }

    // Show skeleton briefly
    if (loadingWrap) loadingWrap.hidden = false;
    if (tableWrap)   tableWrap.hidden   = true;
    if (pagination)  pagination.hidden  = true;
    if (empty)       empty.hidden       = true;

    setTimeout(() => {
      if (loadingWrap) loadingWrap.hidden = true;
      _renderPurTable();
    }, 700);

    // ── Filter + render ─────────────────────────────────────
    function _filterPurchases () {
      const q   = searchQuery.toLowerCase().trim();
      const st  = statusFilter.toLowerCase();
      const pl  = planFilter.toLowerCase();
      return allPurchases.filter(p => {
        const matchQ  = !q || [p.orderId, p.plan, p.licenseKey, String(p.amount ?? '')]
          .some(v => (v || '').toLowerCase().includes(q));
        const matchSt = !st || (p.paymentStatus || '').toLowerCase() === st;
        const matchPl = !pl || (p.planTier || '').toLowerCase() === pl;
        return matchQ && matchSt && matchPl;
      });
    }

    function _renderPurTable () {
      const filtered = _filterPurchases();
      const total    = filtered.length;
      const pages    = Math.max(1, Math.ceil(total / PUR_PAGE_SIZE));
      if (page > pages) page = pages;
      const start    = (page - 1) * PUR_PAGE_SIZE;
      const slice    = filtered.slice(start, start + PUR_PAGE_SIZE);

      if (!total) {
        if (tableWrap)  tableWrap.hidden  = true;
        if (pagination) pagination.hidden = true;
        if (empty)      empty.hidden      = false;
        if (emptyMsg) {
          emptyMsg.innerHTML = searchQuery || statusFilter || planFilter
            ? 'No orders match your search. <button class="btn btn-ghost btn-sm" id="pur-clear-btn" style="margin-left:4px;padding:4px 10px;font-size:0.78rem">Clear filters</button>'
            : 'No purchases yet. <a href="pricing.html">Browse plans →</a>';
          _el('pur-clear-btn')?.addEventListener('click', _clearFilters);
        }
        return;
      }

      if (empty)     empty.hidden     = true;
      if (tableWrap) tableWrap.hidden = false;

      // Render rows
      tbody.innerHTML = slice.map(p => {
        const pStat = PUR_STATUS[p.paymentStatus] || { cls: '', label: p.paymentStatus };
        const lStat = PUR_STATUS[p.licenseStatus] || { cls: '', label: p.licenseStatus };
        const amtStr = Number(p.amount) > 0 ? `$${Number(p.amount).toFixed(2)}` : 'Free';
        // Truncate key for display
        const keyShort = p.licenseKey
          ? p.licenseKey.length > 22
            ? p.licenseKey.slice(0, 10) + '…' + p.licenseKey.slice(-6)
            : p.licenseKey
          : '—';
        const invoiceBtn = p.orderId
          ? `<button class="pur-receipt-btn" data-orderid="${_esc(p.orderId)}" aria-label="Download invoice for ${_esc(p.orderId)}">
               <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
               Invoice
             </button>`
          : '<span style="color:var(--muted);font-size:0.78rem">—</span>';

        return `
          <tr>
            <td><span class="pur-order-id">${_esc(p.orderId)}</span></td>
            <td class="pur-date-cell">${_fmtShort(p.purchaseDate)}</td>
            <td>${_esc(p.plan)}${p.billingPeriod ? `<br><span style="font-size:0.72rem;color:var(--muted)">${_esc(p.billingPeriod)}</span>` : ''}</td>
            <td class="pur-amount">${_esc(amtStr)}</td>
            <td><span class="db-status-badge ${pStat.cls}">${pStat.label}</span></td>
            <td>
              <span class="pur-key-cell" title="${_esc(p.licenseKey || '')}">${_esc(keyShort)}</span>
              ${p.licenseKey ? `<button class="pur-key-copy-btn" data-key="${_esc(p.licenseKey)}" aria-label="Copy license key" title="Copy key">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>` : ''}
            </td>
            <td><span class="db-status-badge ${lStat.cls}">${lStat.label}</span></td>
            <td>${invoiceBtn}</td>
          </tr>
        `;
      }).join('');

      // Wire copy buttons
      tbody.querySelectorAll('.pur-key-copy-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const key = btn.dataset.key;
          if (!key) return;
          navigator.clipboard.writeText(key).then(() => {
            toast('License key copied!', 'success');
          }).catch(() => {
            toast('Could not copy key — please copy manually.', 'error');
          });
        });
      });

      // Wire invoice download buttons
      tbody.querySelectorAll('.pur-receipt-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const p = allPurchases.find(x => x.orderId === btn.dataset.orderid);
          if (!p) return;
          const lines = [
            '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
            '              GHOST — INVOICE',
            '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
            `Invoice ID  : ${p.orderId}`,
            `Date        : ${_fmt(p.purchaseDate)}`,
            `Plan        : ${p.plan}`,
            `Amount      : ${Number(p.amount) > 0 ? '$' + Number(p.amount).toFixed(2) : 'Free'}`,
            `Payment     : ${p.paymentStatus}`,
            `License Key : ${p.licenseKey || 'Not assigned'}`,
            '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
            'ghost.gg — Thank you for your purchase.',
            '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
          ];
          const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
          const url  = URL.createObjectURL(blob);
          const a    = document.createElement('a');
          a.href     = url;
          a.download = `ghost-invoice-${p.orderId.replace(/^#/, '')}.txt`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
          toast('Invoice downloaded!', 'success');
        });
      });

      // ── Pagination ────────────────────────────────────────
      if (total <= PUR_PAGE_SIZE) {
        if (pagination) pagination.hidden = true;
      } else {
        if (pagination) pagination.hidden = false;
        if (pageInfo) {
          pageInfo.textContent = `Showing ${start + 1}–${Math.min(start + PUR_PAGE_SIZE, total)} of ${total}`;
        }
        if (pageCtrl) {
          const btns = [];
          // Prev
          btns.push(`<button class="pur-page-btn" data-p="${page - 1}" ${page === 1 ? 'disabled' : ''} aria-label="Previous page">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          </button>`);
          // Page numbers
          for (let i = 1; i <= pages; i++) {
            if (pages <= 7 || i === 1 || i === pages || (i >= page - 1 && i <= page + 1)) {
              btns.push(`<button class="pur-page-btn${i === page ? ' pur-page-btn--active' : ''}" data-p="${i}" aria-label="Page ${i}" aria-current="${i === page ? 'page' : 'false'}">${i}</button>`);
            } else if (i === 2 || i === pages - 1) {
              btns.push(`<span class="pur-page-btn" style="cursor:default;pointer-events:none">…</span>`);
            }
          }
          // Next
          btns.push(`<button class="pur-page-btn" data-p="${page + 1}" ${page === pages ? 'disabled' : ''} aria-label="Next page">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>
          </button>`);
          pageCtrl.innerHTML = btns.join('');

          pageCtrl.querySelectorAll('.pur-page-btn[data-p]').forEach(pb => {
            pb.addEventListener('click', () => {
              if (pb.disabled) return;
              const np = parseInt(pb.dataset.p, 10);
              if (np >= 1 && np <= pages) { page = np; _renderPurTable(); }
            });
          });
        }
      }
    }

    function _clearFilters () {
      searchQuery  = '';
      statusFilter = '';
      planFilter   = '';
      page         = 1;
      const s = _el('pur-search');       if (s) s.value = '';
      const f = _el('pur-filter-status'); if (f) f.value = '';
      const p = _el('pur-filter-plan');   if (p) p.value = '';
      _renderPurTable();
    }

    // ── Bind search + filter controls ───────────────────────
    _el('pur-search')?.addEventListener('input', e => {
      searchQuery = e.target.value;
      page = 1;
      _renderPurTable();
    });
    _el('pur-filter-status')?.addEventListener('change', e => {
      statusFilter = e.target.value;
      page = 1;
      _renderPurTable();
    });
    _el('pur-filter-plan')?.addEventListener('change', e => {
      planFilter = e.target.value;
      page = 1;
      _renderPurTable();
    });
    _el('pur-retry-btn')?.addEventListener('click', () => {
      if (errEl)  errEl.hidden  = true;
      if (purCard) purCard.hidden = false;
      _renderPurTable();
    });
  }

  /* ── XSS-safe text escaping ──────────────────────────────── */
  function _esc (str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* ── Show/hide UI states ─────────────────────────────────── */
  function showLoading ()  {
    _el('db-loading')     ?.removeAttribute('hidden');
    _el('db-error-state') ?.setAttribute('hidden', '');
    _el('db-content')     ?.setAttribute('hidden', '');
  }
  function showError ()    {
    _el('db-loading')     ?.setAttribute('hidden', '');
    _el('db-error-state') ?.removeAttribute('hidden');
    _el('db-content')     ?.setAttribute('hidden', '');
  }
  function showContent ()  {
    _el('db-loading')     ?.setAttribute('hidden', '');
    _el('db-error-state') ?.setAttribute('hidden', '');
    _el('db-content')     ?.removeAttribute('hidden');
    console.log('loading_hidden');
    console.log('content_shown');
  }

  /* ── Boot sequence ───────────────────────────────────────────────────────── */
  /* ── setup()    : runs once on DOMContentLoaded — wires all static UI       */
  /* ── loadData() : fetches account data and renders — safe to retry          */
  function setup () {
    if (!_isAuthenticated()) {
      window.location.href = 'login.html';
      return;
    }

    initSidebar();
    initNav();
    initLogout();
    initDownloadBtns();

    // Retry button: replace its node to guarantee no stacked listeners,
    // then attach a single click handler that calls loadData().
    const retryBtn = _el('db-retry-btn');
    if (retryBtn) {
      const fresh = retryBtn.cloneNode(true);
      retryBtn.parentNode.replaceChild(fresh, retryBtn);
      fresh.addEventListener('click', loadData);
    }

    loadData();
  }

  /* ── Data-only boot — safe to call again on retry ───────────────────────── */
  async function loadData () {
    // Show loading spinner, hide error and content
    showLoading();

    try {
      const account = await GhostDashboard.loadAccount();
      console.log('dashboard_fetch_complete');

      // Populate all sections
      console.log('dashboard_render_start');
      try { renderUserInfo(account); }
        catch (err) { console.error("renderUserInfo failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderStatCards(account); }
        catch (err) { console.error("renderStatCards failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderKeyCard(account); }
        catch (err) { console.error("renderKeyCard failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderLicenseDetails(account); }
        catch (err) { console.error("renderLicenseDetails failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderSettings(account); }
        catch (err) { console.error("renderSettings failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderActivity(account); }
        catch (err) { console.error("renderActivity failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderDownloads(account); }
        catch (err) { console.error("renderDownloads failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      try { renderPurchases(account); }
        catch (err) { console.error("renderPurchases failed:", err); console.error(err.stack); console.error("account snapshot:", JSON.stringify(account, null, 2)); throw err; }

      // Wire reset modal after key is loaded
      try { initResetModal(account.license.key); }
        catch (err) { console.error("initResetModal failed:", err); console.error(err.stack); console.error("license.key:", account.license?.key); throw err; }

      console.log('dashboard_render_complete');

      // Reveal content — stop here; never fall through to showError()
      showContent();
      return;

    } catch (err) {
      if (err.message === 'auth') {
        window.location.href = 'login.html';
      } else {
        console.error("Dashboard Exception");
        console.error(err);
        console.error(err.stack);
        showError();
      }
    }
  }

  // Kick off on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }

})();
