"""Read-only access to ELOG's data: elogd.cfg, the logbook files and run descriptions.

elogd stores each logbook as one text file per day (`[YYYY/]yymmdda.log`) holding entries
separated by `$@MID@$: <id>` lines. We parse these files directly instead of going through
elogd's HTTP interface: it needs no credentials, sees attachments, and a changed file is
the only thing to re-read.
"""
from __future__ import annotations

import configparser
import email.utils
import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

SYSTEM_FIELDS = {"date", "reply to", "in reply to", "attachment", "encoding", "locked by", "draft"}
SEPARATOR = "=" * 40
LOG_FILE = re.compile(r"^\d{6}a\.log$")
MID = re.compile(r"^\$@MID@\$: *(\d+)[ \t]*\r?$", re.M)


# ---- elogd.cfg ---------------------------------------------------------------------------------

class ElogConfig:
    """Just enough of elogd.cfg: sections, case-insensitive keys, [global] fallback."""

    def __init__(self, text: str):
        self.sections: dict[str, dict[str, str]] = {}
        self.order: list[str] = []
        cur = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] in ";#":
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                cur = m.group(1).strip()
                if cur.lower() not in self.sections:
                    self.order.append(cur)
                self.sections.setdefault(cur.lower(), {})
                continue
            if cur is None or "=" not in line:
                continue
            k, v = line.split("=", 1)
            self.sections[cur.lower()][k.strip().lower()] = v.strip()

    @classmethod
    def load(cls, path: str) -> "ElogConfig":
        with open(path, "rb") as f:
            raw = f.read()
        return cls(decode(raw))

    def get(self, section: str, key: str, default: str = "") -> str:
        s = self.sections.get(section.lower(), {})
        if key.lower() in s:
            return s[key.lower()]
        return self.sections.get("global", {}).get(key.lower(), default)

    def get_global(self, key: str, default: str = "") -> str:
        return self.sections.get("global", {}).get(key.lower(), default)

    def logbooks(self) -> list[str]:
        return [n for n in self.order if n.lower() != "global" and not n.lower().startswith("global ")]

    def logbook_dir(self, cfg_path: str) -> str:
        d = self.get_global("Logbook dir", "logbooks")
        if not os.path.isabs(d):
            d = os.path.join(os.path.dirname(cfg_path), d)
        return d

    def data_dir(self, logbook: str, cfg_path: str) -> str:
        base = self.logbook_dir(cfg_path)
        sub = self.sections.get(logbook.lower(), {}).get("subdir", "") or logbook
        return sub if os.path.isabs(sub) else os.path.join(base, sub)

    def attributes(self, logbook: str) -> list[str]:
        return [a.strip() for a in self.get(logbook, "Attributes").split(",") if a.strip()]

    def admins(self) -> list[str]:
        return [a.strip() for a in self.get_global("Admin user").split(",") if a.strip()]

    def groups(self) -> dict[str, list[str]]:
        out = {}
        for k, v in self.sections.get("global", {}).items():
            if k.startswith("group "):
                out[k[6:].strip()] = [x.strip() for x in v.split(",") if x.strip()]
        return out

    def port(self) -> int:
        try:
            return int(self.get_global("Port", "8080"))
        except ValueError:
            return 8080


def decode(raw: bytes) -> str:
    # elogd writes whatever charset the browser sent; old files are ISO-8859-1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# ---- logbook files -----------------------------------------------------------------------------

@dataclass
class Entry:
    mid: int
    date: str = ""
    time: int = 0
    reply_to: list[int] = field(default_factory=list)
    in_reply_to: int = 0
    attrs: dict[str, str] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    encoding: str = "plain"
    draft: bool = False
    body: str = ""            # as stored (HTML, ELCode or plain)
    text: str = ""            # plain text without quoted replies

    @property
    def subject(self) -> str:
        return self.attrs.get("Subject", "")

    @property
    def author(self) -> str:
        return self.attrs.get("Author", "")

    def content_hash(self) -> str:
        h = hashlib.sha1()
        for part in (self.date, str(self.in_reply_to), ",".join(map(str, self.reply_to)),
                     repr(sorted(self.attrs.items())), ",".join(self.attachments), self.body):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\0")
        return h.hexdigest()


def log_files(data_dir: str) -> list[str]:
    """All day files of a logbook: <dir>/yymmdda.log and <dir>/<YYYY>/yymmdda.log."""
    out = []
    try:
        top = list(os.scandir(data_dir))
    except OSError:
        return out
    for d in top:
        if d.is_file() and LOG_FILE.match(d.name):
            out.append(d.path)
        elif d.is_dir() and re.fullmatch(r"\d{4}", d.name):
            try:
                out.extend(f.path for f in os.scandir(d.path) if f.is_file() and LOG_FILE.match(f.name))
            except OSError:
                pass
    return sorted(out)


