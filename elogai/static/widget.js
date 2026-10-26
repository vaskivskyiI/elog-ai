/* ELOG AI widget, loaded into ELOG's own pages with one line in elogd.cfg:
 *
 *   Bottom text = <script src="/ai/widget.js" defer></script>
 *
 * Entry pages get an AI panel (summary, related entries, samples, runs, thread summary),
 * list pages an "Ask AI" drawer, edit pages a writing assistant. Everything is optional:
 * if the AI service is down or the user is not logged in, nothing is shown. */
(function () {
  'use strict';
  if (window.__elogAI) return;
  window.__elogAI = true;

  var me = document.currentScript || document.querySelector('script[src*="widget.js"]');
  var AI = me ? me.src.replace(/widget\.js(\?.*)?$/, '') : '/ai/';
  var AIPATH = new URL(AI, location.href).pathname;            // e.g. /ai/
  var ROOT = AIPATH.replace(/ai\/$/, '') || '/';              // ELOG's root
  var relPath = location.pathname.indexOf(ROOT) === 0 ? location.pathname.slice(ROOT.length) : location.pathname.slice(1);
  var parts = relPath.split('/');
  var LOGBOOK = parts[0] ? decodeURIComponent(parts[0]) : '';
  var MID = /^\d+$/.test(parts[1] || '') ? +parts[1] : 0;
  if (!LOGBOOK) return;

  function $(s, r) { return (r || document).querySelector(s); }
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
  function entryUrl(lb, mid) { return ROOT + encodeURIComponent(lb) + '/' + mid; }
  function api(path, body) {
    var o = { credentials: 'same-origin', headers: { 'X-Requested-With': 'elog-ai' } };
    if (body !== undefined) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(AI + 'api/' + path, o).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) { var e = new Error(d.error || r.statusText); e.status = r.status; throw e; } return d; });
    });
  }
  function stream(path, body, on) {
    return fetch(AI + 'api/' + path, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'elog-ai' }, body: JSON.stringify(body) })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || r.statusText); });
        var rd = r.body.getReader(), dec = new TextDecoder(), buf = '';
        function pump() {
          return rd.read().then(function (x) {
            if (x.done) { if (buf.trim()) on(JSON.parse(buf)); return; }
            buf += dec.decode(x.value, { stream: true });
            var ls = buf.split('\n'); buf = ls.pop();
            ls.forEach(function (l) { if (l.trim()) on(JSON.parse(l)); });
            return pump();
          });
        }
        return pump();
      });
  }
  function md(src) {
    var html = [], list = false;
    String(src || '').split('\n').forEach(function (line) {
      var s = esc(line).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\[([^\]\[#]+?) #(\d+)\]/g, function (m, lb, id) {
          var raw = lb.replace(/&#(\d+);/g, function (x, c) { return String.fromCharCode(c); });
          return '<a class="eai-cite" href="' + esc(entryUrl(raw, id)) + '">' + lb + ' #' + id + '</a>';
        });
      var m = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(s);
      if (m) { if (!list) { html.push('<ul>'); list = true; } html.push('<li>' + m[1] + '</li>'); return; }
      if (list) { html.push('</ul>'); list = false; }
      if (/^\s*#{1,4}\s/.test(s)) html.push('<b>' + s.replace(/^\s*#+\s*/, '') + '</b><br>');
      else if (s.trim()) html.push('<p>' + s + '</p>');
    });
    if (list) html.push('</ul>');
    return html.join('');
  }
  function fmt(t) {
    var d = new Date(t * 1000), p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
  }
  // how long before or after the entry `ref` something happened: "3 d before", "2 h after"
  function ago(t, ref) {
    if (!ref) return fmt(t);
    var s = t - ref, a = Math.abs(s), w = s < 0 ? ' before' : ' after';
    if (a < 3600) return 'same hour';
    if (a < 86400) return Math.round(a / 3600) + ' h' + w;
    if (a < 86400 * 60) return Math.round(a / 86400) + ' d' + w;
    return fmt(t);
  }
  function css() {
    if ($('link[data-eai]')) return;
    document.head.appendChild(h('link', { rel: 'stylesheet', href: AI + 'widget.css', 'data-eai': '1' }));
  }

  var isEdit = !!$('textarea[name="Text"]');
  var isEntry = !isEdit && !!$('td.attribhead') && MID > 0;
  var isList = !isEdit && !isEntry && !!$('table.listframe');

  // ---- ask drawer (all logbook pages) ----------------------------------------------------------

  // The panel keeps its conversation across ELOG pages: clicking a source opens the entry and the
  // same chat comes back next to it (state per browser tab, conversation from the server).
  var drawer = null, conv = null, msgsEl = null;
  function dstate(upd) {
    var st = {};
    try { st = JSON.parse(sessionStorage.getItem('eai-drawer') || '{}'); } catch (e) {}
    if (upd) {
      for (var k in upd) st[k] = upd[k];
      try { sessionStorage.setItem('eai-drawer', JSON.stringify(st)); } catch (e) {}
    }
    return st;
  }
  function openDrawer(prefill) {
    if (!drawer) buildDrawer();
    drawer.classList.add('eai-open');
    document.documentElement.classList.add('eai-docked');
    dstate({ open: true });
    var ta = $('textarea', drawer);
    if (prefill) ta.value = prefill;
    ta.focus();
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove('eai-open');
    document.documentElement.classList.remove('eai-docked');
    dstate({ open: false });
  }
  function sourceLinks(srcs) {
    return h('div', { class: 'eai-srcs' }, (srcs || []).slice(0, 10).map(function (s) {
      var here = s.logbook.toLowerCase() === LOGBOOK.toLowerCase() && s.mid === MID;
      return h('a', { href: entryUrl(s.logbook, s.mid), class: here ? 'eai-here' : null,
                      title: (here ? 'This entry. ' : '') + (s.summary || '') },
        (s.related ? '↔ ' : '') + s.logbook + ' #' + s.mid + (s.subject ? ' · ' + s.subject : ''));
    }));
  }
  function hint() {
    return h('div', { class: 'eai-hint' }, 'Ask about ', h('b', null, LOGBOOK), ' or all your logbooks. Answers cite the entries they use; ' +
      'the conversation stays here while you open them.');
  }
  function restore(id) {
    api('conversations/' + id).then(function (c) {
      conv = c.id;
      msgsEl.innerHTML = '';
      c.messages.forEach(function (m) {
        if (m.role === 'user') msgsEl.appendChild(h('div', { class: 'eai-q' }, m.content));
        else {
          var a = h('div', { class: 'eai-a', html: md(m.content) });
          if (m.sources && m.sources.length) a.appendChild(sourceLinks(m.sources));
          msgsEl.appendChild(a);
        }
      });
      msgsEl.scrollTop = msgsEl.scrollHeight;
    }).catch(function () { conv = null; dstate({ conv: null }); });
  }
  function buildDrawer() {
    var st = dstate();
    msgsEl = h('div', { class: 'eai-msgs' }, hint());
    var msgs = msgsEl;
    var only = h('input', { type: 'checkbox', checked: st.only !== false });
    only.addEventListener('change', function () { dstate({ only: only.checked }); });
    var ta = h('textarea', { rows: 2, placeholder: 'e.g. When was the compressor last realigned?' });
    var send = h('button', { class: 'eai-btn eai-primary' }, 'Ask');
    drawer = h('aside', { class: 'eai-drawer', role: 'dialog', 'aria-label': 'Ask AI' },
      h('div', { class: 'eai-dhead' }, h('b', null, '✦ Ask AI'), h('span', { class: 'eai-sp' }),
        h('button', { class: 'eai-link', title: 'Start a new conversation', onclick: function () {
          conv = null; dstate({ conv: null }); msgs.innerHTML = ''; msgs.appendChild(hint()); ta.focus();
        } }, 'New chat'),
        h('a', { href: AI + '#ask' + (st.conv ? '?c=' + st.conv : ''), target: '_blank', title: 'Open the AI workspace',
                 onclick: function (e) { e.currentTarget.href = AI + '#ask' + (conv ? '?c=' + conv : ''); } }, 'Workspace ↗'),
        h('button', { class: 'eai-x', 'aria-label': 'Close', onclick: closeDrawer }, '×')),
      msgs,
      h('div', { class: 'eai-compose' }, ta, h('div', { class: 'eai-row' }, h('label', null, only, ' only ' + LOGBOOK), h('span', { class: 'eai-sp' }), send)));
    document.body.appendChild(drawer);
    if (st.conv) restore(st.conv);
    function go() {
      var q = ta.value.trim();
      if (!q) return;
      ta.value = '';
      if ($('.eai-hint', msgs)) msgs.innerHTML = '';
      msgs.appendChild(h('div', { class: 'eai-q' }, q));
      var a = h('div', { class: 'eai-a' }, h('span', { class: 'eai-muted' }, 'Searching…'));
      msgs.appendChild(a);
      msgs.scrollTop = msgs.scrollHeight;
      var text = '', srcs = [];
      send.disabled = true;
      stream('ask', { question: q, conversation_id: conv, logbooks: only.checked ? [LOGBOOK] : [] }, function (ev) {
        if (ev.type === 'conversation') { conv = ev.id; dstate({ conv: conv }); }
        else if (ev.type === 'sources') srcs = ev.sources;
        else if (ev.type === 'delta') { text += ev.text; a.innerHTML = md(text); msgs.scrollTop = msgs.scrollHeight; }
        else if (ev.type === 'error') a.appendChild(h('div', { class: 'eai-err' }, ev.error));
        else if (ev.type === 'done' && srcs.length) a.appendChild(sourceLinks(srcs));
      }).catch(function (e) { a.innerHTML = ''; a.appendChild(h('div', { class: 'eai-err' }, e.message)); })
        .then(function () { send.disabled = false; });
    }
    send.addEventListener('click', go);
    ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && drawer.classList.contains('eai-open')) closeDrawer(); });
  }
  function askButton() {
    var b = h('button', { type: 'button', class: 'eai-ask', title: 'Ask AI about this logbook (Alt+A)', onclick: function () { openDrawer(); } }, '✦ Ask AI');
    var bar = $('.f7-topbar');
    if (bar) { b.classList.add('eai-intop'); bar.appendChild(b); }
    else { b.classList.add('eai-float'); document.body.appendChild(b); }
    document.addEventListener('keydown', function (e) { if (e.altKey && (e.key === 'a' || e.key === 'A')) { e.preventDefault(); openDrawer(); } });
  }

  // ---- entry page panel ------------------------------------------------------------------------

  function entryPanel() {
    var anchor = $('.messageframe') || $('td.attribhead');
    var host = anchor && (anchor.closest('table.frame') || anchor.closest('table'));
    var panel = aiPanel(LOGBOOK, MID, false);
    if (host && host.parentNode) host.parentNode.insertBefore(panel, host.nextSibling);
    else document.body.appendChild(panel);
  }

  // The AI panel of one entry: on the entry page (wide, two columns) and in the list's preview
  // panel (compact). It loads itself and keeps polling while the entry is being analysed.
  function aiPanel(lb, mid, compact) {
    var key = compact ? 'eai-closed-preview' : 'eai-closed';
    var body = h('div', { class: 'eai-body' }, h('span', { class: 'eai-muted' }, 'Loading…'));
    var open = true;
    try { open = localStorage.getItem(key) !== '1'; } catch (e) {}
    var panel = h('details', { class: 'eai-panel' + (compact ? ' eai-compact' : ''), open: open },
      h('summary', null, h('span', { class: 'eai-spark' }, '✦'), ' AI', h('span', { class: 'eai-sub' }, '')), body);
    panel.addEventListener('toggle', function () { try { localStorage.setItem(key, panel.open ? '0' : '1'); } catch (e) {} });
    var tries = 0;
    function load() {
      api('entry?logbook=' + encodeURIComponent(lb) + '&mid=' + mid).then(function (d) {
        if (!panel.isConnected && tries) return;      // the preview moved on to another entry
        render(d);
        if (d.pending && tries++ < 12) setTimeout(function () { if (panel.isConnected) load(); }, 5000);
      }).catch(function (e) {
        if (e.status === 401 || e.status === 404) panel.remove();
        else body.innerHTML = '<span class="eai-muted">AI unavailable: ' + esc(e.message) + '</span>';
      });
    }
    function render(d) {
      body.innerHTML = '';
      var a = d.analysis;
      var sub = $('.eai-sub', panel);
      sub.textContent = a && a.summary ? ' · ' + a.summary : (d.pending ? ' · analysing…' : '');
      var top = h('div', { class: 'eai-cols' });
      var left = h('div'), right = h('div');
      top.appendChild(left); top.appendChild(right);
      body.appendChild(top);
      if (a && (a.fresh || !d.pending)) {
        if (a.summary) left.appendChild(h('p', { class: 'eai-summary' }, a.summary));
        var chips = h('div', { class: 'eai-chips' });
        if (a.kind && a.kind !== 'routine') chips.appendChild(h('span', { class: 'eai-chip eai-kind' }, a.kind));
        if (a.problem) chips.appendChild(h('span', { class: 'eai-chip eai-prob', title: a.problem.symptom || '' },
          '⚠ ' + (a.problem.component || 'problem') + (a.problem.severity ? ' · ' + a.problem.severity : '')));
        (a.tags || []).forEach(function (t) { chips.appendChild(h('a', { class: 'eai-chip', href: AI + '#search?q=' + encodeURIComponent(t), target: '_blank' }, '#' + t)); });
        if (chips.childNodes.length) left.appendChild(chips);
        if (a.english) {
          var tr = h('div', { class: 'eai-trans eai-hidden' }, a.english);
          left.appendChild(h('button', { class: 'eai-link', onclick: function () { tr.classList.toggle('eai-hidden'); } }, 'English translation'));
          left.appendChild(tr);
        }
        if (a.measurements && a.measurements.length) left.appendChild(h('table', { class: 'eai-meas' },
          a.measurements.map(function (m) { return h('tr', null, h('td', null, m.quantity), h('td', null, h('b', null, m.value), ' ', m.unit || '')); })));
        if (a.error && !a.summary) left.appendChild(h('div', { class: 'eai-muted' }, 'Not analysed: ' + a.error));
      } else if (d.pending) left.appendChild(h('div', { class: 'eai-muted' }, h('span', { class: 'eai-spin' }), ' The AI is analysing this entry…'));
      if (d.entities.length) left.appendChild(h('div', { class: 'eai-chips' }, h('span', { class: 'eai-muted' }, 'Also in: '),
        d.entities.filter(function (e) { return e.entries > 1; }).slice(0, 10).map(function (e) {
          return h('a', { class: 'eai-chip', href: AI + '#entities?value=' + encodeURIComponent(e.value), target: '_blank', title: e.type + ' · ' + e.entries + ' entries' },
            e.value + ' (' + e.entries + ')');
        })));
      d.runs.forEach(function (r) {
        left.appendChild(h('div', { class: 'eai-run' }, h('b', null, 'Run ' + r.name), ' · ' + (r.sample || '') + ' · ' + (r.info || ''),
          h('span', { class: 'eai-muted' }, ' ' + (r.start || '') + ' → ' + (r.end || '') + (r.interrupted ? ' (interrupted)' : ''))));
      });
      d.images.forEach(function (im) { left.appendChild(h('div', { class: 'eai-muted eai-img' }, '🖼 ' + im.file.replace(/^\d{6}_\d{6}_/, '') + ': ' + im.description)); });
      if (d.thread.entries > 1) {
        var ts = h('div', { class: 'eai-thread' });
        if (d.thread.summary) ts.innerHTML = md(d.thread.summary);
        else ts.appendChild(h('button', { class: 'eai-btn', onclick: function (e) {
          e.target.disabled = true; e.target.textContent = 'Summarising ' + d.thread.entries + ' entries…';
          api('thread/summary', { logbook: lb, mid: mid }).then(function (r) { ts.innerHTML = md(r.text || 'Nothing to summarise.'); })
            .catch(function (x) { ts.textContent = x.message; });
        } }, 'Summarise this thread (' + d.thread.entries + ' entries)'));
        left.appendChild(ts);
      }
      var ar = d.around || {};
      if ((ar.entries && ar.entries.length) || (ar.values && ar.values.length)) {
        var lbs = (ar.entries || []).concat(ar.values || []).map(function (s) { return s.logbook; })
          .filter(function (x, i, a) { return a.indexOf(x) === i; });
        right.appendChild(h('div', { class: 'eai-h', title: 'Related logbooks: their last entries before this one (however old) and the day after' },
          lbs.join(', ') + ' at that time'));
        if (ar.values && ar.values.length) right.appendChild(h('div', { class: 'eai-chips' }, ar.values.map(function (v) {
          return h('a', { class: 'eai-chip', href: entryUrl(v.logbook, v.mid), title: v.logbook + ' #' + v.mid + ', ' + fmt(v.time) },
            v.name + ' ' + v.raw + ' · ' + ago(v.time, d.time));
        })));
        (ar.entries || []).forEach(function (s) {
          right.appendChild(h('a', { class: 'eai-rel', href: entryUrl(s.logbook, s.mid), title: (s.summary || s.snippet || '') + ' · ' + fmt(s.time) },
            h('span', { class: 'eai-ref' }, s.logbook + ' #' + s.mid),
            h('span', null, s.subject || s.summary || (s.snippet || '').slice(0, 60)),
            h('span', { class: 'eai-muted' }, ago(s.time, d.time))));
        });
        right.appendChild(h('div', { style: 'height:8px' }));
      }
      right.appendChild(h('div', { class: 'eai-h' }, 'Related entries'));
      if (!d.similar.length) right.appendChild(h('div', { class: 'eai-muted' }, 'None found yet.'));
      d.similar.slice(0, compact ? 4 : 8).forEach(function (s) {
        right.appendChild(h('a', { class: 'eai-rel', href: entryUrl(s.logbook, s.mid), title: s.summary || s.snippet || '' },
          h('span', { class: 'eai-ref' }, (s.logbook !== lb ? s.logbook + ' ' : '') + '#' + s.mid),
          h('span', null, s.subject || s.summary || (s.snippet || '').slice(0, 60)),
          h('span', { class: 'eai-muted' }, fmt(s.time))));
      });
      right.appendChild(h('div', { class: 'eai-actions' },
        h('button', { class: 'eai-btn', onclick: function () { openDrawer('Regarding ' + lb + ' #' + mid + ': '); } }, 'Ask about this'),
        a && a.fresh ? null : h('button', { class: 'eai-btn', onclick: function (e) {
          e.target.disabled = true; e.target.textContent = 'Analysing…';
          api('entry/analyze', { logbook: lb, mid: mid }).then(render).catch(function (x) { e.target.textContent = x.message; });
        } }, 'Analyse now')));
    }
    load();
    return panel;
  }

  // ---- list page preview ("quick view") ---------------------------------------------------------

  // The F7 theme fills .f7-preview .f7-pv-body with the clicked entry; add the AI panel on top.
  function previewPanels() {
    var watched = null;
    function attach(pv) {
      if (watched === pv) return;
      watched = pv;
      var body = $('.f7-pv-body', pv);
      if (!body) return;
      new MutationObserver(function () {
        if (!$('table.frame', body) || $('.eai-panel', body)) return;
        var href = ($('.f7-pv-open', pv) || {}).href || '';
        var m = /\/([^/?#]+)\/(\d+)(?:[?#].*)?$/.exec(href);
        if (!m) return;
        var lb = decodeURIComponent(m[1]);
        if (lb.toLowerCase() !== LOGBOOK.toLowerCase()) return;
        body.insertBefore(aiPanel(LOGBOOK, +m[2], true), body.firstChild);
      }).observe(body, { childList: true });
    }
    var pv = $('.f7-preview');
    if (pv) attach(pv);
    // the theme creates the panel on the first click
    new MutationObserver(function () {
      var p = $('.f7-preview');
      if (p) attach(p);
    }).observe(document.body, { childList: true });
  }

  // ---- edit page assistant ---------------------------------------------------------------------

  function editor() { return window.tinymce && (window.tinymce.get('Text') || window.tinymce.activeEditor); }
  function getText() { var ed = editor(); var t = $('textarea[name="Text"]'); return ed ? ed.getContent() : (t ? t.value : ''); }
  function setText(v) { var ed = editor(); var t = $('textarea[name="Text"]'); if (ed) { ed.setContent(v); ed.fire('change'); } else if (t) t.value = v; }
  function fieldName(attr) { return attr.replace(/[^A-Za-z0-9]/g, '_'); }
  function currentAttrs() {
    var out = {}, f = $('form[name="form1"]') || document.forms[0];
    if (!f) return out;
    Array.prototype.forEach.call(f.elements, function (el) {
      if (!el.name || el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.name === 'Text' || el.type === 'file') return;
      if ((el.type === 'radio' || el.type === 'checkbox') && !el.checked) return;
      out[el.name] = el.value;
    });
    return out;
  }
  function applyAttr(attr, value) {
    var f = $('form[name="form1"]') || document.forms[0];
    var els = f ? f.querySelectorAll('[name="' + fieldName(attr) + '"], [name="' + attr + '"]') : [];
    var done = false;
    Array.prototype.forEach.call(els, function (el) {
      if (el.type === 'radio' || el.type === 'checkbox') { if (el.value === value) { el.checked = true; done = true; } }
      else if (el.tagName === 'SELECT') { if (Array.prototype.some.call(el.options, function (o) { return o.value === value; })) { el.value = value; done = true; } }
      else { el.value = value; done = true; }
      if (done) el.dispatchEvent(new Event('change', { bubbles: true }));
    });
    return done;
  }

  function editAssist() {
    var ta = $('textarea[name="Text"]');
    if (!ta) return;
    var out = h('div', { class: 'eai-assist-out' });
    var bar = h('div', { class: 'eai-assist' }, h('span', { class: 'eai-spark' }, '✦'),
      h('button', { type: 'button', class: 'eai-btn', onclick: suggest }, 'Suggest subject & fields'),
      h('button', { type: 'button', class: 'eai-btn', onclick: polish }, 'Polish text'),
      h('button', { type: 'button', class: 'eai-btn', onclick: function () { openDrawer(); } }, 'Ask AI'),
      out);
    ta.parentNode.insertBefore(bar, ta);

    function suggest() {
      out.innerHTML = '<span class="eai-muted"><span class="eai-spin"></span> Reading your draft…</span>';
      var subj = $('input[name="Subject"]');
      api('assist/suggest', { logbook: LOGBOOK, text: getText(), subject: subj ? subj.value : '', attrs: currentAttrs() }).then(function (d) {
        out.innerHTML = '';
        var box = h('div', { class: 'eai-sugg' });
        if (d.subject && subj) box.appendChild(h('div', null, 'Subject: ', h('b', null, d.subject), ' ',
          h('button', { type: 'button', class: 'eai-link', onclick: function (e) { subj.value = d.subject; e.target.textContent = '✓ applied'; } }, 'use')));
        var keys = Object.keys(d.attributes || {});
        if (keys.length) box.appendChild(h('div', null, keys.map(function (k) { return [k + ': ', h('b', null, d.attributes[k]), '  ']; }),
          h('button', { type: 'button', class: 'eai-link', onclick: function (e) {
            var n = keys.filter(function (k) { return applyAttr(k, d.attributes[k]); }).length;
            e.target.textContent = '✓ ' + n + ' applied';
          } }, 'apply')));
        if (d.missing && d.missing.length) box.appendChild(h('div', { class: 'eai-muted' }, 'Consider adding: ' + d.missing.join('; ')));
        if (d.similar && d.similar.length) box.appendChild(h('div', null, h('div', { class: 'eai-h' }, 'Similar existing entries (reply instead?)'),
          d.similar.map(function (s) { return h('a', { class: 'eai-rel', href: entryUrl(s.logbook, s.mid), target: '_blank' }, h('span', { class: 'eai-ref' }, s.logbook + ' #' + s.mid), h('span', null, s.subject || ''), h('span', { class: 'eai-muted' }, fmt(s.time))); })));
        if (!box.childNodes.length) box.appendChild(h('span', { class: 'eai-muted' }, 'Write a bit more first.'));
        out.appendChild(box);
      }).catch(function (e) { out.innerHTML = '<span class="eai-err">' + esc(e.message) + '</span>'; });
    }

    function polish() {
      var orig = getText();
      if (!orig.trim()) return;
      var res = '';
      var view = h('div', { class: 'eai-polish' });
      out.innerHTML = '';
      out.appendChild(view);
      view.innerHTML = '<span class="eai-muted"><span class="eai-spin"></span> Improving…</span>';
      stream('assist/polish', { logbook: LOGBOOK, text: orig }, function (ev) {
        if (ev.type === 'delta') { res += ev.text; view.textContent = res.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '); }
        if (ev.type === 'error') view.textContent = ev.error;
      }).then(function () {
        if (!res.trim()) return;
        out.appendChild(h('div', { class: 'eai-row' },
          h('button', { type: 'button', class: 'eai-btn eai-primary', onclick: function () { setText(/<\w/.test(orig) && !/<\w/.test(res) ? '<p>' + esc(res).replace(/\n\n+/g, '</p><p>').replace(/\n/g, '<br>') + '</p>' : res); out.innerHTML = '<span class="eai-muted">Replaced.' + (editor() ? ' Ctrl+Z in the editor undoes it.' : '') + '</span>'; } }, 'Replace my text'),
          h('button', { type: 'button', class: 'eai-btn', onclick: function () { out.innerHTML = ''; } }, 'Discard')));
      }).catch(function (e) { view.textContent = e.message; });
    }
  }

  // ---- start -----------------------------------------------------------------------------------

  function init() {
    // only show anything if the AI service answers for this user
    api('me').then(function (d) {
      if (!d.logbooks.some(function (l) { return l.name.toLowerCase() === LOGBOOK.toLowerCase(); })) return;
      LOGBOOK = d.logbooks.filter(function (l) { return l.name.toLowerCase() === LOGBOOK.toLowerCase(); })[0].name;
      css();
      askButton();
      if (dstate().open) openDrawer();          // came here from a link in the panel
      if (isEntry) entryPanel();
      if (isList) previewPanels();
      if (isEdit && d.features.chat) editAssist();
    }).catch(function () { /* AI not available: ELOG works as before */ });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
