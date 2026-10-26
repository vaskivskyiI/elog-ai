"""Item lifecycles, alerts, handover, e-mail, reranking, run conditions, feedback notes, personal
tokens with MCP, and the evaluation runner, on a small site whose dates are relative to today."""
from __future__ import annotations

import email.utils
import json
import os
import smtplib
import time
from datetime import date

import pytest

from elogai import alerts, db, digest, evaluate, insights
from fake_ollama import Handler
from test_elogai import CLIENT_IP, FakeElogd, client, elogd, entry, make_app, ollama  # noqa: F401

DAY = 86400
NOW = int(time.time())

CFG = """[global]
port = 8000
logbook dir = logbooks
password file = passwords.pwd
admin user = alice
SMTP host = smtp.example.org
Use Email from = elog@example.org
URL = https://elog.example.org/

[Laser]
Attributes = Author, SN, MP, T, Category, Subject
Change note MP = Stage position changed from $old to $new
Empty option MP = Removed
AI alerts = 1
AI alerts to = ops@example.org, bob
AI handover = daily
AI digest = weekly
AI related = Secret

[Secret]
Attributes = Author, Subject
Login user = alice
"""

PWD = """<?xml version="1.0" encoding="ISO-8859-1"?>
<list>
  <user><name>alice</name><password>x</password><full_name>Alice Admin</full_name><email>alice@example.org</email></user>
  <user><name>bob</name><password>x</password><full_name>Bob Builder</full_name><email>bob@example.org</email><inactive>0</inactive></user>
  <user><name>carol</name><password>x</password><full_name>Carol Gone</full_name><inactive>1</inactive></user>
</list>
"""


def d(t):
    return email.utils.formatdate(t, localtime=True)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("feat"))
    os.makedirs(os.path.join(root, "logbooks", "Laser"))
    os.makedirs(os.path.join(root, "logbooks", "Secret"))
    open(os.path.join(root, "elogd.cfg"), "w").write(CFG)
    open(os.path.join(root, "passwords.pwd"), "w").write(PWD)
    log = ""
    # tip T-1: on the stage from day -10 to day -5 and again since day -2 (change notes in the text)
    for mid, (ago, text) in enumerate([(12, "New tip T-1 in the carousel"),
                                       (10, "Stage position changed from Removed to Tip"),
                                       (5, "Stage position changed from Tip to Removed"),
                                       (2, "Stage position changed from Removed to Tip")], start=1):
        log += entry(mid, d(NOW - ago * DAY), {"Author": "alice", "SN": "T-1", "MP": "Tip", "T": "",
                                               "Category": "Routine entry", "Subject": "Tip"}, f"<p>{text}</p>")
    # a daily temperature, the last one far off
    for i, ago in enumerate(range(14, 0, -1)):
        val = "19.4" if ago > 1 else "35.0"
        log += entry(10 + i, d(NOW - ago * DAY + 3600), {"Author": "alice", "SN": "", "MP": "", "T": val,
                                                       "Category": "Routine entry", "Subject": "Readings"},
                     f"<p>Temperature {val} K</p>")
    # a problem that stays open
    log += entry(40, d(NOW - 9 * DAY), {"Author": "bob", "SN": "", "MP": "", "T": "", "Category": "Problem",
                                        "Subject": "Pump broken"}, "<p>The turbo pump is broken, error on start.</p>")
    open(os.path.join(root, "logbooks", "Laser", "all.log".replace("all", "261001a")), "w").write(log)
    open(os.path.join(root, "logbooks", "Secret", "261001a.log"), "w").write(
        entry(1, d(NOW - 3 * DAY), {"Author": "alice", "Subject": "Secret plan"}, "<p>Classified tip work.</p>"))
    return root


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(scope="module")
def app(site, ollama, elogd, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(smtplib, "SMTP", FakeSMTP)
    FakeElogd.fork = True
    a = make_app(site, ollama, elogd, str(tmp_path_factory.mktemp("feat-state")))
    yield a
    FakeElogd.fork = False
    mp.undo()


def x(app):
    return app.extensions["elogai"]


# ---- lifecycle -----------------------------------------------------------------------------------

def test_lifecycle_from_change_notes(app):
    lc = insights.lifecycle(set(x(app)["ix"].logbooks.values()), "T-1", x(app)["ix"].cfg)
    mp = lc["periods"]["MP"]
    assert [p["value"] for p in mp] == ["Removed", "Tip", "Removed", "Tip"]
    assert lc["current"]["MP"]["value"] == "Tip"
    assert lc["totals"]["MP"]["Tip"] == pytest.approx(5 + 2, abs=0.2)     # 10..5 days ago, and the last 2 days
    assert "total time per MP" in insights.lifecycle_text(lc)


def test_assistant_gets_the_exact_history(app):
    r = client(app, "alice").post("/api/ask", json={"question": "How long was tip T-1 on the stage?"})
    assert r.status_code == 200 and r.data          # reading the stream runs the answer
    prompt = [b for p, b in Handler.calls if p == "/api/chat" and "You are the assistant" in json.dumps(b["messages"][0])][-1]
    assert "History of T-1" in prompt["messages"][-1]["content"]


# ---- alerts, handover, mail ------------------------------------------------------------------------

def test_alerts_once_and_mailed(app):
    FakeSMTP.sent.clear()
    assert alerts.check(x(app)["ix"]) == 2
    kinds = {a["kind"] for a in alerts.recent(set(x(app)["ix"].logbooks.values()))}
    assert kinds == {"reading", "problem"}
    msg = FakeSMTP.sent[-1]
    assert set(msg["To"].split(", ")) == {"ops@example.org", "bob@example.org"} and "2 alerts" in msg["Subject"]
    assert "https://elog.example.org/Laser/" in msg.get_body(("html",)).get_content()
    assert alerts.check(x(app)["ix"]) == 0              # not raised twice
    assert client(app, "bob").get("/api/alerts").json    # shown to readers of the logbook


def test_handover_uses_exact_state(app):
    Handler.calls.clear()
    r = client(app, "bob").post("/api/handover", json={"logbook": "Laser"}).json
    assert r["text"]
    body = [b for p, b in Handler.calls if p == "/api/chat"][-1]["messages"][-1]["content"]
    assert "Open problems" in body and "T-1: MP = Tip" in body and "Last readings" in body
    assert "Classified" not in body                      # Secret is not readable by all Laser readers
    assert client(app, "bob").get("/api/handovers").json[0]["text"]


def test_digest_mail(app):
    FakeSMTP.sent.clear()
    ai = x(app)["ai"]
    last_week = date.fromtimestamp(NOW - 7 * DAY)
    digest.get(ai, x(app)["ix"].logbooks["Laser"], "week", last_week)
    assert digest.mail_scheduled(ai) >= 1
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert any("Weekly summary" in s for s in subjects)
    assert digest.mail_scheduled(ai) == 0                # sent once


# ---- reranking, runs -------------------------------------------------------------------------------

def test_answers_are_reranked(app):
    Handler.calls.clear()
    assert client(app, "alice").post("/api/ask", json={"question": "Why is the turbo pump broken?"}).data
    assert any("You judge which" in json.dumps(b["messages"][0]) for p, b in Handler.calls if p == "/api/chat")


# ---- feedback and notes ----------------------------------------------------------------------------

def test_feedback_becomes_a_note(app):
    c = client(app, "bob")
    evs = [json.loads(l) for l in c.post("/api/ask", json={"question": "What happened to T-1?"}).data.decode().splitlines()]
    mid = next(e for e in evs if e["type"] == "done")["message_id"]
    c.post(f"/api/messages/{mid}/feedback", json={"value": -1, "comment": "MP means measuring position"})
    assert c.get("/api/admin/feedback").status_code == 403            # bob administers nothing
    alice = client(app, "alice")
    fb = alice.get("/api/admin/feedback").json
    assert fb[0]["comment"] == "MP means measuring position"
    sug = alice.post(f"/api/admin/feedback/{mid}/suggest", json={}).json
    assert sug["logbook"] == "Laser" and sug["note"]
    alice.post("/api/admin/notes", json={"logbook": "Laser", "text": "MP is the measuring position (stage)."})
    assert "MP is the measuring position" in x(app)["ai"].ctx.own("Laser")
    from elogai import enrich
    assert any(r["logbook"] == "Laser" for r in enrich.pending_enrich(x(app)["ai"], x(app)["conf"], 100))
    assert c.post("/api/admin/notes", json={"logbook": "Laser", "text": "x"}).status_code == 403


# ---- personal tokens and MCP -----------------------------------------------------------------------

def rpc(app, token, method, params=None, rid=1):
    r = app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                               headers={"Authorization": f"Bearer {token}"})
    return r


