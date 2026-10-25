"""The configured providers, usage accounting, privacy rules and the interactive/background gate."""
from __future__ import annotations

import contextlib
import threading
import time

from . import db, providers

_user = threading.local()


@contextlib.contextmanager
def acting_user(name: str):
    old = getattr(_user, "name", "")
    _user.name = name
    try:
        yield
    finally:
        _user.name = old


def _record(u: dict) -> None:
    try:
        db.run("INSERT INTO usage (at, user, task, model, tokens_in, tokens_out, ms, ok) VALUES (?,?,?,?,?,?,?,?)",
               db.now(), getattr(_user, "name", "") or "", u["task"], u["model"], u["tokens_in"],
               u["tokens_out"], u["ms"], int(u["ok"]))
    except Exception:
        pass


class Gate:
    """Background work (enrichment, digests) waits while people are waiting for an answer:
    on a CPU-only Ollama one request at a time is all the machine can do."""

    def __init__(self):
        self.active = 0
        self.cv = threading.Condition()

    @contextlib.contextmanager
    def interactive(self):
        with self.cv:
            self.active += 1
        try:
            yield
        finally:
            with self.cv:
                self.active -= 1
                self.cv.notify_all()

    def wait_idle(self, stop: threading.Event | None = None, quiet: float = 2.0) -> None:
        """Block until no interactive request ran for `quiet` seconds."""
        while not (stop and stop.is_set()):
            with self.cv:
                if self.active == 0:
                    self.cv.wait(quiet)
                    if self.active == 0:
                        return
                else:
                    self.cv.wait(1.0)


class AI:
    def __init__(self, conf: dict):
        self.conf = conf
        c = conf["chat"]
        t = conf["timeout"]
        self.chat = providers.make(c, "model", t, _record)
        self.fast = providers.make(c, "fast_model", t, _record)
        self.vision = providers.make(c, "vision_model", t, _record) if c.get("vision_model") else None
        self.embedder = providers.make(conf["embed"], "model", t, _record)
        self.embed_model = conf["embed"]["model"] if conf["embed"]["provider"] != "none" else ""
        self.private = {x.lower() for x in conf["private_logbooks"]}
        self.gate = Gate()
        self.language = conf["language"]
        self.health: dict = {}

    @property
    def chat_enabled(self) -> bool:
        return self.chat.kind != "none" and bool(self.chat.model)

    @property
    def embed_enabled(self) -> bool:
        return self.embedder.kind != "none" and bool(self.embed_model)

    def may_send(self, logbooks, provider: providers.Provider | None = None) -> bool:
        """False if a private logbook's text would go to a provider outside the server."""
        p = provider or self.chat
        return p.local or not any((lb or "").lower() in self.private for lb in logbooks)

    def check(self) -> dict:
        """Reachability of the providers, for the status page."""
        out = {}
        for name, p in (("chat", self.chat), ("embed", self.embedder)):
            if p.kind == "none":
                out[name] = {"ok": False, "error": "not configured"}
                continue
            t0 = time.time()
            try:
                models = p.models()
                ok = not models or any(m == p.model or m.split(":")[0] == p.model or m == p.model + ":latest"
                                       for m in models)
                out[name] = {"ok": True, "model": p.model, "model_available": ok, "ms": int((time.time() - t0) * 1000)}
            except providers.ProviderError as e:
                out[name] = {"ok": False, "model": p.model, "error": str(e)}
        self.health = out
        return out
