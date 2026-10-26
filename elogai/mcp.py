"""MCP server (Model Context Protocol, streamable HTTP with JSON responses) at /mcp, so Claude
Desktop, Claude Code and other assistants can search and ask the logbooks as their user:

    claude mcp add --transport http elog https://elog.example.org/ai/mcp \
        --header "Authorization: Bearer eai_..."

The token is created on the workspace's "Connect" page and acts as its owner; every tool sees
only the logbooks that user may read."""
from __future__ import annotations

import json
from datetime import date, datetime

from . import assistant, db, digest, insights, search

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "list_logbooks", "description": "Logbooks you may read, with their number of entries and last entry.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search", "description": "Search logbook entries by words and meaning (hybrid). Returns entries with "
                                      "logbook, id, date, author, subject, attributes, snippet and AI summary.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "logbooks": {"type": "array", "items": {"type": "string"}},
         "since": {"type": "string", "description": "YYYY-MM-DD"}, "until": {"type": "string", "description": "YYYY-MM-DD"},
         "limit": {"type": "integer", "default": 15}}, "required": ["query"]}},
    {"name": "get_entry", "description": "One entry in full: attributes, text, attachments, AI analysis, and the state "
                                         "of related logbooks (e.g. the instrument) at that time.",
     "inputSchema": {"type": "object", "properties": {"logbook": {"type": "string"}, "id": {"type": "integer"}},
                     "required": ["logbook", "id"]}},
    {"name": "ask", "description": "Ask the logbook assistant; the answer cites entries as [Logbook #id].",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"},
                                                      "logbooks": {"type": "array", "items": {"type": "string"}}},
                     "required": ["question"]}},
    {"name": "summary", "description": "AI summary of a logbook for a day, week or month.",
     "inputSchema": {"type": "object", "properties": {
         "logbook": {"type": "string"}, "period": {"type": "string", "enum": ["day", "week", "month"]},
         "date": {"type": "string", "description": "YYYY-MM-DD in the period, default today"}},
         "required": ["logbook"]}},
    {"name": "item_history", "description": "Exact history of a sample, tip or serial number (positions, time on the "
                                            "measuring stage) from change notes and attributes.",
     "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]}},
    {"name": "runs", "description": "DAQ runs. Query: words and conditions separated by commas, "
                                    "e.g. 'TaS2, Legend OUT > 2.5'.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(msg: dict, user, ai, ix) -> dict | None:
    rid = msg.get("id")
    method = msg.get("method", "")
    if rid is None:                      # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        v = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return _ok(rid, {"protocolVersion": v, "capabilities": {"tools": {}},
                         "serverInfo": {"name": "elog-ai", "version": "1.0"},
                         "instructions": "Laboratory electronic logbooks (ELOG). Cite entries as [Logbook #id]; "
                                         "use get_entry for full texts and item_history for samples and tips."})
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            text = call(p.get("name", ""), p.get("arguments") or {}, user, ai, ix)
            return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except LookupError as e:
            return _ok(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
    return _err(rid, -32601, f"method not found: {method}")


def _lb(name: str, user, ix) -> int:
    lid = next((i for n, i in ix.logbooks.items() if n.lower() == (name or "").lower()), None)
    if lid is None or lid not in user.allowed:
        raise LookupError(f"no readable logbook '{name}'")
    return lid


def _rows(rows) -> str:
    return json.dumps([{k: r.get(k) for k in ("logbook", "mid", "date", "author", "subject", "attrs", "summary",
                                              "kind", "snippet") if r.get(k)} for r in rows], ensure_ascii=False, indent=1)


def call(name: str, a: dict, user, ai, ix) -> str:
    allowed = user.allowed
    names = [n for n, i in ix.logbooks.items() if i in allowed]
    lbs = [n for n in (a.get("logbooks") or []) if n in names]
    if name == "list_logbooks":
        rows = db.all(f"""SELECT l.name, count(e.id) AS n, max(e.time) AS last FROM logbooks l
                          LEFT JOIN entries e ON e.logbook_id = l.id WHERE l.id IN ({','.join('?' * len(allowed)) or 'NULL'})
                          GROUP BY l.id ORDER BY l.name""", *sorted(allowed))
        return "\n".join(f"{r['name']}: {r['n']} entries, last {datetime.fromtimestamp(r['last']):%Y-%m-%d}"
                         if r["last"] else f"{r['name']}: empty" for r in rows)
    if name == "search":
        f = {"logbooks": lbs, "since": a.get("since") or "", "until": a.get("until") or ""}
        return _rows(search.search(ai, a.get("query", ""), f, allowed, k=min(int(a.get("limit") or 15), 50))["results"])
    if name == "get_entry":
        lid = _lb(a.get("logbook"), user, ix)
        e = db.get("SELECT * FROM entries WHERE logbook_id = ? AND mid = ?", lid, int(a.get("id") or 0))
        if not e:
            raise LookupError("no such entry")
        x = db.get("SELECT summary, kind, tags_json, problem_json, english FROM enrich WHERE entry_id = ?", e["id"])
        lb = next(n for n, i in ix.logbooks.items() if i == lid)
        rel = [ix.logbooks[n] for n in (ai.ctx.related(lb) if ai.ctx else []) if ix.logbooks.get(n) in allowed]
        around = search.rows_for(insights.state_around(rel, e["time"], before=2, after=1)) if rel else []
        out = {"logbook": lb, "id": e["mid"], "date": e["date"], "author": e["author"], "subject": e["subject"],
               "attributes": db.jl(e["attrs_json"], {}), "text": e["text"], "attachments": db.jl(e["attachments_json"]),
               "in_reply_to": e["in_reply_to"] or None,
               "ai": dict(x) if x else None,
               "related_logbooks_at_that_time": [{"logbook": r["logbook"], "id": r["mid"], "date": r["date"],
                                                  "subject": r["subject"], "summary": r["summary"]} for r in around]}
        return json.dumps(out, ensure_ascii=False, indent=1)
    if name == "ask":
        text, sources = [], []
        for ev in assistant.ask(ai, user.key, a.get("question", ""), allowed, {"logbooks": lbs}, None, names):
            if ev["type"] == "delta":
                text.append(ev["text"])
            elif ev["type"] == "sources":
                sources = ev["sources"]
            elif ev["type"] == "error":
                raise LookupError(ev["error"])
        return "".join(text) + "\n\nSources: " + ", ".join(f"[{s['logbook']} #{s['mid']}]" for s in sources)
    if name == "summary":
        lid = _lb(a.get("logbook"), user, ix)
        kind = a.get("period") if a.get("period") in digest.KINDS else "week"
        day = datetime.strptime(a["date"], "%Y-%m-%d").date() if a.get("date") else date.today()
        d = digest.get(ai, lid, kind, day)
        return d["text"] or f"No entries in this {kind}."
    if name == "item_history":
        lc = insights.lifecycle(allowed, a.get("item", ""), ix.cfg)
        if not lc["entries"]:
            raise LookupError("no entries name this item")
        return insights.lifecycle_text(lc) if lc["periods"] else \
            f"{lc['entries']} entries name {lc['value']}; no recorded changes of position or state."
    if name == "runs":
        return json.dumps([{k: r[k] for k in ("logbook", "mid", "name", "sample", "info", "start", "end",
                                              "interrupted", "conditions")}
                           for r in insights.runs(allowed, a.get("query", ""), 50, ctx=ai.ctx)], ensure_ascii=False, indent=1)
    raise LookupError(f"unknown tool {name}")
