/* Shop Mode page. Every string that came from outside (request, provider, model) is put on the
   page as a text node (or an attribute value), never parsed as markup. */
(function () {
  'use strict';

  var EXAMPLES = [
    'modern couches under $1000',
    'coffee tables with a mix of wood and metal with storage',
    'boho curtains'
  ];
  var RATINGS = [['love', 'Love'], ['like', 'Like'], ['dislike', 'Dislike']];
  var STORAGE_KEY = 'shop.session';

  var state = {
    sessionId: null, search: null, ratings: {}, resolved: {},
    used: 0, cap: 0, busy: false
  };

  var $ = function (id) { return document.getElementById(id); };

  function h(tag, props, children) {
    var el = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === 'class') el.className = props[k];
      else el.setAttribute(k, props[k]);
    });
    (children || []).forEach(function (c) {
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return el;
  }

  function storeGet() {
    try { return window.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }
  function storeSet(value) {
    try {
      if (value) window.localStorage.setItem(STORAGE_KEY, value);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* storage unavailable: the session just will not survive a reload */ }
  }

  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || ('Request failed (' + res.status + ')'));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function setStatus(text, isError) {
    var el = $('status');
    el.textContent = text || '';
    el.className = isError ? 'status error' : 'status';
  }

  function money(value, currency) {
    if (value === null || value === undefined) return null;
    var cur = currency && currency !== 'USD' ? currency + ' ' : '$';
    return cur + Number(value).toLocaleString('en-US', {
      minimumFractionDigits: value % 1 === 0 ? 0 : 2, maximumFractionDigits: 2
    });
  }

  function usd(value) {
    return '$' + Number(value).toFixed(value < 0.1 ? 4 : 3);
  }

  /* ---- chips: how the request was understood ---- */

  function priceText(q) {
    var lo = q.price_min, hi = q.price_max;
    if (lo !== null && hi !== null) return money(lo) + ' to ' + money(hi);
    if (hi !== null) return 'up to ' + money(hi);
    if (lo !== null) return 'from ' + money(lo);
    return null;
  }

  function renderChips(q) {
    var box = $('chips');
    box.textContent = '';
    if (!q) return;
    var add = function (key, value, extra) {
      box.appendChild(h('span', { class: 'chip' + (extra ? ' ' + extra : '') }, [
        h('span', { class: 'k' }, [key]), value
      ]));
    };
    add('Search', q.search_text);
    if (q.category) add('Category', q.category);
    var price = priceText(q);
    if (price) add('Price', price);
    q.style_terms.forEach(function (t) { add('Style', t); });
    q.material_terms.forEach(function (t) { add('Material', t); });
    q.must_have.forEach(function (t) { add('Must have', t); });
    q.avoid.forEach(function (t) { add('Avoid', t, 'avoid'); });
  }

  function renderStats() {
    var s = state.search;
    var el = $('stats');
    if (!s) {
      el.textContent = state.cap ? 'Link lookups: ' + state.used + ' of ' + state.cap + ' this session' : '';
      return;
    }
    el.textContent = [
      'Search took ' + s.seconds.total.toFixed(1) + ' s',
      s.serper_calls + (s.serper_calls === 1 ? ' Serper call' : ' Serper calls'),
      'est. ' + usd(s.total_usd) + ' (Anthropic ' + usd(s.anthropic_usd) + ', Serper ' + usd(s.provider_usd) + ')',
      'link lookups ' + state.used + ' of ' + state.cap
    ].join(' · ');
  }

  /* ---- cards ---- */

  function resolvedLine(card, id) {
    var el = card.querySelector('.resolved');
    var r = state.resolved[id];
    el.textContent = '';
    el.className = 'resolved';
    if (!r) return;
    if (r.status === 'resolved') {
      var parts = ['Sold by ' + r.seller];
      if (r.price !== null) parts.push(money(r.price));
      if (r.shipping_status === 'free') parts.push('free shipping');
      else if (r.shipping_status === 'known' && r.shipping !== null) parts.push('shipping ' + money(r.shipping));
      else parts.push('shipping not listed');
      el.textContent = parts.join(' · ');
    } else {
      el.textContent = r.note;
      el.className = 'resolved warn';
    }
  }

  function syncCard(card, id) {
    var rec = state.ratings[id] || { rating: null, comment: '' };
    card.classList.toggle('disliked', rec.rating === 'dislike');
    card.querySelectorAll('.rate').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-rating') === rec.rating ? 'true' : 'false');
    });
    var note = card.querySelector('.note');
    if (document.activeElement !== note) note.value = rec.comment || '';
    resolvedLine(card, id);
  }

  function safeUrl(value) {
    try {
      var u = new URL(value);
      return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null;
    } catch (e) { return null; }
  }

  function resolve(id) {
    return api('POST', '/api/resolve', { session_id: state.sessionId, result_id: id })
      .then(function (data) {
        state.resolved[id] = data.resolved;
        state.used = data.lookups_used;
        state.cap = data.lookups_cap;
        renderStats();
        var card = document.querySelector('.card[data-id="' + CSS.escape(id) + '"]');
        if (card) resolvedLine(card, id);
        return data.resolved;
      });
  }

  function setRating(id, rating) {
    var prev = state.ratings[id] || { rating: null, comment: '' };
    var next = prev.rating === rating ? null : rating;
    state.ratings[id] = { rating: next, comment: prev.comment };
    var card = document.querySelector('.card[data-id="' + CSS.escape(id) + '"]');
    if (card) syncCard(card, id);
    api('POST', '/api/rate', { session_id: state.sessionId, result_id: id, rating: next })
      .then(function () {
        if (next === 'love') return resolve(id).catch(function () { /* shown on Open instead */ });
      })
      .catch(function (err) {
        state.ratings[id] = prev;
        if (card) syncCard(card, id);
        setStatus(err.message, true);
      });
  }

  function saveComment(id, input) {
    var prev = state.ratings[id] || { rating: null, comment: '' };
    var value = input.value.trim();
    if (value === (prev.comment || '')) return;
    state.ratings[id] = { rating: prev.rating, comment: value };
    api('POST', '/api/rate', { session_id: state.sessionId, result_id: id, comment: value })
      .catch(function (err) {
        state.ratings[id] = prev;
        input.value = prev.comment || '';
        setStatus(err.message, true);
      });
  }

  function openResult(id, button) {
    // Open the tab now (inside the click) so it is not blocked, then send it to the retailer.
    var tab = window.open('about:blank', '_blank');
    button.disabled = true;
    button.textContent = 'Opening…';
    resolve(id).then(function (r) {
      var url = safeUrl(r.link);
      if (tab && url) {
        tab.opener = null;
        tab.location.href = url;
      } else if (tab) {
        tab.close();
      }
      if (!url) setStatus('No link is available for this product.', true);
    }).catch(function (err) {
      if (tab) tab.close();
      setStatus(err.message, true);
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Open';
    });
  }

  function renderCard(r) {
    var thumb = h('div', { class: 'thumb' });
    if (r.thumbnail_url && r.thumbnail_url.indexOf('https://') === 0) {
      var img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', function () {
        thumb.className = 'thumb noimg';
        thumb.textContent = (r.title.replace(/^\[[^\]]*\]\s*/, '').charAt(0) || '?').toUpperCase();
      });
      img.src = r.thumbnail_url;
      thumb.appendChild(img);
    } else {
      thumb.className = 'thumb noimg';
      thumb.textContent = (r.title.charAt(0) || '?').toUpperCase();
    }

    var price = money(r.price, r.currency);
    var priceEl = h('span', { class: price ? 'price' : 'price na' }, [price || 'Price not listed']);
    var title = h('h3', { class: 'title', title: r.title }, [r.title]);
    var body = h('div', { class: 'body' }, [
      title,
      h('div', { class: 'price-row' }, [priceEl]),
      h('div', { class: 'seller' }, [r.seller || 'Seller not listed'])
    ]);
    if (r.size_hint) body.appendChild(h('span', { class: 'size' }, [r.size_hint]));

    var rates = h('div', { class: 'rates', role: 'group', 'aria-label': 'Rate this product' });
    RATINGS.forEach(function (pair) {
      var b = h('button', {
        type: 'button', class: 'rate ' + pair[0], 'data-rating': pair[0], 'aria-pressed': 'false'
      }, [pair[1]]);
      b.addEventListener('click', function () { setRating(r.id, pair[0]); });
      rates.appendChild(b);
    });
    body.appendChild(rates);

    var note = h('input', {
      type: 'text', class: 'note', maxlength: '200', placeholder: 'Add a note (optional)',
      'aria-label': 'Note about this product'
    });
    note.addEventListener('blur', function () { saveComment(r.id, note); });
    note.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); note.blur(); }
    });
    body.appendChild(note);

    var open = h('button', { type: 'button', class: 'open' }, ['Open']);
    open.addEventListener('click', function () { openResult(r.id, open); });
    body.appendChild(h('div', { class: 'open-row' }, [open]));
    body.appendChild(h('div', { class: 'resolved', role: 'status' }));

    var card = h('article', { class: 'card', 'data-id': r.id }, [thumb, body]);
    syncCard(card, r.id);
    return card;
  }

  function renderResults() {
    var grid = $('grid');
    grid.textContent = '';
    var s = state.search;
    $('empty').hidden = !!s;
    renderChips(s && s.query);
    renderStats();
    if (!s) return;
    $('request').value = s.request;
    s.results.forEach(function (r) { grid.appendChild(renderCard(r)); });
    var msg = s.warnings.join(' ');
    setStatus(msg || (s.results.length
      ? s.results.length + ' results'
      : 'No results.'), false);
  }

  function showSkeleton() {
    var grid = $('grid');
    grid.textContent = '';
    for (var i = 0; i < 8; i++) grid.appendChild(h('div', { class: 'skeleton' }));
    $('empty').hidden = true;
  }

  function adopt(data) {
    state.sessionId = data.session_id || state.sessionId;
    state.search = data.search || null;
    state.ratings = data.ratings || {};
    state.resolved = data.resolved || {};
    state.used = data.lookups_used || 0;
    state.cap = data.lookups_cap || 0;
    if (state.sessionId) storeSet(state.sessionId);
    var badge = $('mode-badge');
    if (data.mode === 'mock') {
      badge.hidden = false;
      badge.textContent = 'mock data';
    }
  }

  function search(text) {
    var request = text.trim();
    if (!request || state.busy) return;
    state.busy = true;
    $('search-btn').disabled = true;
    setStatus('Searching…', false);
    showSkeleton();
    api('POST', '/api/search', { session_id: state.sessionId, request: request })
      .then(function (data) {
        adopt(data);
        renderResults();
      })
      .catch(function (err) {
        if (err.status === 404) { state.sessionId = null; storeSet(null); }
        $('grid').textContent = '';
        setStatus(err.message, true);
        $('empty').hidden = !!state.search;
        if (state.search) renderResults();
      })
      .then(function () {
        state.busy = false;
        $('search-btn').disabled = false;
      });
  }

  function init() {
    var ex = $('examples');
    EXAMPLES.forEach(function (text) {
      var b = h('button', { type: 'button' }, [text]);
      b.addEventListener('click', function () { $('request').value = text; search(text); });
      ex.appendChild(b);
    });
    $('search-form').addEventListener('submit', function (e) {
      e.preventDefault();
      search($('request').value);
    });
    var saved = storeGet();
    var path = '/api/session' + (saved ? '?id=' + encodeURIComponent(saved) : '');
    api('GET', path).then(function (data) {
      adopt(data);
      renderResults();
    }).catch(function (err) {
      if (err.status === 404) storeSet(null);
      renderResults();
    });
  }

  init();
})();
