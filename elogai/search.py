"""Hybrid search: FTS5 (BM25) for names, ids and exact terms, embeddings for meaning;
the two rankings are merged with reciprocal rank fusion. Results are limited to the
logbooks the caller may read."""
from __future__ import annotations

import re
from datetime import datetime

from . import db
from .ai import AI
from .indexer import norm
from .vectors import INDEX

STOP = set("""a an the and or of to in on at for with by from is are was were be been it this that these those
what when where which who how did do does we i you he she they there here any some all about into after before
je in na za z s v pri od do ali kaj kdaj kje kako ki se so smo bil bila bilo""".split())

WORD = re.compile(r"[\w][\w\-.]*", re.U)


def fts_query(q: str, mode: str = "or") -> str:
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms = []
    for p in phrases:
        terms.append('"' + p.replace('"', "") + '"')
    for w in WORD.findall(rest):
        w = w.strip(".-")
        if len(w) < 2 or w.lower() in STOP:
            continue
        # unicode61 splits S-179 into "s" "179": a phrase keeps them together
        parts = [p for p in re.split(r"[\W_]+", w) if p]
        if not parts:
            continue
        t = '"' + " ".join(parts) + '"'
        if len(parts) == 1 and len(parts[0]) >= 4 and not parts[0].isdigit():
            t += "*"
        terms.append(t)
    return (" AND " if mode == "and" else " OR ").join(dict.fromkeys(terms))


def _filters(f: dict, allowed: set[int] | None) -> tuple[str, list]:
    where, args = [], []
    if allowed is not None:
        where.append(f"e.logbook_id IN ({','.join('?' * len(allowed)) or 'NULL'})")
        args.extend(sorted(allowed))
    if f.get("logbooks"):
        ids = [r[0] for r in db.all(f"SELECT id FROM logbooks WHERE name IN ({','.join('?' * len(f['logbooks']))})",
                                    *f["logbooks"])]
        where.append(f"e.logbook_id IN ({','.join('?' * len(ids)) or 'NULL'})")
        args.extend(ids)
    if f.get("since"):
        where.append("e.time >= ?")
        args.append(_ts(f["since"]))
    if f.get("until"):
        where.append("e.time < ?")
        args.append(_ts(f["until"]) + 86400)
    if f.get("author"):
        where.append("e.author = ? COLLATE NOCASE")
        args.append(f["author"])
    if f.get("kind"):
        where.append("EXISTS (SELECT 1 FROM enrich x WHERE x.entry_id = e.id AND x.kind = ?)")
        args.append(f["kind"])
    if f.get("entity"):
        where.append("EXISTS (SELECT 1 FROM entities n WHERE n.entry_id = e.id AND n.norm = ?)")
        args.append(norm(f["entity"]))
    return (" AND ".join(where) or "1"), args


def _ts(d) -> int:
    if isinstance(d, (int, float)):
        return int(d)
    return int(datetime.strptime(str(d)[:10], "%Y-%m-%d").timestamp())


def keyword(q: str, f: dict, allowed: set[int] | None, k: int = 50) -> list[tuple[int, float, str]]:
    fq = fts_query(q)
    if not fq:
        return []
    where, args = _filters(f, allowed)
    try:
        rows = db.all(f"""SELECT e.id, bm25(entries_fts, 4.0, 1.0, 1.5, 0.8) AS s,
                                 snippet(entries_fts, 1, '\x02', '\x03', ' … ', 24) AS snip
                          FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid
                          WHERE entries_fts MATCH ? AND {where} ORDER BY s LIMIT ?""", fq, *args, k)
    except Exception:
        return []
    return [(r[0], -r[1], r[2]) for r in rows]


def semantic(ai: AI, q: str, f: dict, allowed: set[int] | None, k: int = 50, qvec=None) -> list[tuple[int, float]]:
    if not ai.embed_enabled:
        return []
    from .enrich import embed_query
    try:
        qvec = qvec if qvec is not None else embed_query(ai, q)
    except Exception:
        return []
    lbs = allowed
    if f.get("logbooks"):
        ids = {r[0] for r in db.all(f"SELECT id FROM logbooks WHERE name IN ({','.join('?' * len(f['logbooks']))})",
                                    *f["logbooks"])}
        lbs = ids if lbs is None else lbs & ids
    hits = INDEX.search(qvec, ai.embed_model, lbs, k * 3)
    if not hits:
        return []
    extra = {k_: v for k_, v in f.items() if k_ not in ("logbooks",) and v}
    if extra:
        where, args = _filters(extra, None)
        ok = {r[0] for r in db.all(f"SELECT e.id FROM entries e WHERE e.id IN ({','.join('?' * len(hits))}) AND {where}",
                                   *[h[0] for h in hits], *args)}
        hits = [h for h in hits if h[0] in ok]
    return hits[:k]


