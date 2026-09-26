/* Hairshalo — policy pages.
 *
 * The business's own details (legal name, contact email, phone, address,
 * GSTIN) and its own returns text come from the Back Office
 * (/api/site-content), the same source as the storefront footer. Nothing is
 * written here: a detail the shop has not entered stays marked "To be
 * provided" on the page, and the draft notice at the top stays visible while
 * anything is missing. Without JavaScript, or with the API down, the page
 * still reads in full with every detail marked.
 */
(function () {
  'use strict';

  function apiBase() {
    if (window.__VERA_API_BASE__) return window.__VERA_API_BASE__;
    var h = location.hostname;
    if (location.protocol === 'file:' || h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]') {
      return 'http://localhost:8010/api';
    }
    return '/api';
  }

  var LABELS = {
    legal_name: 'Registered business name',
    email: 'Contact email',
    phone: 'Contact phone',
    address: 'Registered address',
    gstin: 'GSTIN'
  };

  function fill(business, policies) {
    var missing = 0;
    document.querySelectorAll('[data-biz]').forEach(function (node) {
      var key = node.getAttribute('data-biz');
      var value = String((business || {})[key] || '').trim();
      // The shop name is set by default to "Hairshalo"; that is a trading
      // name, not proof of a registered legal entity, so it is shown but the
      // registered name is still asked for where the page needs it.
      if (value && !(key === 'legal_name' && node.hasAttribute('data-registered') && value === 'Hairshalo')) {
        if (key === 'email') {
          var a = document.createElement('a');
          a.href = 'mailto:' + value;
          a.textContent = value;
          node.replaceChildren(a);
        } else {
          node.textContent = value;
        }
        node.classList.remove('tbp');
      } else {
        missing++;
      }
    });

    // The shop's own returns text, when it has written one.
    var returns = String((policies || {}).returns || '').trim();
    var slot = document.getElementById('ownerReturns');
    if (slot && returns) {
      slot.textContent = returns;
      slot.hidden = false;
      var pending = document.getElementById('returnsPending');
      if (pending) pending.hidden = true;
    }

    var draft = document.getElementById('lgDraft');
    if (draft) draft.hidden = missing === 0 && !document.querySelector('.tbp');
  }

  function markAll() {
    document.querySelectorAll('[data-biz]').forEach(function (node) {
      if (!node.textContent.trim()) node.textContent = 'To be provided: ' + (LABELS[node.getAttribute('data-biz')] || 'detail');
    });
  }

  markAll();
  fetch(apiBase() + '/site-content', { headers: { Accept: 'application/json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (content) { if (content) fill(content.business, content.policies); })
    .catch(function () { /* the page already reads in full, with details marked */ });
})();
