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
  var rel = location.pathname.indexOf(ROOT) === 0 ? location.pathname.slice(ROOT.length) : location.pathname.slice(1);
  var parts = rel.split('/');
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
  function css() {
    if ($('link[data-eai]')) return;
    document.head.appendChild(h('link', { rel: 'stylesheet', href: AI + 'widget.css', 'data-eai': '1' }));
  }

  var isEdit = !!$('textarea[name="Text"]');
  var isEntry = !isEdit && !!$('td.attribhead') && MID > 0;
  var isList = !isEdit && !isEntry && !!$('table.listframe');

  // ---- ask drawer (all logbook pages) ----------------------------------------------------------

  var drawer = null, conv = null;
  function openDrawer(prefill) {
    if (!drawer) buildDrawer();
    drawer.classList.add('eai-open');
    var ta = $('textarea', drawer);
    if (prefill) ta.value = prefill;
    ta.focus();
  }
  function buildDrawer() {
    var msgs = h('div', { class: 'eai-msgs' },
      h('div', { class: 'eai-hint' }, 'Ask about ', h('b', null, LOGBOOK), ' or all your logbooks. Answers cite the entries they use.'));
    var only = h('input', { type: 'checkbox', checked: true });
    var ta = h('textarea', { rows: 2, placeholder: 'e.g. When was the compressor last realigned?' });
    var send = h('button', { class: 'eai-btn eai-primary' }, 'Ask');
    drawer = h('aside', { class: 'eai-drawer', role: 'dialog', 'aria-label': 'Ask AI' },
      h('div', { class: 'eai-dhead' }, h('b', null, '✦ Ask AI'), h('span', { class: 'eai-sp' }),
        h('a', { href: AI + '#ask', target: '_blank', title: 'Open the AI workspace' }, 'Workspace ↗'),
        h('button', { class: 'eai-x', 'aria-label': 'Close', onclick: function () { drawer.classList.remove('eai-open'); } }, '×')),
      msgs,
      h('div', { class: 'eai-compose' }, ta, h('div', { class: 'eai-row' }, h('label', null, only, ' only ' + LOGBOOK), h('span', { class: 'eai-sp' }), send)));
    document.body.appendChild(drawer);
    function go() {
      var q = ta.value.trim();
      if (!q) return;
      ta.value = '';
      if ($('.eai-hint', msgs)) msgs.innerHTML = '';
      msgs.appendChild(h('div', { class: 'eai-q' }, q));
      var a = h('div', { class: 'eai-a' }, h('span', { class: 'eai-muted' }, 'Searching…'));
      msgs.appendChild(a);
      var text = '', srcs = [];
      send.disabled = true;
      stream('ask', { question: q, conversation_id: conv, logbooks: only.checked ? [LOGBOOK] : [] }, function (ev) {
        if (ev.type === 'conversation') conv = ev.id;
        else if (ev.type === 'sources') srcs = ev.sources;
        else if (ev.type === 'delta') { text += ev.text; a.innerHTML = md(text); msgs.scrollTop = msgs.scrollHeight; }
        else if (ev.type === 'error') a.appendChild(h('div', { class: 'eai-err' }, ev.error));
        else if (ev.type === 'done' && srcs.length) a.appendChild(h('div', { class: 'eai-srcs' }, srcs.slice(0, 8).map(function (s) {
          return h('a', { href: entryUrl(s.logbook, s.mid), title: s.summary || '' }, s.logbook + ' #' + s.mid + (s.subject ? ' · ' + s.subject : ''));
        })));
      }).catch(function (e) { a.innerHTML = ''; a.appendChild(h('div', { class: 'eai-err' }, e.message)); })
        .then(function () { send.disabled = false; });
    }
    send.addEventListener('click', go);
    ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') drawer.classList.remove('eai-open'); });
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
    var body = h('div', { class: 'eai-body' }, h('span', { class: 'eai-muted' }, 'Loading…'));
    var panel = h('details', { class: 'eai-panel', open: localStorage.getItem('eai-closed') !== '1' },
      h('summary', null, h('span', { class: 'eai-spark' }, '✦'), ' AI', h('span', { class: 'eai-sub' }, '')), body);
    panel.addEventListener('toggle', function () { try { localStorage.setItem('eai-closed', panel.open ? '0' : '1'); } catch (e) {} });
    if (host && host.parentNode) host.parentNode.insertBefore(panel, host.nextSibling);
    else document.body.appendChild(panel);
    var tries = 0;
    function load() {
      api('entry?logbook=' + encodeURIComponent(LOGBOOK) + '&mid=' + MID).then(function (d) {
        render(d);
        if (d.pending && tries++ < 12) setTimeout(load, 5000);
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
          api('thread/summary', { logbook: LOGBOOK, mid: MID }).then(function (r) { ts.innerHTML = md(r.text || 'Nothing to summarise.'); })
            .catch(function (x) { ts.textContent = x.message; });
        } }, 'Summarise this thread (' + d.thread.entries + ' entries)'));
        left.appendChild(ts);
      }
      right.appendChild(h('div', { class: 'eai-h' }, 'Related entries'));
      if (!d.similar.length) right.appendChild(h('div', { class: 'eai-muted' }, 'None found yet.'));
      d.similar.forEach(function (s) {
        right.appendChild(h('a', { class: 'eai-rel', href: entryUrl(s.logbook, s.mid), title: s.summary || s.snippet || '' },
          h('span', { class: 'eai-ref' }, (s.logbook !== LOGBOOK ? s.logbook + ' ' : '') + '#' + s.mid),
          h('span', null, s.subject || s.summary || (s.snippet || '').slice(0, 60)),
          h('span', { class: 'eai-muted' }, fmt(s.time))));
      });
      right.appendChild(h('div', { class: 'eai-actions' },
        h('button', { class: 'eai-btn', onclick: function () { openDrawer('Regarding ' + LOGBOOK + ' #' + MID + ': '); } }, 'Ask about this'),
        a && a.fresh ? null : h('button', { class: 'eai-btn', onclick: function (e) {
          e.target.disabled = true; e.target.textContent = 'Analysing…';
          api('entry/analyze', { logbook: LOGBOOK, mid: MID }).then(render).catch(function (x) { e.target.textContent = x.message; });
        } }, 'Analyse now')));
    }
    load();
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
      if (isEntry) entryPanel();
      if (isEdit && d.features.chat) editAssist();
    }).catch(function () { /* AI not available: ELOG works as before */ });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
