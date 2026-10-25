"""Questions about the logbooks, answered from retrieved entries with citations."""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

from . import db, prompts, providers, search
from .ai import AI
from .indexer import norm

CONTEXT_CHARS = int(os.environ.get("AI_CONTEXT_CHARS") or 14000)
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def time_range(q: str, today: date | None = None) -> tuple[str | None, str | None]:
    """Dates a question limits itself to: "yesterday", "last week", "in March 2025", "last 3 months" ..."""
    t = today or date.today()
    s = q.lower()
    iso = lambda d: d.isoformat()
    if re.search(r"\btoday\b|\bdanes\b", s):
        return iso(t), iso(t)
    if re.search(r"\byesterday\b|\bvčeraj\b", s):
        return iso(t - timedelta(1)), iso(t - timedelta(1))
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?\b", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 31, "year": 366}[u] * n
        return iso(t - timedelta(days)), None
    if re.search(r"\bthis week\b", s):
        return iso(t - timedelta(t.weekday())), None
    if re.search(r"\blast week\b|\bprevious week\b", s):
        start = t - timedelta(t.weekday() + 7)
        return iso(start), iso(start + timedelta(6))
    if re.search(r"\bthis month\b", s):
        return iso(t.replace(day=1)), None
    if re.search(r"\blast month\b|\bprevious month\b", s):
        end = t.replace(day=1) - timedelta(1)
        return iso(end.replace(day=1)), iso(end)
    if re.search(r"\bthis year\b", s):
        return iso(t.replace(month=1, day=1)), None
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(20\d\d)\b", s)
    if m:
        mo, y = MONTHS[m.group(1)], int(m.group(2))
        return iso(date(y, mo, 1)), iso(date(y, mo, calendar.monthrange(y, mo)[1]))
    m = re.search(r"\b(?:since|after|from)\s+(20\d\d-\d\d-\d\d)\b", s)
    if m:
        return m.group(1), None
    m = re.search(r"\bin\s+(20\d\d)\b", s)
    if m:
        y = int(m.group(1))
        return iso(date(y, 1, 1)), iso(date(y, 12, 31))
    return None, None


def mentioned_logbooks(q: str, names: list[str]) -> list[str]:
    s = q.lower()
    return [n for n in names if re.search(r"(?<![\w-])" + re.escape(n.lower()) + r"(?![\w-])", s)]


def ref(row) -> str:
    return f"[{row['logbook']} #{row['mid']}]"


def _context(rows: list[dict], budget: int) -> tuple[str, list[dict]]:
    parts, used, size = [], [], 0
    for r in rows:
        attrs = "; ".join(f"{k}: {v}" for k, v in r["attrs"].items() if v and k != "Author")
        text = r.get("full_text") or r["snippet"]
        block = f"{ref(r)} {r['date']} by {r['author']}\n{attrs}\nSubject: {r['subject']}\n{text}".strip()
        if r.get("images"):
            block += "\nImages: " + " / ".join(r["images"])
        if size + len(block) > budget:
            block = block[:max(0, budget - size)]
            if len(block) < 200:
                break
        parts.append(block)
        used.append(r)
        size += len(block)
    return "\n\n---\n\n".join(parts), used


def retrieve(ai: AI, question: str, allowed: set[int], scope: dict, logbook_names: list[str],
             history: list[dict]) -> tuple[list[dict], dict]:
    f = {k: v for k, v in scope.items() if v}
    since, until = time_range(question)
    if since and not f.get("since"):
        f["since"] = since
    if until and not f.get("until"):
        f["until"] = until
    if not f.get("logbooks"):
        lbs = mentioned_logbooks(question, logbook_names)
        if lbs:
            f["logbooks"] = lbs
    query = question
    if history and ai.chat_enabled:
        # follow-up questions ("and before that?") need the conversation to be searchable
        try:
            hist = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-4:])
            out = providers.parse_json(ai.fast.chat(
                [{"role": "user", "content": f"Logbooks: {', '.join(logbook_names)}\n\nConversation:\n{hist}\n\n"
                                             f"Last question: {question}"}],
                system=prompts.rewrite_system(), json_mode=True, max_tokens=300, temperature=0, task="rewrite"))
            query = (out.get("query") or question) + " " + " ".join(out.get("keywords") or [])
            for k in ("since", "until"):
                if out.get(k) and not f.get(k) and re.match(r"^\d{4}-\d\d-\d\d$", str(out[k])):
                    f[k] = out[k]
            lbs = [n for n in out.get("logbooks") or [] if n in logbook_names]
            if lbs and not f.get("logbooks"):
                f["logbooks"] = lbs
        except (providers.ProviderError, ValueError, AttributeError):
            pass
    res = search.search(ai, query, f, allowed, k=14)["results"]
    if len(res) < 3 and (f.get("since") or f.get("logbooks")) and not scope.get("since"):
        # the guessed limits may be wrong: widen
        res += [r for r in search.search(ai, query, {}, allowed, k=10)["results"] if r["id"] not in {x["id"] for x in res}]
    if not res and (f.get("since") or f.get("logbooks")):
        res = search._recent(f, allowed, 14)
    ids = [r["id"] for r in res]
    if ids:
        full = {r[0]: r[1] for r in db.all(f"SELECT id, text FROM entries WHERE id IN ({','.join('?' * len(ids))})", *ids)}
        imgs = {}
        for r in db.all(f"SELECT entry_id, description FROM attachments_ai WHERE description != '' AND entry_id IN "
                        f"({','.join('?' * len(ids))})", *ids):
            imgs.setdefault(r[0], []).append(r[1])
        for r in res:
            r["full_text"] = (full.get(r["id"]) or "")[:3000]
            r["images"] = imgs.get(r["id"], [])[:3]
    return res, f


