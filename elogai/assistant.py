"""Questions about the logbooks, answered from retrieved entries with citations."""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

from . import db, prompts, providers, search
from .rerank import rerank
from .ai import AI
from .indexer import norm

CONTEXT_CHARS = int(os.environ.get("AI_CONTEXT_CHARS") or 30000)
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
    """Logbooks a question names, also by a first word only one logbook starts with ("Carbide")."""
    s = q.lower()
    out = [n for n in names if re.search(r"(?<![\w-])" + re.escape(n.lower()) + r"(?![\w-])", s)]
    first: dict[str, list[str]] = {}
    for n in names:
        w = re.split(r"[\s_-]+", n.lower())[0]
        if len(w) >= 4 and w != n.lower():
            first.setdefault(w, []).append(n)
    for w, ns in first.items():
        if len(ns) == 1 and ns[0] not in out and re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", s):
            out.append(ns[0])
    return out


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
        if r.get("related_to"):
            block = f"(related logbook, around the time of {r['related_to']})\n" + block
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
    focus = []                 # logbooks the question names: searched in addition, not as a filter
    if not f.get("logbooks"):
        lbs = mentioned_logbooks(question, logbook_names)
        if lbs:
            # a question about np005 is also about the instrument it ran on, and vice versa
            rel = [r for lb in lbs for r in (ai.ctx.related(lb) if ai.ctx else []) if r in logbook_names]
            focus = list(dict.fromkeys(lbs + rel))
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
    res = search.search(ai, query, f, allowed, k=30)["results"]
    if focus:
        # "Legend compared to Pharos" may be answered in an experiment logbook: interleave the
        # results within the named logbooks with the general ones and let the reranker choose
        named = search.search(ai, query, {**f, "logbooks": focus}, allowed, k=15)["results"]
        seen, merged = set(), []
        for pair in zip(named + [None] * len(res), res + [None] * len(named)):
            for r in pair:
                if r and r["id"] not in seen:
                    seen.add(r["id"])
                    merged.append(r)
        res = merged[:30]
        f["focus"] = focus
    if len(res) < 3 and (f.get("since") or f.get("logbooks")) and not scope.get("since"):
        # the guessed limits may be wrong: widen
        res += [r for r in search.search(ai, query, {}, allowed, k=10)["results"] if r["id"] not in {x["id"] for x in res}]
    if not res and (f.get("since") or f.get("logbooks")):
        res = search._recent(f, allowed, 14)
    _load_full(res)
    res = rerank(ai, query, res, 14)
    # unless the user limited the question to one logbook: what the related logbooks (the instrument,
    # the experiments on it) say around the time of the best results
    if len(scope.get("logbooks") or []) != 1:
        comp = companions(ai, query, res, allowed)
        _load_full(comp)
        res += comp
    return res, f


def _load_full(rows: list[dict]) -> None:
    """Full texts and image descriptions for the prompt."""
    ids = [r["id"] for r in rows]
    if not ids:
        return
    full = {r[0]: r[1] for r in db.all(f"SELECT id, text FROM entries WHERE id IN ({','.join('?' * len(ids))})", *ids)}
    imgs = {}
    for r in db.all(f"SELECT entry_id, description FROM attachments_ai WHERE description != '' AND entry_id IN "
                    f"({','.join('?' * len(ids))})", *ids):
        imgs.setdefault(r[0], []).append(r[1])
    for r in rows:
        r["full_text"] = (full.get(r["id"]) or "")[:3000]
        r["images"] = imgs.get(r["id"], [])[:3]


