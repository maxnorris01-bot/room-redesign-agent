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
  var KEY_SESSION = 'shop.session';
  var KEY_SAVED_OPEN = 'shop.savedOpen';
  var KEY_WHY_OPEN = 'shop.whyOpen';
  var SEARCH_MSG = 'Searching';
  var AGAIN_MSG = 'Finding new results';
  var POLL_MS = 1000;
  var LOOKUP_MSG = 'Looking up the retailer link';

  var state = {
    sessionId: null, search: null, ratings: {}, resolved: {}, known: {},
    profile: null, summary: '', used: 0, cap: 0, againsUsed: 0, againsCap: 0,
    sessionUsd: 0, prepUsd: 0, prep: null, busy: 0, busyMessage: '', savedOpen: false, savedTab: 'love',
    confirming: false, whyOpen: false
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

  /* ---- browser storage (may be unavailable: everything still works without it) ---- */

  function storeGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storeSet(key, value) {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch (e) { /* ignored */ }
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

  /* ---- status line: loading, messages, errors with a retry ---- */

  var retryAction = null;

  function renderStatus(text, isError) {
    var el = $('status');
    var link = $('status-details');
    // the details link opens "How I decided", which holds every warning and number
    link.hidden = !(state.busy === 0 && !isError && state.search && state.search.kind === 'shop_again' &&
      state.profile && state.profile.trace && state.profile.trace.dimensions);
    el.textContent = text || '';
    el.className = isError ? 'status error' : 'status';
    $('retry').hidden = !(isError && retryAction);
  }

  function fail(message, retry) {
    retryAction = retry || null;
    if (state.busy > 0) { state.pendingError = message; return; }
    renderStatus(message, true);
  }

  function begin(message) {
    state.busy += 1;
    state.busyMessage = message;
    state.pendingError = null;
    retryAction = null;
    startStepBar(message);
    renderStatus(message, false);
    updateButtons();
  }

  function end() {
    state.busy = Math.max(0, state.busy - 1);
    if (state.busy === 0) {
      stopStepBar();
      if (state.pendingError) {
        renderStatus(state.pendingError, true);
        state.pendingError = null;
      } else if (!retryAction) {
        renderStatus(defaultStatus(), false);
      }
    }
    updateButtons();
  }

  function defaultStatus() {
    var s = state.search;
    if (!s) return '';
    if (s.kind === 'shop_again' && s.headline) return s.headline;
    var msg = s.warnings.join(' ');
    if (msg) return msg;
    return s.results.length ? s.results.length + (s.kind === 'shop_again' ? ' new results' : ' results') : 'No results.';
  }

  function hasRating() {
    return Object.keys(state.ratings).some(function (id) { return state.ratings[id].rating; });
  }

  function updateButtons() {
    $('search-btn').disabled = state.busy > 0;
    var againOk = hasRating() && state.againsUsed < state.againsCap && !!state.search;
    $('shop-again').disabled = state.busy > 0 || !againOk;
    $('shop-again').setAttribute('aria-disabled', $('shop-again').disabled ? 'true' : 'false');
    var wrap = $('again-wrap');
    if (!hasRating()) wrap.setAttribute('title', 'Rate a few results first');
    else if (state.againsUsed >= state.againsCap) wrap.setAttribute('title', 'Shop Again limit reached for this session');
    else wrap.removeAttribute('title');
  }

  /* ---- formatting ---- */

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

  /* ---- chips: the parsed request, and the preference profile ---- */

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

  // One chip. `tip` is an array of plain lines shown in a hover/focus tooltip (text nodes only).
  // opts: detail (text after the phrase), cls (extra classes), pin (a seller chip: click to pin).
  function prefChip(kind, polarity, text, tag, tip, opts) {
    opts = opts || {};
    var x = h('button', { type: 'button', class: 'x', 'aria-label': 'Remove ' + text }, ['×']);
    x.addEventListener('click', function () { removePref(kind, text, x); });
    var marker = opts.marker || (kind === 'guess' ? '?' : (opts.cls && opts.cls.indexOf('neutral') >= 0 ? '≈'
      : (polarity === 'dislike' ? '−' : '+')));
    var cls = 'chip pref ' + polarity + (kind === 'inferred' || kind === 'guess' ? ' ' + kind : '') +
      (opts.cls ? ' ' + opts.cls : '');
    var label;
    if (opts.pin) {
      label = h('button', {
        type: 'button', class: 'txt pinbtn',
        title: 'Click to pin or unpin this seller (a pinned seller can fill more of a batch)',
        'aria-pressed': opts.pinned ? 'true' : 'false'
      }, [text]);
      label.addEventListener('click', function () { pinSeller(text); });
    } else {
      label = h('span', { class: 'txt' }, [text]);
    }
    var kids = [h('span', { class: 'k' }, [marker]), label];
    if (opts.detail) kids.push(h('span', { class: 'detail' }, [opts.detail]));
    if (tag) kids.push(h('span', { class: 'tag' }, [tag]));
    if (opts.pinned) kids.push(h('span', { class: 'tag pinned-tag' }, ['pinned']));
    kids.push(x);
    if (tip && tip.length) {
      kids.push(h('span', { class: 'tip', role: 'tooltip' }, tip.map(function (line, i) {
        return h('span', { class: i === 0 ? 'tip-head' : 'tip-line' }, [line]);
      })));
    }
    return h('span', { class: cls, 'data-kind': kind, tabindex: '0' }, kids);
  }

  function inferredChip(e) {
    var tip;
    var opts = {};
    var tag = 'inferred';
    if (e.status === 'open') {
      tag = 'open';
      opts = { cls: 'neutral', detail: e.detail };
      tip = ['Open: you liked more than one value, so it does not steer the search or the order.']
        .concat(e.evidence_titles);
    } else if (e.status === 'mixed') {
      tag = 'mixed';
      opts = { cls: 'neutral', detail: e.detail };
      tip = ['Mixed: your loved and liked items cost different amounts, so price does not steer.']
        .concat(e.evidence_titles);
    } else if (e.attr_kind === 'leaning_toward' || e.attr_kind === 'leaning_away') {
      var toward = e.attr_kind === 'leaning_toward';
      tag = 'leaning';
      opts = { cls: 'lean ' + (toward ? 'lean-toward' : 'lean-away'), detail: e.detail, marker: toward ? '↗' : '↘' };
      tip = [toward
        ? 'Leaning toward: this value was liked more often than your ratings overall. A soft share of each batch (at least 30% where the results have it), never a filter.'
        : 'Leaning away: this value was liked far less often than your ratings overall. Held to at most 30% of each batch, never removed.',
        'Remove it to drop its effect for this session.'].concat(e.evidence_titles);
    } else if (e.attr_kind === 'exclude_marketplace') {
      tag = 'filter';
      opts = { cls: 'lean lean-filter', detail: e.detail, marker: '⊘' };
      tip = ['Marketplace and budget retailers are left out of the candidates before ranking.',
        'Remove the chip to include them again. It comes back only after 8 more ratings if the evidence still holds.'];
    } else if (e.attr_kind === 'seller' || e.attr_kind === 'tier') {
      opts = { pin: true, pinned: !!e.pinned, detail: e.detail };
      tip = ['Learned from what you rated. Click the name to pin it:']
        .concat(e.detail ? [e.detail] : [], e.evidence_titles);
    } else {
      tip = ['Learned from what you rated:'].concat(e.evidence_titles);
    }
    return prefChip('inferred', e.polarity, e.text, tag, tip, opts);
  }

  function pinSeller(phrase) {
    api('POST', '/api/preference/pin', { session_id: state.sessionId, phrase: phrase })
      .then(function (data) {
        state.profile = data.profile;
        state.summary = data.profile_summary;
        renderPrefs();
      })
      .catch(function (err) { fail(err.message, null); });
  }

  function renderPrefs() {
    var box = $('prefs');
    var p = state.profile;
    // shown whenever there are results, so the first Shop Again does not move the grid
    box.hidden = !state.search;
    var learned = p && (p.likes.length || p.dislikes.length || p.inferred.length);
    $('prefs-summary').textContent = state.summary || (p && !learned
      ? 'Nothing learned from your ratings yet. Rate a few more results to steer the next set.'
      : (learned ? '' : 'Rate a few results, then press Shop Again to steer the next set.'));
    var note = $('prefs-note');
    var fb = p && p.tagging && p.tagging.fallback;
    note.hidden = !fb;
    note.textContent = fb ? 'Could not read item details; only seller and price were learned.' : '';
    renderWhy();
    var chips = $('pref-chips');
    chips.textContent = '';
    if (!p) return;
    var inferred = function (pol, open) {
      return p.inferred.filter(function (e) {
        return e.polarity === pol && ((e.status === 'open' || e.status === 'mixed') === open);
      });
    };
    p.likes.forEach(function (t) { chips.appendChild(prefChip('like', 'like', t, '', ['From your own words'])); });
    inferred('like', false).forEach(function (e) { chips.appendChild(inferredChip(e)); });
    inferred('like', true).forEach(function (e) { chips.appendChild(inferredChip(e)); });
    p.dislikes.forEach(function (t) { chips.appendChild(prefChip('dislike', 'dislike', t, '', ['From your own words'])); });
    inferred('dislike', false).forEach(function (e) { chips.appendChild(inferredChip(e)); });
    (p.guesses || []).forEach(function (t) {
      chips.appendChild(prefChip('guess', 'guess', t, 'guess', [
        'A guess from your request wording', 'Nothing you rated confirms it, so it is not counted as a like.'
      ]));
    });
  }

  /* ---- "How I decided": the numbers behind every preference (text nodes only) ---- */

  function pct(x) { return Math.round(Number(x) * 100) + '%'; }

  function whyTable(head, rows) {
    var table = h('table', { class: 'why-table' });
    table.appendChild(h('thead', {}, [h('tr', {}, head.map(function (c) { return h('th', {}, [c]); }))]));
    var body = h('tbody');
    rows.forEach(function (r) {
      body.appendChild(h('tr', {}, r.map(function (c) { return h('td', {}, [String(c)]); })));
    });
    table.appendChild(body);
    return table;
  }

  function dimensionBlock(name, d) {
    var box = h('div', { class: 'why-dim' });
    var decision = d.decision || 'none';
    box.appendChild(h('h4', {}, [name.replace(/_/g, ' ') + ': ' + decision]));
    if (d.like_mass) {
      var values = Object.keys(d.like_mass);
      var rows = values.map(function (v) {
        var share = d.total_mass ? d.like_mass[v] / d.total_mass : 0;
        var exp = d.exposure && d.exposure[v] !== undefined ? pct(d.exposure[v]) : '-';
        return [v, d.like_mass[v], pct(share), exp];
      });
      box.appendChild(whyTable(['value', 'like mass', 'share of all', 'shown share'], rows));
      box.appendChild(h('p', { class: 'why-line' }, [
        'known coverage ' + pct(d.known_coverage) + ' of the like mass; ' +
        pct(d.exposure_coverage) + ' of shown results are tagged for it' +
        (d.lift !== undefined && d.lift !== null ? '; lift ' + d.lift : '')
      ]));
    }
    if (d.exposure_sources && Object.keys(d.exposure_sources).length) {
      var src = d.exposure_sources;
      box.appendChild(h('p', { class: 'why-line' }, [
        'shown results tagged for this from: ' + ['photo', 'title', 'keyword'].filter(function (k) {
          return src[k];
        }).map(function (k) { return src[k] + ' ' + (k === 'title' ? 'title (model)' : k); }).join(', ')
      ]));
    }
    if (d.leaves) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'labels: ' + Object.keys(d.leaves).map(function (k) { return k + ' ' + d.leaves[k]; }).join(', ')
      ]));
    }
    box.appendChild(h('p', { class: 'why-reason' }, [d.reason || '']));
    return box;
  }

  function countKeys(maps) {
    var seen = {};
    var keys = [];
    maps.forEach(function (m) {
      Object.keys(m || {}).forEach(function (k) { if (!seen[k]) { seen[k] = 1; keys.push(k); } });
    });
    return keys;
  }

  function compTable(label, before, after, batch, quota) {
    var keys = countKeys([before, after, batch]);
    if (!keys.length) return null;
    var head = [label, 'pool before'];
    if (after) head.push('pool after');
    head.push('final batch');
    if (quota) head.push('slots planned');
    var rows = keys.map(function (k) {
      var row = [k, (before || {})[k] || 0];
      if (after) row.push(after[k] || 0);
      row.push((batch || {})[k] || 0);
      if (quota) row.push(quota[k] === undefined ? '-' : quota[k]);
      return row;
    });
    return whyTable(head, rows);
  }

  function batchBlock(trace, tg) {
    var b = trace && trace.batch;
    if (!b || !b.batch) return null;
    var box = h('div', { class: 'why-dim why-batch' }, [h('h4', {}, ['Batch makeup (' + b.batch.size + ' results)'])]);
    var st = trace.shown_tags || {};
    var lines = [
      'Searches used: ' + (b.serper_calls || 0) + ' (' + (b.serper_credits || 0) + ' credits)',
      'shown results tagged ' + (st.tagged || 0) + ' of ' + (st.total || 0) + ' (' + (st.source || 'none') + ')'
    ];
    if (tg && tg.candidates_total !== undefined) {
      lines.push('candidates tagged ' + tg.candidates_tagged + ' of ' + tg.candidates_total +
        ' in ' + (tg.tag_calls || 0) + ' call(s)');
    }
    box.appendChild(h('p', { class: 'why-line' }, [lines.join('; ') + '.']));
    var tm = b.timings || {};
    var sec = function (v) { return (v === undefined ? 0 : Number(v)).toFixed(1) + ' s'; };
    box.appendChild(h('p', { class: 'why-line' }, [
      'Time: searches ' + sec(tm.searches) + ', preferences ' + sec(tm.preferences) +
      ', tagging what was shown ' + sec(tm.tagging_shown) + ', rank ' + sec(tm.rank) +
      ', tagging candidates ' + sec(tm.tagging) + ', total ' + sec(tm.total) +
      ' (aim ' + sec(tm.target_s) + ', hard stop ' + sec(tm.hard_stop_s) + ')' +
      (tm.stopped_before ? '; stopped before ' + tm.stopped_before : '') + '.'
    ]));
    if (b.tag_budget) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Candidate tagging: ' + b.tag_budget.queued + ' titles queued of a cap of ' + b.tag_budget.cap +
        ' (' + b.tag_budget.reserve + ' held back for supply and refill).'
      ]));
    }
    var tl = b.timeline;
    if (tl && tl.phases && tl.phases.length) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Phases: ' + tl.phases.map(function (p) {
          return p.phase + ' ' + Number(p.start).toFixed(1) + ' to ' +
            (p.end === undefined ? '?' : Number(p.end).toFixed(1)) + ' s';
        }).join(', ') + '.'
      ]));
    }
    if (tl && tl.calls && tl.calls.length) {
      box.appendChild(whyTable(['call', 'start (s)', 'took (s)', 'queue wait (s)', 'retries'],
        tl.calls.map(function (c) {
          return [c.kind + ' ' + c.label, c.start, c.duration, c.queue_wait, c.retries];
        })));
    }
    var mq = b.model_queries;
    if (mq && mq.asked) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Model-written queries: ' + (mq.accepted.length ? mq.accepted.map(function (q) { return '"' + q + '"'; }).join(', ') : 'none accepted') +
        (mq.error ? ' (the call failed: ' + mq.error + ')' : '') + '; cost ' + usd(mq.usd) + '.'
      ]));
      (mq.dropped || []).forEach(function (d) {
        box.appendChild(h('p', { class: 'why-line' }, ['Dropped query "' + d.query + '": ' + d.reason + '.']));
      });
      (mq.searches || []).forEach(function (x) {
        box.appendChild(h('p', { class: 'why-line' }, [
          'Searched "' + x.query + '" page ' + x.page + ', found ' + x.found + ', ' + x.new_candidates + ' new' +
          (x.error ? ' (' + x.error + ')' : '') + '.'
        ]));
      });
    }
    if (b.marketplace_excluded) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Left out by the marketplace exclusion: ' + b.marketplace_excluded + ' candidate(s).'
      ]));
    }
    var ln = b.leanings;
    if (ln && (Object.keys(ln.toward).length || Object.keys(ln.away).length)) {
      Object.keys(ln.toward).forEach(function (k) {
        box.appendChild(h('p', { class: 'why-line' }, [
          'Leaning toward ' + k.replace(':', ' ').replace(/_/g, ' ') + ': ' + ln.toward[k].in_batch + ' of ' + b.batch.size +
          ' in the batch (aimed for at least ' + ln.toward[k].wanted + ' where the pool has it).'
        ]));
      });
      Object.keys(ln.away).forEach(function (k) {
        box.appendChild(h('p', { class: 'why-line' }, [
          'Leaning away from ' + k.replace(':', ' ').replace(/_/g, ' ') + ': ' + ln.away[k].in_batch + ' of ' + b.batch.size +
          ' in the batch (at most ' + ln.away[k].cap + ').'
        ]));
      });
    }
    var dd = b.dedupe;
    if (dd) {
      var rules = Object.keys(dd.by_rule || {}).map(function (k) {
        return k.replace(/_/g, ' ') + ' ' + dd.by_rule[k];
      }).join(', ');
      box.appendChild(h('p', { class: 'why-line' }, [
        'Candidates ' + dd.candidates + ': kept ' + dd.kept + '; seen before: ' + dd.shown_before +
        ' shown, ' + dd.rated_before + ' rated; duplicates ' + dd.duplicates + ' (' + rules + ').'
      ]));
    }
    if (b.shortfall && b.shortfall.got < b.shortfall.wanted) {
      box.appendChild(h('p', { class: 'why-line why-warn' }, [
        'Only ' + b.shortfall.got + ' of ' + b.shortfall.wanted + ' new results: ' + b.shortfall.explanation +
        '; ' + b.shortfall.seller_capped + ' held back by seller caps, ' + b.shortfall.guarded + ' by guards.'
      ]));
    }
    (b.refill || []).forEach(function (x) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Refill search for ' + x.value + ': "' + x.search + '" page ' + x.page + ', found ' + x.found + ', ' +
        x.new_candidates + ' new; ' + x.before + ' results became ' + x.after + '.'
      ]));
    });
    if (b.batch.tag_sources) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Tags behind the batch: ' + b.batch.tag_sources.model + ' from the model, ' +
        b.batch.tag_sources.keyword + ' from title keywords (a guard only acts on a candidate that carries a tag).'
      ]));
    }
    (b.supply || []).forEach(function (x) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Looked for more ' + x.value + ' (' + x.dimension.replace(/_/g, ' ') + '): searched "' + x.search + '" page ' + x.page +
        ', found ' + x.found + ', ' + x.new_candidates + ' new; ' + x.value + ' was ' + x.value_before + ' of the pool, now ' + x.value_after + '.'
      ]));
    });
    (b.peers || []).forEach(function (x) {
      box.appendChild(h('p', { class: 'why-line' }, [
        (x.kind === 'seller' ? 'Seller you liked, ' : 'Peer retailer ') + x.peer + ': searched "' + x.search + '" page ' + x.page + ', found ' + x.found + ', ' +
        x.new_candidates + ' new candidates' + (x.error ? ' (' + x.error + ')' : '') + '.'
      ]));
    });
    if (b.tier) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Affinity tier ' + b.tier.tier.replace(/_/g, ' ') + ': up to ' + b.tier.cap + ' of ' + b.batch.size +
        ' results' + (b.tier.pinned ? ' (pinned)' : '') + ', ' + b.tier.quota + ' reserved.'
      ]));
    }
    var tierRows = countKeys([b.batch.tier]).map(function (k) { return [k.replace(/_/g, ' '), b.batch.tier[k]]; });
    if (tierRows.length) box.appendChild(whyTable(['retailer tier', 'final batch'], tierRows));
    (trace.tag_calls || []).forEach(function (c, i) {
      box.appendChild(h('p', { class: 'why-line' }, [
        'Tagging call ' + (i + 1) + (c.retry ? ' (retry)' : '') + ': ' + c.items + ' items, limit ' + c.max_tokens +
        ' tokens, stop ' + (c.stop_reason || 'none') + ', ' + (c.json_ok ? 'valid JSON' : 'not valid JSON') +
        ', ' + c.rows_parsed + ' rows read' + (c.had_thinking ? ', thinking present' : '') +
        (c.usage_known === false ? ', usage unknown (cost estimated, failed call)' : '') +
        (c.error ? ', error: ' + c.error : '') +
        (c.rows_parsed < c.items ? '; began: ' + c.raw : '') + '.'
      ]));
    });
    var quota = b.strata ? b.strata.quota : null;
    [['shape', 'shape'], ['color_finish', 'finish']].forEach(function (pair) {
      var t = compTable(pair[1], b.pool_before && b.pool_before[pair[0]], b.pool_after && b.pool_after[pair[0]],
        b.batch[pair[0]], pair[0] === 'shape' ? quota : null);
      if (t) box.appendChild(t);
    });
    var sellers = compTable('seller', b.pool_before && b.pool_before.seller, b.pool_after && b.pool_after.seller, b.batch.seller, null);
    if (sellers) box.appendChild(sellers);
    return box;
  }

  function renderWhy() {
    var panel = $('why');
    var toggle = $('why-toggle');
    var trace = state.profile && state.profile.trace;
    var has = trace && trace.dimensions;
    toggle.hidden = !has;
    toggle.textContent = state.whyOpen ? 'Hide how I decided' : 'How I decided';
    toggle.setAttribute('aria-expanded', state.whyOpen ? 'true' : 'false');
    panel.hidden = !(has && state.whyOpen);
    panel.textContent = '';
    if (!has || !state.whyOpen) return;
    var tg = state.profile.tagging || {};
    if (tg.from_cache !== undefined) {
      panel.appendChild(h('p', { class: 'why-line' }, [
        'Ratings read: ' + tg.from_cache + ' of ' + tg.items_total + ' reused from the background preparation (photo tags' +
        ' and comment readings already done); ' + tg.model_items + ' sent to the model now' +
        (tg.model_calls ? ' in ' + tg.model_calls + ' call(s)' : ', so no preferences call was needed') + '.'
      ]));
    }
    (state.search && state.search.warnings || []).forEach(function (w) {
      panel.appendChild(h('p', { class: 'why-line why-warn' }, ['Warning: ' + w]));
    });
    panel.appendChild(h('p', { class: 'why-line' }, [
      'Mode: ' + trace.mode + '. Item details read for ' + (tg.items_tagged || 0) + ' of ' + (tg.items_total || 0) +
      ' rated items; photos used ' + (tg.images_used || 0) + ' of ' + (tg.images_offered || 0) +
      (tg.retried ? '; the first call failed and was retried in two parts' : '') +
      (tg.fallback ? '; the details could not be read at all' : '') + '.'
    ]));
    var ev = trace.evidence;
    if (ev && ev.rows && ev.rows.length) {
      var evBox = h('div', { class: 'why-dim why-evidence' }, [
        h('h4', {}, ['like-rate evidence (baseline ' + pct(ev.baseline) + ' of all rated items liked)'])
      ]);
      evBox.appendChild(whyTable(['group', 'name', 'rated', 'liked', 'like rate', 'baseline', 'verdict', 'why'],
        ev.rows.map(function (r) {
          return [r.kind.replace(/_/g, ' '), r.name, r.rated, r.liked, pct(r.like_rate), pct(r.baseline), r.verdict, r.reason];
        })));
      evBox.appendChild(h('p', { class: 'why-reason' }, [
        'A seller, retailer tier or value earns a chip or a leaning from its own like rate (at least 4 rated, 2 liked, 1.5 times the baseline and 35%); how often it was shown does not matter.'
      ]));
      panel.appendChild(evBox);
    }
    var mk = trace.marketplace;
    if (mk && mk.reason) {
      panel.appendChild(h('p', { class: 'why-line' }, [
        'Marketplace exclusion: ' + (mk.decision === 'chip' ? 'on (' + mk.reason + ')' : 'off (' + mk.reason + ')') + '.'
      ]));
    }
    Object.keys(trace.dimensions).forEach(function (name) {
      panel.appendChild(dimensionBlock(name, trace.dimensions[name]));
    });
    var sel = trace.seller || {};
    var seller = h('div', { class: 'why-dim' }, [h('h4', {}, ['seller: ' + (sel.decision || 'none')])]);
    if (sel.candidates && sel.candidates.length) {
      seller.appendChild(whyTable(['seller', 'rated', 'liked', 'like rate', 'baseline', 'verdict'],
        sel.candidates.map(function (c) {
          return [c.seller, c.rated, c.liked, pct(c.like_rate), pct(c.baseline), c.verdict];
        })));
    }
    seller.appendChild(h('p', { class: 'why-reason' }, [sel.reason || '']));
    panel.appendChild(seller);
    var tr = trace.tier || {};
    if (tr.candidates && tr.candidates.length) {
      var tierBox = h('div', { class: 'why-dim' }, [h('h4', {}, ['retailer tier: ' + (tr.decision || 'none')])]);
      tierBox.appendChild(whyTable(['tier', 'rated', 'liked', 'like rate', 'liked sellers', 'verdict'],
        tr.candidates.map(function (c) {
          return [c.label, c.rated, c.liked, pct(c.like_rate),
            Object.keys(c.sellers).map(function (k) { return k + ' ' + c.sellers[k]; }).join(', '), c.verdict];
        })));
      panel.appendChild(tierBox);
    }
    var pr = trace.price || {};
    var batch = batchBlock(state.search && state.search.trace, state.search && state.search.tagging);
    if (batch) panel.appendChild(batch);
    var price = h('div', { class: 'why-dim' }, [h('h4', {}, ['price: ' + (pr.decision || 'none')])]);
    var bits = ['shown prices ' + (pr.shown_prices || 0)];
    if (pr.loved) bits.push('loved $' + pr.loved[0] + ' to $' + pr.loved[1]);
    if (pr.liked) bits.push('liked $' + pr.liked[0] + ' to $' + pr.liked[1]);
    if (pr.mean_position !== undefined) bits.push('mean position ' + pr.mean_position);
    if (pr.band) bits.push('band ' + pr.band + ' holds ' + pct(pr.mass_in_band) + ' of like mass, ' + pct(pr.liked_in_band) + ' of liked');
    price.appendChild(h('p', { class: 'why-line' }, [bits.join('; ')]));
    price.appendChild(h('p', { class: 'why-reason' }, [pr.reason || '']));
    panel.appendChild(price);
  }

  function removePref(kind, phrase, button) {
    button.disabled = true;
    api('POST', '/api/preference/remove', { session_id: state.sessionId, kind: kind, phrase: phrase })
      .then(function (data) {
        state.profile = data.profile;
        state.summary = data.profile_summary;
        renderPrefs();
      })
      .catch(function (err) {
        button.disabled = false;
        fail(err.message, null);
      });
  }

  /* ---- footer ---- */

  function diversityText(s) {
    if (s.kind !== 'shop_again' || !s.diversity) return '';
    var parts = Object.keys(s.diversity).map(function (d) {
      return d.replace('_', ' ') + ' ' + Math.round(s.diversity[d].share * 100) + '%';
    });
    if (s.seller_share) parts.push('top seller ' + Math.round(s.seller_share.share * 100) + '%');
    return parts.length ? 'largest share: ' + parts.join(', ') : '';
  }

  function alignmentText(s) {
    if (!s.alignment) return '';
    var c = (s.trace && s.trace.alignment_counted) || {};
    if (s.alignment.signals === 0) {
      if (c.other_chips > 0) {
        return 'matches your preferences: nothing to match against yet (only ' + c.other_chips +
          ' chip(s) that narrow the search: leaning away, the marketplace exclusion, open or mixed)';
      }
      return 'matches your preferences: n/a (nothing decided yet)';
    }
    var parts = [];
    if (c.converged) parts.push(c.converged + ' converged');
    if (c.seller_tier) parts.push(c.seller_tier + ' seller or retailer chip' + (c.seller_tier === 1 ? '' : 's'));
    if (c.leaning_toward) parts.push(c.leaning_toward + ' leaning toward');
    if (c.stated) parts.push(c.stated + ' of your own words');
    return 'matches your preferences: ' + s.alignment.matches + ' of ' + s.alignment.total +
      (parts.length ? ' (counted: ' + parts.join(', ') + ')' : '');
  }

  function timingText(s) {
    var tm = s.trace && s.trace.batch && s.trace.batch.timings;
    if (s.kind !== 'shop_again' || !tm) return '';
    var f = function (v) { return Number(v || 0).toFixed(1); };
    var bits = ['searches ' + f(tm.searches) + ' s', 'preferences ' + f(tm.preferences) + ' s',
      'tagging ' + f(Number(tm.tagging || 0) + Number(tm.tagging_shown || 0)) + ' s', 'rank ' + f(tm.rank) + ' s'];
    var tg = s.tagging || {};
    if (tg.tag_failed_usd) {
      bits.push('failed tagging calls ' + usd(tg.tag_failed_usd) +
        (tg.tag_estimated_usd ? ' (estimated, failed call)' : ''));
    }
    return bits.join(', ');
  }

  function tierText(s) {
    var b = s.trace && s.trace.batch;
    if (s.kind !== 'shop_again' || !b || !b.batch) return '';
    var bits = [];
    if (b.tier && b.batch.tier && b.batch.size) {
      bits.push('affinity tier ' + Math.round(100 * (b.batch.tier[b.tier.tier] || 0) / b.batch.size) + '%');
    }
    if (b.batch.distinct_sellers) bits.push(b.batch.distinct_sellers + ' sellers');
    return bits.join(', ');
  }

  function taggingText(s) {
    var tg = s.tagging;
    if (s.kind !== 'shop_again' || !tg || tg.items_total === undefined) return '';
    var bits = ['tagged ' + tg.items_tagged + ' of ' + tg.items_total + ' items',
      tg.images_used ? 'photos ' + tg.images_used : 'no photos'];
    if (tg.retried) bits.push('retried');
    if (tg.fallback) bits.push('fallback');
    if (tg.candidates_total !== undefined) {
      bits.push('candidates tagged ' + tg.candidates_tagged + ' of ' + tg.candidates_total);
    }
    return bits.join(', ');
  }

  function renderStats() {
    var s = state.search;
    var el = $('stats');
    var lookups = state.cap ? 'link lookups ' + state.used + ' of ' + state.cap : '';
    if (!s) { el.textContent = lookups ? lookups + ' this session' : ''; return; }
    var calls = s.serper_calls;
    el.textContent = [
      (s.kind === 'shop_again' ? 'Shop Again took ' : 'Search took ') + s.seconds.total.toFixed(1) + ' s',
      calls + (calls === 1 ? ' Serper call' : ' Serper calls') +
        (s.queries && s.queries.length > 1 ? ' (' + s.queries.length + ' searches)' : '') +
        ' = ' + s.serper_credits + ' credits (' + usd(s.provider_usd) + ')',
      'Anthropic ' + usd(s.anthropic_usd),
      'est. total ' + usd(s.total_usd),
      alignmentText(s),
      diversityText(s),
      tierText(s),
      timingText(s),
      taggingText(s),
      'session ' + usd(state.sessionUsd),
      state.prepUsd ? 'background preparation ' + usd(state.prepUsd) : '',
      lookups
    ].filter(Boolean).join(' · ');
  }

  /* ---- cards (the grid and the Saved panel share them) ---- */

  function cardsFor(id) {
    return document.querySelectorAll('.card[data-id="' + CSS.escape(id) + '"]');
  }

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

  function syncAll(id) {
    cardsFor(id).forEach(function (card) { syncCard(card, id); });
  }

  function safeUrl(value) {
    try {
      var u = new URL(value);
      return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null;
    } catch (e) { return null; }
  }

  function resolve(id, quiet) {
    if (!quiet) begin(LOOKUP_MSG);
    return api('POST', '/api/resolve', { session_id: state.sessionId, result_id: id })
      .then(function (data) {
        state.resolved[id] = data.resolved;
        state.used = data.lookups_used;
        state.cap = data.lookups_cap;
        renderStats();
        syncAll(id);
        return data.resolved;
      })
      .then(function (r) { if (!quiet) end(); return r; }, function (err) {
        if (!quiet) { fail(err.message, function () { resolve(id, false); }); end(); }
        throw err;
      });
  }

  function savedIdsNow() {
    return Object.keys(state.ratings).filter(function (id) {
      var r = state.ratings[id].rating;
      return (r === 'love' || r === 'like') && state.known[id];
    });
  }

  function setRating(id, rating) {
    var prev = state.ratings[id] || { rating: null, comment: '', rated_at: null };
    var next = prev.rating === rating ? null : rating;
    var before = savedIdsNow().join();
    state.ratings[id] = {
      rating: next, comment: prev.comment, rated_at: next ? new Date().toISOString().replace('Z', '+00:00') : null
    };
    syncAll(id);
    updateButtons();
    if (savedIdsNow().join() !== before) renderSaved();
    api('POST', '/api/rate', { session_id: state.sessionId, result_id: id, rating: next })
      .catch(function (err) {
        state.ratings[id] = prev;
        syncAll(id);
        updateButtons();
        renderSaved();
        fail(err.message, null);
      });
  }

  function saveComment(id, input) {
    var prev = state.ratings[id] || { rating: null, comment: '', rated_at: null };
    var value = input.value.trim();
    if (value === (prev.comment || '')) return;
    state.ratings[id] = { rating: prev.rating, comment: value, rated_at: prev.rated_at };
    api('POST', '/api/rate', { session_id: state.sessionId, result_id: id, comment: value })
      .then(function () { syncAll(id); })
      .catch(function (err) {
        state.ratings[id] = prev;
        input.value = prev.comment || '';
        fail(err.message, null);
      });
  }

  function openResult(id, button) {
    // Open the tab now (inside the click) so it is not blocked, then send it to the retailer.
    var tab = window.open('about:blank', '_blank');
    button.disabled = true;
    button.textContent = 'Opening…';
    resolve(id, false).then(function (r) {
      var url = safeUrl(r.link);
      if (tab && url) {
        tab.opener = null;
        tab.location.href = url;
      } else if (tab) {
        tab.close();
      }
      if (!url) fail('No link is available for this product.', null);
    }).catch(function () {
      if (tab) tab.close();
    }).then(function () {
      button.disabled = false;
      button.textContent = 'Open';
    });
  }

  function renderCard(r) {
    var thumb = h('div', { class: 'thumb' });
    var initial = (r.title.replace(/^\[[^\]]*\]\s*/, '').charAt(0) || '?').toUpperCase();
    if (r.thumbnail_url && r.thumbnail_url.indexOf('https://') === 0) {
      var img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', function () {
        thumb.className = 'thumb noimg';
        thumb.textContent = initial;
      });
      img.src = r.thumbnail_url;
      thumb.appendChild(img);
    } else {
      thumb.className = 'thumb noimg';
      thumb.textContent = initial;
    }

    var price = money(r.price, r.currency);
    var body = h('div', { class: 'body' }, [
      h('h3', { class: 'title', title: r.title }, [r.title]),
      h('div', { class: 'price-row' }, [
        h('span', { class: price ? 'price' : 'price na' }, [price || 'Price not listed'])
      ]),
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

    var open = h('button', {
      type: 'button', class: 'open', title: 'Look up the retailer link and open it'
    }, ['Open']);
    open.addEventListener('click', function () { openResult(r.id, open); });
    body.appendChild(h('div', { class: 'open-row' }, [open]));
    body.appendChild(h('div', { class: 'resolved', role: 'status' }));

    var card = h('article', { class: 'card', 'data-id': r.id }, [thumb, body]);
    syncCard(card, r.id);
    return card;
  }

  /* ---- the Saved panel ---- */

  function savedItems(kind) {
    return savedIdsNow().filter(function (id) { return state.ratings[id].rating === kind; })
      .sort(function (a, b) {
        return String(state.ratings[b].rated_at || '').localeCompare(String(state.ratings[a].rated_at || ''));
      }).map(function (id) { return state.known[id]; });
  }

  function renderSaved() {
    var loved = savedItems('love');
    var liked = savedItems('like');
    var total = loved.length + liked.length;
    $('saved-toggle').textContent = (state.savedOpen ? 'Hide saved' : 'Show saved') + ' (' + total + ')';
    $('saved-toggle').setAttribute('aria-expanded', state.savedOpen ? 'true' : 'false');
    $('saved').hidden = !state.savedOpen;
    $('tab-loved').textContent = 'Loved (' + loved.length + ')';
    $('tab-liked').textContent = 'Liked (' + liked.length + ')';
    $('tab-loved').setAttribute('aria-selected', state.savedTab === 'love' ? 'true' : 'false');
    $('tab-liked').setAttribute('aria-selected', state.savedTab === 'like' ? 'true' : 'false');
    var row = $('saved-row');
    row.textContent = '';
    if (!state.savedOpen) return;
    var items = state.savedTab === 'love' ? loved : liked;
    if (!items.length) {
      row.appendChild(h('p', { class: 'saved-empty' }, [
        state.savedTab === 'love'
          ? 'Nothing loved yet. Love a result to keep it here.'
          : 'Nothing liked yet. Like a result to keep it here.'
      ]));
      return;
    }
    items.forEach(function (r) { row.appendChild(renderCard(r)); });
  }

  /* ---- results ---- */

  function renderResults() {
    var grid = $('grid');
    grid.textContent = '';
    grid.style.minHeight = '';
    var s = state.search;
    $('empty').hidden = !!s;
    renderChips(s && s.query);
    renderPrefs();
    renderStats();
    renderSaved();
    updateButtons();
    if (!s) return;
    $('request').value = s.request;
    s.results.forEach(function (r) { grid.appendChild(renderCard(r)); });
    if (state.busy === 0 && !retryAction) renderStatus(defaultStatus(), false);
  }

  function showSkeleton() {
    var grid = $('grid');
    var height = grid.offsetHeight;
    grid.textContent = '';
    if (height) grid.style.minHeight = height + 'px'; // hold the space so nothing jumps
    for (var i = 0; i < 8; i++) grid.appendChild(h('div', { class: 'skeleton' }));
    $('empty').hidden = true;
  }

  function adopt(data) {
    state.sessionId = data.session_id || state.sessionId;
    state.search = data.search || null;
    state.ratings = data.ratings || {};
    state.resolved = data.resolved || {};
    state.profile = data.profile || null;
    state.summary = data.profile_summary || '';
    state.used = data.lookups_used || 0;
    state.cap = data.lookups_cap || 0;
    state.againsUsed = data.shop_agains_used || 0;
    state.againsCap = data.shop_agains_cap || 0;
    state.sessionUsd = data.session_usd || 0;
    state.prepUsd = data.prep_usd || 0;
    state.prep = data.prep || null;
    (data.saved || []).forEach(function (r) { state.known[r.id] = r; });
    if (state.search) state.search.results.forEach(function (r) { state.known[r.id] = r; });
    if (state.sessionId) storeSet(KEY_SESSION, state.sessionId);
    var badge = $('mode-badge');
    if (data.mode === 'mock') {
      badge.hidden = false;
      badge.textContent = 'mock data';
    }
  }

  /* A search or a Shop Again is a background job: POST answers with a job id, then the page polls
     for the phase ("Tagging results") and, at the end, the same view a direct call would give. */
  var DEFAULT_STEPS = {
    '/api/search': [['searching', 'Searching'], ['tagging', 'Tagging'], ['ranking', 'Ranking']],
    '/api/shop-again': [['ratings', 'Finishing your last ratings'], ['searching', 'Searching'],
      ['tagging', 'Tagging'], ['ranking', 'Ranking']]
  };
  var bar = { timer: null, started: 0, steps: [] };
  var SLOW_S = 30;

  function stepText(s) {
    return s.label + (s.total ? ' (' + (s.done_count || 0) + ' of ' + s.total + ')' : '');
  }

  function renderSteps(steps) {
    bar.steps = steps;
    var list = $('steps');
    list.textContent = '';
    var done = 0;
    var active = null;
    steps.forEach(function (s) {
      var finished = s.state === 'done' || s.state === 'skipped';
      if (finished) done += 1;
      if (s.state === 'active' && !active) active = s;
      list.appendChild(h('li', { class: 'step ' + s.state }, [
        h('span', { class: 'seg' }),
        h('span', { class: 'name' }, [stepText(s)])
      ]));
    });
    var box = $('stepbar');
    box.setAttribute('aria-valuenow', String(done));
    box.setAttribute('aria-valuemax', String(Math.max(1, steps.length)));
    if (active) renderStatus(stepText(active) + '\u2026', false);
  }

  function tickElapsed() {
    var seconds = Math.max(0, Math.floor((Date.now() - bar.started) / 1000));
    $('elapsed').textContent = seconds + ' s';
    $('slow-note').hidden = seconds < SLOW_S;
  }

  function startStepBar(message) {
    var kind = message === AGAIN_MSG ? '/api/shop-again' : '/api/search';
    bar.started = Date.now();
    var box = $('stepbar');
    box.hidden = false;
    renderSteps(DEFAULT_STEPS[kind].map(function (p) {
      return { id: p[0], label: p[1], state: 'pending', done_count: null, total: null };
    }));
    tickElapsed();
    if (bar.timer) window.clearInterval(bar.timer);
    bar.timer = window.setInterval(tickElapsed, 1000);
  }

  function stopStepBar() {
    if (bar.timer) window.clearInterval(bar.timer);
    bar.timer = null;
    $('stepbar').hidden = true;
    $('slow-note').hidden = true;
  }

  function runJob(path, body) {
    return api('POST', path, body).then(function (job) {
      return new Promise(function (resolve, reject) {
        function poll() {
          var owner = state.sessionId ? '?session=' + encodeURIComponent(state.sessionId) : '';
          api('GET', '/api/job/' + encodeURIComponent(job.job_id) + owner).then(function (st) {
            if (st.state === 'running') {
              if (st.steps) renderSteps(st.steps);
              if (typeof st.elapsed_s === 'number') bar.started = Date.now() - st.elapsed_s * 1000;
              if (!st.steps) renderStatus((st.phase || state.busyMessage) + '\u2026', false);
              window.setTimeout(poll, POLL_MS);
            } else if (st.state === 'done') {
              resolve(st.result);
            } else {
              var err = new Error(st.error || 'Request failed');
              err.status = st.status || 500;
              reject(err);
            }
          }).catch(reject);
        }
        poll();
      });
    });
  }

  function search(text) {
    var request = text.trim();
    if (!request || state.busy > 0) return;
    begin(SEARCH_MSG);
    showSkeleton();
    runJob('/api/search', { session_id: state.sessionId, request: request })
      .then(function (data) {
        adopt(data);
        renderResults();
        end();
      })
      .catch(function (err) {
        if (err.status === 404) { state.sessionId = null; storeSet(KEY_SESSION, null); }
        renderResults();
        fail(err.message, function () { search(request); });
        end();
      });
  }

  function shopAgain() {
    if (state.busy > 0 || $('shop-again').disabled) return;
    begin(AGAIN_MSG);
    showSkeleton();
    runJob('/api/shop-again', { session_id: state.sessionId })
      .then(function (data) {
        adopt(data);
        renderResults();
        end();
      })
      .catch(function (err) {
        renderResults();
        fail(err.message, function () { shopAgain(); });
        end();
      });
  }

  function startOver() {
    if (state.sessionId) {  // stop the background preparation for the old session
      api('POST', '/api/reset', { session_id: state.sessionId }).catch(function () { /* ignored */ });
    }
    state.sessionId = null; state.search = null; state.ratings = {}; state.resolved = {};
    state.known = {}; state.profile = null; state.summary = ''; state.used = 0;
    state.againsUsed = 0; state.sessionUsd = 0; state.prepUsd = 0; state.prep = null; state.confirming = false;
    storeSet(KEY_SESSION, null);
    retryAction = null;
    $('request').value = '';
    $('start-over-confirm').hidden = true;
    $('start-over').hidden = false;
    renderResults();
    renderStatus('', false);
  }

  function openDetails(ev) {
    ev.preventDefault();
    state.whyOpen = true;
    storeSet(KEY_WHY_OPEN, '1');
    renderWhy();
    var panel = $('why');
    if (panel.scrollIntoView) panel.scrollIntoView({ block: 'start' });
  }

  function init() {
    $('status-details').addEventListener('click', openDetails);
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
    $('shop-again').addEventListener('click', shopAgain);
    $('retry').addEventListener('click', function () {
      var action = retryAction;
      retryAction = null;
      renderStatus('', false);
      if (action) action();
    });
    $('saved-toggle').addEventListener('click', function () {
      state.savedOpen = !state.savedOpen;
      storeSet(KEY_SAVED_OPEN, state.savedOpen ? '1' : '0');
      renderSaved();
    });
    $('why-toggle').addEventListener('click', function () {
      state.whyOpen = !state.whyOpen;
      storeSet(KEY_WHY_OPEN, state.whyOpen ? '1' : '0');
      renderWhy();
    });
    $('tab-loved').addEventListener('click', function () { state.savedTab = 'love'; renderSaved(); });
    $('tab-liked').addEventListener('click', function () { state.savedTab = 'like'; renderSaved(); });
    $('start-over').addEventListener('click', function () {
      $('start-over').hidden = true;
      $('start-over-confirm').hidden = false;
    });
    $('start-over-cancel').addEventListener('click', function () {
      $('start-over-confirm').hidden = true;
      $('start-over').hidden = false;
    });
    $('start-over-yes').addEventListener('click', startOver);

    state.savedOpen = storeGet(KEY_SAVED_OPEN) === '1';
    state.whyOpen = storeGet(KEY_WHY_OPEN) === '1';
    var saved = storeGet(KEY_SESSION);
    var path = '/api/session' + (saved ? '?id=' + encodeURIComponent(saved) : '');
    api('GET', path).then(function (data) {
      adopt(data);
      renderResults();
    }).catch(function () {
      renderResults();
    });
  }

  init();
})();
