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
    h = hashlib.sha1(("\n".join(lines) + prompts.VERSION + ai.language).encode()).hexdigest()
    cached = db.get("SELECT * FROM digests WHERE logbook_id = ? AND kind = ? AND start = ?", lb_id, kind, start)
    if cached and (cached["hash"] == h or not generate):
        return {**base, "text": cached["text"], "model": cached["model"], "at": cached["at"], "cached": True,
                "stale": cached["hash"] != h}
    if not generate or not ai.chat_enabled or not ai.may_send([lbname]):
        return {**base, "text": "", "cached": False}
    msgs = [{"role": "user", "content": f"Logbook {lbname}, {kind} starting "
                                        f"{datetime.fromtimestamp(start):%Y-%m-%d}:\n\n" + "\n".join(lines)}]
    model = ai.fast if background else ai.chat
    text = model.chat(msgs, system=prompts.digest_system(ai.language), max_tokens=900, task=f"digest-{kind}")
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
    h = hashlib.sha1(("\n".join(lines) + prompts.VERSION + ai.language).encode()).hexdigest()
    cached = db.get("SELECT * FROM digests WHERE logbook_id = ? AND kind = 'thread' AND start = ?", lb_id, root_mid)
    if cached and cached["hash"] == h:
        return {"entries": len(rows), "text": cached["text"], "cached": True}
    model = ai.fast if background else ai.chat
    if not ai.chat_enabled or not ai.may_send([rows[0]["logbook"]], model):
        return {"entries": len(rows), "text": ""}
    msgs = [{"role": "user", "content": "\n".join(lines)}]
    if background:
        text = model.chat(msgs, system=prompts.thread_system(ai.language), max_tokens=600, task="thread-bg")
    else:
        with ai.gate.interactive():
            text = model.chat(msgs, system=prompts.thread_system(ai.language), max_tokens=600, task="thread")
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
    return n
