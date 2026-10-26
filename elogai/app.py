"""HTTP API and pages. Served behind the same reverse proxy as ELOG, e.g. at /ai/, so the
browser sends ELOG's session cookie with every request (see auth.py)."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
from datetime import date, datetime

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, stream_with_context

from . import alerts, assistant, db, digest, enrich, insights, mcp, prompts, providers, search, settings
from .ai import AI, acting_user
from .auth import Auth, logbook_ids
from .context import Contexts
from .indexer import Indexer
from .worker import Worker

log = logging.getLogger("elogai")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(conf: dict, start_worker: bool = True) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    db.init(conf["state_dir"])
    ix = Indexer(conf["elogd_cfg"])
    ix.load_config(force=True)
    ai = AI(conf)
    ai.ctx = Contexts(ix)
    ai.ctx.refresh()
    auth = Auth(conf, ix)
    worker = Worker(ai, ix, conf)
    app.extensions["elogai"] = {"ai": ai, "ix": ix, "auth": auth, "worker": worker, "conf": conf}
    if start_worker:
        worker.start()

    # ---- request plumbing ----------------------------------------------------------------------

    @app.before_request
    def who():
        if request.path.startswith("/api/"):
            u = auth.identify(request.cookies, request.headers)
            if u is None:
                return jsonify({"error": "Please log in to ELOG first.", "login": True}), 401
            # a cross-site page cannot send this header without a CORS preflight, which we never allow
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "elog-ai":
                return jsonify({"error": "missing X-Requested-With header"}), 403
            g.user = u

    @app.route("/mcp", methods=["GET", "POST", "DELETE"])
    def mcp_endpoint():
        auth_h = request.headers.get("Authorization", "")
        u = auth.identify({}, {"Authorization": auth_h}) if auth_h.startswith("Bearer ") else None
        if u is None:
            return jsonify({"error": "Bearer token required (create one on the AI workspace's Connect page)"}), 401
        if request.method != "POST":
            return "", 405
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify(mcp.handle({"id": None}, u, ai, ix) or {"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "parse error"}}), 400
        with acting_user(u.key):
            if isinstance(payload, list):
                out = [r for r in (mcp.handle(m, u, ai, ix) for m in payload) if r]
                return (jsonify(out), 200) if out else ("", 202)
            r = mcp.handle(payload, u, ai, ix)
        return (jsonify(r), 200) if r else ("", 202)

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(providers.ProviderError)
    def provider_error(e):
        return jsonify({"error": f"AI provider: {e}"}), 502

    def allowed() -> set[int]:
        return g.user.allowed

    def need_admin():
        if not g.user.admin:
            abort(403)

    def logbook_id(name: str) -> int:
        lid = ix.logbooks.get(name) or next((v for k, v in ix.logbooks.items() if k.lower() == (name or "").lower()), None)
        if lid is None or lid not in allowed():
            abort(404)
        return lid

    def entry_row(lb: str, mid: int):
        lid = logbook_id(lb)
        e = db.get("SELECT * FROM entries WHERE logbook_id = ? AND mid = ?", lid, mid)
        if not e:
            abort(404)
        return e

    def scope_args(src) -> dict:
        lbs = src.get("logbooks") or []
        if isinstance(lbs, str):
            lbs = [x for x in lbs.split(",") if x]
        # only logbooks the user may read: the rest are dropped, not even echoed back
        return {"logbooks": [x for x in lbs if ix.logbooks.get(x) in allowed()], "since": src.get("since") or "",
                "until": src.get("until") or "", "author": src.get("author") or "", "kind": src.get("kind") or "",
                "entity": src.get("entity") or ""}

    def ndjson(gen):
        def out():
            with acting_user(g_user):
                try:
                    for ev in gen:
                        yield json.dumps(ev, ensure_ascii=False) + "\n"
                except providers.ProviderError as e:
                    yield json.dumps({"type": "error", "error": f"AI provider: {e}"}) + "\n"
        g_user = g.user.key
        return Response(stream_with_context(out()), mimetype="application/x-ndjson",
                        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    # ---- pages ---------------------------------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/<path:name>")
    def static_file(name):
        if name.startswith("api/"):
            abort(404)
        resp = send_from_directory(STATIC, name)
        if name in ("widget.js", "widget.css"):
            resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    # ---- who am I, what is there ---------------------------------------------------------------

    @app.get("/api/me")
    def me():
        lbs = db.all(f"""SELECT l.id, l.name, l.numeric_json, count(e.id) AS n, max(e.time) AS last
                         FROM logbooks l LEFT JOIN entries e ON e.logbook_id = l.id
                         WHERE l.active = 1 AND l.id IN ({','.join('?' * len(allowed())) or 'NULL'})
                         GROUP BY l.id ORDER BY l.name COLLATE NOCASE""", *sorted(allowed()))
        groups = {k: [n for n in v if ix.logbooks.get(n) in allowed()] for k, v in (ix.cfg.groups() if ix.cfg else {}).items()}
        return jsonify({"user": g.user.name, "login": g.user.login, "admin": g.user.admin,
                        "logbooks": [{"name": r["name"], "entries": r["n"], "last": r["last"],
                                      "series": db.jl(r["numeric_json"])} for r in lbs],
                        "groups": {k: v for k, v in groups.items() if v},
                        "features": {"chat": ai.chat_enabled, "embed": ai.embed_enabled, "vision": bool(ai.vision)},
                        "admin_of": [n for n, i in ix.logbooks.items() if i in allowed() and (
                            g.user.admin or (g.user.login and g.user.login.lower() in
                                             [a.strip().lower() for a in ix.cfg.get(n, "Admin user").split(",")]))],
                        "handovers": digest.handover_logbooks(ix),
                        "models": {"chat": ai.chat.model, "fast": ai.fast.model, "embed": ai.embed_model},
                        "elog_url": (ix.cfg.get_global("URL") if ix.cfg else "") or "/"})

    # ---- search --------------------------------------------------------------------------------

    @app.get("/api/search")
    def do_search():
        a = request.args
        k = min(int(a.get("k") or 20), 100)
        mode = a.get("mode") if a.get("mode") in ("hybrid", "keyword", "semantic") else "hybrid"
        with acting_user(g.user.key):
            return jsonify(search.search(ai, a.get("q", ""), scope_args(a), allowed(), k=k, mode=mode,
                                         offset=int(a.get("offset") or 0)))

    # ---- one entry -----------------------------------------------------------------------------

    @app.get("/api/entry")
    def entry():
        return entry_json(request.args.get("logbook", ""), int(request.args.get("mid") or 0))

    def entry_json(lb: str, mid: int):
        e = entry_row(lb, mid)
        x = db.get("SELECT * FROM enrich WHERE entry_id = ?", e["id"])
        fresh = bool(x and x["hash"] == e["hash"])
        if not fresh and ai.chat_enabled:
            db.run("UPDATE entries SET priority = 10 WHERE id = ?", e["id"])     # someone is looking at it
            worker.poke()
        readable = ",".join(str(i) for i in sorted(allowed())) or "NULL"
        ents = db.all(f"""SELECT n.type, n.value, n.norm, n.source,
                                 (SELECT count(DISTINCT m.entry_id) FROM entities m JOIN entries x ON x.id = m.entry_id
                                  WHERE m.norm = n.norm AND x.logbook_id IN ({readable})) AS n
                          FROM entities n WHERE n.entry_id = ? GROUP BY n.type, n.norm""", e["id"])
        thread_n = db.scalar("SELECT count(*) FROM entries WHERE logbook_id = ? AND (thread_root = ? OR mid = ?)",
                             e["logbook_id"], e["thread_root"] or e["mid"], e["thread_root"] or e["mid"], default=1)
        tdig = db.get("SELECT text FROM digests WHERE logbook_id = ? AND kind = 'thread' AND start = ?",
                      e["logbook_id"], e["thread_root"] or e["mid"])
        with acting_user(g.user.key):
            sim = search.similar(ai, e["id"], allowed(), 6)
        around = related_around(e)
        return jsonify({
            "id": e["id"], "logbook": lb, "mid": e["mid"], "time": e["time"],
            "analysis": None if not x else {
                "fresh": fresh, "summary": x["summary"], "kind": x["kind"], "tags": db.jl(x["tags_json"]),
                "measurements": db.jl(x["measurements_json"]), "problem": db.jl(x["problem_json"], None),
                "lang": x["lang"], "english": x["english"], "model": x["model"], "error": x["error"]},
            "entities": [{"type": r["type"], "value": r["value"], "norm": r["norm"], "source": r["source"],
                          "entries": r["n"]} for r in ents],
            "runs": [{"name": r["name"], "sample": r["sample"], "info": r["info"], "start": r["start"], "end": r["end"],
                      "interrupted": bool(r["interrupted"]), "params": db.jl(r["params_json"], {})}
                     for r in db.all("SELECT * FROM runs WHERE entry_id = ?", e["id"])],
            "images": [dict(r) for r in db.all("SELECT file, description FROM attachments_ai WHERE entry_id = ? "
                                                "AND description != ''", e["id"])],
            "thread": {"root": e["thread_root"] or e["mid"], "entries": thread_n, "summary": tdig["text"] if tdig else ""},
            "similar": sim, "around": around, "pending": not fresh and ai.chat_enabled})

    def related_around(e) -> dict:
        """The state of related logbooks (AI related) the user may read at the time of the entry:
        the instrument for an experiment entry, the experiments for an instrument entry. The last
        entries before it however old they are (conditions are often stable for days), the next day
        after it, and the last numeric readings."""
        lb = db.scalar("SELECT name FROM logbooks WHERE id = ?", e["logbook_id"])
        ids = [ix.logbooks[n] for n in ai.ctx.related(lb) if ix.logbooks.get(n) in allowed()] if ai.ctx else []
        if not ids or not e["time"]:
            return {"entries": [], "values": []}
        rows = sorted(search.rows_for(insights.state_around(ids, e["time"], before=3 if len(ids) < 3 else 1)),
                      key=lambda r: r["time"])
        names = {v: k for k, v in ix.logbooks.items()}
        values = [{**v, "logbook": names[i]} for i in ids for v in insights.last_values(i, e["time"])]
        return {"entries": rows[:12], "values": values[:12]}

    @app.post("/api/entry/analyze")
    def entry_analyze():
        b = request.get_json(force=True)
        e = entry_row(b.get("logbook", ""), int(b.get("mid") or 0))
        r = db.get(f"SELECT {enrich.ENTRY_COLS} FROM entries e JOIN logbooks l ON l.id = e.logbook_id WHERE e.id = ?", e["id"])
        with acting_user(g.user.key), ai.gate.interactive():
            enrich.enrich_batch(ai, ix, [r])
        return entry_json(b.get("logbook", ""), e["mid"])

    @app.post("/api/thread/summary")
    def thread_summary():
        b = request.get_json(force=True)
        e = entry_row(b.get("logbook", ""), int(b.get("mid") or 0))
        with acting_user(g.user.key):
            return jsonify(digest.thread(ai, e["logbook_id"], e["thread_root"] or e["mid"]))

    # ---- assistant -----------------------------------------------------------------------------

    @app.post("/api/ask")
    def ask():
        b = request.get_json(force=True)
        q = (b.get("question") or "").strip()
        if not q:
            abort(400)
        names = [n for n, i in ix.logbooks.items() if i in allowed()]
        return ndjson(assistant.ask(ai, g.user.key, q, allowed(), scope_args(b), b.get("conversation_id"), names))

    @app.get("/api/conversations")
    def convs():
        return jsonify(assistant.conversations(g.user.key))

    @app.get("/api/conversations/<int:cid>")
    def conv(cid):
        c = assistant.conversation(g.user.key, cid)
        if not c:
            abort(404)
        return jsonify(c)

    @app.delete("/api/conversations/<int:cid>")
    def conv_delete(cid):
        if not db.get("SELECT 1 FROM conversations WHERE id = ? AND user = ?", cid, g.user.key):
            abort(404)
        with db.tx() as c:
            c.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            c.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        return jsonify({"ok": True})

    @app.post("/api/messages/<int:mid>/feedback")
    def feedback(mid):
        b = request.get_json(force=True)
        v = int(b.get("value") or 0)
        db.run("""UPDATE messages SET feedback = ?, comment = ? WHERE id = ? AND conversation_id IN
                  (SELECT id FROM conversations WHERE user = ?)""",
               max(-1, min(1, v)), (b.get("comment") or "")[:2000] or None, mid, g.user.key)
        return jsonify({"ok": True})

    # ---- personal tokens (MCP, scripts) ---------------------------------------------------------

    @app.get("/api/tokens")
    def tokens_list():
        return jsonify([{"id": r["id"], "name": r["name"], "created": r["created"], "last_used": r["last_used"]}
                        for r in db.all("SELECT * FROM tokens WHERE user = ? ORDER BY id", g.user.key)])

    @app.post("/api/tokens")
    def tokens_create():
        if not g.user.login or g.user.login == "api":
            abort(403)                     # a token acts as an ELOG account: log in with yours
        token = "eai_" + secrets.token_urlsafe(32)
        db.run("INSERT INTO tokens (user, login, name, hash, created) VALUES (?,?,?,?,?)", g.user.key, g.user.login,
               (request.get_json(force=True).get("name") or "token")[:80], hashlib.sha256(token.encode()).hexdigest(),
               db.now())
        return jsonify({"token": token})

    @app.delete("/api/tokens/<int:tid>")
    def tokens_delete(tid):
        db.run("DELETE FROM tokens WHERE id = ? AND user = ?", tid, g.user.key)
        auth.cache.clear()
        return jsonify({"ok": True})

    # ---- feedback loop: bad answers become notes for the logbook ---------------------------------

    def can_admin(lb: str) -> bool:
        """Global admins, and admins of the logbook (its "Admin user" list)."""
        if g.user.admin:
            return True
        admins = [a.strip().lower() for a in (ix.cfg.get(lb, "Admin user") if ix.cfg else "").split(",")]
        return bool(g.user.login) and g.user.login.lower() in admins

    def admin_logbooks() -> list[str]:
        return [n for n, i in ix.logbooks.items() if i in allowed() and can_admin(n)]

    def question_of(m):
        q = db.get("SELECT content FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' "
                   "ORDER BY id DESC LIMIT 1", m["conversation_id"], m["id"])
        return q["content"] if q else ""

    @app.get("/api/admin/feedback")
    def admin_feedback():
        """Answers rated bad whose sources are all in logbooks the caller administers (all for global admins)."""
        mine = {n.lower() for n in admin_logbooks()}
        if not mine:
            abort(403)
        out = []
        for m in db.all("""SELECT m.*, c.user FROM messages m JOIN conversations c ON c.id = m.conversation_id
                           WHERE m.role = 'assistant' AND m.feedback = -1 ORDER BY m.id DESC LIMIT 200"""):
            srcs = db.jl(m["sources_json"])
            lbs = sorted({s["logbook"] for s in srcs})
            if not g.user.admin and not (lbs and all(lb.lower() in mine for lb in lbs)):
                continue
            out.append({"id": m["id"], "user": m["user"], "at": m["at"], "question": question_of(m),
                        "answer": m["content"], "comment": m["comment"] or "", "sources": srcs, "logbooks": lbs})
        return jsonify(out)

    @app.post("/api/admin/feedback/<int:mid>/suggest")
    def admin_feedback_suggest(mid):
        mine = admin_logbooks()
        if not mine:
            abort(403)
        m = db.get("SELECT * FROM messages WHERE id = ? AND role = 'assistant'", mid)
        if not m:
            abort(404)
        lbs = [lb for lb in sorted({s["logbook"] for s in db.jl(m["sources_json"])}) if lb in mine]
        msg = (f"Logbooks: {', '.join(lbs or mine)}\nQuestion: {question_of(m)}\n\n"
               f"Answer that was rated bad:\n{m['content']}\n\nUser's comment: {m['comment'] or '(none)'}")
        with acting_user(g.user.key), ai.gate.interactive():
            raw = ai.chat.chat([{"role": "user", "content": msg}], system=prompts.note_system(), json_mode=True,
                               max_tokens=400, task="note")
        try:
            sug = providers.parse_json(raw)
            lb = sug.get("logbook") if sug.get("logbook") in mine else (lbs or mine)[0]
            return jsonify({"logbook": lb, "note": str(sug.get("note") or "")[:1000]})
        except (ValueError, AttributeError):
            return jsonify({"logbook": (lbs or mine)[0], "note": ""})

    @app.get("/api/admin/notes")
    def admin_notes():
        mine = admin_logbooks()
        if not mine:
            abort(403)
        ids = [ix.logbooks[n] for n in mine]
        rows = db.all(f"""SELECT n.*, l.name AS logbook FROM notes n JOIN logbooks l ON l.id = n.logbook_id
                          WHERE n.logbook_id IN ({','.join('?' * len(ids))}) ORDER BY l.name, n.id""", *ids)
        return jsonify({"logbooks": mine,
                        "notes": [{"id": r["id"], "logbook": r["logbook"], "text": r["text"], "by": r["created_by"],
                                   "at": r["at"]} for r in rows],
                        "config": {n: (ix.cfg.sections.get(n.lower(), {}).get("ai context", "") if ix.cfg else "")
                                   for n in mine}})

    @app.post("/api/admin/notes")
    def admin_note_add():
        b = request.get_json(force=True)
        lb, text = b.get("logbook", ""), (b.get("text") or "").strip()[:1000]
        if not text:
            abort(400)
        if lb not in admin_logbooks():
            abort(403)
        db.run("INSERT INTO notes (logbook_id, text, created_by, at, message_id) VALUES (?,?,?,?,?)",
               ix.logbooks[lb], text, g.user.key, db.now(), b.get("message_id"))
        ai.ctx.refresh(force=True)        # new versions: the logbook is analysed again with the note
        return jsonify({"ok": True})

    @app.delete("/api/admin/notes/<int:nid>")
    def admin_note_delete(nid):
        r = db.get("SELECT n.id, l.name FROM notes n JOIN logbooks l ON l.id = n.logbook_id WHERE n.id = ?", nid)
        if not r or r["name"] not in admin_logbooks():
            abort(404)
        db.run("DELETE FROM notes WHERE id = ?", nid)
        ai.ctx.refresh(force=True)
        return jsonify({"ok": True})

    # ---- digests -------------------------------------------------------------------------------

    def day_arg(s: str | None) -> date:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date() if s else date.today()
        except ValueError:
            abort(400)

    @app.get("/api/digests")
    def digests():
        kind = request.args.get("kind") or "week"
        if kind not in digest.KINDS:
            abort(400)
        return jsonify(digest.overview(allowed(), kind, day_arg(request.args.get("date"))))

    @app.post("/api/digest")
    def make_digest():
        b = request.get_json(force=True)
        kind = b.get("kind") or "week"
        if kind not in digest.KINDS:
            abort(400)
        with acting_user(g.user.key), ai.gate.interactive():
            return jsonify(digest.get(ai, logbook_id(b.get("logbook", "")), kind, day_arg(b.get("date")),
                                      generate=bool(b.get("generate", True))))

    # ---- insights ------------------------------------------------------------------------------

    @app.get("/api/insights/problems")
    def ins_problems():
        since = int(request.args.get("since") or 0)
        lbs = scope_args(request.args)["logbooks"]
        return jsonify(insights.problems(logbook_ids(lbs) & allowed() if lbs else allowed(), since))

    @app.get("/api/insights/series")
    def ins_series():
        return jsonify(insights.series(logbook_id(request.args.get("logbook", "")),
                                       int(request.args.get("since") or 0)))

    @app.get("/api/insights/entities")
    def ins_entities():
        a = request.args
        return jsonify({"types": insights.entity_types(allowed()),
                        "items": insights.entities(allowed(), a.get("type", ""), a.get("q", ""))})

    @app.get("/api/insights/entity")
    def ins_entity():
        ids = insights.entity_timeline(allowed(), request.args.get("value", ""))
        return jsonify({"entries": search.rows_for(ids)})

    @app.get("/api/alerts")
    def get_alerts():
        return jsonify(alerts.recent(allowed()))

    @app.get("/api/handovers")
    def get_handovers():
        return jsonify(digest.handovers(allowed(), ix))

    @app.post("/api/handover")
    def make_handover():
        b = request.get_json(force=True)
        with acting_user(g.user.key), ai.gate.interactive():
            return jsonify(digest.handover(ai, logbook_id(b.get("logbook", "")), day_arg(b.get("date"))))

    @app.get("/api/insights/lifecycle")
    def ins_lifecycle():
        return jsonify(insights.lifecycle(allowed(), request.args.get("value", ""), ix.cfg))

    @app.get("/api/insights/runs")
    def ins_runs():
        lbs = scope_args(request.args)["logbooks"]
        if request.args.get("logbooks") and not lbs:
            return jsonify([])               # only logbooks the user cannot read were asked for
        return jsonify(insights.runs(allowed(), request.args.get("q", ""), ctx=ai.ctx, cfg=ix.cfg, logbooks=lbs))

    @app.get("/api/insights/activity")
    def ins_activity():
        return jsonify(insights.activity(allowed()))

    # ---- writing help (edit page) --------------------------------------------------------------

    def options(lb: str) -> dict:
        out = {}
        for a in ix.cfg.attributes(lb) if ix.cfg else []:
            for kind in ("Options", "MOptions", "ROptions", "IOptions"):
                v = ix.cfg.get(lb, f"{kind} {a}")
                if v:
                    out[a] = [x.strip().split("{")[0].strip() for x in v.split(",") if x.strip()]
                    break
        return out

    @app.post("/api/assist/suggest")
    def suggest():
        b = request.get_json(force=True)
        lb = b.get("logbook", "")
        lid = logbook_id(lb)
        from .elogsrc import to_text
        text = to_text(b.get("text") or "", "html")[:6000]
        subject = (b.get("subject") or "").strip()
        out = {"similar": [], "subject": "", "attributes": {}, "missing": []}
        if len(text) + len(subject) < 15:
            return jsonify(out)
        with acting_user(g.user.key):
            res = search.search(ai, f"{subject}\n{text[:1500]}", {}, allowed(), k=5,
                                mode="semantic" if ai.embed_enabled else "keyword")
            out["similar"] = [r for r in res["results"] if (r.get("similarity") or 0) > 0.6][:5]
            if ai.chat_enabled and ai.may_send([lb]) and b.get("ai", True):
                opts = options(lb)
                attrs = {a: opts.get(a, "free text") for a in (ix.cfg.attributes(lb) if ix.cfg else [])
                         if a.lower() not in ("author", "subject")}
                msg = (f"Logbook: {lb}\nAttributes and allowed options: {json.dumps(attrs, ensure_ascii=False)}\n"
                       f"Current values: {json.dumps(b.get('attrs') or {}, ensure_ascii=False)}\n"
                       f"Subject: {subject or '(empty)'}\n\nText:\n{text}")
                with ai.gate.interactive():
                    notes = ai.ctx.notes([lb]) if ai.ctx else ""
                    raw = ai.fast.chat([{"role": "user", "content": msg}], system=prompts.suggest_system(notes),
                                       json_mode=True, max_tokens=500, task="suggest")
                try:
                    s = providers.parse_json(raw)
                    out["subject"] = str(s.get("subject") or "")[:120]
                    out["attributes"] = {k: str(v) for k, v in (s.get("attributes") or {}).items()
                                         if k in attrs and v not in (None, "") and
                                         (attrs[k] == "free text" or str(v) in attrs[k])}
                    out["missing"] = [str(m)[:200] for m in (s.get("missing") or [])][:5]
                except (ValueError, AttributeError):
                    pass
        return jsonify(out)

    @app.post("/api/assist/polish")
    def polish():
        b = request.get_json(force=True)
        lb = b.get("logbook", "")
        logbook_id(lb)
        if not ai.chat_enabled or not ai.may_send([lb]):
            abort(409)
        text = (b.get("text") or "")[:20000]
        with acting_user(g.user.key):
            return ndjson(({"type": "delta", "text": p} for p in ai.chat.chat(
                [{"role": "user", "content": text}], system=prompts.polish_system(ai.language),
                stream=True, max_tokens=3000, task="polish")))

    # ---- administration ------------------------------------------------------------------------

    @app.get("/api/status")
    def status():
        need_admin()
        st = enrich.stats()
        usage = [dict(r) for r in db.all("""SELECT task, model, count(*) AS calls, sum(tokens_in) AS tokens_in,
                                                   sum(tokens_out) AS tokens_out, avg(ms) AS avg_ms, sum(1 - ok) AS failed
                                            FROM usage WHERE at >= ? GROUP BY task, model ORDER BY calls DESC""",
                                         db.now() - 30 * 86400)]
        size = os.path.getsize(os.path.join(conf["state_dir"], "elog-ai.sqlite"))
        return jsonify({"index": st, "worker": worker.state, "providers": ai.health or ai.check(),
                        "usage_30d": usage, "db_bytes": size,
                        "config": {"provider": conf["chat"]["provider"], "chat_model": ai.chat.model,
                                   "fast_model": ai.fast.model, "embed_provider": conf["embed"]["provider"],
                                   "embed_model": ai.embed_model, "vision_model": conf["chat"]["vision_model"],
                                   "enrich": conf["enrich"], "enrich_since": conf["enrich_since"],
                                   "private_logbooks": conf["private_logbooks"], "elogd": auth.elogd()}})

    @app.post("/api/admin/check")
    def admin_check():
        need_admin()
        return jsonify(ai.check())

    @app.post("/api/admin/redo")
    def admin_redo():
        """Discard stored results so they are computed again (after a model or prompt change)."""
        need_admin()
        b = request.get_json(force=True)
        what = b.get("what")
        lbs = b.get("logbooks") or []
        cond, args = ("", [])
        if lbs:
            ids = logbook_ids(lbs)
            cond = f" WHERE entry_id IN (SELECT id FROM entries WHERE logbook_id IN ({','.join('?' * len(ids))}))"
            args = sorted(ids)
        if what == "enrich":
            db.run("DELETE FROM enrich" + cond, *args)
            db.run("DELETE FROM entities WHERE source = 'ai'" + cond.replace(" WHERE", " AND"), *args)
        elif what == "embed":
            db.run("DELETE FROM chunks" + cond, *args)
        elif what == "digests":
            db.run("DELETE FROM digests")
        elif what == "images":
            db.run("DELETE FROM attachments_ai" + cond, *args)
        else:
            abort(400)
        worker.poke()
        return jsonify({"ok": True})

    @app.post("/api/admin/rescan")
    def admin_rescan():
        need_admin()
        ix.load_config(force=True)
        if request.get_json(force=True).get("full"):
            db.run("DELETE FROM files")
        worker.poke()
        return jsonify({"ok": True})

    return app


def main(argv=None):
    p = argparse.ArgumentParser(prog="elogai", description="AI module for ELOG")
    p.add_argument("--env-file", help="read settings from this .env file")
    p.add_argument("--no-worker", action="store_true", help="serve only, no background analysis")
    p.add_argument("--scan", action="store_true", help="index the logbooks once and exit")
    p.add_argument("--eval", metavar="QUESTIONS.jsonl", help="run the evaluation questions and exit")
    p.add_argument("--eval-no-answer", action="store_true", help="evaluate retrieval only (no model answers)")
    p.add_argument("--eval-out", metavar="REPORT.json", default="", help="write the full evaluation report")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.env_file:
        settings.load_env_file(args.env_file)
    elif os.path.exists(".env"):
        settings.load_env_file(".env")
    try:
        conf = settings.from_env()
        if not os.path.isfile(conf["elogd_cfg"]):
            raise settings.SetupError(f"elogd.cfg not found at {conf['elogd_cfg']}. Set ELOG_DIR or ELOGD_CFG.")
    except settings.SetupError as e:
        print(f"elog-ai: {e}", file=sys.stderr)
        sys.exit(2)
    if args.scan:
        db.init(conf["state_dir"])
        ix = Indexer(conf["elogd_cfg"])
        print(ix.scan())
        return
    if args.eval:
        from . import evaluate
        app = create_app(conf, start_worker=False)
        x = app.extensions["elogai"]
        x["ix"].scan()
        evaluate.print_report(evaluate.run(x["ai"], x["ix"], args.eval, not args.eval_no_answer, args.eval_out))
        return
    try:
        app = create_app(conf, start_worker=not args.no_worker)
    except PermissionError as e:
        print(f"elog-ai: {e}", file=sys.stderr)
        sys.exit(2)
    from waitress import serve
    log.info("listening on %s:%d, elogd.cfg %s, state %s", conf["host"], conf["port"], conf["elogd_cfg"],
             conf["state_dir"])
    serve(app, **serve_options(conf))


def serve_options(conf: dict) -> dict:
    # waitress drops X-Forwarded-For by default; auth.py needs it to check ELOG sessions, which
    # elogd binds to the browser's address. Only nginx reaches us (we listen on 127.0.0.1).
    return {"host": conf["host"], "port": conf["port"], "threads": 12, "channel_timeout": 600,
            "clear_untrusted_proxy_headers": False}
