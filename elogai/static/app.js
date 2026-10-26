/* ELOG AI workspace: one page, hash routes, no build step. */
(function () {
  'use strict';

  var ELOG = location.pathname.replace(/ai\/[^/]*$/, '') || '/';
  var ME = null;
  var main = document.getElementById('main');

  // ---- helpers ---------------------------------------------------------------------------------

  function h(tag, attrs) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (var i = 2; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  }
  function add(e, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(e, x); });
    else e.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
  function api(path, body, method) {
    var o = { credentials: 'same-origin', headers: { 'X-Requested-With': 'elog-ai' } };
    if (body !== undefined) { o.method = method || 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    else if (method) o.method = method;
    return fetch('api/' + path, o).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var e = new Error(d.error || r.statusText); e.status = r.status; e.data = d; throw e; }
        return d;
      });
    });
  }
  function stream(path, body, onEvent) {
    return fetch('api/' + path, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'elog-ai' }, body: JSON.stringify(body) })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || r.statusText); });
        var rd = r.body.getReader(), dec = new TextDecoder(), buf = '';
        function pump() {
          return rd.read().then(function (x) {
            if (x.done) { if (buf.trim()) onEvent(JSON.parse(buf)); return; }
            buf += dec.decode(x.value, { stream: true });
            var lines = buf.split('\n'); buf = lines.pop();
            lines.forEach(function (l) { if (l.trim()) onEvent(JSON.parse(l)); });
            return pump();
          });
        }
        return pump();
      });
  }
  function qs(o) {
    return Object.keys(o).filter(function (k) { return o[k] !== '' && o[k] != null && !(Array.isArray(o[k]) && !o[k].length); })
      .map(function (k) { return k + '=' + encodeURIComponent(Array.isArray(o[k]) ? o[k].join(',') : o[k]); }).join('&');
  }
  function entryUrl(lb, mid) { return ELOG + encodeURIComponent(lb) + '/' + mid; }
  function fmtDate(t, withTime) {
    if (!t) return '';
    var d = new Date(t * 1000), p = function (n) { return (n < 10 ? '0' : '') + n; };
    var s = p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
    return withTime ? s + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) : s;
  }
  function ago(t) {
    var s = Date.now() / 1000 - t;
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    if (s < 86400 * 60) return Math.round(s / 86400) + ' days ago';
    return fmtDate(t);
  }
  function iso(d) { return d.toISOString().slice(0, 10); }
  function pref(k, d) { try { var v = localStorage.getItem('elog-ai:' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem('elog-ai:' + k, JSON.stringify(v)); } catch (e) {} }
  function loading() { return h('div', { class: 'empty' }, h('span', { class: 'spin' })); }
  function fail(e) { return h('div', { class: 'empty err' }, e.message || String(e)); }

  // Markdown subset for model answers; HTML is escaped first, citations become links.
  function md(src) {
    var out = [], list = null, para = [], table = null;
    function inline(s) {
      s = esc(s);
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
        .replace(/\[([^\]\[#]+?) #(\d+)\]/g, function (m, lb, id) {
          lb = lb.replace(/&#(\d+);/g, function (x, c) { return String.fromCharCode(c); });
          return '<a class="cite" target="_blank" href="' + esc(entryUrl(lb, id)) + '" title="' + esc(lb) + ' entry ' + id + '">' + esc(lb) + ' #' + id + '</a>';
        });
      return s;
    }
    function flush() {
      if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
      if (list) { out.push('<' + list.t + '>' + list.items.map(function (i) { return '<li>' + inline(i) + '</li>'; }).join('') + '</' + list.t + '>'); list = null; }
      if (table) { out.push('<table>' + table.map(function (r, i) { return '<tr>' + r.map(function (c) { return (i ? '<td>' : '<th>') + inline(c) + (i ? '</td>' : '</th>'); }).join('') + '</tr>'; }).join('') + '</table>'); table = null; }
    }
    var code = null;
    String(src || '').split('\n').forEach(function (line) {
      if (/^```/.test(line)) { if (code) { out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); code = null; } else { flush(); code = []; } return; }
      if (code) { code.push(line); return; }
      var m;
      if (/^\s*\|.*\|\s*$/.test(line)) {
        if (/^\s*\|[\s:|-]+\|\s*$/.test(line)) return;
        if (!table) { flush(); table = []; }
        table.push(line.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); })); return;
      }
      if ((m = /^\s*(#{1,4})\s+(.*)$/.exec(line))) { flush(); out.push('<h4>' + inline(m[2]) + '</h4>'); return; }
      if ((m = /^\s*[-*•]\s+(.*)$/.exec(line))) { if (para.length || (list && list.t !== 'ul') || table) flush(); list = list || { t: 'ul', items: [] }; list.items.push(m[1]); return; }
      if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(line))) { if (para.length || (list && list.t !== 'ol') || table) flush(); list = list || { t: 'ol', items: [] }; list.items.push(m[1]); return; }
      if (!line.trim()) { flush(); return; }
      if (list && /^\s{2,}/.test(line)) { list.items[list.items.length - 1] += ' ' + line.trim(); return; }
      if (list || table) flush();
      para.push(line);
    });
    if (code) out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
    flush();
    return out.join('');
  }
  function snippet(s) {
    // FTS marks matches with \x02 ... \x03
    return h('div', { class: 'snip', html: esc((s || '').replace(/\n\s*\n+/g, '\n')).replace(/\x02/g, '<mark>').replace(/\x03/g, '</mark>') });
  }

  function logbookNames() { return ME.logbooks.map(function (l) { return l.name; }); }

  // logbook multi-select grouped like ELOG's start page
  function multiSelect(selected, onChange, opts) {
    opts = opts || {};
    var sel = selected.slice();
    var btn = h('button', { type: 'button' });
    var pop = h('div', { class: 'pop hidden' });
    var wrap = h('div', { class: 'ms' + (opts.down ? ' down' : '') }, btn, pop);
    function label() { btn.textContent = (opts.prefix || '') + (sel.length ? (sel.length > 2 ? sel.length + ' logbooks' : sel.join(', ')) : 'All logbooks') + ' ▾'; }
    function build() {
      pop.innerHTML = '';
      pop.appendChild(h('label', null, h('input', { type: 'checkbox', checked: !sel.length, onchange: function () { sel = []; build(); label(); onChange(sel); } }), 'All logbooks'));
      var seen = {}, groups = ME.groups || {};
      Object.keys(groups).forEach(function (g) {
        pop.appendChild(h('div', { class: 'grp' }, g));
        groups[g].forEach(function (n) { seen[n] = 1; pop.appendChild(item(n)); });
      });
      var rest = logbookNames().filter(function (n) { return !seen[n]; });
      if (rest.length && Object.keys(groups).length) pop.appendChild(h('div', { class: 'grp' }, 'Other'));
      rest.forEach(function (n) { pop.appendChild(item(n)); });
    }
    function item(n) {
      return h('label', null, h('input', { type: 'checkbox', checked: sel.indexOf(n) >= 0, onchange: function (e) {
        if (e.target.checked) sel.push(n); else sel = sel.filter(function (x) { return x !== n; });
        build(); label(); onChange(sel);
      } }), n);
    }
    btn.addEventListener('click', function (e) { e.stopPropagation(); pop.classList.toggle('hidden'); });
    document.addEventListener('click', function (e) { if (!wrap.contains(e.target)) pop.classList.add('hidden'); });
    build(); label();
    return wrap;
  }

  function resultCard(r, extra) {
    return h('div', { class: 'res' },
      h('div', { class: 'head' },
        h('a', { class: 'ref', href: entryUrl(r.logbook, r.mid), target: '_blank' }, r.logbook + ' #' + r.mid),
        h('span', { class: 'subject' }, r.subject || ''),
        r.problem ? h('span', { class: 'chip ' + (r.problem.resolved ? 'fixed' : 'problem') }, r.problem.resolved ? 'problem · fixed' : 'problem') : null,
        h('span', { class: 'spacer' }),
        h('span', { class: 'meta' }, (r.author ? r.author + ' · ' : '') + fmtDate(r.time, true))),
      r.summary && r.summary !== r.subject ? h('div', { class: 'summary' }, r.summary) : null,
      r.snippet ? snippet(r.snippet) : null,
      h('div', { class: 'tags' },
        r.kind && r.kind !== 'routine' ? h('span', { class: 'chip kind' }, r.kind) : null,
        Object.keys(r.attrs || {}).filter(function (k) { return r.attrs[k] && k !== 'Author'; }).slice(0, 6).map(function (k) {
          return h('span', { class: 'chip', title: k }, k + ': ' + r.attrs[k]);
        }),
        (r.tags || []).slice(0, 5).map(function (t) { return h('a', { class: 'chip', href: '#search?q=' + encodeURIComponent(t) }, '#' + t); }),
        r.attachments ? h('span', { class: 'chip' }, '📎 ' + r.attachments) : null,
        r.similarity ? h('span', { class: 'chip', title: 'semantic similarity' }, '≈ ' + Math.round(r.similarity * 100) + '%') : null,
        extra || null));
  }

  // ---- routing ---------------------------------------------------------------------------------

  var routes = {};
  function parseHash() {
    var x = location.hash.replace(/^#/, ''), i = x.indexOf('?');
    var name = (i < 0 ? x : x.slice(0, i)) || 'home', params = {};
    if (i >= 0) x.slice(i + 1).split('&').forEach(function (p) { var kv = p.split('='); if (kv[0]) params[kv[0]] = decodeURIComponent(kv[1] || ''); });
    return { name: name, params: params };
  }
  function setHash(name, params, replace) {
    var q = qs(params || {});
    var hsh = '#' + name + (q ? '?' + q : '');
    if (replace) history.replaceState(null, '', hsh); else location.hash = hsh;
  }
  function route() {
    var r = parseHash();
    var fn = routes[r.name] || routes.home;
    document.querySelectorAll('#nav a').forEach(function (a) { a.classList.toggle('on', a.getAttribute('href') === '#' + r.name); });
    main.innerHTML = '';
    fn(r.params);
  }

  // ---- overview --------------------------------------------------------------------------------

  routes.home = function () {
    main.appendChild(h('h1', null, 'Overview'));
    main.appendChild(h('p', { class: 'sub' }, 'What happened in your logbooks, and what the AI has noticed.'));
    var top = h('div', { class: 'grid' });
    var act = h('div', { class: 'card' }, h('b', null, 'Activity, last 26 weeks'), loading());
    var probs = h('div', { class: 'card' }, h('b', null, 'Open problems'), loading());
    top.appendChild(act); top.appendChild(probs);
    main.appendChild(top);
    var al = h('div');
    main.appendChild(al);
    api('alerts').then(function (rows) {
      if (!rows.length) return;
      al.appendChild(h('h2', null, 'Alerts'));
      al.appendChild(h('div', { class: 'card' }, rows.slice(0, 12).map(function (a) {
        return h('div', { style: 'margin:3px 0' }, h('span', { class: 'chip ' + (a.kind === 'problem' ? 'problem' : 'warn') }, a.kind),
          ' ', h('span', { html: md(a.text).replace(/^<p>|<\/p>$/g, '') }), ' ', h('span', { class: 'muted small' }, ago(a.at)));
      })));
    });
    if ((ME.handovers || []).length) {
      var ho = h('div', { class: 'grid digest' });
      main.appendChild(h('h2', null, 'Handover'));
      main.appendChild(ho);
      api('handovers').then(function (rows) {
        rows.forEach(function (r) {
          var body = h('div', { class: 'md', html: r.text ? md(r.text) : '<span class="muted">Not written yet today.</span>' });
          var btn = h('button', { class: 'btn small' }, r.text ? 'Refresh' : 'Write now');
          btn.onclick = function () {
            btn.disabled = true; body.innerHTML = '<span class="spin"></span>';
            api('handover', { logbook: r.logbook }).then(function (d) { body.innerHTML = md(d.text || 'Nothing to report.'); })
              .catch(function (e) { body.innerHTML = ''; body.appendChild(fail(e)); }).then(function () { btn.disabled = false; });
          };
          ho.appendChild(h('div', { class: 'card' }, h('div', { class: 'row' }, h('b', null, r.logbook),
            h('span', { class: 'muted small' }, r.day || ''), h('span', { class: 'spacer' }), ME.features.chat ? btn : null), body));
        });
      });
    }
    var wk = h('div');
    main.appendChild(h('h2', null, 'This week'));
    main.appendChild(wk);

    api('insights/activity').then(function (rows) {
      act.lastChild.remove();
      var max = 1; rows.forEach(function (r) { r.weeks.forEach(function (n) { max = Math.max(max, n); }); });
      var heat = h('div', { class: 'heat' });
      rows.slice(0, 14).forEach(function (r) {
        heat.appendChild(h('a', { class: 'name', href: '#digest?logbooks=' + encodeURIComponent(r.logbook), title: r.logbook }, r.logbook));
        heat.appendChild(h('div', { class: 'cells' }, r.weeks.map(function (n, i) {
          var a = n ? 0.15 + 0.85 * Math.sqrt(n / max) : 0;
          return h('span', { title: n + ' entries, week ' + (i - 25), style: n ? 'background: color-mix(in srgb, var(--accent) ' + Math.round(a * 100) + '%, transparent)' : null });
        })));
        heat.appendChild(h('span', { class: 'muted' }, r.total));
      });
      act.appendChild(rows.length ? heat : h('div', { class: 'muted' }, 'No entries in this period.'));
    }).catch(function (e) { act.lastChild.replaceWith(fail(e)); });

    api('insights/problems?' + qs({ since: Math.floor(Date.now() / 1000) - 120 * 86400 })).then(function (d) {
      probs.lastChild.remove();
      var open = d.items.filter(function (i) { return !i.resolved; }).slice(0, 8);
      if (!open.length) { probs.appendChild(h('div', { class: 'muted' }, 'No open problems reported in the last four months.')); return; }
      probs.appendChild(h('div', { class: 'results' }, open.map(function (i) {
        return h('div', null, h('a', { href: entryUrl(i.logbook, i.mid), target: '_blank' }, i.logbook + ' #' + i.mid), ' ',
          h('span', null, i.component + (i.symptom ? ': ' + i.symptom : '')), ' ', h('span', { class: 'muted small' }, ago(i.time)));
      })));
      probs.appendChild(h('div', { style: 'margin-top:8px' }, h('a', { href: '#problems' }, 'All problems →')));
    }).catch(function (e) { probs.lastChild.replaceWith(fail(e)); });

    digestList(wk, 'week', new Date(), true);
  };

  // ---- ask -------------------------------------------------------------------------------------

  routes.ask = function (p) {
    var state = { conv: p.c ? +p.c : null, scope: pref('ask-scope', { logbooks: [], since: '' }), busy: false };
    if (p.logbooks) state.scope.logbooks = p.logbooks.split(',');
    var layout = h('div', { class: 'ask' });
    var side = h('div', { class: 'side' });
    var convList = h('div', { class: 'convs' });
    side.appendChild(h('button', { class: 'btn primary', style: 'width:100%;margin-bottom:10px', onclick: function () { setHash('ask', {}); } }, '+ New question'));
    side.appendChild(convList);
    var chat = h('div', { class: 'chat' });
    var msgs = h('div', { class: 'msgs' });
    var ta = h('textarea', { rows: 1, placeholder: ME.features.chat ? 'Ask about your logbooks… (Enter to send, Shift+Enter for a new line)' : 'No language model configured: questions return the most relevant entries.' });
    var send = h('button', { class: 'btn primary' }, 'Ask');
    var since = h('select', null, [['', 'any time'], ['7', 'last week'], ['31', 'last month'], ['92', 'last 3 months'], ['365', 'last year']].map(function (o) {
      return h('option', { value: o[0], selected: String(state.scope.sinceDays || '') === o[0] }, o[1]);
    }));
    since.addEventListener('change', function () { state.scope.sinceDays = since.value; setPref('ask-scope', state.scope); });
    var scope = h('div', { class: 'scope' }, 'Search in', multiSelect(state.scope.logbooks, function (s) { state.scope.logbooks = s; setPref('ask-scope', state.scope); }), since,
      h('span', { class: 'spacer' }), h('span', null, ME.models.chat ? 'model: ' + ME.models.chat : ''));
    chat.appendChild(msgs);
    chat.appendChild(h('div', { class: 'composer' }, h('div', { class: 'box' }, ta, send), scope));
    layout.appendChild(side); layout.appendChild(chat);
    main.appendChild(layout);

    ta.addEventListener('input', function () { ta.style.height = 'auto'; ta.style.height = Math.min(200, ta.scrollHeight) + 'px'; });
    ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } });
    send.addEventListener('click', go);

    function loadConvs() {
      api('conversations').then(function (cs) {
        convList.innerHTML = '';
        if (!cs.length) convList.appendChild(h('div', { class: 'muted small' }, 'Your questions will be listed here.'));
        cs.forEach(function (c) {
          convList.appendChild(h('a', { href: '#ask?c=' + c.id, class: c.id === state.conv ? 'on' : '', title: c.title }, c.title || '(untitled)'));
        });
      });
    }
    loadConvs();

    function starters() {
      var lbs = ME.logbooks.slice().sort(function (a, b) { return (b.last || 0) - (a.last || 0); });
      var a = lbs[0] ? lbs[0].name : '', b = lbs[1] ? lbs[1].name : a;
      var qs_ = ['What happened in ' + a + ' this week?', 'Which problems are still open, and in which logbooks?',
        'Summarise the last month in ' + b + '.', 'When was the last alignment or maintenance, and what was done?'];
      msgs.appendChild(h('div', null,
        h('h1', null, 'Ask your logbooks'),
        h('p', { class: 'sub' }, 'Answers are written from the entries you can read, with links to them. Dates and logbook names in the question are used as filters.'),
        h('div', { class: 'starters' }, qs_.map(function (q) { return h('button', { onclick: function () { ta.value = q; go(); } }, q); }))));
    }

    function render(m) {
      if (m.role === 'user') return h('div', { class: 'msg user' }, m.content);
      var body = h('div', { class: 'md', html: md(m.content) });
      var box = h('div', { class: 'msg assistant' }, body);
      box.appendChild(sourcesEl(m.sources || []));
      if (m.id) box.appendChild(tools(m));
      return box;
    }
    function sourcesEl(src) {
      if (!src.length) return h('div');
      return h('div', { class: 'sources' }, h('span', { class: 'muted small' }, 'Sources:'), src.map(function (s) {
        return h('a', { href: entryUrl(s.logbook, s.mid), target: '_blank', title: (s.summary || s.subject || '') + ' · ' + (s.author || '') + ' ' + fmtDate(s.time) },
          s.logbook + ' #' + s.mid + (s.subject ? ' · ' + s.subject : ''));
      }));
    }
    function tools(m) {
      var up = h('button', { title: 'Good answer', class: m.feedback === 1 ? 'on' : '' }, '👍');
      var down = h('button', { title: 'Bad answer', class: m.feedback === -1 ? 'on' : '' }, '👎');
      var copy = h('button', { title: 'Copy' }, 'Copy');
      function fb(v) {
        var comment = v === -1 ? (prompt('What was wrong or missing? (optional; logbook admins see it and can turn it into a note for the AI)') || '') : '';
        api('messages/' + m.id + '/feedback', { value: v, comment: comment });
        up.classList.toggle('on', v === 1); down.classList.toggle('on', v === -1);
      }
      up.onclick = function () { fb(1); }; down.onclick = function () { fb(-1); };
      copy.onclick = function () { navigator.clipboard && navigator.clipboard.writeText(m.content); copy.textContent = 'Copied'; };
      return h('div', { class: 'tools' }, up, down, copy);
    }

    if (state.conv) {
      msgs.appendChild(loading());
      api('conversations/' + state.conv).then(function (c) {
        msgs.innerHTML = '';
        c.messages.forEach(function (m) { msgs.appendChild(render(m)); });
        window.scrollTo(0, document.body.scrollHeight);
      }).catch(function () { setHash('ask', {}, true); route(); });
    } else starters();
    ta.focus();

    function go() {
      var q = ta.value.trim();
      if (!q || state.busy) return;
      if (!state.conv) msgs.innerHTML = '';
      state.busy = true; send.disabled = true;
      ta.value = ''; ta.style.height = 'auto';
      msgs.appendChild(render({ role: 'user', content: q }));
      var body = h('div', { class: 'md typing' }, h('span', { class: 'muted' }, 'Searching the logbooks…'));
      var box = h('div', { class: 'msg assistant' }, body);
      msgs.appendChild(box);
      window.scrollTo(0, document.body.scrollHeight);
      var text = '', sources = [], first = true;
      var sinceDate = state.scope.sinceDays ? iso(new Date(Date.now() - state.scope.sinceDays * 86400000)) : '';
      stream('ask', { question: q, conversation_id: state.conv, logbooks: state.scope.logbooks, since: sinceDate }, function (ev) {
        if (ev.type === 'conversation') { if (state.conv !== ev.id) { state.conv = ev.id; setHash('ask', { c: ev.id }, true); loadConvs(); } }
        else if (ev.type === 'sources') {
          sources = ev.sources;
          body.innerHTML = '<span class="muted">' + (sources.length ? 'Reading ' + sources.length + ' entries…' : 'No matching entries found.') + '</span>';
          if (ev.withheld) box.appendChild(h('div', { class: 'muted small' }, ev.withheld + ' matching entries are in private logbooks and were not sent to the external model.'));
        } else if (ev.type === 'delta') {
          if (first) { body.innerHTML = ''; first = false; }
          text += ev.text; body.innerHTML = md(text);
          if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 120) window.scrollTo(0, document.body.scrollHeight);
        } else if (ev.type === 'error') {
          box.appendChild(h('div', { class: 'err' }, ev.error));
        } else if (ev.type === 'done') {
          body.classList.remove('typing');
          box.appendChild(sourcesEl(sources));
          if (ev.message_id) box.appendChild(tools({ id: ev.message_id, content: text }));
        }
      }).catch(function (e) { body.classList.remove('typing'); box.appendChild(h('div', { class: 'err' }, e.message)); })
        .then(function () { state.busy = false; send.disabled = false; body.classList.remove('typing'); loadConvs(); ta.focus(); });
    }
  };

  // ---- search ----------------------------------------------------------------------------------

  routes.search = function (p) {
    var f = { q: p.q || '', logbooks: p.logbooks ? p.logbooks.split(',') : [], since: p.since || '', until: p.until || '',
      author: p.author || '', kind: p.kind || '', entity: p.entity || '', mode: p.mode || 'hybrid' };
    var q = h('input', { type: 'search', value: f.q, placeholder: 'Search by meaning or exact words: "compressor misaligned", S-179, run 905 …', style: 'flex:1;min-width:240px;padding:8px 10px' });
    var mode = h('select', null, [['hybrid', 'Words + meaning'], ['keyword', 'Exact words'], ['semantic', 'Meaning only']].map(function (o) { return h('option', { value: o[0], selected: f.mode === o[0] }, o[1]); }));
    var since = h('input', { type: 'date', value: f.since, title: 'from' });
    var until = h('input', { type: 'date', value: f.until, title: 'until' });
    var kind = h('select', null, [['', 'any kind']].concat(['problem', 'fix', 'measurement', 'analysis', 'alignment', 'maintenance', 'sample', 'configuration', 'observation', 'plan', 'info'].map(function (k) { return [k, k]; }))
      .map(function (o) { return h('option', { value: o[0], selected: f.kind === o[0] }, o[1]); }));
    var res = h('div', { class: 'results' });
    var info = h('div', { class: 'muted small', style: 'margin:8px 0' });
    main.appendChild(h('h1', null, 'Search'));
    main.appendChild(h('div', { class: 'row', style: 'margin:12px 0' }, q, h('button', { class: 'btn primary', onclick: go }, 'Search')));
    main.appendChild(h('div', { class: 'row small' }, multiSelect(f.logbooks, function (s) { f.logbooks = s; go(); }, { down: true }), mode, kind, 'from', since, 'until', until,
      f.entity ? h('span', { class: 'chip on' }, f.entity, h('a', { href: '#', style: 'color:inherit', onclick: function (e) { e.preventDefault(); f.entity = ''; go(); } }, ' ×')) : null));
    main.appendChild(info);
    main.appendChild(res);
    q.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
    [mode, kind, since, until].forEach(function (x) { x.addEventListener('change', go); });
    var offset = 0;
    function go(more) {
      f.q = q.value.trim(); f.mode = mode.value; f.kind = kind.value; f.since = since.value; f.until = until.value;
      if (more !== true) { offset = 0; res.innerHTML = ''; setHash('search', f, true); }
      res.appendChild(loading());
      api('search?' + qs(Object.assign({ k: 25, offset: offset }, f))).then(function (d) {
        res.lastChild.remove();
        info.textContent = f.q ? (d.total || 0) + ' results' + (d.semantic === false && f.mode !== 'keyword' && ME.features.embed ? ' (semantic index not ready yet: words only)' : '') : 'Latest entries';
        if (!d.results.length && !offset) res.appendChild(h('div', { class: 'empty' }, 'Nothing found. Try other words, or "Meaning only".'));
        d.results.forEach(function (r) {
          res.appendChild(resultCard(r, h('a', { class: 'chip', href: '#', onclick: function (e) { e.preventDefault(); similarTo(r); } }, 'similar…')));
        });
        offset += d.results.length;
        if (d.total > offset) res.appendChild(h('button', { class: 'btn', onclick: function (e) { e.target.remove(); go(true); } }, 'More'));
      }).catch(function (e) { res.lastChild.replaceWith(fail(e)); });
    }
    function similarTo(r) {
      res.innerHTML = ''; info.textContent = 'Entries similar to ' + r.logbook + ' #' + r.mid + ' · ' + (r.subject || '');
      res.appendChild(loading());
      api('entry?' + qs({ logbook: r.logbook, mid: r.mid })).then(function (d) {
        res.innerHTML = '';
        res.appendChild(resultCard(r));
        res.appendChild(h('h2', null, 'Similar'));
        if (!d.similar.length) res.appendChild(h('div', { class: 'empty' }, 'No similar entries.'));
        d.similar.forEach(function (s) { res.appendChild(resultCard(s)); });
      });
    }
    go();
  };

  // ---- digests ---------------------------------------------------------------------------------

  function digestList(container, kind, day, compact, only) {
    container.innerHTML = '';
    container.appendChild(loading());
    api('digests?' + qs({ kind: kind, date: iso(day) })).then(function (rows) {
      container.innerHTML = '';
      if (only && only.length) rows = rows.filter(function (r) { return only.indexOf(r.logbook) >= 0; });
      if (!rows.length) { container.appendChild(h('div', { class: 'empty' }, 'No entries in this period.')); return; }
      var grid = h('div', { class: 'grid digest' });
      container.appendChild(grid);
      var queue = [];
      rows.forEach(function (r) {
        var body = h('div', { class: 'md' });
        var btn = h('button', { class: 'btn small' }, r.text ? 'Refresh' : 'Summarise');
        var card = h('div', { class: 'card' },
          h('div', { class: 'row' }, h('a', { href: ELOG + encodeURIComponent(r.logbook) + '/', target: '_blank' }, h('b', null, r.logbook)),
            h('span', { class: 'muted small' }, r.entries + ' entries'), h('span', { class: 'spacer' }), ME.features.chat ? btn : null),
          body);
        grid.appendChild(card);
        if (r.text) body.innerHTML = md(r.text);
        else body.appendChild(h('div', { class: 'muted small' }, ME.features.chat ? 'Not summarised yet.' : 'No language model configured.'));
        function run() {
          btn.disabled = true; body.innerHTML = ''; body.appendChild(h('span', { class: 'spin' }));
          return api('digest', { logbook: r.logbook, kind: kind, date: iso(day) }).then(function (d) {
            body.innerHTML = d.text ? md(d.text) : '<span class="muted">Nothing to summarise.</span>'; btn.textContent = 'Refresh';
          }).catch(function (e) { body.innerHTML = ''; body.appendChild(fail(e)); }).then(function () { btn.disabled = false; });
        }
        btn.addEventListener('click', run);
        if (!r.text && ME.features.chat && !compact) queue.push(run);
      });
      if (queue.length) {
        var all = h('button', { class: 'btn' }, 'Summarise all ' + queue.length);
        all.onclick = function () { all.remove(); queue.reduce(function (p, f) { return p.then(f); }, Promise.resolve()); };
        container.insertBefore(h('div', { style: 'margin-bottom:10px' }, all), grid);
      }
    }).catch(function (e) { container.innerHTML = ''; container.appendChild(fail(e)); });
  }

  routes.digest = function (p) {
    var kind = p.kind || pref('digest-kind', 'week');
    var day = p.date ? new Date(p.date) : new Date();
    var only = p.logbooks ? p.logbooks.split(',') : [];
    var label = h('b', { style: 'min-width:220px;text-align:center' });
    var list = h('div');
    function shift(n) {
      if (kind === 'day') day.setDate(day.getDate() + n);
      else if (kind === 'week') day.setDate(day.getDate() + 7 * n);
      else day.setMonth(day.getMonth() + n);
      show();
    }
    function show() {
      var d = new Date(day);
      if (kind === 'week') { var wd = (d.getDay() + 6) % 7; d.setDate(d.getDate() - wd); var e = new Date(d); e.setDate(e.getDate() + 6); label.textContent = fmtDate(d / 1000) + ' – ' + fmtDate(e / 1000); }
      else if (kind === 'month') label.textContent = d.toLocaleString(undefined, { month: 'long', year: 'numeric' });
      else label.textContent = d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
      setHash('digest', { kind: kind, date: iso(day), logbooks: only }, true);
      digestList(list, kind, day, false, only);
    }
    main.appendChild(h('h1', null, 'Digests'));
    main.appendChild(h('p', { class: 'sub' }, 'Summaries of each logbook per day, week or month. Yesterday\'s and last week\'s are prepared every morning; others on request, then kept until an entry changes.'));
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:14px' },
      ['day', 'week', 'month'].map(function (k) {
        return h('button', { class: 'btn' + (k === kind ? ' primary' : ''), onclick: function () { setPref('digest-kind', k); setHash('digest', { kind: k, date: iso(day), logbooks: only }); } }, k[0].toUpperCase() + k.slice(1));
      }),
      h('span', { style: 'width:16px' }),
      h('button', { class: 'btn', onclick: function () { shift(-1); } }, '‹'), label, h('button', { class: 'btn', onclick: function () { shift(1); } }, '›'),
      h('span', { style: 'width:8px' }),
      multiSelect(only, function (s) { only = s; show(); }, { down: true })));
    main.appendChild(list);
    show();
  };

  // ---- problems --------------------------------------------------------------------------------

  routes.problems = function (p) {
    var lbs = p.logbooks ? p.logbooks.split(',') : [];
    var months = +(p.months || 12);
    var out = h('div');
    main.appendChild(h('h1', null, 'Problems'));
    main.appendChild(h('p', { class: 'sub' }, 'Malfunctions and degradations reported in the entries, grouped by what they affect. A problem counts as fixed when a later entry in its thread reports a fix.'));
    var per = h('select', null, [[3, '3 months'], [12, '12 months'], [36, '3 years'], [0, 'all time']].map(function (o) { return h('option', { value: o[0], selected: o[0] === months }, o[1]); }));
    per.onchange = function () { months = +per.value; load(); };
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:12px' }, multiSelect(lbs, function (s) { lbs = s; load(); }, { down: true }), per));
    main.appendChild(out);
    function load() {
      setHash('problems', { logbooks: lbs, months: months }, true);
      out.innerHTML = ''; out.appendChild(loading());
      api('insights/problems?' + qs({ logbooks: lbs, since: months ? Math.floor(Date.now() / 1000) - months * 31 * 86400 : 0 })).then(function (d) {
        out.innerHTML = '';
        if (!d.items.length) { out.appendChild(h('div', { class: 'empty' }, 'No problems found' + (ME.features.chat ? '' : ' (problems are recognised by the AI analysis, which needs a language model)') + '.')); return; }
        out.appendChild(h('h2', null, 'Recurring'));
        var rec = d.groups.filter(function (g) { return g.count > 1; }).slice(0, 30);
        out.appendChild(rec.length ? h('table', { class: 't' },
          h('tr', null, h('th', null, 'Logbook'), h('th', null, 'Affects'), h('th', null, 'Times'), h('th', null, 'Open'), h('th', null, 'First'), h('th', null, 'Last'), h('th', null, 'Entries')),
          rec.map(function (g) {
            return h('tr', null, h('td', null, g.logbook), h('td', null, g.component), h('td', null, g.count),
              h('td', null, g.open ? h('span', { class: 'chip problem' }, g.open) : h('span', { class: 'chip fixed' }, '0')),
              h('td', { class: 'muted' }, fmtDate(g.first)), h('td', { class: 'muted' }, fmtDate(g.last)),
              h('td', null, g.entries.map(function (e) { return [h('a', { href: entryUrl(g.logbook, e.mid), target: '_blank', title: e.subject }, '#' + e.mid), ' ']; })));
          })) : h('div', { class: 'muted' }, 'Nothing recurring.'));
        out.appendChild(h('h2', null, 'All reports'));
        out.appendChild(h('div', { class: 'results' }, d.items.slice(0, 150).map(function (i) {
          return h('div', { class: 'res' }, h('div', { class: 'head' },
            h('a', { class: 'ref', href: entryUrl(i.logbook, i.mid), target: '_blank' }, i.logbook + ' #' + i.mid),
            h('span', { class: 'subject' }, i.subject || ''),
            h('span', { class: 'chip ' + (i.resolved ? 'fixed' : 'problem') }, i.resolved ? 'fixed' : 'open'),
            i.severity ? h('span', { class: 'chip' + (i.severity === 'high' ? ' problem' : '') }, i.severity) : null,
            h('span', { class: 'spacer' }), h('span', { class: 'meta' }, (i.author || '') + ' · ' + fmtDate(i.time, true))),
            h('div', { class: 'summary' }, [i.component, i.symptom].filter(Boolean).join(': ') + (i.summary ? ' — ' + i.summary : '')),
            i.fixes.length ? h('div', { class: 'small' }, 'Fixed in ', i.fixes.map(function (f) { return [h('a', { href: entryUrl(f.logbook, f.mid), target: '_blank' }, '#' + f.mid), ' ']; })) : null);
        })));
      }).catch(function (e) { out.innerHTML = ''; out.appendChild(fail(e)); });
    }
    load();
  };

  // ---- trends ----------------------------------------------------------------------------------

  function chart(s, lb) {
    var W = 600, H = 150, pl = 40, pr = 8, pt = 8, pb = 18;
    var pts = s.points, t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
    var vals = pts.map(function (p) { return p[1]; }).slice().sort(function (a, b) { return a - b; });
    // scale to the 2..98 percentile so one wild value does not flatten the rest
    var lo = vals[Math.floor(vals.length * 0.02)], hi = vals[Math.ceil(vals.length * 0.98) - 1];
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
    var X = function (t) { return pl + (W - pl - pr) * (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)); };
    var Y = function (v) { return pt + (H - pt - pb) * (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)); };
    var out = {}; s.outliers.forEach(function (m) { out[m] = 1; });
    var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1); }).join(' ');
    var ns = 'http://www.w3.org/2000/svg';
    function el(tag, a) { var e = document.createElementNS(ns, tag); for (var k in a) e.setAttribute(k, a[k]); return e; }
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', role: 'img', 'aria-label': s.name });
    [lo + pad, hi - pad].forEach(function (v) {
      svg.appendChild(el('line', { x1: pl, x2: W - pr, y1: Y(v), y2: Y(v), class: 'axis' }));
      var t = el('text', { x: pl - 4, y: Y(v) + 3, 'text-anchor': 'end' }); t.textContent = +v.toPrecision(3); svg.appendChild(t);
    });
    [t0, t1].forEach(function (t, i) { var x = el('text', { x: i ? W - pr : pl, y: H - 4, 'text-anchor': i ? 'end' : 'start' }); x.textContent = fmtDate(t); svg.appendChild(x); });
    svg.appendChild(el('path', { d: d, class: 'line', 'vector-effect': 'non-scaling-stroke' }));
    pts.forEach(function (p) {
      if (out[p[2]] || pts.length < 40) {
        var c = el('circle', { cx: X(p[0]), cy: Y(p[1]), r: out[p[2]] ? 4 : 2, class: out[p[2]] ? 'out' : 'dot' });
        var tt = el('title', {}); tt.textContent = '#' + p[2] + ' · ' + fmtDate(p[0], true) + ' · ' + p[1] + ' ' + (s.unit || '') + (out[p[2]] ? ' (unusual)' : '');
        c.appendChild(tt);
        c.addEventListener('click', function () { window.open(entryUrl(lb, p[2]), '_blank'); });
        svg.appendChild(c);
      }
    });
    return h('div', { class: 'chart' },
      h('div', { class: 'ttl' }, h('b', null, s.name + (s.unit ? ' [' + s.unit + ']' : '')),
        h('span', { class: 'muted small' }, (s.source === 'text' ? 'from text · ' : '') + pts.length + ' values · last ' + (+s.last.toPrecision(4)) + (s.outliers.length ? ' · ' : '')),
        s.outliers.length ? h('span', { class: 'chip problem', title: 'values far from the recent median' }, s.outliers.length + ' unusual') : null),
      svg);
  }

  routes.trends = function (p) {
    var withSeries = ME.logbooks.filter(function (l) { return l.series && l.series.length; }).map(function (l) { return l.name; });
    var lb = p.logbook || withSeries[0] || (ME.logbooks[0] || {}).name;
    var years = +(p.years || 0);
    var out = h('div', { class: 'grid' });
    var sel = h('select', null, logbookNames().map(function (n) { return h('option', { value: n, selected: n === lb }, n + (withSeries.indexOf(n) >= 0 ? ' ●' : '')); }));
    var per = h('select', null, [[1, 'last year'], [3, 'last 3 years'], [0, 'all']].map(function (o) { return h('option', { value: o[0], selected: o[0] === years }, o[1]); }));
    sel.onchange = function () { lb = sel.value; load(); };
    per.onchange = function () { years = +per.value; load(); };
    main.appendChild(h('h1', null, 'Trends'));
    main.appendChild(h('p', { class: 'sub' }, 'Numeric attributes (●) and readings the AI found in the text, over time. Red points differ strongly from the preceding values; click a point to open its entry.'));
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:12px' }, sel, per));
    main.appendChild(out);
    function load() {
      setHash('trends', { logbook: lb, years: years }, true);
      out.innerHTML = ''; out.appendChild(loading());
      api('insights/series?' + qs({ logbook: lb, since: years ? Math.floor(Date.now() / 1000) - years * 366 * 86400 : 0 })).then(function (ss) {
        out.innerHTML = '';
        if (!ss.length) out.appendChild(h('div', { class: 'empty' }, 'No numeric values in this logbook.'));
        ss.forEach(function (s) { out.appendChild(chart(s, lb)); });
      }).catch(function (e) { out.innerHTML = ''; out.appendChild(fail(e)); });
    }
    load();
  };

  // ---- entities --------------------------------------------------------------------------------

  routes.entities = function (p) {
    var type = p.type || '', q = p.q || '';
    var types = h('div', { class: 'row', style: 'margin-bottom:10px' });
    var input = h('input', { type: 'search', value: q, placeholder: 'Filter: S-179, TaS2, Legend …', style: 'min-width:260px' });
    var list = h('div'), detail = h('div');
    main.appendChild(h('h1', null, 'Samples & things'));
    main.appendChild(h('p', { class: 'sub' }, 'Samples, tips, serial numbers, instruments and materials named in attributes or recognised in the text, across all your logbooks.'));
    main.appendChild(types);
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:10px' }, input));
    main.appendChild(h('div', { class: 'grid', style: 'grid-template-columns: minmax(300px, 1fr) minmax(300px, 1.4fr); align-items:start' }, list, detail));
    var timer;
    input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () { q = input.value.trim(); load(); }, 250); });
    function load() {
      setHash('entities', { type: type, q: q, value: p.value }, true);
      list.innerHTML = ''; list.appendChild(loading());
      api('insights/entities?' + qs({ type: type, q: q })).then(function (d) {
        types.innerHTML = '';
        types.appendChild(h('a', { href: '#', class: 'chip' + (!type ? ' on' : ''), onclick: function (e) { e.preventDefault(); type = ''; load(); } }, 'all'));
        d.types.slice(0, 16).forEach(function (t) {
          types.appendChild(h('a', { href: '#', class: 'chip' + (t.type === type ? ' on' : ''), onclick: function (e) { e.preventDefault(); type = t.type; load(); } }, t.type + ' (' + t.values_ + ')'));
        });
        list.innerHTML = '';
        if (!d.items.length) { list.appendChild(h('div', { class: 'empty' }, 'Nothing found.')); return; }
        list.appendChild(h('table', { class: 't' }, h('tr', null, h('th', null, 'Name'), h('th', null, 'Type'), h('th', null, 'Entries'), h('th', null, 'Last seen')),
          d.items.map(function (it) {
            return h('tr', { class: 'click', onclick: function () { p.value = it.value; show(it); setHash('entities', { type: type, q: q, value: it.value }, true); } },
              h('td', null, h('b', null, it.value), it.logbooks.length > 1 ? h('div', { class: 'muted small' }, it.logbooks.join(', ')) : h('div', { class: 'muted small' }, it.logbooks[0])),
              h('td', { class: 'muted' }, it.type), h('td', null, it.entries), h('td', { class: 'muted' }, ago(it.last)));
          })));
      }).catch(function (e) { list.innerHTML = ''; list.appendChild(fail(e)); });
    }
    function show(it) {
      detail.innerHTML = ''; detail.appendChild(loading());
      Promise.all([api('insights/entity?' + qs({ value: it.value })), api('insights/lifecycle?' + qs({ value: it.value }))]).then(function (x) {
        var d = x[0], lc = x[1];
        detail.innerHTML = '';
        detail.appendChild(h('div', { class: 'row', style: 'margin-bottom:8px' }, h('h2', { style: 'margin:0' }, it.value), h('span', { class: 'muted' }, d.entries.length + ' entries'),
          h('span', { class: 'spacer' }),
          h('a', { class: 'btn small', href: '#ask?' + qs({}) , onclick: function (e) { e.preventDefault(); location.hash = '#ask'; setTimeout(function () { var t = document.querySelector('.composer textarea'); if (t) { t.value = 'Give me the history of ' + it.value + ': where it was used, measured, moved, and any problems.'; t.focus(); } }, 50); } }, 'Ask about it'),
          h('a', { class: 'btn small', href: '#search?' + qs({ entity: it.value }) }, 'Search')));
        detail.appendChild(lifecycleView(lc));
        detail.appendChild(h('div', { class: 'results' }, d.entries.map(function (r) { return resultCard(r); })));
      });
    }
    load();
    if (p.value) show({ value: p.value });
  };

  // history of an item from change notes and attributes: current state, time per value, periods
  function lifecycleView(lc) {
    var keys = Object.keys(lc.periods || {});
    if (!keys.length) return h('div');
    var box = h('div', { class: 'card', style: 'margin-bottom:12px' });
    box.appendChild(h('div', { class: 'row' }, h('b', null, 'History'), h('span', { class: 'muted small' }, 'from change notes and attributes of ' + lc.entries + ' entries')));
    box.appendChild(h('div', { class: 'tags', style: 'margin:6px 0' }, keys.map(function (k) {
      var c = lc.current[k];
      return h('a', { class: 'chip kind', href: entryUrl(c.logbook, c.mid), target: '_blank' }, k + ': ' + c.value + ' since ' + fmtDate(c.since));
    })));
    keys.forEach(function (k) {
      var tot = lc.totals[k] || {};
      box.appendChild(h('div', { class: 'small', style: 'margin-top:8px' }, h('b', null, k), ' · total: ',
        Object.keys(tot).map(function (v) { return v + ' ' + tot[v] + ' d'; }).join(', ')));
      box.appendChild(h('table', { class: 't small', style: 'margin-top:4px' },
        h('tr', null, h('th', null, k), h('th', null, 'From'), h('th', null, 'To'), h('th', null, 'Days'), h('th', null, 'Entry')),
        lc.periods[k].slice().reverse().slice(0, 40).map(function (p) {
          return h('tr', null, h('td', null, p.value), h('td', { class: 'muted' }, fmtDate(p.from, true)),
            h('td', { class: 'muted' }, p.to ? fmtDate(p.to, true) : 'now'), h('td', null, p.days),
            h('td', null, h('a', { href: entryUrl(p.logbook, p.mid), target: '_blank' }, '#' + p.mid)));
        })));
    });
    return box;
  }

  // ---- runs ------------------------------------------------------------------------------------

  routes.runs = function (p) {
    var q = p.q || '';
    var input = h('input', { type: 'search', value: q, placeholder: 'TaS2, Legend OUT > 2.5, pump_ND.currentposition >= 10 …', style: 'min-width:420px' });
    var out = h('div');
    main.appendChild(h('h1', null, 'Runs'));
    main.appendChild(h('p', { class: 'sub' }, 'Data acquisition runs, read from the run description (.ini) attached to DAQ entries, with the conditions at run start (last readings of the logbook and its related logbooks). Search: words, and conditions on parameters or readings separated by commas, e.g. "TaS2, Legend OUT > 2.5".'));
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:12px' }, input));
    main.appendChild(out);
    var timer;
    input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () { q = input.value.trim(); load(); }, 300); });
    function load() {
      setHash('runs', { q: q }, true);
      out.innerHTML = ''; out.appendChild(loading());
      api('insights/runs?' + qs({ q: q })).then(function (rows) {
        out.innerHTML = '';
        if (!rows.length) { out.appendChild(h('div', { class: 'empty' }, 'No runs found.')); return; }
        var tb = h('table', { class: 't' }, h('tr', null, h('th', null, 'Run'), h('th', null, 'Logbook'), h('th', null, 'Sample'), h('th', null, 'Info'), h('th', null, 'Start'), h('th', null, 'End'), h('th', null, '')));
        rows.forEach(function (r) {
          var det = h('tr', { class: 'hidden' }, h('td', { colspan: 7 }, h('div', { class: 'small', style: 'columns: 3 260px; font-family: var(--mono)' },
            Object.keys(r.params).map(function (k) { return h('div', null, k + ' = ' + r.params[k]); }))));
          tb.appendChild(h('tr', { class: 'click', onclick: function () { det.classList.toggle('hidden'); } },
            h('td', null, h('a', { href: entryUrl(r.logbook, r.mid), target: '_blank', onclick: function (e) { e.stopPropagation(); } }, h('b', null, r.name))),
            h('td', { class: 'muted' }, r.logbook), h('td', null, r.sample), h('td', null, r.info),
            h('td', { class: 'muted small' }, r.start), h('td', { class: 'muted small' }, r.end),
            h('td', null, r.interrupted ? h('span', { class: 'chip warn' }, 'interrupted') : null,
              Object.keys(r.conditions || {}).slice(0, 6).map(function (k) {
                var c = r.conditions[k];
                return h('span', { class: 'chip', title: 'last noted ' + fmtDate(c.time, true) + ' (' + c.logbook + ' #' + c.mid + ')' }, k + ' ' + c.raw);
              }))));
          tb.appendChild(det);
        });
        out.appendChild(tb);
      }).catch(function (e) { out.innerHTML = ''; out.appendChild(fail(e)); });
    }
    load();
  };

  // ---- feedback & notes (logbook admins) -------------------------------------------------------

  routes.notes = function () {
    main.appendChild(h('h1', null, 'Feedback & notes'));
    main.appendChild(h('p', { class: 'sub' }, 'Notes tell the AI how to read a logbook: what matters, what is routine, what abbreviations mean. ' +
      'They are added to the logbook\'s "AI context" from elogd.cfg. Answers rated bad are listed below; "Suggest a note" drafts one from the answer and the comment. ' +
      'After a change the logbook is analysed again in the background.'));
    var notesBox = h('div'), fbBox = h('div');
    main.appendChild(h('h2', null, 'Notes'));
    main.appendChild(notesBox);
    main.appendChild(h('h2', null, 'Answers rated bad'));
    main.appendChild(fbBox);
    var lbSel = h('select');
    var text = h('textarea', { rows: 2, style: 'flex:1;min-width:300px', placeholder: 'e.g. Moving a sample between carousel positions is routine; moves to MP (measuring stage) matter.' });
    function loadNotes() {
      notesBox.innerHTML = ''; notesBox.appendChild(loading());
      api('admin/notes').then(function (d) {
        notesBox.innerHTML = '';
        lbSel.innerHTML = '';
        d.logbooks.forEach(function (n) { lbSel.appendChild(h('option', { value: n }, n)); });
        notesBox.appendChild(h('div', { class: 'row', style: 'margin-bottom:10px; align-items:flex-start' }, lbSel, text,
          h('button', { class: 'btn primary', onclick: function () {
            if (!text.value.trim()) return;
            api('admin/notes', { logbook: lbSel.value, text: text.value.trim() }).then(function () { text.value = ''; loadNotes(); }).catch(function (e) { alert(e.message); });
          } }, 'Add note')));
        var cfgd = Object.keys(d.config).filter(function (n) { return d.config[n]; });
        if (!d.notes.length && !cfgd.length) notesBox.appendChild(h('div', { class: 'muted' }, 'No notes yet.'));
        if (d.notes.length || cfgd.length) notesBox.appendChild(h('table', { class: 't' }, h('tr', null, h('th', null, 'Logbook'), h('th', null, 'Note'), h('th', null, 'From'), h('th', null, '')),
          cfgd.map(function (n) { return h('tr', null, h('td', null, n), h('td', null, d.config[n]), h('td', { class: 'muted small' }, 'elogd.cfg'), h('td')); }),
          d.notes.map(function (n) {
            return h('tr', null, h('td', null, n.logbook), h('td', null, n.text), h('td', { class: 'muted small' }, n.by + ', ' + fmtDate(n.at)),
              h('td', null, h('button', { class: 'btn small', onclick: function () { if (confirm('Delete this note?')) api('admin/notes/' + n.id, undefined, 'DELETE').then(loadNotes); } }, 'Delete')));
          })));
      }).catch(function (e) { notesBox.innerHTML = ''; notesBox.appendChild(fail(e)); });
    }
    api('admin/feedback').then(function (rows) {
      if (!rows.length) { fbBox.appendChild(h('div', { class: 'muted' }, 'No answers rated bad.')); return; }
      rows.forEach(function (r) {
        var out = h('div');
        fbBox.appendChild(h('div', { class: 'res' },
          h('div', { class: 'head' }, h('b', null, r.question), h('span', { class: 'spacer' }), h('span', { class: 'meta' }, r.user + ' · ' + fmtDate(r.at, true))),
          r.comment ? h('div', { class: 'summary' }, '“' + r.comment + '”') : null,
          h('details', null, h('summary', { class: 'small muted' }, 'Answer'), h('div', { class: 'md small', html: md(r.answer) })),
          h('div', { class: 'tags' }, h('button', { class: 'btn small', onclick: function (e) {
            e.target.disabled = true;
            api('admin/feedback/' + r.id + '/suggest', {}).then(function (sg) {
              out.innerHTML = '';
              var t = h('textarea', { rows: 2, style: 'width:100%' }); t.value = sg.note;
              var sel = h('select', null, (ME.admin_of || []).map(function (n) { return h('option', { value: n, selected: n === sg.logbook }, n); }));
              out.appendChild(h('div', { class: 'row', style: 'margin-top:6px' }, sel, t, h('button', { class: 'btn primary', onclick: function () {
                api('admin/notes', { logbook: sel.value, text: t.value, message_id: r.id }).then(function () { out.innerHTML = '<span class="muted">Note added.</span>'; loadNotes(); });
              } }, 'Add')));
            }).catch(function (x) { out.textContent = x.message; }).then(function () { e.target.disabled = false; });
          } }, 'Suggest a note')), out));
      });
    }).catch(function (e) { fbBox.appendChild(fail(e)); });
    loadNotes();
  };

  // ---- connect: personal tokens for MCP clients and scripts -----------------------------------

  routes.connect = function () {
    var base = location.origin + location.pathname.replace(/[^/]*$/, '');
    main.appendChild(h('h1', null, 'Connect'));
    main.appendChild(h('p', { class: 'sub' }, 'Use the logbooks from Claude Desktop, Claude Code or another assistant (MCP), or from scripts. ' +
      'A token acts as you: it sees only the logbooks you may read, and stops working if your ELOG account is deactivated. Keep it secret.'));
    var list = h('div'), shown = h('div');
    var name = h('input', { type: 'text', placeholder: 'Name, e.g. Claude Desktop on my laptop', style: 'min-width:300px' });
    main.appendChild(h('div', { class: 'row', style: 'margin-bottom:12px' }, name, h('button', { class: 'btn primary', onclick: function () {
      api('tokens', { name: name.value || 'token' }).then(function (d) {
        shown.innerHTML = '';
        var cmd = 'claude mcp add --transport http elog ' + base + 'mcp --header "Authorization: Bearer ' + d.token + '"';
        shown.appendChild(h('div', { class: 'card', style: 'margin-bottom:12px' },
          h('b', null, 'Your new token (shown only now):'), h('pre', { class: 'small', style: 'white-space:pre-wrap;word-break:break-all' }, d.token),
          h('div', { class: 'small' }, 'Claude Code:'), h('pre', { class: 'small', style: 'white-space:pre-wrap;word-break:break-all' }, cmd),
          h('div', { class: 'small' }, 'Claude Desktop and other MCP clients: server URL ', h('code', null, base + 'mcp'), ', header ',
            h('code', null, 'Authorization: Bearer <token>'), '. Tools: search, ask, get_entry, summary, item_history, runs, list_logbooks.')));
        name.value = ''; load();
      }).catch(function (e) { alert(e.status === 403 ? 'Log in to ELOG with your own account to create a token.' : e.message); });
    } }, 'Create token')));
    main.appendChild(shown);
    main.appendChild(list);
    function load() {
      api('tokens').then(function (rows) {
        list.innerHTML = '';
        if (!rows.length) { list.appendChild(h('div', { class: 'muted' }, 'No tokens.')); return; }
        list.appendChild(h('table', { class: 't' }, h('tr', null, h('th', null, 'Name'), h('th', null, 'Created'), h('th', null, 'Last used'), h('th', null, '')),
          rows.map(function (t) {
            return h('tr', null, h('td', null, t.name), h('td', { class: 'muted' }, fmtDate(t.created)), h('td', { class: 'muted' }, t.last_used ? ago(t.last_used) : 'never'),
              h('td', null, h('button', { class: 'btn small', onclick: function () { if (confirm('Revoke this token?')) api('tokens/' + t.id, undefined, 'DELETE').then(load); } }, 'Revoke')));
          })));
      });
    }
    load();
  };

  // ---- status (admins) -------------------------------------------------------------------------

  routes.status = function () {
    main.appendChild(h('h1', null, 'Status'));
    var out = h('div'); main.appendChild(out); out.appendChild(loading());
    function bar(n, total) { return h('div', { class: 'bar', title: n + ' / ' + total }, h('span', { style: 'width:' + (total ? Math.round(100 * n / total) : 0) + '%' })); }
    function load() {
      api('status').then(function (s) {
        out.innerHTML = '';
        var ix = s.index;
        out.appendChild(h('div', { class: 'grid' },
          h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'Entries indexed'), h('b', null, ix.entries), h('span', { class: 'muted small' }, ix.runs + ' DAQ runs · database ' + (s.db_bytes / 1048576).toFixed(1) + ' MB')),
          h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'Semantic index (' + (s.config.embed_model || 'off') + ')'), h('b', null, ix.embedded + ' / ' + ix.entries), bar(ix.embedded, ix.entries), h('span', { class: 'muted small' }, s.worker.embed || '')),
          h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'AI analysis (' + (s.config.fast_model || 'off') + ')'), h('b', null, ix.enriched + ' / ' + ix.entries), bar(ix.enriched, ix.entries),
            h('span', { class: 'muted small' }, (s.worker.enrich || '') + (ix.enrich_errors ? ' · ' + ix.enrich_errors + ' failed' : '') + (s.config.enrich_since ? ' · since ' + s.config.enrich_since : '')))));
        out.appendChild(h('h2', null, 'Providers'));
        out.appendChild(h('table', { class: 't' }, h('tr', null, h('th', null, ''), h('th', null, 'Model'), h('th', null, 'State')),
          Object.keys(s.providers).map(function (k) {
            var p = s.providers[k];
            return h('tr', null, h('td', null, k), h('td', null, p.model || ''), h('td', null, p.ok ? h('span', { class: 'chip ' + (p.model_available === false ? 'warn' : 'fixed') }, p.model_available === false ? 'reachable, model not pulled' : 'ok · ' + p.ms + ' ms') : h('span', { class: 'chip problem' }, p.error)));
          })));
        var errs = Object.keys(s.worker.errors || {});
        if (errs.length) out.appendChild(h('div', { class: 'err', style: 'margin-top:8px' }, errs.map(function (k) { return h('div', null, k + ': ' + s.worker.errors[k]); })));
        out.appendChild(h('h2', null, 'Usage, last 30 days'));
        out.appendChild(s.usage_30d.length ? h('table', { class: 't' }, h('tr', null, ['Task', 'Model', 'Calls', 'Tokens in', 'Tokens out', 'Avg time', 'Failed'].map(function (x) { return h('th', null, x); })),
          s.usage_30d.map(function (u) { return h('tr', null, h('td', null, u.task), h('td', null, u.model), h('td', null, u.calls), h('td', null, u.tokens_in), h('td', null, u.tokens_out), h('td', null, (u.avg_ms / 1000).toFixed(1) + ' s'), h('td', null, u.failed || '')); })) : h('div', { class: 'muted' }, 'No model calls yet.'));
        out.appendChild(h('h2', null, 'Configuration'));
        out.appendChild(h('pre', { class: 'card small', style: 'overflow:auto' }, JSON.stringify(s.config, null, 2)));
        out.appendChild(h('h2', null, 'Maintenance'));
        var act = function (label, path, body, confirmText) {
          return h('button', { class: 'btn', onclick: function (e) {
            if (confirmText && !confirm(confirmText)) return;
            e.target.disabled = true;
            api(path, body).then(function () { e.target.textContent = label + ' ✓'; setTimeout(load, 1500); }).catch(function (x) { alert(x.message); e.target.disabled = false; });
          } }, label);
        };
        out.appendChild(h('div', { class: 'row' },
          act('Check providers', 'admin/check', {}),
          act('Rescan logbooks', 'admin/rescan', { full: false }),
          act('Rebuild index', 'admin/rescan', { full: true }, 'Re-read every logbook file? AI results are kept for unchanged entries.'),
          act('Redo AI analysis', 'admin/redo', { what: 'enrich' }, 'Analyse all entries again (e.g. after changing the model)? This can take long on a CPU.'),
          act('Redo embeddings', 'admin/redo', { what: 'embed' }, 'Embed all entries again (needed after changing the embedding model)?'),
          act('Clear digests', 'admin/redo', { what: 'digests' }, 'Delete all stored digests?')));
      }).catch(function (e) { out.innerHTML = ''; out.appendChild(fail(e)); });
    }
    load();
    var t = setInterval(function () { if (parseHash().name !== 'status') clearInterval(t); else if (!document.hidden) load(); }, 15000);
  };

  // ---- start -----------------------------------------------------------------------------------

  api('me').then(function (me) {
    ME = me;
    document.getElementById('who').textContent = me.user + (me.admin ? ' · admin' : '');
    document.getElementById('nav-status').classList.toggle('hidden', !me.admin);
    document.getElementById('nav-notes').classList.toggle('hidden', !(me.admin_of || []).length);
    document.getElementById('elog-link').href = ELOG;
    window.addEventListener('hashchange', route);
    route();
  }).catch(function (e) {
    main.innerHTML = '';
    if (e.status === 401) {
      main.appendChild(h('div', { class: 'card login' }, h('h1', null, 'ELOG AI'), h('p', null, 'Log in to ELOG first; the AI uses your ELOG account and shows only logbooks you can read.'),
        h('a', { class: 'btn primary', href: ELOG }, 'Go to ELOG')));
    } else main.appendChild(fail(e));
  });
})();
