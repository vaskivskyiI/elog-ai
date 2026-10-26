"""SQLite store (WAL): the entry index, AI results and caches.

Everything here can be rebuilt from the logbook files; AI results are kept as long as the
entry's content hash and the model match, so a restart or a rescan costs nothing.
Migrations are appended to MIGRATIONS and never edited once shipped.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

MIGRATIONS = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE logbooks (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, dir TEXT,
      attributes_json TEXT DEFAULT '[]', numeric_json TEXT DEFAULT '[]', active INTEGER DEFAULT 1);
    CREATE TABLE files (
      path TEXT PRIMARY KEY, logbook_id INTEGER NOT NULL, mtime_ns INTEGER, size INTEGER);
    CREATE TABLE entries (
      id INTEGER PRIMARY KEY, logbook_id INTEGER NOT NULL, mid INTEGER NOT NULL, file TEXT,
      time INTEGER, date TEXT, author TEXT, subject TEXT, attrs_json TEXT, attachments_json TEXT,
      encoding TEXT, in_reply_to INTEGER, thread_root INTEGER, text TEXT, hash TEXT,
      priority INTEGER DEFAULT 0, indexed_at INTEGER,
      UNIQUE (logbook_id, mid));
    CREATE INDEX entries_time ON entries (time);
    CREATE INDEX entries_lb_time ON entries (logbook_id, time);
    CREATE INDEX entries_file ON entries (file);
    CREATE VIRTUAL TABLE entries_fts USING fts5 (
      subject, text, attrs, ai, tokenize = 'unicode61 remove_diacritics 2');
    CREATE TABLE chunks (
      id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, seq INTEGER NOT NULL, hash TEXT,
      model TEXT, dim INTEGER, vec BLOB, UNIQUE (entry_id, seq));
    CREATE TABLE enrich (
      entry_id INTEGER PRIMARY KEY, hash TEXT, model TEXT, at INTEGER, summary TEXT, kind TEXT,
      tags_json TEXT, entities_json TEXT, measurements_json TEXT, problem_json TEXT, lang TEXT,
      english TEXT, error TEXT);
    CREATE TABLE entities (
      entry_id INTEGER NOT NULL, type TEXT NOT NULL, value TEXT NOT NULL, norm TEXT NOT NULL,
      source TEXT NOT NULL);
    CREATE INDEX entities_norm ON entities (type, norm);
    CREATE INDEX entities_entry ON entities (entry_id);
    CREATE TABLE runs (
      id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, file TEXT, name TEXT, sample TEXT,
      info TEXT, experiment TEXT, start TEXT, end TEXT, interrupted INTEGER, params_json TEXT,
      UNIQUE (entry_id, file));
    CREATE INDEX runs_sample ON runs (sample);
    CREATE TABLE attachments_ai (
      entry_id INTEGER NOT NULL, file TEXT NOT NULL, hash TEXT, model TEXT, description TEXT,
      at INTEGER, PRIMARY KEY (entry_id, file));
    CREATE TABLE digests (
      id INTEGER PRIMARY KEY, logbook_id INTEGER NOT NULL, kind TEXT NOT NULL, start INTEGER NOT NULL,
      end INTEGER NOT NULL, hash TEXT, model TEXT, text TEXT, entry_ids_json TEXT, at INTEGER,
      UNIQUE (logbook_id, kind, start));
    CREATE TABLE conversations (
      id INTEGER PRIMARY KEY, user TEXT NOT NULL, title TEXT, scope_json TEXT, created INTEGER,
      updated INTEGER);
    CREATE INDEX conversations_user ON conversations (user, updated);
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
      sources_json TEXT, feedback INTEGER, at INTEGER);
    CREATE INDEX messages_conv ON messages (conversation_id, id);
    CREATE TABLE usage (
      id INTEGER PRIMARY KEY, at INTEGER, user TEXT, task TEXT, model TEXT, tokens_in INTEGER,
      tokens_out INTEGER, ms INTEGER, ok INTEGER);
    CREATE INDEX usage_at ON usage (at);
    """,
    # per-logbook AI notes (elogd.cfg "AI context"/"AI related"): an analysis made with other notes is redone
    """
    CREATE TABLE logbook_ctx (logbook_id INTEGER PRIMARY KEY, h TEXT);
    ALTER TABLE enrich ADD COLUMN ctx TEXT;
    """,
    # alerts, mailed digests, notes from feedback, personal tokens (MCP)
    """
    CREATE TABLE alerts (
      id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, logbook_id INTEGER NOT NULL, kind TEXT, text TEXT,
      mid INTEGER, at INTEGER, sent_to TEXT);
    CREATE INDEX alerts_at ON alerts (at);
    ALTER TABLE digests ADD COLUMN mailed INTEGER;
    ALTER TABLE messages ADD COLUMN comment TEXT;
    CREATE TABLE notes (
      id INTEGER PRIMARY KEY, logbook_id INTEGER NOT NULL, text TEXT NOT NULL, created_by TEXT, at INTEGER,
      message_id INTEGER);
    CREATE TABLE tokens (
      id INTEGER PRIMARY KEY, user TEXT NOT NULL, login TEXT, name TEXT, hash TEXT NOT NULL UNIQUE,
      created INTEGER, last_used INTEGER);
    """,
]

