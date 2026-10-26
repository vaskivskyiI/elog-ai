"""Alerts for logbooks with `AI alerts = 1` in elogd.cfg: a numeric reading far from the
recent ones (the latest value of a series, see insights.series), or a problem still open after
`AI alert open days` (default 7). Each alert is raised once, stored, shown on the Overview to
readers of the logbook, and e-mailed to `AI alerts to` (addresses or ELOG logins; default: the
logbook's admins)."""
from __future__ import annotations

import logging

from . import db, insights, mailer

log = logging.getLogger("elogai.alerts")


def _enabled(cfg, lb: str) -> bool:
    return (cfg.get(lb, "AI alerts") or "0").strip().lower() in ("1", "yes", "true", "on")


def check(ix, now: int | None = None) -> int:
    """Raise new alerts; returns how many. Cheap: no model calls."""
    cfg = ix.cfg
    if cfg is None:
        return 0
    now = now or db.now()
    new = 0
    for lb, lb_id in ix.logbooks.items():
        if not _enabled(cfg, lb):
            continue
        found = []
        for s in insights.series(lb_id, since=now - 365 * 86400):
            last = s["points"][-1]
            if s["source"] == "attr" and last[2] in s["outliers"] and now - last[0] < 3 * 86400:
                found.append((f"outlier:{lb_id}:{s['name']}:{last[2]}", "reading", last[2],
                              f"{s['name']} = {last[1]:g}{(' ' + s['unit']) if s['unit'] else ''} in [{lb} #{last[2]}] "
                              f"is far from the usual (median {s['median']:g})"))
        try:
            days = float(cfg.get(lb, "AI alert open days") or 7)
        except ValueError:
            days = 7
        for p in insights.problems({lb_id}, since=now - 120 * 86400)["items"]:
            if not p["resolved"] and now - p["time"] > days * 86400:
                found.append((f"open:{lb_id}:{p['mid']}", "problem", p["mid"],
                              f"Problem open for {int((now - p['time']) / 86400)} days: {p['component']}"
                              f"{': ' + p['symptom'] if p['symptom'] else ''} [{lb} #{p['mid']}]"))
        fresh = []
        for key, kind, mid, text in found:
            if db.get("SELECT 1 FROM alerts WHERE key = ?", key):
                continue
            db.run("INSERT INTO alerts (key, logbook_id, kind, text, mid, at) VALUES (?,?,?,?,?,?)",
                   key, lb_id, kind, text, mid, now)
            fresh.append(text)
        if fresh:
            new += len(fresh)
            to = mailer.recipients(ix, lb, "AI alerts to")
            body = f"ELOG AI noticed in {lb}:\n\n" + "\n".join(f"- {t}" for t in fresh)
            if mailer.send(cfg, to, f"[ELOG {lb}] {len(fresh)} alert{'s' if len(fresh) > 1 else ''}", body):
                db.run(f"UPDATE alerts SET sent_to = ? WHERE key IN ({','.join('?' * len(found))})",
                       ", ".join(to), *[f[0] for f in found])
    return new


def recent(allowed: set[int], days: int = 14) -> list[dict]:
    if not allowed:
        return []
    rows = db.all(f"""SELECT a.*, l.name AS logbook FROM alerts a JOIN logbooks l ON l.id = a.logbook_id
                      WHERE a.at >= ? AND a.logbook_id IN ({','.join('?' * len(allowed))})
                      ORDER BY a.at DESC LIMIT 100""", db.now() - days * 86400, *sorted(allowed))
    return [{"logbook": r["logbook"], "kind": r["kind"], "text": r["text"], "mid": r["mid"], "at": r["at"],
             "mailed": bool(r["sent_to"])} for r in rows]
