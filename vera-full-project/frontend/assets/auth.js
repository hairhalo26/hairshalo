/* Hairshalo — shared client for the standalone account pages.
 *
 * These pages talk to the SAME backend and use the SAME token key as the
 * storefront modal in index.html ('vera.customerToken'), so signing in here
 * signs you in there too. The key is an internal identifier, not branding —
 * renaming it would sign every existing customer out.
 *
 * Nothing here decides anything the server owns. It validates input early to
 * save a round trip, but the backend re-validates everything and its answer
 * wins.
 */
(function (global) {
  'use strict';

  /* Same resolution rule as index.html: a local page talks to the dev API on
     8010, anything else is a real deployment serving /api from its own origin. */
  function resolveApiBase(protocol, hostname) {
    if (protocol === 'file:') return 'http://localhost:8010/api';
    if (hostname === 'localhost' || hostname === '127.0.0.1' ||
        hostname === '::1' || hostname === '[::1]') return 'http://localhost:8010/api';
    return '/api';
  }

  var API_BASE = global.__VERA_API_BASE__ ||
    resolveApiBase(global.location.protocol, global.location.hostname);

  var TOKEN_KEY = 'vera.customerToken';

  /* ---------------------------------------------------------------- storage */
  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || null; } catch (e) { return null; }
  }
  function setToken(token) {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* private mode: the session simply will not persist */ }
  }

  /* ------------------------------------------------------------------- http */
  var NETWORK_MESSAGE =
    'We could not reach Hairshalo just now. Check your connection and try again.';
  var SERVER_MESSAGE =
    'Something went wrong on our side. Please try again in a moment.';

  /* One place decides what a customer is allowed to see about a failure.
     4xx carries the server's own wording (it is written for customers);
     5xx and transport errors never do, so no stack trace, SQL fragment or
     file path can reach the page. */
  function friendly(status, detail) {
    if (status >= 500 || status === 0) return SERVER_MESSAGE;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    if (Array.isArray(detail) && detail.length) {
      var first = detail[0];
      if (first && typeof first.msg === 'string') return first.msg;
    }
    if (status === 401) return 'Those details did not match an account.';
    if (status === 403) return 'You do not have access to that.';
    if (status === 404) return 'We could not find that.';
    if (status === 429) return 'Too many attempts. Please wait a moment and try again.';
    return SERVER_MESSAGE;
  }

  function api(path, options) {
    options = options || {};
    var headers = {};
    if (options.body) headers['Content-Type'] = 'application/json';
    if (options.auth) {
      var t = getToken();
      if (t) headers['Authorization'] = 'Bearer ' + t;
    }
    return fetch(API_BASE + path, {
      method: options.method || 'GET',
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (res) {
      if (res.status === 204) return {};
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(friendly(res.status, data && data.detail));
          err.status = res.status;
          // A token that is gone or expired should not leave the page half
          // signed-in; drop it so the next load starts clean.
          if (res.status === 401 && options.auth) setToken(null);
          throw err;
        }
        return data;
      });
    }).catch(function (err) {
      if (err instanceof TypeError) throw new Error(NETWORK_MESSAGE);
      throw err;
    });
  }

  /* --------------------------------------------------------- password policy
     Mirrored from backend/app/accounts.py so the customer sees the real rules
     instead of discovering them one failed submit at a time. The backend still
     enforces them; this only shortens the feedback loop. */
  var MIN_PASSWORD_LENGTH = 10;
  var MAX_PASSWORD_BYTES = 72;
  var WEAK_PASSWORDS = [
    'password', 'password1', 'password123', '12345678', '123456789', '1234567890',
    'qwertyuiop', 'letmein123', 'iloveyou1', 'admin12345', 'welcome123',
    'changeme123', 'verahair123', 'hairhalo123', 'hairshalo123'
  ];

  function byteLength(s) {
    try { return new TextEncoder().encode(s).length; } catch (e) { return s.length; }
  }

  /* Returns the rule states, so a page can render a live checklist and also
     ask "is this submittable yet?". */
  function passwordRules(password, email) {
    password = password || '';
    var local = String(email || '').trim().toLowerCase().split('@')[0];
    var lower = password.toLowerCase();
    return [
      { id: 'len', label: 'At least ' + MIN_PASSWORD_LENGTH + ' characters',
        ok: password.length >= MIN_PASSWORD_LENGTH },
      { id: 'max', label: 'Not longer than ' + MAX_PASSWORD_BYTES + ' characters',
        ok: password.length === 0 || byteLength(password) <= MAX_PASSWORD_BYTES },
      { id: 'weak', label: 'Not a commonly used password',
        ok: password.length === 0 || WEAK_PASSWORDS.indexOf(lower) === -1 },
      { id: 'email', label: 'Does not contain your email name',
        ok: password.length === 0 || !local || lower.indexOf(local) === -1 }
    ];
  }

  function passwordOk(password, email) {
    if (!password) return false;
    return passwordRules(password, email).every(function (r) { return r.ok; });
  }

  /* ------------------------------------------------------------- validation */
  // Deliberately permissive: the server is the authority on deliverability,
  // and an over-strict pattern rejects addresses that genuinely work.
  function emailLooksValid(value) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(value || '').trim());
  }

  // Optional field. Accepts international formats; only rejects input that
  // cannot be a phone number at all.
  function phoneLooksValid(value) {
    var v = String(value || '').trim();
    if (!v) return true;
    var digits = v.replace(/[^0-9]/g, '');
    return /^[0-9+()\-.\s]+$/.test(v) && digits.length >= 7 && digits.length <= 15;
  }

  /* ------------------------------------------------------------------- dom */
  function $(sel, root) { return (root || document).querySelector(sel); }

  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.dataset.label || button.querySelector('.label').textContent;
      button.querySelector('.label').textContent = busyLabel || 'Please wait…';
      button.classList.add('busy');
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    } else {
      if (button.dataset.label) button.querySelector('.label').textContent = button.dataset.label;
      button.classList.remove('busy');
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }

  function showNotice(el, kind, message) {
    if (!el) return;
    el.className = 'notice show ' + kind;
    el.textContent = message;
    // Announced by screen readers without stealing focus mid-typing.
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  }
  function hideNotice(el) {
    if (!el) return;
    el.className = 'notice';
    el.textContent = '';
  }

  function fieldError(input, message) {
    var box = input.closest('.field').querySelector('.err');
    if (message) {
      box.textContent = message;
      box.classList.add('show');
      input.setAttribute('aria-invalid', 'true');
    } else {
      box.textContent = '';
      box.classList.remove('show');
      input.removeAttribute('aria-invalid');
    }
    return !message;
  }

  /* Show/hide control. Type toggling only — the value is never logged or
     copied anywhere. */
  function attachPasswordToggle(input) {
    var wrap = input.closest('.control');
    if (!wrap) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle';
    btn.textContent = 'Show';
    btn.setAttribute('aria-label', 'Show password');
    btn.addEventListener('click', function () {
      var showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.textContent = showing ? 'Show' : 'Hide';
      btn.setAttribute('aria-label', (showing ? 'Show' : 'Hide') + ' password');
      input.focus();
    });
    wrap.classList.add('has-toggle');
    wrap.appendChild(btn);
  }

  function renderPasswordRules(listEl, password, email) {
    if (!listEl) return;
    listEl.innerHTML = passwordRules(password, email).map(function (r) {
      var cls = !password ? '' : (r.ok ? 'ok' : 'bad');
      return '<li class="' + cls + '">' + r.label + '</li>';
    }).join('');
  }

  function queryParam(name) {
    try {
      return new URLSearchParams(global.location.search).get(name);
    } catch (e) { return null; }
  }

  function redirect(path) { global.location.href = path; }

  global.HairshaloAuth = {
    API_BASE: API_BASE,
    TOKEN_KEY: TOKEN_KEY,
    MIN_PASSWORD_LENGTH: MIN_PASSWORD_LENGTH,
    getToken: getToken,
    setToken: setToken,
    api: api,
    passwordRules: passwordRules,
    passwordOk: passwordOk,
    emailLooksValid: emailLooksValid,
    phoneLooksValid: phoneLooksValid,
    $: $,
    setBusy: setBusy,
    showNotice: showNotice,
    hideNotice: hideNotice,
    fieldError: fieldError,
    attachPasswordToggle: attachPasswordToggle,
    renderPasswordRules: renderPasswordRules,
    queryParam: queryParam,
    redirect: redirect
  };
})(window);
