"""Who is asking, and which logbooks may they read? elogd decides.

The AI pages run on the same origin as ELOG, so the browser sends ELOG's session cookie
(`sid`). We forward it to elogd and ask, per logbook, for `?jcmd=Status`: elogd answers
with JSON only if that session may read the logbook (Login user lists, guest access,
read passwords - every rule elogd knows). The answers are cached for a few minutes.
Nothing about passwords or access rules is duplicated here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import db

TTL = 120          # seconds; a changed Login user list applies within this time


@dataclass
class User:
    key: str                  # stable id for conversations: login, else full name
    name: str                 # for display
    login: str = ""
    admin: bool = False
    allowed: set[int] = field(default_factory=set)


class Auth:
    def __init__(self, conf: dict, indexer):
        self.mode = conf["auth"]
        self.ix = indexer
        self.token = conf["api_token"]
        self.extra_admins = {a.lower() for a in conf["admins"]}
        self.base = conf["elog_internal_url"]
        self.cache: dict[str, tuple[float, User | None]] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(4)
        self.pwd_cache: tuple[float, dict] = (0, {})

    def elogd(self) -> str:
        return self.base or f"http://127.0.0.1:{self.ix.cfg.port() if self.ix.cfg else 8080}"

    def all_logbooks(self) -> set[int]:
        return set(self.ix.logbooks.values())

    def identify(self, cookies: dict, headers: dict) -> User | None:
        if self.mode == "none":
            return User("local", "local", "local", True, self.all_logbooks())
        auth = headers.get("Authorization", "")
        if self.token and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token):
            return User("api", "API", "api", True, self.all_logbooks())
        if auth.startswith("Bearer eai_"):
            return self.by_token(auth[7:].strip())
        sid = cookies.get("sid", "")
        if not re.fullmatch(r"[A-Za-z0-9]{0,64}", sid):
            return None
        # elogd binds a session to the browser's address, which it takes from X-Forwarded-For:
        # pass on exactly what nginx gave us, or elogd sees 127.0.0.1 and no valid session
        fwd = (headers.get("X-Forwarded-For") or "")[:200]
        ck = hashlib.sha256(f"{sid}|{fwd}".encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(ck)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
        user = self._probe(sid, fwd)
        with self.lock:
            self.cache[ck] = (time.time(), user)
            if len(self.cache) > 2000:
                old = sorted(self.cache.items(), key=lambda x: x[1][0])[:1000]
                for k, _ in old:
                    self.cache.pop(k, None)
        return user

    # ---- elogd -----------------------------------------------------------------------------

    def _get(self, path: str, sid: str, fwd: str):
        h = {}
        if sid:
            h["Cookie"] = f"sid={sid}"
        if fwd:
            h["X-Forwarded-For"] = fwd
        req = urllib.request.Request(self.elogd() + path, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status != 200:
                    return None
                body = r.read(2_000_000)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None        # elogd answered with its login page

    def _probe(self, sid: str, fwd: str = "") -> User | None:
        cfg = self.ix.cfg
        has_passwords = bool(cfg and cfg.get_global("Password file"))
        # the F7 fork answers in one request: login and the logbooks this session may read
        acc = self._get("/?access=1", sid, fwd)
        if isinstance(acc, dict) and isinstance(acc.get("readable"), list):
            names = {n.lower() for n in acc["readable"] if isinstance(n, str)}
            allowed = {i for n, i in self.ix.logbooks.items() if n.lower() in names}
            login = acc.get("login") or ""
            name = acc.get("user") or login
        else:
            # any other elogd: ask every logbook for its status
            start = self._get("/?json=1", sid, fwd)
            name = start.get("user", "") if isinstance(start, dict) else ""

            def can(item):
                n, i = item
                d = self._get("/" + urllib.parse.quote(n) + "/?jcmd=Status", sid, fwd)
                return i if isinstance(d, dict) and "entries" in d else None

            allowed = {i for i in self.pool.map(can, list(self.ix.logbooks.items())) if i is not None}
            login = self._login_of(name) if name else ""
        if not allowed and not (login or name):
            return None
        admins = {a.lower() for a in (cfg.admins() if cfg else [])} | self.extra_admins
        admin = bool((login and login.lower() in admins) or (name and name.lower() in self.extra_admins)
                     or not has_passwords)
        key = login or name or "guest-" + hashlib.sha256(f"{sid}|{fwd}".encode()).hexdigest()[:12]
        return User(key, name or "Guest", login, admin, allowed)

    # ---- personal tokens (MCP and scripts) ----------------------------------------------------

    def by_token(self, token: str) -> User | None:
        """A personal token acts as its owner. There is no ELOG session to ask elogd about, so the
        readable logbooks follow elogd.cfg with elogd's rules (see readable_by)."""
        h = hashlib.sha256(token.encode()).hexdigest()
        row = db.get("SELECT * FROM tokens WHERE hash = ?", h)
        if not row:
            return None
        ck = "token|" + h
        with self.lock:
            hit = self.cache.get(ck)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
        login = row["login"] or ""
        users = self._accounts()
        acc = users.get(login.lower()) if login else None
        has_passwords = bool(self.ix.cfg and self.ix.cfg.get_global("Password file"))
        if has_passwords and (acc is None or acc["inactive"]):
            user = None                       # account deleted or deactivated: the token stops working
        else:
            allowed = {i for n, i in self.ix.logbooks.items() if self.readable_by(n, login)}
            admins = {a.lower() for a in (self.ix.cfg.admins() if self.ix.cfg else [])} | self.extra_admins
            user = User(row["user"], (acc or {}).get("full_name") or login, login, login.lower() in admins, allowed)
        db.run("UPDATE tokens SET last_used = ? WHERE id = ?", db.now(), row["id"])
        with self.lock:
            self.cache[ck] = (time.time(), user)
        return user

    def readable_by(self, lb: str, login: str) -> bool:
        """elogd's rule (logbook_readable in the F7 fork): no password file, guest access, or an
        account listed in "Login user" / "Admin user" (no "Login user" list: every account)."""
        cfg = self.ix.cfg
        if not cfg.get(lb, "Password file") or cfg.get(lb, "Guest menu commands"):
            return True
        if not login:
            return False
        lst = [x.strip() for x in cfg.get(lb, "Login user").split(",") if x.strip()]
        adm = [x.strip() for x in cfg.get(lb, "Admin user").split(",") if x.strip()]
        return not lst or login in lst or login in adm

    def _accounts(self) -> dict:
        cfg = self.ix.cfg
        path = cfg.get_global("Password file") if cfg else ""
        if not path:
            return {}
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(self.ix.cfg_path), path)
        try:
            with open(path, "rb") as f:
                xml = f.read().decode("utf-8", "replace")
        except OSError:
            return {}
        out = {}
        for u in re.findall(r"<user>(.*?)</user>", xml, re.S):
            tag = lambda t: (re.search(f"<{t}>(.*?)</{t}>", u, re.S) or [None, ""])[1].strip()
            if tag("name"):
                out[tag("name").lower()] = {"full_name": tag("full_name"), "inactive": tag("inactive") == "1"}
        return out

    def _login_of(self, full_name: str) -> str:
        """elogd reports the full name; the password file maps it to the login (if unique)."""
        cfg = self.ix.cfg
        path = cfg.get_global("Password file") if cfg else ""
        if not path:
            return ""
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(self.ix.cfg_path), path)
        try:
            m = os.stat(path).st_mtime
            if m != self.pwd_cache[0]:
                with open(path, "rb") as f:
                    xml = f.read().decode("utf-8", "replace")
                users = {}
                for u in re.findall(r"<user>(.*?)</user>", xml, re.S):
                    ln = re.search(r"<name>(.*?)</name>", u, re.S)
                    fn = re.search(r"<full_name>(.*?)</full_name>", u, re.S)
                    if ln:
                        users.setdefault((fn.group(1) if fn else ln.group(1)).strip().lower(), []).append(ln.group(1).strip())
                self.pwd_cache = (m, users)
        except OSError:
            return full_name if full_name in self.ix.cfg.admins() else ""
        logins = self.pwd_cache[1].get(full_name.strip().lower(), [])
        if len(logins) == 1:
            return logins[0]
        return full_name if any(full_name == l for v in self.pwd_cache[1].values() for l in v) else ""


def logbook_ids(names: list[str]) -> set[int]:
    if not names:
        return set()
    return {r[0] for r in db.all(f"SELECT id FROM logbooks WHERE name IN ({','.join('?' * len(names))})", *names)}