def parse_log(text: str) -> list[Entry]:
    entries = []
    marks = list(MID.finditer(text))
    for i, m in enumerate(marks):
        chunk = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        e = Entry(mid=int(m.group(1)))
        head, sep, body = chunk.partition(SEPARATOR)
        if not sep:
            head, body = chunk, ""
        for line in head.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            lk = k.lower()
            if lk == "date":
                e.date = v
                e.time = parse_date(v)
            elif lk == "reply to":
                e.reply_to = [int(x) for x in re.findall(r"\d+", v)]
            elif lk == "in reply to":
                e.in_reply_to = int(v) if v.isdigit() else 0
            elif lk == "attachment":
                e.attachments = [a.strip() for a in v.split(",") if a.strip()]
            elif lk == "encoding":
                e.encoding = v.lower() or "plain"
            elif lk == "draft":
                e.draft = bool(v)
            elif lk not in SYSTEM_FIELDS:
                e.attrs[k] = v
        e.body = body.lstrip("\r\n").rstrip()
        e.text = to_text(e.body, e.encoding)
        entries.append(e)
    return entries


def read_log(path: str) -> list[Entry]:
    with open(path, "rb") as f:
        return parse_log(decode(f.read()))


def parse_date(s: str) -> int:
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError, IndexError):
        return 0


# ---- entry text --------------------------------------------------------------------------------

BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
         "table", "ul", "ol", "hr", "dt", "dd", "section", "details", "summary", "figure"}
SKIP = {"script", "style", "head", "title", "math", "annotation"}


class _Text(HTMLParser):
    """HTML to plain text. Quoted replies (`table.reply_table`) are dropped: they repeat the
    parent entry and would make every reply look like its whole thread."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0           # depth inside SKIP elements
        self.quote = 0          # depth of <table> inside a reply table, 0 = not quoting

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.quote:
            if tag == "table":
                self.quote += 1
            return
        if tag == "table" and "reply_table" in (a.get("class") or ""):
            self.quote = 1
            return
        if tag in SKIP:
            self.skip += 1
        elif tag in BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            self.out.append(f" [image{': ' + alt if alt else ''}] ")
        elif tag == "annotation":
            self.skip += 1

    def handle_endtag(self, tag):
        if self.quote:
            if tag == "table":
                self.quote -= 1
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK and tag not in ("li", "tr", "dt", "dd"):
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.skip and not self.quote:
            self.out.append(data)


def to_text(body: str, encoding: str = "html") -> str:
    if not body:
        return ""
    if encoding.startswith("html"):
        p = _Text()
        try:
            p.feed(body)
            p.close()
        except Exception:       # malformed markup: fall back to tag stripping
            p.out = [re.sub(r"<[^>]+>", " ", html.unescape(body))]
        s = "".join(p.out)
    elif encoding == "elcode":
        s = re.sub(r"\[quote[^\]]*\].*?\[/quote\]", "", body, flags=re.S | re.I)
        s = re.sub(r"\[/?[a-z*]+(=[^\]]*)?\]", "", s, flags=re.I)
    else:
        s = re.sub(r"(?m)^>.*$", "", body)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]*\|[ \t]*\n", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---- run descriptions (DAQ .ini attachments) ---------------------------------------------------

NOISE_SECTIONS = re.compile(r"(_general|_comm|_serial)$", re.I)
NOISE_KEYS = {"port", "baudrate", "databits", "stopbits", "parity", "serialno", "channel", "timeout",
              "minpulsewidth", "stepsperunit", "limitswitches", "maxspeed", "acceleration", "velocity",
              "readinterval", "address", "ip", "host"}


def parse_run_ini(text: str) -> dict | None:
    """A DAQ run description: `[run]` with name/sample/info and one section per device.

    Returns {"run": {...}, "params": {"Section.key": value}} without connection settings."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    if not cp.has_section("run"):
        return None
    run = dict(cp.items("run"))
    params = {}
    for sec in cp.sections():
        if sec == "run" or NOISE_SECTIONS.search(sec):
            continue
        alias = cp.get(sec, "alias", fallback="") or sec
        for k, v in cp.items(sec):
            if k.lower() in NOISE_KEYS or k == "alias" or k == "name" or v in ("", "None"):
                continue
            params[f"{alias}.{k}"] = v[:200]
    return {"run": run, "params": params}
