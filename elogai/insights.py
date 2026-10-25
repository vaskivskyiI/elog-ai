"""Views computed from the index and the stored AI results, without further model calls:
recurring problems, numeric series with outliers, entities across logbooks, DAQ runs, activity."""
from __future__ import annotations

import re
import statistics
from collections import defaultdict

from . import db
from .indexer import norm, number


def _in(allowed: set[int]) -> tuple[str, list]:
    return f"({','.join('?' * len(allowed)) or 'NULL'})", sorted(allowed)


def problems(allowed: set[int], since: int = 0, limit: int = 300) -> dict:
    """Problems reported in entries (AI analysis or a Category/Type attribute saying so),
    grouped by the affected component, with the fix if a later entry of the thread reports one."""
    ins, args = _in(allowed)
    rows = db.all(f"""SELECT e.id, e.mid, e.time, e.subject, e.author, e.thread_root, e.logbook_id, e.attrs_json, e.text,
                             l.name AS logbook, x.problem_json, x.summary, x.kind
                      FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                      WHERE e.logbook_id IN {ins} AND e.time >= ?
                        AND (x.problem_json IS NOT NULL OR x.kind = 'problem'
                             OR e.attrs_json LIKE '%"Category": "Problem%' OR e.attrs_json LIKE '%"Type": "Problem%')
                      ORDER BY e.time DESC LIMIT ?""", *args, since, limit)
    fixes = defaultdict(list)
    roots = {(r["logbook_id"], r["thread_root"]) for r in rows}
    if roots:
        for f in db.all(f"""SELECT e.logbook_id, e.thread_root, e.mid, e.time, l.name AS logbook FROM entries e
                            JOIN logbooks l ON l.id = e.logbook_id
                            LEFT JOIN enrich x ON x.entry_id = e.id
                            WHERE e.logbook_id IN {ins} AND (x.kind = 'fix' OR e.attrs_json LIKE '%"Category": "Fix%')""",
                        *args):
            if (f["logbook_id"], f["thread_root"]) in roots:
                fixes[(f["logbook_id"], f["thread_root"])].append({"logbook": f["logbook"], "mid": f["mid"], "time": f["time"]})
    groups: dict[str, dict] = {}
    items = []
    for r in rows:
        p = db.jl(r["problem_json"], None) or {}
        subj = re.sub(r"^(re:\s*)+", "", r["subject"] or "", flags=re.I).strip()
        comp = (p.get("component") or "").strip() or subj[:60] or (r["summary"] or "")[:60] \
            or " ".join((r["text"] or "").split()[:8]) or "unspecified"
        key = r["logbook"] + "|" + norm(comp)[:40]
        fx = [f for f in fixes.get((r["logbook_id"], r["thread_root"]), []) if f["time"] >= r["time"]]
        resolved = bool(fx) or p.get("resolved") is True
        it = {"id": r["id"], "mid": r["mid"], "logbook": r["logbook"], "time": r["time"], "subject": r["subject"],
              "author": r["author"], "summary": r["summary"] or "", "component": comp, "symptom": p.get("symptom", ""),
              "severity": p.get("severity", ""), "resolved": resolved, "fixes": fx[:3]}
        items.append(it)
        g = groups.setdefault(key, {"logbook": r["logbook"], "component": comp, "count": 0, "open": 0,
                                    "last": 0, "first": r["time"], "entries": []})
        g["count"] += 1
        g["open"] += 0 if resolved else 1
        g["last"] = max(g["last"], r["time"])
        g["first"] = min(g["first"], r["time"])
        if len(g["entries"]) < 8:
            g["entries"].append({"mid": r["mid"], "time": r["time"], "subject": r["subject"], "resolved": resolved})
    recurring = sorted(groups.values(), key=lambda g: (-g["count"], -g["last"]))
    return {"items": items, "groups": recurring}


def _robust_outliers(points: list[tuple[int, float, int]], window: int = 25, z: float = 6.0,
                     rel: float = 0.08) -> list[int]:
    """Indices of points far from the median of the preceding values (median/MAD), and by more than
    `rel` of it: readings noted by hand jitter, only real jumps should stand out."""
    out = []
    vals = [p[1] for p in points]
    for i in range(8, len(vals)):
        prev = vals[max(0, i - window):i]
        med = statistics.median(prev)
        mad = statistics.median(abs(v - med) for v in prev) or (abs(med) * 0.02) or 1e-9
        if abs(vals[i] - med) / (1.4826 * mad) > z and abs(vals[i] - med) > rel * abs(med):
            out.append(i)
    return out