def companions(ai: AI, query: str, res: list[dict], allowed: set[int], top: int = 5, limit: int = 8) -> list[dict]:
    """Entries of related logbooks the user may read, for each of the best results: the state of
    the instrument during an experiment, or the experiment running while something happened on the
    instrument. The last entries before the result however old (conditions are often stable for
    days) and entries matching the question from the two weeks before to a day after."""
    if not ai.ctx or not res:
        return []
    from .auth import logbook_ids
    from .insights import state_around
    seen = {r["id"] for r in res}
    out = []
    for r in res[:top]:
        names = [n for n in ai.ctx.related(r["logbook"]) if ai.may_send([n]) and logbook_ids([n]) & allowed]
        if not names or not r["time"]:
            continue
        f = {"logbooks": names, "since": r["time"] - 14 * 86400, "until": r["time"]}   # until: + 1 day
        hits = search.search(ai, query, f, allowed, k=2)["results"]
        hits += search.rows_for(state_around(logbook_ids(names) & allowed, r["time"], before=2, after=1))
        for h in hits:
            if h["id"] not in seen:
                seen.add(h["id"])
                h["related_to"] = f"[{r['logbook']} #{r['mid']}]"
                out.append(h)
        if len(out) >= limit:
            break
    return out[:limit]


def runs_context(question: str, allowed: set[int], ai: AI | None = None) -> str:
    """Runs the question names by number, sample or file (all entries of each run together)."""
    from .insights import runs
    words = [w for w in re.findall(r"[\w\-.]+", question) if len(w) >= 3 or w.isdigit()]
    if not words or not allowed:
        return ""
    norms = {norm(w) for w in words}
    nums = {str(int(w)) for w in words if w.isdigit()}
    cfg = ai.ctx.ix.cfg if ai and ai.ctx else None
    hits = [g for g in runs(allowed, "", 5000, ctx=None, cfg=cfg, with_conditions=False)
            if g["run"] in nums or norm(g["run"]) in norms or (g["sample"] and norm(g["sample"]) in norms)
            or any(norm(f) in norms for f in g["files"])][:12]
    if not hits:
        return ""
    lines = []
    for g in hits:
        when = datetime.fromtimestamp(g["first"]).strftime("%Y-%m-%d %H:%M")
        ents = "; ".join(f"[{g['logbook']} #{e['mid']}] {e['subject'] or e['summary'] or ''}".strip() for e in g["entries"][:8])
        lines.append(f"run {g['run']} in {g['logbook']}: sample {g['sample'] or '?'}; {g['info']}; from {when}, "
                     f"{g['duration_h']} h{', ' + g['status'] if g['status'] else ''}. Entries: {ents}")
    return "Runs matching the question (all entries of each run):\n" + "\n".join(lines)


def lifecycle_context(question: str, allowed: set[int], cfg) -> str:
    """Exact histories (positions, time on the stage ...) of items the question names."""
    from .insights import lifecycle, lifecycle_text
    words = {norm(w) for w in re.findall(r"[\w\-.]+", question) if len(w) >= 3 and re.search(r"\d", w)}
    if not words or not allowed:
        return ""
    known = [r[0] for r in db.all(f"""SELECT DISTINCT n.value FROM entities n JOIN entries e ON e.id = n.entry_id
                                      WHERE n.source = 'attr' AND n.norm IN ({','.join('?' * len(words))})
                                      AND e.logbook_id IN ({','.join('?' * len(allowed))}) LIMIT 3""",
                                  *words, *sorted(allowed))]
    texts = [lifecycle_text(lc) for lc in (lifecycle(allowed, v, cfg) for v in known) if lc["periods"]]
    return "\n\n".join(texts)


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
    extra = "\n\n".join(x for x in (runs_context(question, allowed, ai),
                                     lifecycle_context(question, allowed, ai.ctx.ix.cfg if ai.ctx else None)) if x)
    sources = [{**{k: r[k] for k in ("id", "mid", "logbook", "date", "time", "author", "subject", "summary", "kind")},
                "related": bool(r.get("related_to"))} for r in used]
    involved = list(dict.fromkeys(r["logbook"] for r in used)) or (used_filters.get("logbooks") or [])
    notes = ai.ctx.notes(involved) + ai.ctx.relations_note(involved) if ai.ctx else ""
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
            for piece in ai.chat.chat(msgs, system=prompts.answer_system(ai.language, date.today().isoformat(), notes),
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
