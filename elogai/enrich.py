"""Background analysis of entries: embeddings, per-entry enrichment and image descriptions.

Work is found by comparing stored hashes with the entries' current hashes, so there is no
queue to lose or to grow: whatever is missing or outdated is simply picked up next, newest
and most requested entries first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from . import db, prompts, providers
from .ai import AI
from .indexer import Indexer, fts_ai, norm
from .vectors import INDEX, chunks, prefixes, to_blob

log = logging.getLogger("elogai.enrich")

ENTRY_COLS = """e.id, e.mid, e.logbook_id, l.name AS logbook, e.date, e.time, e.author, e.subject,
                e.attrs_json, e.text, e.hash, e.in_reply_to, e.attachments_json"""


# ---- embeddings --------------------------------------------------------------------------------

def embed_pending(ai: AI, ix: Indexer, limit: int = 64) -> int:
    if not ai.embed_enabled:
        return 0
    m = ai.embed_model
    rows = db.all(f"""SELECT {ENTRY_COLS} FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      LEFT JOIN chunks c ON c.entry_id = e.id AND c.seq = 0
                      WHERE l.active = 1 AND (c.id IS NULL OR c.hash != e.hash OR c.model != ?)
                      ORDER BY e.priority DESC, e.time DESC LIMIT ?""", m, limit)
    rows = [r for r in rows if ai.may_send([r["logbook"]], ai.embedder)]
    if not rows:
        return 0
    _, doc_prefix = prefixes(m)
    pieces, owners = [], []
    for r in rows:
        for i, ch in enumerate(chunks(ix.entry_doc(r))):
            pieces.append(doc_prefix + ch)
            owners.append((r["id"], i, r["hash"]))
    vecs = []
    for i in range(0, len(pieces), 32):
        vecs.extend(ai.embedder.embed(pieces[i:i + 32], m))
    with db.tx() as c:
        for r in rows:
            c.execute("DELETE FROM chunks WHERE entry_id = ?", (r["id"],))
        for (eid, seq, h), v in zip(owners, vecs):
            c.execute("INSERT INTO chunks (entry_id, seq, hash, model, dim, vec) VALUES (?,?,?,?,?,?)",
                      (eid, seq, h, m, len(v), to_blob(v)))
    INDEX.touch()
    return len(rows)


_qcache: dict[tuple[str, str], list] = {}


def embed_query(ai: AI, text: str):
    """Query vectors are cached: the same question from several people costs one call."""
    key = (ai.embed_model, text)
    v = _qcache.get(key)
    if v is None:
        qp, _ = prefixes(ai.embed_model)
        v = ai.embedder.embed([qp + text], ai.embed_model)[0]
        if len(_qcache) > 1000:
            _qcache.clear()
        _qcache[key] = v
    return v


# ---- enrichment --------------------------------------------------------------------------------

def _is_trivial(r) -> bool:
    t = (r["text"] or "").strip()
    return len(t) < 12 and len(r["subject"] or "") < 25


def pending_enrich(ai: AI, conf: dict, limit: int) -> list:
    if conf["enrich"] == "off":
        return []
    where = ""
    args: list = []
    if conf["enrich"] == "new":
        where = "AND e.time >= ?"
        args.append(db.meta_get("enrich_new_since", 0))
    elif conf["enrich_since"]:
        from datetime import datetime
        where = "AND (e.time >= ? OR e.priority > 0)"
        args.append(int(datetime.strptime(conf["enrich_since"], "%Y-%m-%d").timestamp()))
    return db.all(f"""SELECT {ENTRY_COLS} FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      LEFT JOIN enrich x ON x.entry_id = e.id
                      WHERE l.active = 1 AND (x.entry_id IS NULL OR x.hash != e.hash) {where}
                      ORDER BY e.priority DESC, e.time DESC LIMIT ?""", *args, limit)


def _doc(ix: Indexer, r) -> str:
    doc = ix.entry_doc(r)
    if r["in_reply_to"]:
        p = db.get("SELECT subject FROM entries WHERE logbook_id = ? AND mid = ?", r["logbook_id"], r["in_reply_to"])
        if p and p["subject"]:
            doc = doc.replace("\n", f"\nIn reply to: {p['subject']}\n", 1)
    return doc[:4000]


def enrich_batch(ai: AI, ix: Indexer, rows) -> int:
    """Analyse a few entries in one request; returns how many were stored."""
    done = 0
    todo = []
    for r in rows:
        if _is_trivial(r):
            _store(r, {"summary": r["subject"] or "", "kind": "routine", "tags": [], "entities": [],
                       "measurements": [], "problem": None, "lang": "", "english": ""}, "rule")
            done += 1
        elif not ai.may_send([r["logbook"]], ai.fast):
            _store(r, None, ai.fast.model, error="private logbook: not sent to a remote provider")
            done += 1
        else:
            todo.append(r)
    if not todo or not ai.chat_enabled:
        return done
    # long entries go alone, short ones together - but never entries of different logbooks:
    # a model that mixes up the items of a batch must not move text into a logbook with other readers
    groups, cur, size = [], [], 0
    for r in sorted(todo, key=lambda r: r["logbook_id"]):
        d = _doc(ix, r)
        if cur and (size + len(d) > 6000 or len(cur) >= ai.conf["enrich_batch"]
                    or cur[0][0]["logbook_id"] != r["logbook_id"]):
            groups.append(cur)
            cur, size = [], 0
        cur.append((r, d))
        size += len(d)
    if cur:
        groups.append(cur)
    for g in groups:
        done += _enrich_group(ai, g)
    return done


def _enrich_group(ai: AI, group) -> int:
    ids = {r["id"]: r for r, _ in group}
    try:
        out = ai.fast.chat([{"role": "user", "content": prompts.enrich_user([(r["id"], d) for r, d in group])}],
                           system=prompts.enrich_system(), json_mode=True, max_tokens=700 * len(group) + 300,
                           temperature=0.1, task="enrich")
        data = providers.parse_json(out)
        items = data.get("items", data if isinstance(data, list) else [data]) if isinstance(data, (dict, list)) else []
    except (providers.ProviderError, ValueError) as e:
        if len(group) > 1 and not isinstance(e, providers.ProviderError):
            # small models sometimes garble batches: retry one by one
            return sum(_enrich_group(ai, [g]) for g in group)
        if isinstance(e, providers.ProviderError):
            raise
        for r, _ in group:
            _store(r, None, ai.fast.model, error=f"unreadable answer: {e}")
        return len(group)
    n = 0
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            eid = int(it.get("id"))
        except (TypeError, ValueError):
            eid = next(iter(ids)) if len(ids) == 1 else None
        r = ids.pop(eid, None)
        if r is None:
            continue
        _store(r, it, ai.fast.model)
        n += 1
    if ids and len(group) > 1:
        n += sum(_enrich_group(ai, [(r, d)]) for r, d in group if r["id"] in ids)
    elif ids:
        for r in ids.values():
            _store(r, None, ai.fast.model, error="no result for this entry")
            n += 1
    return n


def _clean(it: dict) -> dict:
    def s(x, n=500):
        return str(x or "").strip()[:n]

    kind = s(it.get("kind"), 30).lower()
    if kind not in prompts.KINDS:
        kind = "other"
    tags = [s(t, 40).lower() for t in it.get("tags") or [] if s(t)][:8]
    ents = []
    for e in it.get("entities") or []:
        if isinstance(e, dict) and s(e.get("value")) and len(s(e.get("value"))) <= 80:
            t = s(e.get("type"), 20).lower()
            ents.append({"type": t if t in prompts.ENTITY_TYPES else "other", "value": s(e.get("value"), 80)})
    meas = []
    for m in it.get("measurements") or []:
        if not isinstance(m, dict):
            continue
        try:
            v = float(str(m.get("value")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        meas.append({"quantity": s(m.get("quantity"), 80), "value": v, "unit": s(m.get("unit"), 20)})
    prob = it.get("problem")
    if isinstance(prob, dict) and (prob.get("component") or prob.get("symptom")):
        sev = s(prob.get("severity"), 10).lower()
        prob = {"component": s(prob.get("component"), 80), "symptom": s(prob.get("symptom"), 200),
                "severity": sev if sev in ("low", "medium", "high") else "medium",
                "resolved": prob.get("resolved") if isinstance(prob.get("resolved"), bool) else None}
    else:
        prob = None
    eng = s(it.get("english"), 4000)
    lang = s(it.get("lang"), 5).lower()
    return {"summary": s(it.get("summary"), 300), "kind": kind, "tags": tags, "entities": ents[:20],
            "measurements": meas[:30], "problem": prob, "lang": lang, "english": "" if lang == "en" else eng}


def _store(r, it: dict | None, model: str, error: str = "") -> None:
    x = _clean(it) if it else {"summary": "", "kind": "", "tags": [], "entities": [], "measurements": [],
                                 "problem": None, "lang": "", "english": ""}
    with db.tx() as c:
        c.execute("""INSERT OR REPLACE INTO enrich (entry_id, hash, model, at, summary, kind, tags_json, entities_json,
                       measurements_json, problem_json, lang, english, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (r["id"], r["hash"], model, db.now(), x["summary"], x["kind"], json.dumps(x["tags"]),
                   json.dumps(x["entities"], ensure_ascii=False), json.dumps(x["measurements"], ensure_ascii=False),
                   json.dumps(x["problem"], ensure_ascii=False) if x["problem"] else None, x["lang"], x["english"],
                   error or None))
        c.execute("DELETE FROM entities WHERE entry_id = ? AND source = 'ai'", (r["id"],))
        for e in x["entities"]:
            c.execute("INSERT INTO entities (entry_id, type, value, norm, source) VALUES (?, ?, ?, ?, 'ai')",
                      (r["id"], e["type"], e["value"], norm(e["value"])))
        c.execute("UPDATE entries SET priority = 0 WHERE id = ? AND priority > 0", (r["id"],))
        row = c.execute("SELECT summary, tags_json, english FROM enrich WHERE entry_id = ?", (r["id"],)).fetchone()
        f = c.execute("SELECT subject, text, attrs FROM entries_fts WHERE rowid = ?", (r["id"],)).fetchone()
        if f:
            c.execute("DELETE FROM entries_fts WHERE rowid = ?", (r["id"],))
            c.execute("INSERT INTO entries_fts (rowid, subject, text, attrs, ai) VALUES (?,?,?,?,?)",
                      (r["id"], f[0], f[1], f[2], fts_ai(row)))