def search(ai: AI, q: str, f: dict | None = None, allowed: set[int] | None = None, k: int = 20,
           mode: str = "hybrid", offset: int = 0) -> dict:
    f = f or {}
    q = (q or "").strip()
    if not q:
        return {"results": _recent(f, allowed, k, offset), "mode": "recent"}
    kw = keyword(q, f, allowed, 100) if mode in ("hybrid", "keyword") else []
    sem = semantic(ai, q, f, allowed, 100) if mode in ("hybrid", "semantic") else []
    score: dict[int, float] = {}
    snips = {eid: snip for eid, _, snip in kw}
    for rank, (eid, _, _) in enumerate(kw):
        score[eid] = score.get(eid, 0) + 1 / (60 + rank)
    for rank, (eid, s) in enumerate(sem):
        score[eid] = score.get(eid, 0) + 1 / (60 + rank) * (1.0 if s > 0.3 else 0.5)
    # exact identifiers (sample S-179, run 905) count double
    ids = [norm(w) for w in WORD.findall(q) if re.search(r"\d", w)]
    if ids and score:
        for r in db.all(f"""SELECT DISTINCT entry_id FROM entities WHERE norm IN ({','.join('?' * len(ids))})
                            AND entry_id IN ({','.join('?' * len(score))})""", *ids, *score):
            score[r[0]] += 1 / 60
    ranked = sorted(score, key=lambda e: -score[e])
    total = len(ranked)
    page = ranked[offset:offset + k]
    sims = dict(sem)
    out = rows_for(page)
    for r in out:
        r["score"] = round(score[r["id"]] * 1000, 2)
        r["similarity"] = round(sims.get(r["id"], 0), 3) or None
        if r["id"] in snips and "\x02" in (snips[r["id"]] or ""):
            r["snippet"] = snips[r["id"]]
    return {"results": out, "total": total, "mode": mode if (kw or sem) else "none",
            "semantic": bool(sem), "keyword": bool(kw)}


def _recent(f: dict, allowed: set[int] | None, k: int, offset: int = 0) -> list[dict]:
    where, args = _filters(f, allowed)
    ids = [r[0] for r in db.all(f"SELECT e.id FROM entries e WHERE {where} ORDER BY e.time DESC LIMIT ? OFFSET ?",
                                *args, k, offset)]
    return rows_for(ids)


def rows_for(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = db.all(f"""SELECT e.id, e.mid, l.name AS logbook, e.time, e.date, e.author, e.subject, e.attrs_json,
                             e.text, e.in_reply_to, e.thread_root, e.attachments_json, x.summary, x.kind,
                             x.tags_json, x.problem_json, x.lang
                      FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                      WHERE e.id IN ({','.join('?' * len(ids))})""", *ids)
    by = {r["id"]: r for r in rows}
    out = []
    for i in ids:
        r = by.get(i)
        if not r:
            continue
        text = r["text"] or ""
        out.append({"id": r["id"], "mid": r["mid"], "logbook": r["logbook"], "time": r["time"], "date": r["date"],
                    "author": r["author"], "subject": r["subject"], "attrs": db.jl(r["attrs_json"], {}),
                    "snippet": text[:240] + ("…" if len(text) > 240 else ""), "summary": r["summary"] or "",
                    "kind": r["kind"] or "", "tags": db.jl(r["tags_json"]), "problem": db.jl(r["problem_json"], None),
                    "attachments": len(db.jl(r["attachments_json"])), "reply_to": r["in_reply_to"] or 0,
                    "thread": r["thread_root"] or r["mid"]})
    return out


def similar(ai: AI, entry_id: int, allowed: set[int] | None, k: int = 8) -> list[dict]:
    """Related entries in any readable logbook, not counting the entry's own thread."""
    e = db.get("SELECT id, logbook_id, thread_root, mid, subject, text FROM entries WHERE id = ?", entry_id)
    if not e:
        return []
    own = {r[0] for r in db.all("SELECT id FROM entries WHERE logbook_id = ? AND thread_root = ?",
                                e["logbook_id"], e["thread_root"] or e["mid"])} | {entry_id}
    hits = []
    if ai.embed_enabled:
        INDEX.ensure(ai.embed_model)
        v = INDEX.vector_of(entry_id)
        if v is not None:
            hits = [h for h in INDEX.search(v, ai.embed_model, allowed, k * 2, exclude=own) if h[1] > 0.45]
    if not hits:   # not embedded yet: words of the subject and text
        kw = keyword(f"{e['subject']} {(e['text'] or '')[:300]}", {}, allowed, k * 2)
        hits = [(i, 0.0) for i, _, _ in kw if i not in own]
    out = rows_for([h[0] for h in hits[:k]])
    sims = dict(hits)
    for r in out:
        r["similarity"] = round(float(sims.get(r["id"], 0)), 3)
    return out