def series(lb_id: int, since: int = 0) -> list[dict]:
    """Numeric attributes of a logbook and quantities the AI found in its texts, as time series."""
    numeric = db.jl(db.scalar("SELECT numeric_json FROM logbooks WHERE id = ?", lb_id))
    rows = db.all("""SELECT e.mid, e.time, e.attrs_json, x.measurements_json FROM entries e
                     LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                     WHERE e.logbook_id = ? AND e.time >= ? ORDER BY e.time""", lb_id, since)
    data: dict[tuple, list] = defaultdict(list)
    for r in rows:
        attrs = db.jl(r["attrs_json"], {})
        for k in numeric:
            v = number(attrs.get(k, ""))
            if v is not None:
                data[("attr", k, "")].append((r["time"], v, r["mid"]))
        for m in db.jl(r["measurements_json"]):
            q = re.sub(r"\s+", " ", (m.get("quantity") or "").strip().lower())
            if q:
                data[("text", q, (m.get("unit") or "").strip())].append((r["time"], m["value"], r["mid"]))
    out = []
    for (src, name, unit), pts in data.items():
        if len(pts) < (3 if src == "attr" else 5):
            continue
        idx = _robust_outliers(pts)
        vals = [p[1] for p in pts]
        out.append({"source": src, "name": name, "unit": unit, "points": pts,
                    "outliers": [pts[i][2] for i in idx], "last": pts[-1][1], "min": min(vals), "max": max(vals),
                    "median": statistics.median(vals)})
    out.sort(key=lambda s: (s["source"] != "attr", -len(s["points"])))
    return out[:24]


def entities(allowed: set[int], type_: str = "", q: str = "", limit: int = 200) -> list[dict]:
    ins, args = _in(allowed)
    where, extra = "", []
    if type_:
        where += " AND n.type = ? COLLATE NOCASE"
        extra.append(type_)
    if q:
        where += " AND n.norm LIKE ?"
        extra.append("%" + norm(q) + "%")
    rows = db.all(f"""SELECT n.type, n.norm, min(n.value) AS value, count(DISTINCT n.entry_id) AS n,
                             max(e.time) AS last, min(e.time) AS first,
                             group_concat(DISTINCT l.name) AS logbooks
                      FROM entities n JOIN entries e ON e.id = n.entry_id JOIN logbooks l ON l.id = e.logbook_id
                      WHERE e.logbook_id IN {ins} {where} AND length(n.norm) > 0
                      GROUP BY n.type COLLATE NOCASE, n.norm ORDER BY last DESC LIMIT ?""", *args, *extra, limit)
    return [{"type": r["type"], "norm": r["norm"], "value": r["value"], "entries": r["n"], "last": r["last"],
             "first": r["first"], "logbooks": (r["logbooks"] or "").split(",")} for r in rows]


def entity_types(allowed: set[int]) -> list[dict]:
    ins, args = _in(allowed)
    return [dict(r) for r in db.all(f"""SELECT n.type, count(DISTINCT n.norm) AS values_, count(*) AS mentions
                                        FROM entities n JOIN entries e ON e.id = n.entry_id
                                        WHERE e.logbook_id IN {ins} GROUP BY n.type COLLATE NOCASE
                                        HAVING values_ > 1 ORDER BY mentions DESC""", *args)]


def entity_timeline(allowed: set[int], value: str, limit: int = 300) -> list[int]:
    ins, args = _in(allowed)
    return [r[0] for r in db.all(f"""SELECT DISTINCT e.id FROM entities n JOIN entries e ON e.id = n.entry_id
                                     WHERE n.norm = ? AND e.logbook_id IN {ins} ORDER BY e.time DESC LIMIT ?""",
                                 norm(value), *args, limit)]


def runs(allowed: set[int], q: str = "", limit: int = 300) -> list[dict]:
    ins, args = _in(allowed)
    where, extra = "", []
    if q:
        where = " AND (r.sample LIKE ? OR r.info LIKE ? OR r.name = ? OR r.params_json LIKE ?)"
        extra = [f"%{q}%", f"%{q}%", q, f"%{q}%"]
    rows = db.all(f"""SELECT r.*, e.mid, e.time, l.name AS logbook FROM runs r JOIN entries e ON e.id = r.entry_id
                      JOIN logbooks l ON l.id = e.logbook_id WHERE e.logbook_id IN {ins} {where}
                      ORDER BY e.time DESC LIMIT ?""", *args, *extra, limit)
    return [{"logbook": r["logbook"], "mid": r["mid"], "time": r["time"], "name": r["name"], "sample": r["sample"],
             "info": r["info"], "experiment": r["experiment"], "start": r["start"], "end": r["end"],
             "interrupted": bool(r["interrupted"]), "params": db.jl(r["params_json"], {})} for r in rows]


def activity(allowed: set[int], weeks: int = 26) -> list[dict]:
    """Entries per logbook and week, for the activity overview."""
    ins, args = _in(allowed)
    since = db.now() - weeks * 7 * 86400
    rows = db.all(f"""SELECT l.name, (e.time - ?) / 604800 AS w, count(*) AS n, count(DISTINCT e.author) AS authors
                      FROM entries e JOIN logbooks l ON l.id = e.logbook_id
                      WHERE e.logbook_id IN {ins} AND e.time >= ? GROUP BY l.name, w""", since, *args, since)
    by = defaultdict(lambda: [0] * weeks)
    for r in rows:
        if 0 <= r["w"] < weeks:
            by[r["name"]][r["w"]] = r["n"]
    return sorted(({"logbook": k, "weeks": v, "total": sum(v)} for k, v in by.items()), key=lambda x: -x["total"])
