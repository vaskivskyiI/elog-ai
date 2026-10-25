"""Semantic index: entry chunks embedded once, stored as float16 blobs in SQLite, searched
in memory with numpy (brute force is exact and takes milliseconds for 10^5 chunks)."""
from __future__ import annotations

import threading
import time

import numpy as np

from . import db

CHUNK = 1200
OVERLAP = 200
MAX_CHUNKS = 8


def prefixes(model: str) -> tuple[str, str]:
    """Query / document prefixes some embedding models are trained with."""
    m = model.lower()
    if "nomic" in m:
        return "search_query: ", "search_document: "
    if "e5" in m:
        return "query: ", "passage: "
    return "", ""


def chunks(doc: str) -> list[str]:
    if len(doc) <= CHUNK:
        return [doc]
    head = doc.split("\n", 1)[0][:200]      # keep the entry header with every piece
    out, i = [], 0
    while i < len(doc) and len(out) < MAX_CHUNKS:
        piece = doc[i:i + CHUNK]
        if i + CHUNK < len(doc):
            cut = max(piece.rfind("\n"), piece.rfind(". "))
            if cut > CHUNK // 2:
                piece = piece[:cut + 1]
        out.append(piece if i == 0 else head + "\n…" + piece)
        i += max(len(piece) - OVERLAP, CHUNK // 2)
    return out


def to_blob(v) -> bytes:
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    if n > 0:
        a = a / n
    return a.astype(np.float16).tobytes()


class Index:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0          # bumped by writers
        self.loaded = -1
        self.model = ""
        self.mat = np.zeros((0, 1), dtype=np.float32)
        self.entry = np.zeros(0, dtype=np.int64)
        self.logbook = np.zeros(0, dtype=np.int64)
        self.last_load = 0.0

    def touch(self):
        self.version += 1

    def _load(self, model: str):
        rows = db.all("""SELECT c.entry_id, c.vec, e.logbook_id FROM chunks c JOIN entries e ON e.id = c.entry_id
                         WHERE c.model = ? AND c.vec IS NOT NULL ORDER BY c.entry_id, c.seq""", model)
        if not rows:
            self.mat = np.zeros((0, 1), dtype=np.float32)
            self.entry = np.zeros(0, dtype=np.int64)
            self.logbook = np.zeros(0, dtype=np.int64)
        else:
            dim = len(rows[0][1]) // 2
            rows = [r for r in rows if len(r[1]) == dim * 2]
            buf = b"".join(r[1] for r in rows)
            self.mat = np.frombuffer(buf, dtype=np.float16).reshape(len(rows), dim).astype(np.float32)
            self.entry = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            self.logbook = np.fromiter((r[2] for r in rows), dtype=np.int64, count=len(rows))
        self.model = model

    def ensure(self, model: str):
        with self.lock:
            # writers bump the version per batch; reload at most every few seconds while indexing
            if (self.loaded != self.version and time.time() - self.last_load > 5) or self.model != model \
                    or self.loaded < 0:
                v = self.version
                self._load(model)
                self.loaded = v
                self.last_load = time.time()

    def search(self, qvec, model: str, logbook_ids: set[int] | None, k: int = 50,
               exclude: set[int] | None = None) -> list[tuple[int, float]]:
        """Best entries by cosine similarity (max over their chunks)."""
        self.ensure(model)
        if not len(self.entry):
            return []
        q = np.asarray(qvec, dtype=np.float32)
        q /= (np.linalg.norm(q) or 1.0)
        if q.shape[0] != self.mat.shape[1]:
            return []
        scores = self.mat @ q
        if logbook_ids is not None:
            scores = np.where(np.isin(self.logbook, list(logbook_ids)), scores, -2.0)
        n = min(len(scores), k * 4)
        top = np.argpartition(-scores, n - 1)[:n] if n < len(scores) else np.arange(len(scores))
        best: dict[int, float] = {}
        for i in top[np.argsort(-scores[top])]:
            s = float(scores[i])
            if s <= -1.5:
                break
            eid = int(self.entry[i])
            if exclude and eid in exclude:
                continue
            if eid not in best:
                best[eid] = s
                if len(best) >= k:
                    break
        return sorted(best.items(), key=lambda x: -x[1])

    def vector_of(self, entry_id: int):
        idx = np.nonzero(self.entry == entry_id)[0]
        if not len(idx):
            return None
        v = self.mat[idx].mean(axis=0)
        return v / (np.linalg.norm(v) or 1.0)


INDEX = Index()