_local = threading.local()
_path = None
_write_lock = threading.RLock()


def init(state_dir: str) -> str:
    global _path
    try:
        os.makedirs(state_dir, exist_ok=True)
        probe = os.path.join(state_dir, ".write-test")
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError as e:
        user = f"uid {os.getuid()}" if hasattr(os, "getuid") else "this user"
        raise PermissionError(
            f"cannot write the state directory {state_dir} as {user} ({e.strerror}). In Docker, make the volume "
            f"writable: docker compose run --rm -u root elog-ai chown $ELOG_UID:$ELOG_GID /state") from None
    _path = os.path.join(state_dir, "elog-ai.sqlite")
    _local.__dict__.clear()
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER)")
    row = c.execute("SELECT v FROM schema_version").fetchone()
    v = row[0] if row else 0
    if not row:
        c.execute("INSERT INTO schema_version VALUES (0)")
    for i, sql in enumerate(MIGRATIONS[v:], start=v + 1):
        with tx():
            _apply(c, sql, i)
    return _path


def _apply(c: sqlite3.Connection, sql: str, version: int) -> None:
    for stmt in _statements(sql):
        c.execute(stmt)
    c.execute("UPDATE schema_version SET v = ?", (version,))


def _statements(sql: str):
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf.strip()
            buf = ""
    if buf.strip():
        yield buf.strip()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(_path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("PRAGMA cache_size = -32000")
        c.execute("PRAGMA temp_store = MEMORY")
        _local.conn = c
    return c


class tx:
    """Write transaction; nested use joins the outer one. Writers are serialised in-process
    so that background workers never hit SQLITE_BUSY against each other."""

    def __enter__(self):
        _write_lock.acquire()
        c = conn()
        self.outer = c.in_transaction
        if not self.outer:
            c.execute("BEGIN IMMEDIATE")
        return c

    def __exit__(self, et, ev, tb):
        c = conn()
        try:
            if not self.outer:
                c.execute("ROLLBACK" if et else "COMMIT")
        finally:
            _write_lock.release()


def get(sql: str, *args):
    return conn().execute(sql, args).fetchone()


def all(sql: str, *args) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def run(sql: str, *args) -> sqlite3.Cursor:
    with tx() as c:
        return c.execute(sql, args)


def scalar(sql: str, *args, default=None):
    row = get(sql, *args)
    return row[0] if row and row[0] is not None else default


def meta_get(key: str, default=None):
    v = scalar("SELECT value FROM meta WHERE key = ?", key)
    return json.loads(v) if v is not None else default


def meta_set(key: str, value) -> None:
    run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        key, json.dumps(value))


def now() -> int:
    return int(time.time())


def jl(s, default=None):
    """Load a *_json column."""
    if s is None or s == "":
        return [] if default is None else default
    try:
        return json.loads(s)
    except ValueError:
        return [] if default is None else default
