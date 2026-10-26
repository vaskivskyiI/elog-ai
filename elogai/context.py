"""Per-logbook instructions for the AI and relations between logbooks, set in elogd.cfg:

    [Nanoprobe]
    AI context = Moving a sample or tip between carousel positions is routine bookkeeping: do not
                 mention it. What matters: moves to or from the measuring stage (MP), measurement
                 conditions (temperature, field, bias, setpoint), tip changes and conditioning.
    AI related = np*

    [np005]
    AI context = pages/np-ai.txt          # a .txt/.md file (relative to the logbook dir) for long text

`AI context` in [global] applies to every logbook. `AI related` takes logbook names and
wildcards and works both ways: np005 related to Nanoprobe makes Nanoprobe related to np005.

Related logbooks are used in two ways:
* per user (answers, the entry panel): entries of related logbooks the *user* may read;
* in shared results (entry analysis, digests), which every reader of a logbook sees: only
  related logbooks that everybody who can read this one can read as well (`safe`), so nothing
  moves into a logbook with a wider audience.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re

from . import db

FILE_REF = re.compile(r"^[\w./ -]+\.(txt|md)$", re.I)
MAX_TEXT = 4000


class Contexts:
    def __init__(self, indexer):
        self.ix = indexer
        self.loaded = None          # cfg mtime the caches belong to
        self._related: dict[str, list[str]] = {}
        self._readers: dict[str, frozenset | None] = {}

    # ---- configuration ---------------------------------------------------------------------

    def _refresh(self):
        cfg = self.ix.cfg
        if cfg is None or self.loaded == self.ix.cfg_mtime:
            return
        names = [n for n in cfg.logbooks() if n in self.ix.logbooks]
        rel: dict[str, set[str]] = {n: set() for n in names}
        for n in names:
            own = cfg.sections.get(n.lower(), {}).get("ai related", "")
            for pat in (p.strip() for p in own.split(",")):
                if not pat:
                    continue
                for m in names:
                    if m != n and fnmatch.fnmatchcase(m.lower(), pat.lower()):
                        rel[n].add(m)
                        rel[m].add(n)
        self._related = {n: sorted(v, key=str.lower) for n, v in rel.items()}
        self._readers = {n: self._readers_of(n) for n in names}
        self.loaded = self.ix.cfg_mtime
        self._store_versions(names)

    def _readers_of(self, lb: str):
        """Accounts that may read the logbook, None for everybody (as far as elogd.cfg tells)."""
        cfg = self.ix.cfg
        if not cfg.get(lb, "Password file") or cfg.get(lb, "Guest menu commands"):
            return None
        login = [x.strip() for x in cfg.get(lb, "Login user").split(",") if x.strip()]
        if not login:
            return None
        admins = [x.strip() for x in cfg.get(lb, "Admin user").split(",") if x.strip()]
        return frozenset(login + admins)

    def _read(self, value: str) -> str:
        value = (value or "").strip()
        if FILE_REF.match(value):
            path = value if os.path.isabs(value) else os.path.join(self.ix.cfg.logbook_dir(self.ix.cfg_path), value)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read(MAX_TEXT).strip()
            except OSError:
                return ""
        return value[:MAX_TEXT]

    # ---- queries ---------------------------------------------------------------------------

    def own(self, lb: str) -> str:
        """`AI context` of the logbook plus the notes admins added from feedback (stored here)."""
        self._refresh()
        cfg = self.ix.cfg
        text = self._read(cfg.sections.get(lb.lower(), {}).get("ai context", "")) if cfg else ""
        lid = self.ix.logbooks.get(lb)
        learned = [r[0] for r in db.all("SELECT text FROM notes WHERE logbook_id = ? ORDER BY id", lid)] if lid else []
        return " ".join([t for t in [text] + learned if t])[:MAX_TEXT * 2]

    def site(self) -> str:
        cfg = self.ix.cfg
        return self._read(cfg.get_global("AI context")) if cfg else ""

    def related(self, lb: str) -> list[str]:
        self._refresh()
        return self._related.get(lb, [])

    def safe(self, lb: str, other: str) -> bool:
        """May results that every reader of `lb` sees contain text from `other`?"""
        self._refresh()
        r_lb, r_other = self._readers.get(lb), self._readers.get(other)
        if r_other is None:
            return True
        return r_lb is not None and r_lb <= r_other

    def shared_related(self, lb: str) -> list[str]:
        return [r for r in self.related(lb) if self.safe(lb, r)]

    def notes(self, logbooks, extra=()) -> str:
        """The instructions for a prompt: the site's, then those of each logbook involved."""
        lines = []
        site = self.site()
        if site:
            lines.append(f"- all logbooks: {site}")
        seen = set()
        for lb in list(logbooks) + list(extra):
            if lb and lb not in seen:
                seen.add(lb)
                t = self.own(lb)
                if t:
                    lines.append(f"- {lb}: {t}")
        if not lines:
            return ""
        return "\n\nNotes from the logbook administrators (follow them; they say what matters and what is routine):\n" \
            + "\n".join(lines) + "\n"

    def relations_note(self, logbooks) -> str:
        pairs = [f"{lb} ↔ {', '.join(self.related(lb))}" for lb in logbooks if self.related(lb)]
        return ("\nRelated logbooks (e.g. an instrument and the experiments done with it): " + "; ".join(pairs) + "\n") \
            if pairs else ""

    # ---- versions: analysis is redone when what it was made with changes --------------------

    def enrich_notes(self, lb: str) -> str:
        return self.notes([lb], self.shared_related(lb)) + self.relations_note([lb])

    def version(self, lb: str) -> str:
        return hashlib.sha1(self.enrich_notes(lb).encode()).hexdigest()[:16]

    def refresh(self, force: bool = False):
        if force:
            self.loaded = None
        self._refresh()

    def _store_versions(self, names):
        rows = [(self.ix.logbooks[n], self.version(n)) for n in names]
        with db.tx() as c:
            c.execute("DELETE FROM logbook_ctx")
            c.executemany("INSERT INTO logbook_ctx (logbook_id, h) VALUES (?, ?)", rows)