# ---- images ------------------------------------------------------------------------------------

IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)$", re.I)


def describe_images(ai: AI, ix: Indexer, limit: int = 4) -> int:
    """Optional (AI_VISION_MODEL): a short description of each image attachment, searchable."""
    if not ai.vision:
        return 0
    rows = db.all(f"""SELECT {ENTRY_COLS} FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      WHERE l.active = 1 AND e.attachments_json != '[]'
                      AND EXISTS (SELECT 1 FROM json_each(e.attachments_json) j
                                  WHERE NOT EXISTS (SELECT 1 FROM attachments_ai a WHERE a.entry_id = e.id AND a.file = j.value))
                      ORDER BY e.priority DESC, e.time DESC LIMIT 20""")
    n = 0
    for r in rows:
        for name in db.jl(r["attachments_json"]):
            if n >= limit:
                return n
            if db.get("SELECT 1 FROM attachments_ai WHERE entry_id = ? AND file = ?", r["id"], name):
                continue
            path = ix.attachment_path(r["logbook_id"], name)
            desc, h = "", ""
            if IMAGE.search(name) and path and os.path.getsize(path) < 8_000_000 \
                    and ai.may_send([r["logbook"]], ai.vision):
                with open(path, "rb") as f:
                    data = f.read()
                h = hashlib.sha1(data).hexdigest()
                desc = ai.vision.chat([{"role": "user", "content": prompts.vision_prompt(), "images": [data]}],
                                      max_tokens=200, task="vision")
                n += 1
            db.run("INSERT OR REPLACE INTO attachments_ai (entry_id, file, hash, model, description, at) "
                   "VALUES (?,?,?,?,?,?)", r["id"], name, h, ai.vision.model if desc else "", desc, db.now())
            if desc:
                with db.tx() as c:
                    f = c.execute("SELECT subject, text, attrs, ai FROM entries_fts WHERE rowid = ?", (r["id"],)).fetchone()
                    if f:
                        c.execute("DELETE FROM entries_fts WHERE rowid = ?", (r["id"],))
                        c.execute("INSERT INTO entries_fts (rowid, subject, text, attrs, ai) VALUES (?,?,?,?,?)",
                                  (r["id"], f[0], f[1], f[2], (f[3] or "") + " " + desc))
    return n


def stats() -> dict:
    total = db.scalar("SELECT count(*) FROM entries e JOIN logbooks l ON l.id = e.logbook_id WHERE l.active = 1", default=0)
    return {
        "entries": total,
        "embedded": db.scalar("""SELECT count(*) FROM entries e JOIN chunks c ON c.entry_id = e.id AND c.seq = 0
                                 AND c.hash = e.hash""", default=0),
        "enriched": db.scalar("SELECT count(*) FROM entries e JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash",
                              default=0),
        "enrich_errors": db.scalar("SELECT count(*) FROM enrich WHERE error IS NOT NULL", default=0),
        "runs": db.scalar("SELECT count(*) FROM runs", default=0),
        "images_described": db.scalar("SELECT count(*) FROM attachments_ai WHERE description != ''", default=0),
    }
