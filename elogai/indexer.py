"""Keeps the index in step with the logbook files.

A scan stats every day file (a few thousand stat calls, milliseconds) and re-reads only
files whose size or mtime changed. Entries are compared by content hash, so an edit
re-indexes one entry and invalidates exactly its AI results.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict

from . import db, elogsrc
from .vectors import INDEX

log = logging.getLogger("elogai.index")

NOT_ENTITY = re.compile(r"^(subject|author|category|type|file|draft|responsible|status|plan|encoding)$", re.I)
NOT_SERIES = re.compile(r"^(author|subject)$|run|^id$|^sn$|number|^nr\.?$|^no\.?$|^mp$|batch", re.I)
NUM = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*([a-zA-Z%°µ/\.]{0,8})\s*$")


def norm(value: str) -> str:
    return re.sub(r"[\s\-_./]+", "", value.lower())


def number(value: str) -> float | None:
    m = NUM.match(value or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


class Indexer:
    def __init__(self, cfg_path: str):
        self.cfg_path = cfg_path
        self.cfg: elogsrc.ElogConfig | None = None
        self.cfg_mtime = 0.0
        self.logbooks: dict[str, int] = {}       # name -> id
        self.dirs: dict[int, str] = {}

    # ---- configuration -------------------------------------------------------------------------

    def load_config(self, force: bool = False) -> bool:
        try:
            m = os.stat(self.cfg_path).st_mtime
        except OSError:
            return False
        if not force and self.cfg is not None and m == self.cfg_mtime:
            return False
        self.cfg = elogsrc.ElogConfig.load(self.cfg_path)
        self.cfg_mtime = m
        names = self.cfg.logbooks()
        with db.tx() as c:
            c.execute("UPDATE logbooks SET active = 0")
            for n in names:
                d = self.cfg.data_dir(n, self.cfg_path)
                c.execute("""INSERT INTO logbooks (name, dir, attributes_json, active) VALUES (?, ?, ?, 1)
                             ON CONFLICT (name) DO UPDATE SET name = excluded.name, dir = excluded.dir,
                               attributes_json = excluded.attributes_json, active = 1""",
                          (n, d, json.dumps(self.cfg.attributes(n))))
        rows = db.all("SELECT id, name, dir FROM logbooks WHERE active = 1")
        self.logbooks = {r["name"]: r["id"] for r in rows}
        self.dirs = {r["id"]: r["dir"] for r in rows}
        log.info("elogd.cfg: %d logbooks", len(rows))
        return True

    # ---- scanning ------------------------------------------------------------------------------

    def scan(self) -> dict:
        self.load_config()
        stats = {"files": 0, "changed": 0, "added": 0, "updated": 0, "deleted": 0}
        known = {r["path"]: (r["mtime_ns"], r["size"], r["logbook_id"]) for r in db.all("SELECT * FROM files")}
        seen = set()
        touched_lbs = set()
        for lb_id, d in self.dirs.items():
            for path in elogsrc.log_files(d):
                seen.add(path)
                stats["files"] += 1
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                k = known.get(path)
                if k and k[0] == st.st_mtime_ns and k[1] == st.st_size and k[2] == lb_id:
                    continue
                stats["changed"] += 1
                touched_lbs.add(lb_id)
                self._sync_file(lb_id, path, st, stats)
        # files that disappeared, or belong to logbooks no longer in elogd.cfg
        for path, (_, _, lb_id) in known.items():
            if path not in seen:
                touched_lbs.add(lb_id)
                with db.tx() as c:
                    ids = [r[0] for r in c.execute("SELECT id FROM entries WHERE file = ?", (path,))]
                    self._delete(c, ids)
                    c.execute("DELETE FROM files WHERE path = ?", (path,))
                stats["deleted"] += len(ids)
        if touched_lbs:
            for lb_id in touched_lbs:
                self._threads(lb_id)
                self._numeric(lb_id)
            INDEX.touch()
        return stats

    def _sync_file(self, lb_id: int, path: str, st, stats: dict) -> None:
        try:
            entries = [e for e in elogsrc.read_log(path) if not e.draft]
        except OSError as e:
            log.warning("cannot read %s: %s", path, e)
            return
        with db.tx() as c:
            old = {r["mid"]: (r["id"], r["hash"]) for r in
                   c.execute("SELECT id, mid, hash FROM entries WHERE logbook_id = ? AND file = ?", (lb_id, path))}
            for e in entries:
                h = e.content_hash()
                prev = old.pop(e.mid, None)
                if prev and prev[1] == h:
                    continue
                if not prev:
                    # an entry may move between day files when its date is edited
                    row = c.execute("SELECT id, hash FROM entries WHERE logbook_id = ? AND mid = ?",
                                    (lb_id, e.mid)).fetchone()
                    prev = (row["id"], row["hash"]) if row else None
                self._upsert(c, lb_id, path, e, h, prev[0] if prev else None)
                stats["updated" if prev else "added"] += 1
            self._delete(c, [v[0] for v in old.values()])
            stats["deleted"] += len(old)
            c.execute("INSERT INTO files (path, logbook_id, mtime_ns, size) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT (path) DO UPDATE SET logbook_id = excluded.logbook_id, "
                      "mtime_ns = excluded.mtime_ns, size = excluded.size", (path, lb_id, st.st_mtime_ns, st.st_size))

    def _upsert(self, c, lb_id: int, path: str, e: elogsrc.Entry, h: str, eid: int | None) -> None:
        attrs = {k: v for k, v in e.attrs.items() if k not in ("Subject",)}
        vals = (lb_id, e.mid, path, e.time, e.date, e.author, e.subject, json.dumps(attrs, ensure_ascii=False),
                json.dumps(e.attachments), e.encoding, e.in_reply_to, e.text, h, db.now())
        if eid is None:
            eid = c.execute("""INSERT INTO entries (logbook_id, mid, file, time, date, author, subject, attrs_json,
                                 attachments_json, encoding, in_reply_to, text, hash, indexed_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals).lastrowid
        else:
            c.execute("""UPDATE entries SET logbook_id=?, mid=?, file=?, time=?, date=?, author=?, subject=?,
                           attrs_json=?, attachments_json=?, encoding=?, in_reply_to=?, text=?, hash=?, indexed_at=?
                         WHERE id = ?""", vals + (eid,))
            c.execute("DELETE FROM entries_fts WHERE rowid = ?", (eid,))
            c.execute("DELETE FROM entities WHERE entry_id = ? AND source = 'attr'", (eid,))
            c.execute("DELETE FROM runs WHERE entry_id = ?", (eid,))
        ai = c.execute("SELECT summary, tags_json, english FROM enrich WHERE entry_id = ?", (eid,)).fetchone()
        c.execute("INSERT INTO entries_fts (rowid, subject, text, attrs, ai) VALUES (?, ?, ?, ?, ?)",
                  (eid, e.subject, e.text, " ".join(f"{k}: {v}" for k, v in attrs.items() if v),
                   fts_ai(ai) if ai else ""))
        for k, v in e.attrs.items():
            if v and len(v) <= 80 and not NOT_ENTITY.match(k) and number(v) is None:
                for part in (v.split(" | ") if " | " in v else [v]):
                    c.execute("INSERT INTO entities (entry_id, type, value, norm, source) VALUES (?, ?, ?, ?, 'attr')",
                              (eid, k, part.strip(), norm(part)))
        self._runs(c, lb_id, eid, e)

    def _runs(self, c, lb_id: int, eid: int, e: elogsrc.Entry) -> None:
        for a in e.attachments:
            if not a.lower().endswith(".ini"):
                continue
            path = self.attachment_path(lb_id, a)
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    r = elogsrc.parse_run_ini(elogsrc.decode(f.read(200_000)))
            except OSError:
                continue
            if not r:
                continue
            run = r["run"]
            c.execute("""INSERT OR REPLACE INTO runs (entry_id, file, name, sample, info, experiment, start, end,
                           interrupted, params_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (eid, a, run.get("name", ""), run.get("sample", ""), run.get("info", ""),
                       run.get("experiment", ""), run.get("start time", ""), run.get("end time", ""),
                       1 if run.get("interrupted", "0") not in ("0", "", "False") else 0,
                       json.dumps(r["params"], ensure_ascii=False)))
            if run.get("sample"):
                c.execute("INSERT INTO entities (entry_id, type, value, norm, source) VALUES (?, 'Sample', ?, ?, 'attr')",
                          (eid, run["sample"], norm(run["sample"])))

    def attachment_path(self, lb_id: int, name: str) -> str | None:
        d = self.dirs.get(lb_id)
        if not d or "/" in name or "\\" in name or name.startswith("."):
            return None
        cands = [os.path.join(d, name)]
        if re.match(r"^\d{6}_", name):
            cands.insert(0, os.path.join(d, "20" + name[:2], name))
        for p in cands:
            if os.path.isfile(p):
                return p
        return None

    def _delete(self, c, ids: list[int]) -> None:
        for eid in ids:
            for t in ("chunks", "entities", "runs", "attachments_ai", "enrich"):
                c.execute(f"DELETE FROM {t} WHERE entry_id = ?", (eid,))
            c.execute("DELETE FROM entries_fts WHERE rowid = ?", (eid,))
            c.execute("DELETE FROM entries WHERE id = ?", (eid,))

    # ---- derived data --------------------------------------------------------------------------

    def _threads(self, lb_id: int) -> None:
        rows = db.all("SELECT id, mid, in_reply_to, thread_root FROM entries WHERE logbook_id = ?", lb_id)
        parent = {r["mid"]: r["in_reply_to"] for r in rows}
        updates = []
        for r in rows:
            m, guard = r["mid"], 0
            while parent.get(m) and parent[m] in parent and guard < 1000:
                m = parent[m]
                guard += 1
            if r["thread_root"] != m:
                updates.append((m, r["id"]))
        if updates:
            with db.tx() as c:
                c.executemany("UPDATE entries SET thread_root = ? WHERE id = ?", updates)

    def _numeric(self, lb_id: int) -> None:
        """Attributes that hold numbers (power, temperature, humidity ...): plotted as series."""
        seen, nums = defaultdict(int), defaultdict(int)
        for r in db.all("SELECT attrs_json FROM entries WHERE logbook_id = ?", lb_id):
            for k, v in db.jl(r[0], {}).items():
                if v and not NOT_SERIES.search(k):
                    seen[k] += 1
                    if number(v) is not None:
                        nums[k] += 1
        numeric = sorted(k for k in seen if seen[k] >= 5 and nums[k] >= 0.8 * seen[k])
        db.run("UPDATE logbooks SET numeric_json = ? WHERE id = ?", json.dumps(numeric), lb_id)

    # ---- helpers -------------------------------------------------------------------------------

    def entry_doc(self, row) -> str:
        """The text that represents an entry for embeddings and prompts."""
        attrs = db.jl(row["attrs_json"], {})
        lb = row["logbook"] if "logbook" in row.keys() else ""
        head = f"[{lb} #{row['mid']}] {row['date']}"
        a = "; ".join(f"{k}: {v}" for k, v in attrs.items() if v and k != "Author")
        parts = [head, f"Author: {row['author']}" if row["author"] else "", a,
                 f"Subject: {row['subject']}" if row["subject"] else "", row["text"] or ""]
        return "\n".join(p for p in parts if p)


def fts_ai(row) -> str:
    tags = " ".join(db.jl(row["tags_json"]))
    return " ".join(x for x in (row["summary"] or "", tags, row["english"] or "") if x)
