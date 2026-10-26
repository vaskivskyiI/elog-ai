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


COND = re.compile(r"^\s*(.+?)\s*(>=|<=|!=|>|<|=)\s*(-?\d+(?:[.,]\d+)?)\s*$")
OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       "=": lambda a, b: abs(a - b) < 1e-9, "!=": lambda a, b: abs(a - b) >= 1e-9}


def _run_start(r) -> int:
    from datetime import datetime
    try:
        return int(datetime.strptime((r["start"] or "")[:19], "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return r["time"]


def run_conditions(lb_ids: list[int], names: dict[int, str], t: int) -> dict[str, dict]:
    """Numeric readings of the given logbooks (its own and related ones) last noted before t."""
    out = {}
    for i in lb_ids:
        for v in last_values(i, t, max_age_days=14):
            out[f"{names[i]} {v['name']}"] = {"value": v["value"], "raw": v["raw"], "time": v["time"],
                                              "logbook": names[i], "mid": v["mid"]}
    return out


def runs(allowed: set[int], q: str = "", limit: int = 300, ctx=None) -> list[dict]:
    """DAQ runs, filtered by text (sample, info, run number, parameters) and by numeric conditions
    on run parameters and on the conditions at run start, comma separated:
    `TaS2, Legend OUT > 2.5, pump_ND.currentpositionunits >= 10`."""
    ins, args = _in(allowed)
    texts, conds = [], []
    for part in [p for p in re.split(r"[,;]", q or "") if p.strip()]:
        m = COND.match(part)
        if m:
            conds.append((norm(m.group(1)), m.group(2), float(m.group(3).replace(",", "."))))
        else:
            texts.append(part.strip())
    where, extra = "", []
    for t in texts:
        where += " AND (r.sample LIKE ? OR r.info LIKE ? OR r.name = ? OR r.params_json LIKE ?)"
        extra += [f"%{t}%", f"%{t}%", t, f"%{t}%"]
    rows = db.all(f"""SELECT r.*, e.mid, e.time, e.logbook_id, l.name AS logbook FROM runs r
                      JOIN entries e ON e.id = r.entry_id JOIN logbooks l ON l.id = e.logbook_id
                      WHERE e.logbook_id IN {ins} {where}
                      ORDER BY e.time DESC LIMIT ?""", *args, *extra, 2000 if conds else limit)
    names = {r[0]: r[1] for r in db.all("SELECT id, name FROM logbooks")}
    ids_of = {v: k for k, v in names.items()}
    out = []
    for r in rows:
        params = db.jl(r["params_json"], {})
        rel = [ids_of[n] for n in (ctx.related(r["logbook"]) if ctx else []) if ids_of.get(n) in allowed]
        need = conds or len(out) < 100
        cond = run_conditions([r["logbook_id"]] + rel, names, _run_start(r)) if need else {}
        if conds:
            values = {norm(k): number(v) for k, v in params.items()}
            values.update({norm(k): c["value"] for k, c in cond.items()})
            ok = True
            for name, op, val in conds:
                hits = [v for k, v in values.items() if v is not None and (k == name or k.endswith(name))]
                if not hits or not any(OPS[op](h, val) for h in hits):
                    ok = False
                    break
            if not ok:
                continue
        out.append({"logbook": r["logbook"], "mid": r["mid"], "time": r["time"], "name": r["name"],
                    "sample": r["sample"], "info": r["info"], "experiment": r["experiment"], "start": r["start"],
                    "end": r["end"], "interrupted": bool(r["interrupted"]), "params": params, "conditions": cond})
        if len(out) >= limit:
            break
    return out


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


def state_around(lb_ids, t: int, before: int = 3, after: int = 2) -> list[int]:
    """The state of related logbooks at time t: per logbook the last `before` entries up to t,
    however long ago (conditions are often stable for days), and up to `after` entries within the
    following day. Routine entries are skipped."""
    out = []
    for lb in lb_ids:
        out += [r[0] for r in db.all("""SELECT e.id FROM entries e LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                                        WHERE e.logbook_id = ? AND e.time <= ? AND coalesce(x.kind, '') != 'routine'
                                        ORDER BY e.time DESC LIMIT ?""", lb, t, before)]
        out += [r[0] for r in db.all("""SELECT e.id FROM entries e LEFT JOIN enrich x ON x.entry_id = e.id AND x.hash = e.hash
                                        WHERE e.logbook_id = ? AND e.time > ? AND e.time <= ?
                                        AND coalesce(x.kind, '') != 'routine'
                                        ORDER BY e.time LIMIT ?""", lb, t, t + 86400, after)]
    return out


def last_values(lb_id: int, t: int, max_age_days: int = 60) -> list[dict]:
    """Latest value of each numeric attribute of a logbook at time t (e.g. laser power, temperature)."""
    numeric = db.jl(db.scalar("SELECT numeric_json FROM logbooks WHERE id = ?", lb_id))
    out = []
    for k in numeric:
        r = db.get("""SELECT e.mid, e.time, json_extract(e.attrs_json, ?) AS v FROM entries e
                      WHERE e.logbook_id = ? AND e.time <= ? AND e.time >= ?
                      AND coalesce(json_extract(e.attrs_json, ?), '') != ''
                      ORDER BY e.time DESC LIMIT 1""", f'$."{k}"', lb_id, t, t - max_age_days * 86400, f'$."{k}"')
        if r and number(r["v"]) is not None:
            out.append({"name": k, "value": number(r["v"]), "raw": r["v"], "time": r["time"], "mid": r["mid"]})
    return out


LIFECYCLE_SKIP = {"subject", "author", "category", "responsible", "file", "runn", "draft", "type"}


def change_note_patterns(cfg, lb: str) -> dict[str, re.Pattern]:
    """`Change note <attr> = Storage position changed from $old to $new` (elogd.cfg) as regexes that
    find the note in entry texts: the history of an attribute even when entries were edited later."""
    if cfg is None:
        return {}
    out = {}
    for key, tpl in list(cfg.sections.get("global", {}).items()) + list(cfg.sections.get(lb.lower(), {}).items()):
        if key.startswith("change note ") and "$new" in tpl:
            rx = re.escape(tpl.strip()).replace(r"\$old", r"(?P<old>.+?)").replace(r"\$new", r"(?P<new>[^\n]+?)")
            out[key[len("change note "):].strip()] = re.compile(rx + r"\s*(?:[.;]?\s*$|\n)", re.I | re.M)
    # keys are lower-case in the parsed config: map them back to the attribute names
    names = {a.lower(): a for a in cfg.attributes(lb)}
    return {names.get(k, k): v for k, v in out.items()}


def lifecycle(allowed: set[int], value: str, cfg=None, limit: int = 2000) -> dict:
    """History of one item (sample, tip, serial number) from its entries: the change notes in the
    texts ("Stage position changed from Removed to Sample", see `Change note` in elogd.cfg) and the
    attribute values of successive entries. For each tracked attribute (`AI track = Carousel, MP`,
    otherwise every attribute that changes, except bookkeeping ones): the periods with one value
    and how long they lasted, the changes, and the current state."""
    ins, args = _in(allowed)
    n = norm(value)
    rows = db.all(f"""SELECT DISTINCT e.id, e.mid, e.time, e.attrs_json, e.text, l.name AS logbook FROM entities x
                      JOIN entries e ON e.id = x.entry_id JOIN logbooks l ON l.id = e.logbook_id
                      WHERE x.norm = ? AND x.source = 'attr' AND e.logbook_id IN {ins}
                      ORDER BY e.time, e.mid LIMIT ?""", n, *args, limit)
    if not rows:
        return {"value": value, "entries": 0, "current": {}, "periods": {}, "changes": []}

    def label(lb: str, attr: str, v: str) -> str:
        return v or (cfg.get(lb, f"Empty option {attr}") if cfg else "") or "(empty)"

    events = []          # (time, mid, logbook, attr, old, new)
    last: dict[str, str] = {}
    for r in rows:
        lb = r["logbook"]
        notes = change_note_patterns(cfg, lb)
        for attr, rx in notes.items():
            for m in rx.finditer(r["text"] or ""):
                events.append((r["time"], r["mid"], lb, attr, m.group("old").strip(), m.group("new").strip()))
        attrs = db.jl(r["attrs_json"], {})
        track = [a.strip() for a in (cfg.get(lb, "AI track") if cfg else "").split(",") if a.strip()]
        keys = track or [k for k in attrs if k.lower() not in LIFECYCLE_SKIP and norm(attrs.get(k) or "") != n]
        for k in keys:
            if k not in attrs or k in notes:       # attributes with change notes: the notes are the history
                continue
            v = label(lb, k, (attrs.get(k) or "").strip())
            if k in last and last[k] != v:
                events.append((r["time"], r["mid"], lb, k, last[k], v))
            elif k not in last:
                events.append((r["time"], r["mid"], lb, k, None, v))
            last[k] = v
    events.sort(key=lambda e: (e[0], e[1]))
    periods: dict[str, list] = defaultdict(list)
    changes = []
    for t, mid, lb, k, old, new in events:
        ps = periods[k]
        if ps and ps[-1]["value"] == new:
            continue
        if ps:
            ps[-1]["to"] = t
        elif old is not None:          # the value before the first note, from the first entry on
            ps.append({"value": old, "from": rows[0]["time"], "to": t, "logbook": lb, "mid": rows[0]["mid"]})
        if old is not None:
            changes.append({"time": t, "logbook": lb, "mid": mid, "attr": k, "old": old, "new": new})
        ps.append({"value": new, "from": t, "to": None, "logbook": lb, "mid": mid})
    now = db.now()
    out_periods, current = {}, {}
    for k, ps in periods.items():
        current[k] = {"value": ps[-1]["value"], "since": ps[-1]["from"], "logbook": ps[-1]["logbook"], "mid": ps[-1]["mid"]}
        if len(ps) < 2:
            continue           # never changed: not part of the history
        for p in ps:
            p["days"] = round(((p["to"] or now) - p["from"]) / 86400, 1)
        out_periods[k] = ps
    # time spent per value, e.g. how long on the measuring stage in total
    totals = {k: {} for k in out_periods}
    for k, ps in out_periods.items():
        for p in ps:
            totals[k][p["value"]] = round(totals[k].get(p["value"], 0) + p["days"], 1)
    return {"value": value, "entries": len(rows), "first": rows[0]["time"], "last": rows[-1]["time"],
            "current": current, "periods": out_periods, "totals": totals, "changes": changes}


def lifecycle_text(lc: dict, max_changes: int = 25) -> str:
    """Compact text of a lifecycle for prompts."""
    from datetime import datetime
    d = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "now"
    cur = "; ".join(f"{k} = {v['value']} (since {d(v['since'])})" for k, v in lc["current"].items() if k in lc["periods"])
    lines = [f"History of {lc['value']} (exact, from {lc['entries']} logbook entries)"
             f" ({d(lc.get('first'))} to {d(lc.get('last'))}). Current: {cur or 'unchanged'}"]
    for k, ps in lc["periods"].items():
        lines.append(f"  {k}: " + " → ".join(f"{p['value']} {d(p['from'])}..{d(p['to'])} ({p['days']} d) "
                                              f"[{p['logbook']} #{p['mid']}]" for p in ps[-max_changes:]))
        lines.append(f"  total time per {k}: " + ", ".join(f"{v}: {t} d" for v, t in lc["totals"][k].items()))
    return "\n".join(lines)
