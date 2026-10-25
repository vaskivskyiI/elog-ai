"""Tests on a small fictional ELOG installation, with the fake Ollama for the AI parts and a
fake elogd for the access checks."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_ollama  # noqa: E402
from elogai import assistant, db, elogsrc, enrich, indexer, providers, search, settings  # noqa: E402
from elogai.app import create_app  # noqa: E402

CFG = """[global]
port = 8000
logbook dir = logbooks
password file = passwords.pwd
admin user = alice
Group Lasers = Laser, Secret

[Laser]
Attributes = Author, Category, Power, T, Sample, Subject
Options Category = Info, Problem, Fix, Routine entry

[Secret]
Subdir = secret-data
Attributes = Author, Subject
Login user = alice
"""

PWD = """<?xml version="1.0" encoding="ISO-8859-1"?>
<list>
  <user><name>alice</name><password>x</password><full_name>Alice Admin</full_name></user>
  <user><name>bob</name><password>x</password><full_name>Bob Builder</full_name></user>
</list>
"""


def entry(mid, date_, attrs, body, reply_to=None, in_reply_to=0, attachments="", encoding="HTML", draft=""):
    head = [f"$@MID@$: {mid}", f"Date: {date_}"]
    if reply_to:
        head.append(f"Reply to: {reply_to}")
    if in_reply_to:
        head.append(f"In reply to: {in_reply_to}")
    if draft:
        head.append(f"Draft: {draft}")
    head += [f"{k}: {v}" for k, v in attrs.items()]
    head += [f"Attachment: {attachments}", f"Encoding: {encoding}", "=" * 40]
    return "\n".join(head) + "\n" + body + "\n"


def write_site(root):
    os.makedirs(os.path.join(root, "logbooks", "Laser", "2026"), exist_ok=True)
    os.makedirs(os.path.join(root, "logbooks", "secret-data", "2026"), exist_ok=True)
    with open(os.path.join(root, "elogd.cfg"), "w") as f:
        f.write(CFG)
    with open(os.path.join(root, "passwords.pwd"), "w") as f:
        f.write(PWD)
    laser = (
        entry(1, "Mon, 05 Oct 2026 09:00:00 +0200", {"Author": "alice", "Category": "Info", "Power": "2.85 W", "T": "19.4",
                                                     "Sample": "S-179", "Subject": "Morning check"},
              "<p>Legend power 2.85 W after warm-up, compressor fine.</p>", attachments="261005_090000_run.ini")
        + entry(2, "Mon, 05 Oct 2026 14:00:00 +0200", {"Author": "bob", "Category": "Problem", "Power": "1.2 W", "T": "19.6",
                                                       "Sample": "", "Subject": "Power drop"},
                "<p>Power dropped to 1.2 W, compressor misaligned.</p>", reply_to="3")
        + entry(3, "Mon, 05 Oct 2026 16:00:00 +0200", {"Author": "alice", "Category": "Fix", "Power": "2.8 W", "T": "19.5",
                                                       "Sample": "", "Subject": "Re: Power drop"},
                "<p>Realigned the compressor, 2.8 W again.</p><table class=\"reply_table\"><tr><td>bob wrote:"
                "<p>Power dropped to 1.2 W, compressor misaligned.</p></td></tr></table>", in_reply_to=2)
        + entry(4, "Tue, 06 Oct 2026 10:00:00 +0200", {"Author": "bob", "Category": "Routine entry", "Power": "2.9", "T": "19.3",
                                                       "Sample": "S-179", "Subject": "Draft"}, "<p>not finished</p>", draft="bob")
    )
    for i, p in enumerate(["2.86", "2.84", "2.87", "2.85", "2.83", "2.86", "2.85", "2.84", "2.86", "9.9"]):
        laser += entry(10 + i, f"Wed, {7 + i:02d} Oct 2026 08:00:00 +0200",
                       {"Author": "alice", "Category": "Routine entry", "Power": p, "T": "19", "Sample": "", "Subject": ""},
                       "<p>ok</p>")
    with open(os.path.join(root, "logbooks", "Laser", "2026", "261005a.log"), "w") as f:
        f.write(laser)
    with open(os.path.join(root, "logbooks", "Laser", "2026", "261005_090000_run.ini"), "w") as f:
        f.write("[run]\nname = 905\nsample = S-179\ninfo = pump probe 300K\nstart time = 2026-10-05 09:00\n"
                "end time = 2026-10-05 12:00\ninterrupted = 0\n\n[ThorlabsMotor_DelayLine]\nalias = DelayLine\n"
                "serialno = 123\nposition = 12.5\n\n[Motor_general]\nport = COM3\n")
    with open(os.path.join(root, "logbooks", "secret-data", "2026", "261005a.log"), "w") as f:
        f.write(entry(1, "Mon, 05 Oct 2026 11:00:00 +0200", {"Author": "alice", "Subject": "Secret compressor plan"},
                      "<p>The secret compressor upgrade is planned.</p>")
                # the things below would show up in Laser views if access filtering leaked
                + entry(2, "Mon, 05 Oct 2026 12:00:00 +0200", {"Author": "alice", "Subject": "Sample S-179 cut"},
                        "<p>Cut S-179 for the classified run, compressor broken afterwards.</p>",
                        reply_to="3", attachments="261005_120000_run.ini")
                + entry(3, "Mon, 05 Oct 2026 13:00:00 +0200", {"Author": "alice", "Subject": "Re: Sample S-179 cut"},
                        "<p>Still broken.</p>", in_reply_to=2, reply_to="4")
                + entry(4, "Mon, 05 Oct 2026 14:00:00 +0200", {"Author": "alice", "Subject": "Re: Sample S-179 cut"},
                        "<p>Fixed by replacing the pump.</p>", in_reply_to=3))
    with open(os.path.join(root, "logbooks", "secret-data", "2026", "261005_120000_run.ini"), "w") as f:
        f.write("[run]\nname = 777\nsample = S-179\ninfo = classified\n")


CLIENT_IP = "10.0.0.5"


class FakeElogd(BaseHTTPRequestHandler):
    """sid=alice may read both logbooks, sid=bob only Laser, no sid nothing (login page).
    Like elogd, a session is only valid from the address it was created from (X-Forwarded-For).
    `fork` switches the one-request access endpoint of the F7 fork on."""
    fork = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        sid = (self.headers.get("Cookie") or "").replace("sid=", "")
        if self.headers.get("X-Forwarded-For") != CLIENT_IP:
            sid = ""
        user = {"alice": "Alice Admin", "bob": "Bob Builder"}.get(sid)
        path = unquote(self.path)
        if path == "/?access=1" and FakeElogd.fork:
            return self._send({"login": sid if user else "", "user": user or "",
                               "readable": (["Laser", "Secret"] if sid == "alice" else ["Laser"]) if user else []})
        if path == "/?json=1" and user:
            return self._send({"user": user, "tree": []})
        if path.endswith("/?jcmd=Status") and user:
            lb = path.split("/")[1]
            if lb == "Laser" or (lb == "Secret" and sid == "alice"):
                return self._send({"entries": 3, "last_id": 3, "stamp": "x"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>login</html>")

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("elog"))
    write_site(root)
    return root


@pytest.fixture(scope="module")
def ollama():
    srv, url = fake_ollama.start()
    yield url
    srv.shutdown()


@pytest.fixture(scope="module")
def elogd():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeElogd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_app(site, ollama, elogd, state, analyse=True, **env):
    base = {"ELOGD_CFG": os.path.join(site, "elogd.cfg"), "ELOGAI_STATE_DIR": state, "ELOG_INTERNAL_URL": elogd,
            "AI_PROVIDER": "ollama", "AI_BASE_URL": ollama, "AI_CHAT_MODEL": "fake", "AI_FAST_MODEL": "fake",
            "AI_EMBED_MODEL": "fake-embed", "ELOGAI_AUTH": "elogd"}
    base.update(env)
    old = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        conf = settings.from_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    app = create_app(conf, start_worker=False)
    x = app.extensions["elogai"]
    x["ix"].scan()
    while enrich.embed_pending(x["ai"], x["ix"], 100):
        pass
    while analyse:
        rows = enrich.pending_enrich(x["ai"], conf, 10)
        if not rows:
            break
        enrich.enrich_batch(x["ai"], x["ix"], rows)
    return app


@pytest.fixture(scope="module")
def app(site, ollama, elogd, tmp_path_factory):
    return make_app(site, ollama, elogd, str(tmp_path_factory.mktemp("state")))


def client(app, sid=None, ip=CLIENT_IP):
    c = app.test_client()
    if sid:
        c.set_cookie("sid", sid)
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "elog-ai"
    c.environ_base["HTTP_X_FORWARDED_FOR"] = ip
    return c


# ---- parsing -------------------------------------------------------------------------------------

def test_parse_entries_quotes_and_drafts(site):
    es = elogsrc.read_log(os.path.join(site, "logbooks", "Laser", "2026", "261005a.log"))
    by = {e.mid: e for e in es}
    assert by[3].in_reply_to == 2 and by[2].reply_to == [3]
    assert "Realigned the compressor" in by[3].text and "dropped" not in by[3].text     # quoted reply removed
    assert by[4].draft
    assert by[1].attrs["Power"] == "2.85 W" and by[1].attachments == ["261005_090000_run.ini"]
    assert by[1].time > 0


def test_html_to_text():
    t = elogsrc.to_text("<p>a&nbsp;b</p><ul><li>x</li><li>y</li></ul><img src='q.png' alt='plot'><script>x()</script>")
    assert t == "a b\n\nx\ny\n[image: plot]"


def test_run_ini():
    r = elogsrc.parse_run_ini("[run]\nname = 7\nsample = A\n[Dev]\nalias = D\nport = COM1\nposition = 3\n")
    assert r["run"]["name"] == "7" and r["params"] == {"D.position": "3"}


def test_config_subdir(site):
    cfg = elogsrc.ElogConfig.load(os.path.join(site, "elogd.cfg"))
    assert cfg.logbooks() == ["Laser", "Secret"]
    assert cfg.data_dir("Secret", os.path.join(site, "elogd.cfg")).endswith("secret-data")
    assert cfg.groups() == {"lasers": ["Laser", "Secret"]}


def test_json_from_chatty_models():
    assert providers.parse_json('Sure! ```json\n{"a": [1, {"b": "}"}]}\n``` done') == {"a": [1, {"b": "}"}]}
    assert providers.parse_json("<think>{no}</think>{\"x\": 1}") == {"x": 1}
    with pytest.raises(ValueError):
        providers.parse_json("nothing here")


def test_think_filter_streaming():
    f = providers._ThinkFilter()
    out = "".join(f.feed(p) for p in ["<thi", "nk>secret", "</think>", " Hello", " world"])
    assert out == "Hello world"
    f = providers._ThinkFilter()
    assert "".join(f.feed(p) for p in ["Hi", " there"]) == "Hi there"


def test_time_range():
    t = date(2026, 10, 25)
    assert assistant.time_range("what happened last week", t) == ("2026-10-12", "2026-10-18")
    assert assistant.time_range("power in March 2025?", t) == ("2025-03-01", "2025-03-31")
    assert assistant.time_range("last 3 days", t) == ("2026-10-22", None)
    assert assistant.time_range("how is the laser", t) == (None, None)


def test_fts_query():
    assert search.fts_query("S-179 compressor") == '"S 179" OR "compressor"*'
    assert search.fts_query('the "exact phrase"') == '"exact phrase"'


# ---- index -------------------------------------------------------------------------------------

def test_index_incremental(app, site):
    ix = app.extensions["elogai"]["ix"]
    assert db.scalar("SELECT count(*) FROM entries") == 17        # draft left out
    assert ix.scan()["changed"] == 0
    path = os.path.join(site, "logbooks", "Laser", "2026", "261005a.log")
    text = open(path).read()
    with open(path, "w") as f:
        f.write(text.replace("Realigned the compressor", "Realigned the compressor mirror"))
    os.utime(path, (time.time() + 5, time.time() + 5))
    st = ix.scan()
    assert st["updated"] == 1 and st["added"] == 0
    with open(path, "w") as f:
        f.write(text)
    os.utime(path, (time.time() + 10, time.time() + 10))
    ix.scan()


def test_derived_data(app):
    assert db.jl(db.scalar("SELECT numeric_json FROM logbooks WHERE name = 'Laser'")) == ["Power", "T"]
    assert db.scalar("SELECT thread_root FROM entries WHERE mid = 3 AND logbook_id = "
                     "(SELECT id FROM logbooks WHERE name = 'Laser')") == 2
    run = db.get("SELECT * FROM runs")
    assert run["name"] == "905" and json.loads(run["params_json"]) == {"DelayLine.position": "12.5"}
    assert db.scalar("SELECT count(*) FROM entities WHERE norm = 's179'") >= 2


# ---- access --------------------------------------------------------------------------------------

def test_requires_elog_login(app):
    r = client(app).get("/api/me")
    assert r.status_code == 401 and r.json["login"]


def test_access_follows_elogd(app):
    bob = client(app, "bob").get("/api/me").json
    assert [l["name"] for l in bob["logbooks"]] == ["Laser"] and not bob["admin"] and bob["login"] == "bob"
    alice = client(app, "alice").get("/api/me").json
    assert {l["name"] for l in alice["logbooks"]} == {"Laser", "Secret"} and alice["admin"]
    res = client(app, "bob").get("/api/search?q=secret compressor").json["results"]
    assert all(r["logbook"] == "Laser" for r in res)
    assert any(r["logbook"] == "Secret" for r in client(app, "alice").get("/api/search?q=secret compressor").json["results"])
    assert client(app, "bob").get("/api/entry?logbook=Secret&mid=1").status_code == 404
    assert client(app, "bob").get("/api/status").status_code == 403


def test_csrf_header_required(app):
    c = app.test_client()
    c.set_cookie("sid", "alice")
    c.environ_base["HTTP_X_FORWARDED_FOR"] = CLIENT_IP
    assert c.post("/api/ask", json={"question": "x"}).status_code == 403


# ---- features ------------------------------------------------------------------------------------

def test_search_hybrid(app):
    r = client(app, "bob").get("/api/search?q=compressor misaligned").json
    assert r["semantic"] and r["keyword"]
    assert r["results"][0]["mid"] == 2
    assert "\x02" in r["results"][0]["snippet"]


def test_entry_view(app):
    d = client(app, "bob").get("/api/entry?logbook=Laser&mid=2").json
    assert d["analysis"]["kind"] == "problem" and d["analysis"]["problem"]["component"] == "compressor"
    assert d["thread"]["entries"] == 2
    assert d["analysis"]["measurements"][0]["value"] == 1.2


def test_ask_streams_with_sources(app):
    c = client(app, "bob")
    r = c.post("/api/ask", json={"question": "Why did the power drop?"})
    evs = [json.loads(l) for l in r.data.decode().splitlines()]
    assert [e["type"] for e in evs][:2] == ["conversation", "sources"]
    assert evs[1]["sources"] and all(s["logbook"] == "Laser" for s in evs[1]["sources"])
    answer = "".join(e.get("text", "") for e in evs if e["type"] == "delta")
    assert "[Laser #" in answer and "hidden" not in answer
    cid = evs[0]["id"]
    conv = c.get(f"/api/conversations/{cid}").json
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert client(app, "alice").get(f"/api/conversations/{cid}").status_code == 404     # private per user


def test_private_logbook_not_sent_to_remote(site, ollama, elogd, tmp_path):
    app = make_app(site, ollama, elogd, str(tmp_path), analyse=False, AI_PROVIDER="openai",
                   AI_BASE_URL="https://api.example.invalid/v1", AI_EMBED_PROVIDER="ollama", AI_EMBED_BASE_URL=ollama,
                   AI_PRIVATE_LOGBOOKS="Secret")
    x = app.extensions["elogai"]
    assert x["ai"].may_send(["Laser"]) and not x["ai"].may_send(["Secret"])
    rows = [r for r in enrich.pending_enrich(x["ai"], x["ai"].conf, 50) if r["logbook"] == "Secret"]
    enrich.enrich_batch(x["ai"], x["ix"], rows)        # would fail if it tried the unreachable remote API
    assert "private" in db.scalar("SELECT error FROM enrich WHERE entry_id = ?", rows[0]["id"])
    # the assistant does not put private entries into the prompt either
    evs = [json.loads(l) for l in client(app, "alice").post("/api/ask", json={"question": "secret compressor plan"})
           .data.decode().splitlines()]
    src = next(e for e in evs if e["type"] == "sources")
    assert src["withheld"] >= 1 and all(s["logbook"] != "Secret" for s in src["sources"])


def test_digest_cached(app):
    c = client(app, "bob")
    d1 = c.post("/api/digest", json={"logbook": "Laser", "kind": "week", "date": "2026-10-05"}).json
    assert d1["entries"] >= 3 and d1["text"] and not d1["cached"]
    d2 = c.post("/api/digest", json={"logbook": "Laser", "kind": "week", "date": "2026-10-05"}).json
    assert d2["cached"] and d2["text"] == d1["text"]


def test_insights(app):
    c = client(app, "bob")
    p = c.get("/api/insights/problems").json
    item = next(i for i in p["items"] if i["mid"] == 2)
    assert item["resolved"] and item["fixes"][0]["mid"] == 3        # fixed by the reply
    s = c.get("/api/insights/series?logbook=Laser").json
    power = next(x for x in s if x["name"] == "Power")
    assert 19 in power["outliers"]                                   # 9.9 W after a run of 2.85
    assert len(power["outliers"]) == 1
    runs = c.get("/api/insights/runs?q=S-179").json
    assert runs[0]["name"] == "905"
    ents = c.get("/api/insights/entity?value=s179").json["entries"]
    assert {e["mid"] for e in ents} >= {1}


def test_suggest(app):
    r = client(app, "bob").post("/api/assist/suggest", json={"logbook": "Laser", "text": "<p>power is 2.8 W, all fine today</p>"}).json
    assert r["subject"] and r["attributes"] == {"Category": "Info"}


def test_widget_served(app):
    r = app.test_client().get("/widget.js")
    assert r.status_code == 200 and b"elog-ai" in r.data


def test_embedding_prefixes():
    from elogai.vectors import prefixes
    q, d = prefixes("qwen3-embedding:4b")
    assert q.startswith("Instruct: ") and q.endswith("Query: ") and d == ""
    assert prefixes("bge-m3") == ("", "")
