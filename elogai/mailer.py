"""E-mail through the SMTP server elogd already uses (elogd.cfg [global]: SMTP host, SMTP port,
SMTP username, SMTP password, Use Email from), for alerts, handovers and AI digests."""
from __future__ import annotations

import html
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote

log = logging.getLogger("elogai.mail")


def enabled(cfg) -> bool:
    return bool(cfg and cfg.get_global("SMTP host"))


def send(cfg, to: list[str], subject: str, markdown: str) -> bool:
    to = [t for t in dict.fromkeys(a.strip() for a in to) if "@" in t]
    if not to or not enabled(cfg):
        return False
    g = cfg.get_global
    sender = os.environ.get("ELOGAI_MAIL_FROM") or g("Use Email from") or "elog-ai@localhost"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid("elog-ai")
    msg.set_content(markdown)
    msg.add_alternative(to_html(markdown, g("URL")), subtype="html")
    try:
        port = int(g("SMTP port") or 25)
        with smtplib.SMTP(g("SMTP host"), port, timeout=30) as s:
            if port == 587:
                s.starttls()
            if g("SMTP username"):
                s.login(g("SMTP username"), g("SMTP password"))
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        log.warning("mail to %s failed: %s", to, e)
        return False


def to_html(md: str, url: str = "") -> str:
    """Minimal Markdown to HTML; [Logbook #12] becomes a link to the entry."""
    base = (url or "").rstrip("/") + "/"
    out, in_list = [], False
    for line in md.splitlines():
        s = html.escape(line)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[([^\]\[#]+?) #(\d+)\]",
                   lambda m: f'<a href="{base}{quote(html.unescape(m.group(1)))}/{m.group(2)}">'
                             f'{m.group(1)} #{m.group(2)}</a>', s)
        m = re.match(r"^\s*[-*•]\s+(.*)$", s)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if s.strip():
            out.append(f"<p>{s}</p>")
    if in_list:
        out.append("</ul>")
    return "<div style=\"font-family: sans-serif; font-size: 14px\">" + "\n".join(out) + "</div>"


def emails_of(ix, logins: list[str]) -> list[str]:
    """E-mail addresses of ELOG accounts, from the password file."""
    cfg = ix.cfg
    path = cfg.get_global("Password file") if cfg else ""
    if not path:
        return []
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(ix.cfg_path), path)
    try:
        with open(path, "rb") as f:
            xml = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    want = {l.lower() for l in logins}
    out = []
    for u in re.findall(r"<user>(.*?)</user>", xml, re.S):
        name = re.search(r"<name>(.*?)</name>", u, re.S)
        mail = re.search(r"<email>(.*?)</email>", u, re.S)
        if name and mail and name.group(1).strip().lower() in want and "@" in mail.group(1):
            out.append(mail.group(1).strip())
    return out


def recipients(ix, lb: str, key: str, fallback: str = "") -> list[str]:
    """Addresses from `<key>` of the logbook (addresses or ELOG logins), else from `fallback`
    (e.g. elogd's own "Digest to"), else the logbook's admins."""
    cfg = ix.cfg
    raw = cfg.get(lb, key) or (cfg.get(lb, fallback) if fallback else "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        items = [x.strip() for x in cfg.get(lb, "Admin user").split(",") if x.strip()]
    mails = [x for x in items if "@" in x]
    return mails + emails_of(ix, [x for x in items if "@" not in x])
