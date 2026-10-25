"""Background threads: scanning + embeddings, and enrichment + scheduled digests + images.
Both yield to interactive requests (see ai.Gate)."""
from __future__ import annotations

import logging
import threading
import time

from . import db, digest, enrich, providers
from .ai import AI
from .indexer import Indexer

log = logging.getLogger("elogai.worker")


class Worker:
    def __init__(self, ai: AI, ix: Indexer, conf: dict):
        self.ai, self.ix, self.conf = ai, ix, conf
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.state = {"scan": {}, "embed": "", "enrich": "", "last_scan": 0, "errors": {}}
        self.threads: list[threading.Thread] = []
        self.fails: dict[int, int] = {}

    def start(self):
        if self.conf["enrich"] == "new" and db.meta_get("enrich_new_since") is None:
            db.meta_set("enrich_new_since", db.now())
        for fn in (self._scan_loop, self._enrich_loop):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self.threads.append(t)

    def poke(self):
        self.wake.set()

    def _sleep(self, s: float):
        self.wake.wait(s)
        self.wake.clear()

    def _scan_loop(self):
        backoff = 0
        while not self.stop.is_set():
            try:
                st = self.ix.scan()
                self.state["scan"] = st
                self.state["last_scan"] = db.now()
                if st["added"] or st["updated"] or st["deleted"]:
                    log.info("scan: %s", st)
                while not self.stop.is_set():
                    self.ai.gate.wait_idle(self.stop, quiet=0.5)
                    n = enrich.embed_pending(self.ai, self.ix)
                    self.state["embed"] = f"{n} embedded" if n else "up to date"
                    if not n:
                        break
                self.state["errors"].pop("embed", None)
                backoff = 0
            except providers.ProviderError as e:
                backoff = min(600, (backoff or 15) * 2)
                self.state["errors"]["embed"] = str(e)
                log.warning("embeddings: %s (retry in %ds)", e, backoff)
                self._sleep(backoff)
                continue
            except Exception:
                log.exception("scan failed")
                self._sleep(60)
                continue
            self._sleep(self.conf["scan_interval"])

    def _enrich_loop(self):
        backoff = 0
        last_digest = 0.0
        while not self.stop.is_set():
            try:
                self.ai.gate.wait_idle(self.stop)
                rows = enrich.pending_enrich(self.ai, self.conf, self.conf["enrich_batch"] * 2) \
                    if self.ai.chat_enabled else []
                if rows:
                    n = enrich.enrich_batch(self.ai, self.ix, rows)
                    self.state["enrich"] = f"{n} analysed"
                    self.state["errors"].pop("enrich", None)
                    backoff = 0
                    continue
                self.state["enrich"] = "up to date" if self.ai.chat_enabled else "off"
                if time.time() - last_digest > 1800:
                    last_digest = time.time()
                    digest.scheduled(self.ai, self.stop)
                if enrich.describe_images(self.ai, self.ix):
                    continue
                if digest.busy_threads(self.ai, self.stop):
                    continue
            except providers.ProviderError as e:
                # one entry that always fails must not block the rest
                first = rows[0]["id"] if rows else 0
                self.fails[first] = self.fails.get(first, 0) + 1
                if first and self.fails[first] >= 3:
                    enrich._store(rows[0], None, self.ai.fast.model, error=str(e)[:300])
                    self.fails.pop(first, None)
                backoff = min(600, (backoff or 15) * 2)
                self.state["errors"]["enrich"] = str(e)
                log.warning("enrichment: %s (retry in %ds)", e, backoff)
                self._sleep(backoff)
                continue
            except Exception:
                log.exception("enrichment failed")
                self._sleep(60)
                continue
            self._sleep(15)