def test_tokens_and_mcp(app):
    tok_alice = client(app, "alice").post("/api/tokens", json={"name": "test"}).json["token"]
    tok_bob = client(app, "bob").post("/api/tokens", json={"name": "test"}).json["token"]
    assert app.test_client().post("/mcp", json={}).status_code == 401
    init = rpc(app, tok_alice, "initialize", {"protocolVersion": "2025-06-18"}).json
    assert init["result"]["serverInfo"]["name"] == "elog-ai"
    names = {t["name"] for t in rpc(app, tok_alice, "tools/list").json["result"]["tools"]}
    assert {"search", "ask", "get_entry", "item_history", "runs", "summary", "list_logbooks"} <= names
    call = lambda tok, name, args: rpc(app, tok, "tools/call", {"name": name, "arguments": args}).json["result"]
    assert "Secret" in call(tok_alice, "list_logbooks", {})["content"][0]["text"]
    assert "Secret" not in call(tok_bob, "list_logbooks", {})["content"][0]["text"]          # bob's access
    assert "Classified" not in call(tok_bob, "search", {"query": "classified tip work"})["content"][0]["text"]
    assert call(tok_bob, "get_entry", {"logbook": "Secret", "id": 1})["isError"]
    assert "History of T-1" in call(tok_bob, "item_history", {"item": "T-1"})["content"][0]["text"]
    assert "[Laser #" in call(tok_bob, "ask", {"question": "Why is the pump broken?"})["content"][0]["text"]
    # a token stops working when its account is deactivated
    db.run("UPDATE tokens SET login = 'carol', user = 'carol' WHERE id = (SELECT max(id) FROM tokens)")
    x(app)["auth"].cache.clear()
    assert rpc(app, tok_bob, "ping").status_code == 401


# ---- evaluation ------------------------------------------------------------------------------------

def test_eval_runner(app, tmp_path):
    q = tmp_path / "q.jsonl"
    q.write_text(json.dumps({"question": "Why is the turbo pump broken?", "expect": ["Laser #40"],
                             "keywords": ["pump"]}) + "\n")
    rep = evaluate.run(x(app)["ai"], x(app)["ix"], str(q))
    assert rep["summary"]["recall"] == 1.0 and rep["results"][0]["rank"] >= 1
    assert db.scalar("SELECT count(*) FROM conversations WHERE user = 'eval'") == 0
