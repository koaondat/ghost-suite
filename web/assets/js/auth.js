/* ============================================================
   auth.js — Ghost authentication forms
   ============================================================
   Architecture notes
   ------------------
   • All API calls go through the `GhostAuth` object below.
     To connect real endpoints, replace the stub bodies inside
     GhostAuth.login() and GhostAuth.register() — everything
     else (validation, loading state, error display) is
     already wired and will work unchanged.

   • Passwords are NEVER logged or stored here.  The form data
     is serialised directly into the fetch body as JSON and the
     reference is discarded after the call.

   • Secrets such as JWT signing keys belong on the server
     only.  This file contains no credentials.
   ============================================================ */

(function () {
  'use strict';

  /* ── API stub layer ────────────────────────────────────────
     Replace stub implementations with real fetch calls.
     Each function must return a Promise that resolves with
     { ok: true }  on success, or
     { ok: false, field?: string, message: string }  on failure.
     The `field` key, when present, pins the error to a specific
     input rather than the global alert banner.
  ─────────────────────────────────────────────────────────── */
  const GhostAuth = {

    /**
     * POST /api/auth/login
     * @param {{ identity: string, password: string, remember: boolean }} payload
     * @returns {Promise<{ok: boolean, message?: string}>}
     */
    login: async function (payload) {
      const res = await fetch('/api/auth/login', {
        method:      'POST',
        headers:     { 'Content-Type': 'application/json' },
        credentials: 'include',
        body:        JSON.stringify({
          identity: payload.identity,
          password: payload.password,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!data.ok) {
        return { ok: false, field: data.field, message: data.error || 'Invalid credentials. Please try again.' };
      }
      // Store the token for pages that read it from localStorage
      if (data.token) localStorage.setItem('ghost_token', data.token);
      return { ok: true, username: data.username, tier: data.tier };
    },

    /**
     * POST /api/auth/register
     * @param {{ username: string, email: string, password: string, license_key?: string }} payload
     * @returns {Promise<{ok: boolean, field?: string, message?: string}>}
     */
    register: async function (payload) {
      const res = await fetch('/api/auth/register', {
        method:      'POST',
        headers:     { 'Content-Type': 'application/json' },
        credentials: 'include',
        body:        JSON.stringify({
          username:    payload.username,
          email:       payload.email,
          password:    payload.password,
          license_key: payload.license_key || undefined,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!data.ok) {
        return { ok: false, field: data.field, message: data.error || 'Registration failed. Please try again.' };
      }
      return { ok: true };
    },

    /**
     * POST /api/auth/logout
     */
    logout: async function () {
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
      localStorage.removeItem('ghost_token');
    },
  };

  /* ── Helpers ─────────────────────────────────────────────── */
  function _delay (ms) { return new Promise(r => setTimeout(r, ms)); }

  /** Set or clear a per-field error. */
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

  /** Show / hide the global alert banner. */
  function showAlert (alertId, type, message) {
    const el = document.getElementById(alertId);
    if (!el) return;

    // icon paths
    const icons = {
      error:   '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
      success: '<polyline points="20 6 9 17 4 12"/>',
    };

    el.querySelector('.alert-icon').innerHTML = icons[type] || icons.error;
    el.querySelector('.alert-msg').textContent = message;
    el.className = `auth-alert auth-alert--${type}`;
    el.hidden = false;
  }

  function hideAlert (alertId) {
    const el = document.getElementById(alertId);
    if (el) el.hidden = true;
  }

  /** Toggle button loading state. */
  function setLoading (btnId, loading) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const text    = btn.querySelector('.btn-text');
    const spinner = btn.querySelector('.btn-spinner');
    btn.disabled = loading;
    if (loading) { btn.setAttribute('disabled', ''); } else { btn.removeAttribute('disabled'); }
    if (text)    text.style.opacity = loading ? '0.6' : '1';
    if (spinner) spinner.hidden = !loading;
  }

  /** Wire show/hide password toggle buttons. */
  function wirePasswordToggle (btnId, inputId) {
    const btn   = document.getElementById(btnId);
    const input = document.getElementById(inputId);
    if (!btn || !input) return;
    btn.addEventListener('click', () => {
      const isText   = input.type === 'text';
      input.type     = isText ? 'password' : 'text';
      btn.setAttribute('aria-pressed', String(!isText));
      btn.setAttribute('aria-label', isText ? 'Show password' : 'Hide password');
      btn.querySelector('.eye-show').style.display = isText ? '' : 'none';
      btn.querySelector('.eye-hide').style.display = isText ? 'none' : '';
    });
  }

  /* ── Password strength scorer ────────────────────────────── */
  function scorePassword (pw) {
    if (!pw) return 0;
    let score = 0;
    if (pw.length >= 8)  score++;
    if (pw.length >= 12) score++;
    if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) score++;
    if (/[0-9]/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    // clamp to 1-4
    return Math.max(1, Math.min(4, Math.round(score * 4 / 5)));
  }

  const STRENGTH_LABELS = { 1: 'Weak', 2: 'Fair', 3: 'Good', 4: 'Strong' };
  const STRENGTH_COLORS = { 1: '#ef4444', 2: '#f59e0b', 3: '#22d3ee', 4: '#22c55e' };

  function updateStrengthMeter (pw) {
    const fill  = document.getElementById('pw-strength-fill');
    const label = document.getElementById('pw-strength-label');
    if (!fill || !label) return;
    if (!pw) {
      fill.removeAttribute('data-level');
      fill.style.width = '0%';
      label.textContent = '';
      return;
    }
    const lvl = scorePassword(pw);
    fill.setAttribute('data-level', lvl);
    label.textContent  = STRENGTH_LABELS[lvl];
    label.style.color  = STRENGTH_COLORS[lvl];
  }

  /* ── Validators ─────────────────────────────────────────── */
  function validateLogin (identity, password) {
    const errors = {};
    if (!identity.trim())  errors.identity = 'Please enter your username or email.';
    if (!password)         errors.password = 'Please enter your password.';
    return errors;
  }

  function validateRegister (fields) {
    const errors = {};
    const { username, email, password, confirmPassword, licenseKey, terms } = fields;

    if (!username.trim())
      errors.username = 'Username is required.';
    else if (username.length < 3)
      errors.username = 'Username must be at least 3 characters.';
    else if (!/^[a-zA-Z0-9_\-]+$/.test(username))
      errors.username = 'Only letters, numbers, underscores, and hyphens allowed.';

    if (!email.trim())
      errors.email = 'Email address is required.';
    else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
      errors.email = 'Please enter a valid email address.';

    if (!password)
      errors['reg-password'] = 'Password is required.';
    else if (password.length < 8)
      errors['reg-password'] = 'Password must be at least 8 characters.';
    else if (scorePassword(password) < 2)
      errors['reg-password'] = 'Password is too weak. Add uppercase letters, numbers, or symbols.';

    if (!confirmPassword)
      errors.confirm = 'Please confirm your password.';
    else if (password !== confirmPassword)
      errors.confirm = 'Passwords do not match.';

    if (!licenseKey || !licenseKey.trim()) {
      errors.license = 'License key is required to register.';
    } else {
      const trimmed = licenseKey.trim();
      if (typeof LicenseFormat !== 'undefined' && !LicenseFormat.isValidLicenseFormat(trimmed)) {
        // Format pre-check: flag obviously wrong formats before the round-trip.
        // Expected: GHOST-XXXX-XXXX-XXXX-XXXX (4 groups of 4 alphanumeric chars).
        errors.license = 'License key format is invalid. Expected: GHOST-XXXX-XXXX-XXXX-XXXX';
      }
    }

    if (!terms)
      errors.terms = 'You must agree to the Terms of Service.';

    return errors;
  }

  /* ── License key normaliser ───────────────────────────────
     Trims whitespace and uppercases only.  Never alters the
     key value, inserts a prefix, or truncates the input.
  ─────────────────────────────────────────────────────────── */
  function wireLicenseKeyFormatter (inputId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener('input', () => {
      const cursor = input.selectionStart;
      // Only uppercase — do NOT strip dashes or prepend anything
      input.value = input.value.toUpperCase();
      try { input.setSelectionRange(cursor, cursor); } catch (_) { /* ignore */ }
    });
    // On paste: trim surrounding whitespace and uppercase
    input.addEventListener('paste', (e) => {
      e.preventDefault();
      const pasted = (e.clipboardData || window.clipboardData).getData('text');
      input.value  = pasted.trim().toUpperCase();
    });
  }

  /* ── Login form ─────────────────────────────────────────── */
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    wirePasswordToggle('pw-toggle-login', 'password');

    // Inline validation on blur
    loginForm.querySelector('#identity').addEventListener('blur', function () {
      fieldState('fg-identity', 'identity-err', this.value.trim() ? '' : 'Please enter your username or email.');
    });
    loginForm.querySelector('#password').addEventListener('blur', function () {
      fieldState('fg-password', 'password-err', this.value ? '' : 'Please enter your password.');
    });

    loginForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      hideAlert('login-alert');

      const identity = loginForm.querySelector('#identity').value.trim();
      const password = loginForm.querySelector('#password').value;
      const remember = loginForm.querySelector('#remember').checked;

      // Client-side validation
      const errors = validateLogin(identity, password);
      if (Object.keys(errors).length) {
        fieldState('fg-identity', 'identity-err', errors.identity || '');
        fieldState('fg-password', 'password-err', errors.password || '');
        return;
      }

      fieldState('fg-identity', 'identity-err', '');
      fieldState('fg-password', 'password-err', '');
      setLoading('login-btn', true);

      try {
        const result = await GhostAuth.login({ identity, password, remember });
        if (result.ok) {
          if (result.username) localStorage.setItem('ghost_username', result.username);
          showAlert('login-alert', 'success', 'Login successful! Redirecting…');
          setTimeout(() => { window.location.href = 'dashboard.html'; }, 800);
        } else {
          if (result.field === 'identity') {
            fieldState('fg-identity', 'identity-err', result.message);
          } else if (result.field === 'password') {
            fieldState('fg-password', 'password-err', result.message);
          } else {
            showAlert('login-alert', 'error', result.message || 'Login failed. Please try again.');
          }
          setLoading('login-btn', false);
        }
      } catch (_) {
        showAlert('login-alert', 'error', 'Network error. Please check your connection and try again.');
        setLoading('login-btn', false);
      }
    });
  }

  /* ── Register form ──────────────────────────────────────── */
  const registerForm = document.getElementById('register-form');
  if (registerForm) {
    wirePasswordToggle('pw-toggle-reg',     'reg-password');
    wirePasswordToggle('pw-toggle-confirm', 'confirm-password');
    wireLicenseKeyFormatter('license-key');

    // Live password strength meter
    registerForm.querySelector('#reg-password').addEventListener('input', function () {
      updateStrengthMeter(this.value);
    });

    // Inline validation on blur
    const blurRules = [
      ['#username',         'fg-username',     'username-err',       v => v.trim().length < 3  ? 'Username must be at least 3 characters.' : (!v.trim() ? 'Username is required.' : '')],
      ['#email',            'fg-email',        'email-err',          v => !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) ? 'Enter a valid email address.' : ''],
      ['#reg-password',     'fg-reg-password', 'reg-password-err',   v => v.length < 8 && v ? 'Password must be at least 8 characters.' : ''],
      ['#confirm-password', 'fg-confirm',      'confirm-err',        v => {
        const pw = registerForm.querySelector('#reg-password').value;
        return v && v !== pw ? 'Passwords do not match.' : '';
      }],
    ];
    blurRules.forEach(([sel, groupId, errId, validate]) => {
      const el = registerForm.querySelector(sel);
      if (el) el.addEventListener('blur', function () { fieldState(groupId, errId, validate(this.value)); });
    });

    registerForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      hideAlert('register-alert');

      const fields = {
        username:        registerForm.querySelector('#username').value.trim(),
        email:           registerForm.querySelector('#email').value.trim(),
        password:        registerForm.querySelector('#reg-password').value,
        confirmPassword: registerForm.querySelector('#confirm-password').value,
        licenseKey:      registerForm.querySelector('#license-key').value.trim(),
        terms:           registerForm.querySelector('#terms').checked,
      };

      const errors = validateRegister(fields);

      // Apply field-level errors
      fieldState('fg-username',     'username-err',      errors.username     || '');
      fieldState('fg-email',        'email-err',         errors.email        || '');
      fieldState('fg-reg-password', 'reg-password-err',  errors['reg-password'] || '');
      fieldState('fg-confirm',      'confirm-err',       errors.confirm      || '');
      fieldState('fg-license',      'license-err',       errors.license      || '');
      fieldState('fg-terms',        'terms-err',         errors.terms        || '');

      if (Object.keys(errors).length) return;

      setLoading('register-btn', true);

      // Build the payload — never include confirmPassword in transit
      const payload = {
        username:    fields.username,
        email:       fields.email,
        password:    fields.password,
        license_key: fields.licenseKey || undefined,
      };

      try {
        const result = await GhostAuth.register(payload);
        if (result.ok) {
          showAlert('register-alert', 'success', 'Account created! Redirecting to login…');
          setTimeout(() => { window.location.href = 'login.html?registered=1'; }, 1000);
        } else {
          // Map server-returned field errors back to inputs
          const fieldMap = {
            username:    ['fg-username', 'username-err'],
            email:       ['fg-email',    'email-err'],
            license:     ['fg-license',  'license-err'],
            license_key: ['fg-license',  'license-err'],
            password:    ['fg-reg-password', 'reg-password-err'],
          };
          if (result.field && fieldMap[result.field]) {
            const [gid, eid] = fieldMap[result.field];
            fieldState(gid, eid, result.message);
          } else {
            showAlert('register-alert', 'error', result.message || 'Registration failed. Please try again.');
          }
          setLoading('register-btn', false);
        }
      } catch (_) {
        showAlert('register-alert', 'error', 'Network error. Please check your connection and try again.');
        setLoading('register-btn', false);
      }
    });
  }

  /* ── "registered=1" success banner on login page ──────── */
  if (window.location.search.includes('registered=1') && document.getElementById('login-alert')) {
    showAlert('login-alert', 'success', 'Account created successfully! You can now sign in.');
    // Clean the URL without reloading
    history.replaceState(null, '', window.location.pathname);
  }

})();
