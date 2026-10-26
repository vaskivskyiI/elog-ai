"""Summaries of periods (day, week, month) per logbook and of threads, cached by input hash:
asking again costs nothing until an entry of the period changes."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

from . import db, prompts, providers
from .ai import AI

KINDS = ("day", "week", "month")


def bounds(kind: str, day: date) -> tuple[int, int]:
    if kind == "day":
        start = day
        end = day + timedelta(1)
    elif kind == "week":
        start = day - timedelta(day.weekday())
        end = start + timedelta(7)
    elif kind == "month":
        start = day.replace(day=1)
        end = (start + timedelta(32)).replace(day=1)
    else:
        raise ValueError(kind)
    ts = lambda d: int(datetime(d.year, d.month, d.day).timestamp())
    return ts(start), ts(end)


def _lines(rows) -> list[str]:
    out = []
    for r in rows:
        attrs = "; ".join(f"{k}: {v}" for k, v in db.jl(r["attrs_json"], {}).items() if v and k != "Author")
        body = r["summary"] if r["summary"] and len(r["text"] or "") > 400 else (r["text"] or "")[:400]
        when = datetime.fromtimestamp(r["time"]).strftime("%a %d.%m. %H:%M")
        out.append(f"[{r['logbook']} #{r['mid']}] {when} {r['author']} ({attrs}) {r['subject'] or ''}: {body}".strip())
    return out


def _entries(lb_id: int, start: int, end: int):
    return db.all("""SELECT e.id, e.mid, e.time, e.author, e.subject, e.attrs_json, e.text, l.name AS logbook,
                            x.summary, x.kind
                     FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                     LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                     WHERE e.logbook_id = ? AND e.time >= ? AND e.time < ? ORDER BY e.time""", lb_id, start, end)


def get(ai: AI, lb_id: int, kind: str, day: date, generate: bool = True, background: bool = False) -> dict | None:
    start, end = bounds(kind, day)
    rows = _entries(lb_id, start, end)
    lbname = db.scalar("SELECT name FROM logbooks WHERE id = ?", lb_id)
    base = {"logbook": lbname, "kind": kind, "start": start, "end": end, "entries": len(rows)}
    if not rows:
        return {**base, "text": "", "cached": True}
    lines = _lines(rows)
    if len(lines) > 150:       # very busy periods: automatic entries are summarised by count
        human = [l for l, r in zip(lines, rows) if (r["kind"] or "") != "routine"] or lines
        lines = human[:150] + [f"(+{len(rows) - min(150, len(human))} further routine entries)"]
    # the state of related logbooks in the same period (e.g. the instrument for an experiment);
    # a digest is shown to every reader of this logbook, so only logbooks they can all read
    related = [r for r in (ai.ctx.shared_related(lbname) if ai.ctx else []) if ai.may_send([r])]
    rel_lines = []
    for rel in related:
        rid = db.scalar("SELECT id FROM logbooks WHERE name = ?", rel)
        rel_lines += _lines([r for r in _entries(rid, start, end) if (r["kind"] or "") != "routine"][-40:])
    if rel_lines:
        lines = lines + ["", "Related logbooks (context, do not summarise them):"] + rel_lines[-80:]
    notes = ai.ctx.notes([lbname], related) + ai.ctx.relations_note([lbname]) if ai.ctx else ""
    h = hashlib.sha1(("\n".join(lines) + notes + prompts.VERSION + ai.language).encode()).hexdigest()
    cached = db.get("SELECT * FROM digests WHERE logbook_id = ? AND kind = ? AND start = ?", lb_id, kind, start)
    if cached and (cached["hash"] == h or not generate):
        return {**base, "text": cached["text"], "model": cached["model"], "at": cached["at"], "cached": True,
                "stale": cached["hash"] != h}
    if not generate or not ai.chat_enabled or not ai.may_send([lbname]):
        return {**base, "text": "", "cached": False}
    msgs = [{"role": "user", "content": f"Logbook {lbname}, {kind} starting "
                                        f"{datetime.fromtimestamp(start):%Y-%m-%d}:\n\n" + "\n".join(lines)}]
    model = ai.fast if background else ai.chat
    text = model.chat(msgs, system=prompts.digest_system(ai.language, notes), max_tokens=900, task=f"digest-{kind}")
    db.run("""INSERT INTO digests (logbook_id, kind, start, end, hash, model, text, entry_ids_json, at)
              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (logbook_id, kind, start) DO UPDATE SET
              hash = excluded.hash, model = excluded.model, text = excluded.text,
              entry_ids_json = excluded.entry_ids_json, at = excluded.at""",
           lb_id, kind, start, end, h, model.model, text, json.dumps([r["id"] for r in rows]), db.now())
    return {**base, "text": text, "model": model.model, "at": db.now(), "cached": False}


def overview(allowed: set[int], kind: str, day: date) -> list[dict]:
    """Logbooks with entries in the period, with their cached digest if there is one."""
    start, end = bounds(kind, day)
    if not allowed:
        return []
    rows = db.all(f"""SELECT l.id, l.name, count(*) AS n, max(e.time) AS last
                      FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      WHERE e.time >= ? AND e.time < ? AND l.active = 1 AND l.id IN ({','.join('?' * len(allowed))})
                      GROUP BY l.id ORDER BY n DESC""", start, end, *sorted(allowed))
    out = []
    for r in rows:
        d = db.get("SELECT text, at, model FROM digests WHERE logbook_id = ? AND kind = ? AND start = ?", r["id"], kind, start)
        out.append({"logbook": r["name"], "entries": r["n"], "last": r["last"], "start": start, "end": end,
                    "text": d["text"] if d else "", "at": d["at"] if d else None})
    return out


def thread(ai: AI, lb_id: int, root_mid: int, background: bool = False) -> dict:
    rows = db.all("""SELECT e.id, e.mid, e.time, e.author, e.subject, e.attrs_json, e.text, l.name AS logbook,
                            x.summary, x.kind
                     FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                     LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                     WHERE e.logbook_id = ? AND (e.thread_root = ? OR e.mid = ?) ORDER BY e.time""", lb_id, root_mid, root_mid)
    if len(rows) < 2:
        return {"entries": len(rows), "text": ""}
    lines = _lines(rows)
    notes = ai.ctx.notes([rows[0]["logbook"]]) if ai.ctx else ""
    h = hashlib.sha1(("\n".join(lines) + notes + prompts.VERSION + ai.language).encode()).hexdigest()
    cached = db.get("SELECT * FROM digests WHERE logbook_id = ? AND kind = 'thread' AND start = ?", lb_id, root_mid)
    if cached and cached["hash"] == h:
        return {"entries": len(rows), "text": cached["text"], "cached": True}
    model = ai.fast if background else ai.chat
    if not ai.chat_enabled or not ai.may_send([rows[0]["logbook"]], model):
        return {"entries": len(rows), "text": ""}
    msgs = [{"role": "user", "content": "\n".join(lines)}]
    if background:
        text = model.chat(msgs, system=prompts.thread_system(ai.language, notes), max_tokens=600, task="thread-bg")
    else:
        with ai.gate.interactive():
            text = model.chat(msgs, system=prompts.thread_system(ai.language, notes), max_tokens=600, task="thread")
    db.run("""INSERT INTO digests (logbook_id, kind, start, end, hash, model, text, entry_ids_json, at)
              VALUES (?, 'thread', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (logbook_id, kind, start) DO UPDATE SET
              hash = excluded.hash, text = excluded.text, model = excluded.model, at = excluded.at""",
           lb_id, root_mid, rows[-1]["mid"], h, model.model, text, json.dumps([r["id"] for r in rows]), db.now())
    return {"entries": len(rows), "text": text, "cached": False}


def busy_threads(ai: AI, stop=None, days: int = 60, limit: int = 5) -> int:
    """Summaries of threads with three or more entries that were active lately, computed once
    for everybody, so the entry panel shows them without waiting."""
    if not ai.chat_enabled:
        return 0
    rows = db.all("""SELECT e.logbook_id, e.thread_root AS root, l.name AS logbook, count(*) AS n, max(e.time) AS last
                     FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                     LEFT JOIN digests d ON d.logbook_id = e.logbook_id AND d.kind = 'thread' AND d.start = e.thread_root
                     WHERE l.active = 1 AND e.thread_root IS NOT NULL
                     GROUP BY e.logbook_id, e.thread_root
                     HAVING n >= 3 AND last >= ? AND (max(d.at) IS NULL OR max(d.at) < last)
                     ORDER BY last DESC LIMIT ?""", db.now() - days * 86400, limit)
    n = 0
    for r in rows:
        if stop and stop.is_set():
            break
        if not ai.may_send([r["logbook"]], ai.fast):
            continue
        ai.gate.wait_idle(stop)
        thread(ai, r["logbook_id"], r["root"], background=True)
        n += 1
    return n


def scheduled(ai: AI, stop=None) -> int:
    """Every morning: yesterday, the week so far, and on Mondays / the 1st the past week / month,
    for logbooks that had entries. Computed once and shown to everyone who may read the logbook."""
    today = date.today()
    if datetime.now().hour < 5 or not ai.chat_enabled:
        return 0
    n = 0
    morning = int(datetime.combine(today, datetime.min.time()).timestamp())
    jobs = [("day", today - timedelta(1), 0)]
    if today.weekday() > 0:
        jobs.append(("week", today, morning))          # refreshed once a day
    if today.weekday() == 0:
        jobs.append(("week", today - timedelta(7), 0))
    if today.day == 1:
        jobs.append(("month", today - timedelta(1), 0))
    for kind, day, fresh in jobs:
        start, end = bounds(kind, day)
        for r in db.all("""SELECT DISTINCT e.logbook_id FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                           WHERE l.active = 1 AND e.time >= ? AND e.time < ?
                           AND NOT EXISTS (SELECT 1 FROM digests d WHERE d.logbook_id = e.logbook_id
                                           AND d.kind = ? AND d.start = ? AND d.at >= ?)""",
                        start, end, kind, start, fresh):
            if stop and stop.is_set():
                return n
            ai.gate.wait_idle(stop)
            try:
                get(ai, r[0], kind, day, background=True)
                n += 1
            except providers.ProviderError:
                return n
    if ai.ctx:
        for lb in handover_logbooks(ai.ctx.ix):
            lid = ai.ctx.ix.logbooks[lb]
            if db.get("SELECT 1 FROM digests WHERE logbook_id = ? AND kind = 'handover' AND start = ?", lid,
                      _morning(today)) or (stop and stop.is_set()):
                continue
            ai.gate.wait_idle(stop)
            try:
                handover(ai, lid, today, background=True)
                n += 1
            except providers.ProviderError:
                return n
        mail_scheduled(ai)
    return n


# ---- shift handover ------------------------------------------------------------------------------

def _morning(day: date) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp())


def handover(ai: AI, lb_id: int, day: date, background: bool = False) -> dict:
    """Morning handover of an instrument logbook (`AI handover = daily`): what changed since the last
    one, open problems, last readings, and where the items are, from exact data. Shared by all
    readers of the logbook, so it only uses related logbooks they can all read."""
    from . import insights
    ix = ai.ctx.ix
    lb = db.scalar("SELECT name FROM logbooks WHERE id = ?", lb_id)
    start = _morning(day) - (3 if day.weekday() == 0 else 1) * 86400
    end = min(db.now(), _morning(day) + 86400)
    related = [r for r in ai.ctx.shared_related(lb) if ai.may_send([r])]
    rel_ids = {ix.logbooks[r] for r in related if r in ix.logbooks}
    scope = {lb_id} | rel_ids
    parts = []
    recent = [r for r in _entries(lb_id, start, end) if (r["kind"] or "") != "routine"]
    parts.append("Entries since the last handover:\n" + ("\n".join(_lines(recent[-60:])) or "(none)"))
    for rid in sorted(rel_ids):
        rel = [r for r in _entries(rid, start, end) if (r["kind"] or "") != "routine"]
        if rel:
            parts.append("Related logbook entries in the same time:\n" + "\n".join(_lines(rel[-30:])))
    probs = [p for p in insights.problems(scope, since=end - 60 * 86400)["items"] if not p["resolved"]]
    if probs:
        parts.append("Open problems:\n" + "\n".join(
            f"- [{p['logbook']} #{p['mid']}] {datetime.fromtimestamp(p['time']):%Y-%m-%d}: {p['component']}"
            f"{': ' + p['symptom'] if p['symptom'] else ''}" for p in probs[:15]))
    vals = [v for i in sorted(scope) for v in insights.last_values(i, end)]
    if vals:
        parts.append("Last readings:\n" + "\n".join(
            f"- {v['name']} = {v['raw']} ({datetime.fromtimestamp(v['time']):%Y-%m-%d %H:%M}, #{v['mid']})" for v in vals))
    items = [r[0] for r in db.all("""SELECT n.value FROM entities n JOIN entries e ON e.id = n.entry_id
                                    WHERE e.logbook_id = ? AND n.source = 'attr' AND e.time >= ?
                                    GROUP BY n.norm ORDER BY max(e.time) DESC LIMIT 25""", lb_id, end - 60 * 86400)]
    states = []
    for v in items:
        lc = insights.lifecycle({lb_id}, v, ix.cfg)
        if lc["periods"]:
            states.append(f"- {v}: " + ", ".join(f"{k} = {c['value']} since {datetime.fromtimestamp(c['since']):%Y-%m-%d}"
                                                 for k, c in lc["current"].items() if k in lc["periods"]))
    if states:
        parts.append("Where the items are (from change notes):\n" + "\n".join(states))
    body = "\n\n".join(parts)
    notes = ai.ctx.notes([lb], related)
    h = hashlib.sha1((body + notes + prompts.VERSION + ai.language).encode()).hexdigest()
    cached = db.get("SELECT * FROM digests WHERE logbook_id = ? AND kind = 'handover' AND start = ?", lb_id, _morning(day))
    if cached and cached["hash"] == h:
        return {"logbook": lb, "day": day.isoformat(), "text": cached["text"], "at": cached["at"], "cached": True}
    if not ai.chat_enabled or not ai.may_send([lb]):
        return {"logbook": lb, "day": day.isoformat(), "text": "", "cached": False}
    model = ai.fast if background else ai.chat
    text = model.chat([{"role": "user", "content": f"Logbook {lb}, handover for {day:%A %Y-%m-%d}:\n\n{body}"}],
                      system=prompts.handover_system(ai.language, notes), max_tokens=900, task="handover")
    db.run("""INSERT INTO digests (logbook_id, kind, start, end, hash, model, text, entry_ids_json, at)
              VALUES (?, 'handover', ?, ?, ?, ?, ?, '[]', ?) ON CONFLICT (logbook_id, kind, start) DO UPDATE SET
              hash = excluded.hash, model = excluded.model, text = excluded.text, at = excluded.at""",
           lb_id, _morning(day), end, h, model.model, text, db.now())
    return {"logbook": lb, "day": day.isoformat(), "text": text, "at": db.now(), "cached": False}


def handover_logbooks(ix) -> list[str]:
    cfg = ix.cfg
    return [lb for lb in ix.logbooks if cfg and (cfg.get(lb, "AI handover") or "").strip().lower()
            in ("1", "daily", "yes", "on")]


def handovers(allowed: set[int], ix) -> list[dict]:
    """Latest handover of each handover logbook the user may read."""
    out = []
    for lb in handover_logbooks(ix):
        lid = ix.logbooks[lb]
        if lid not in allowed:
            continue
        d = db.get("SELECT start, text, at FROM digests WHERE logbook_id = ? AND kind = 'handover' "
                   "ORDER BY start DESC LIMIT 1", lid)
        out.append({"logbook": lb, "day": datetime.fromtimestamp(d["start"]).date().isoformat() if d else "",
                    "text": d["text"] if d else "", "at": d["at"] if d else None})
    return out


def mail_scheduled(ai: AI) -> int:
    """Send stored digests and handovers that logbooks asked for by e-mail and were not sent yet:
    `AI digest = daily|weekly|monthly` (to `AI digest to`, else ELOG's `Digest to`, else admins),
    `AI handover to` for handovers."""
    from . import mailer
    ix = ai.ctx.ix
    cfg = ix.cfg
    if not mailer.enabled(cfg):
        return 0
    n = 0
    for lb, lid in ix.logbooks.items():
        want = (cfg.get(lb, "AI digest") or "").strip().lower()
        kinds = [{"daily": "day", "weekly": "week", "monthly": "month"}.get(want, want)]
        kinds = [k for k in kinds if k in ("day", "week", "month")]
        if cfg.get(lb, "AI handover to"):
            kinds.append("handover")
        for kind in kinds:
            d = db.get("""SELECT * FROM digests WHERE logbook_id = ? AND kind = ? AND mailed IS NULL AND text != ''
                          AND at >= ? ORDER BY start DESC LIMIT 1""", lid, kind, db.now() - 2 * 86400)
            if not d or (kind == "week" and d["end"] > db.now()):        # the week so far is not mailed
                continue
            to = mailer.recipients(ix, lb, "AI handover to" if kind == "handover" else "AI digest to",
                                   "" if kind == "handover" else "Digest to")
            title = {"handover": "Handover", "day": "Daily summary", "week": "Weekly summary",
                     "month": "Monthly summary"}[kind]
            if mailer.send(cfg, to, f"[ELOG {lb}] {title} {datetime.fromtimestamp(d['start']):%Y-%m-%d}", d["text"]):
                db.run("UPDATE digests SET mailed = ? WHERE id = ?", db.now(), d["id"])
                n += 1
    return n