def runs_context(question: str, allowed: set[int]) -> str:
    """Runs of samples or run numbers the question names (DAQ run descriptions)."""
    words = [w for w in re.findall(r"[\w\-.]+", question) if len(w) >= 3]
    if not words or not allowed:
        return ""
    norms = {norm(w) for w in words}
    rows = db.all(f"""SELECT r.name, r.sample, r.info, r.start, r.end, r.interrupted, l.name AS logbook, e.mid
                      FROM runs r JOIN entries e ON e.id = r.entry_id JOIN logbooks l ON l.id = e.logbook_id
                      WHERE e.logbook_id IN ({','.join('?' * len(allowed))})
                      ORDER BY e.time DESC LIMIT 2000""", *sorted(allowed))
    hits = [r for r in rows if norm(r["sample"] or "") in norms or norm(r["name"] or "") in norms][:15]
    if not hits:
        return ""
    lines = [f"[{r['logbook']} #{r['mid']}] run {r['name']}: sample {r['sample']}; {r['info']}; "
             f"{r['start']} → {r['end']}{' (interrupted)' if r['interrupted'] else ''}" for r in hits]
    return "DAQ runs matching the question:\n" + "\n".join(lines)


def ask(ai: AI, user: str, question: str, allowed: set[int], scope: dict, conversation_id: int | None,
        logbook_names: list[str]):
    """Generator of NDJSON-able events: conversation, sources, delta*, done | error."""
    question = question.strip()[:4000]
    now = db.now()
    if conversation_id:
        conv = db.get("SELECT * FROM conversations WHERE id = ? AND user = ?", conversation_id, user)
        if not conv:
            conversation_id = None
    if not conversation_id:
        conversation_id = db.run("INSERT INTO conversations (user, title, scope_json, created, updated) "
                                 "VALUES (?,?,?,?,?)", user, question[:80], json.dumps(scope), now, now).lastrowid
    history = [dict(r) for r in db.all("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                                       conversation_id)]
    db.run("INSERT INTO messages (conversation_id, role, content, at) VALUES (?, 'user', ?, ?)",
           conversation_id, question, now)
    yield {"type": "conversation", "id": conversation_id}

    rows, used_filters = retrieve(ai, question, allowed, scope, logbook_names, history)
    readable = [r for r in rows if ai.may_send([r["logbook"]])]
    withheld = len(rows) - len(readable)
    ctx, used = _context(readable, CONTEXT_CHARS)
    extra = runs_context(question, allowed)
    sources = [{k: r[k] for k in ("id", "mid", "logbook", "date", "time", "author", "subject", "summary", "kind")}
               for r in used]
    yield {"type": "sources", "sources": sources, "filters": used_filters, "withheld": withheld}

    if not ai.chat_enabled:
        yield {"type": "delta", "text": "No language model is configured; these are the most relevant entries."}
        yield {"type": "done"}
        return
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    note = f"\n(Filters applied: {json.dumps(used_filters)})" if used_filters else ""
    if withheld:
        note += f"\n({withheld} matching entries are in private logbooks and were not shown to you.)"
    msgs.append({"role": "user", "content": f"Logbook entries:\n\n{ctx or '(no matching entries)'}"
                                            f"{chr(10) * 2 + extra if extra else ''}{note}\n\nQuestion: {question}"})
    answer = []
    try:
        with ai.gate.interactive():
            for piece in ai.chat.chat(msgs, system=prompts.answer_system(ai.language, date.today().isoformat()),
                                      stream=True, max_tokens=1500, task="ask"):
                answer.append(piece)
                yield {"type": "delta", "text": piece}
    except providers.ProviderError as e:
        yield {"type": "error", "error": str(e)}
    text = "".join(answer).strip()
    mid = db.run("INSERT INTO messages (conversation_id, role, content, sources_json, at) VALUES (?, 'assistant', ?, ?, ?)",
                 conversation_id, text, json.dumps(sources, ensure_ascii=False), db.now()).lastrowid
    db.run("UPDATE conversations SET updated = ? WHERE id = ?", db.now(), conversation_id)
    yield {"type": "done", "message_id": mid}


def conversations(user: str, limit: int = 50) -> list[dict]:
    return [dict(r) for r in db.all("SELECT id, title, created, updated FROM conversations WHERE user = ? "
                                    "ORDER BY updated DESC LIMIT ?", user, limit)]


def conversation(user: str, cid: int) -> dict | None:
    c = db.get("SELECT * FROM conversations WHERE id = ? AND user = ?", cid, user)
    if not c:
        return None
    msgs = [{"id": m["id"], "role": m["role"], "content": m["content"], "sources": db.jl(m["sources_json"]),
             "feedback": m["feedback"], "at": m["at"]}
            for m in db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", cid)]
    return {"id": c["id"], "title": c["title"], "scope": db.jl(c["scope_json"], {}), "messages": msgs}
