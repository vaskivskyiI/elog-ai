"""Per-user access: every endpoint shows bob (Laser only) nothing from the Secret logbook,
while alice (both) sees it. Run once against the F7 fork's one-request access endpoint and once
against a plain elogd answered logbook by logbook. Background results are computed once for
all logbooks; only what is shown is filtered."""
from __future__ import annotations

import json

import pytest

from elogai import db, digest
from test_elogai import CLIENT_IP, FakeElogd, client, elogd, make_app, ollama, site  # noqa: F401  (fixtures)

SECRET_WORDS = ("Secret", "classified", "777")


@pytest.fixture(scope="module", params=[True, False], ids=["fork-access-endpoint", "plain-elogd"])
def app(request, site, ollama, elogd, tmp_path_factory):
    FakeElogd.fork = request.param
    a = make_app(site, ollama, elogd, str(tmp_path_factory.mktemp("perm")))
    x = a.extensions["elogai"]
    digest.busy_threads(x["ai"])           # background: thread summaries of every logbook
    yield a
    FakeElogd.fork = False


def leaks(obj) -> bool:
    s = json.dumps(obj)
    return '"Secret"' in s or "classified" in s or "Secret compressor" in s or '"777"' in s


def test_background_work_is_global(app):
    # analysed, embedded and summarised regardless of who will look at it
    lb = db.scalar("SELECT id FROM logbooks WHERE name = 'Secret'")
    assert db.scalar("SELECT count(*) FROM enrich x JOIN entries e ON e.id = x.entry_id WHERE e.logbook_id = ?", lb) == 4
    assert db.scalar("SELECT count(*) FROM chunks c JOIN entries e ON e.id = c.entry_id WHERE e.logbook_id = ?", lb) >= 4
    assert db.scalar("SELECT count(*) FROM digests WHERE kind = 'thread' AND logbook_id = ?", lb) == 1


def test_identity(app):
    bob = client(app, "bob").get("/api/me").json
    assert bob["login"] == "bob" and bob["user"] == "Bob Builder"
    assert [l["name"] for l in bob["logbooks"]] == ["Laser"] and bob["groups"] == {"lasers": ["Laser"]}
    alice = client(app, "alice").get("/api/me").json
    assert {l["name"] for l in alice["logbooks"]} == {"Laser", "Secret"} and alice["admin"]


def test_session_from_another_address_is_not_valid(app):
    # elogd binds sessions to the browser's address; a stolen cookie elsewhere gets nothing
    assert client(app, "alice", ip="10.9.9.9").get("/api/me").status_code == 401


@pytest.mark.parametrize("path", [
    "/api/search?q=S-179", "/api/search?q=compressor broken", "/api/search?q=classified run",
    "/api/search?q=&logbooks=Secret", "/api/search?q=compressor&logbooks=Secret,Laser&mode=semantic",
    "/api/insights/problems", "/api/insights/problems?logbooks=Secret",
    "/api/insights/entities", "/api/insights/entities?q=S-179", "/api/insights/entity?value=S-179",
    "/api/insights/runs", "/api/insights/runs?q=S-179", "/api/insights/activity",
    "/api/digests?kind=week&date=2026-10-05", "/api/entry?logbook=Laser&mid=1", "/api/entry?logbook=Laser&mid=2",
])
def test_bob_sees_nothing_secret(app, path):
    r = client(app, "bob").get(path)
    assert r.status_code == 200
    assert not leaks(r.json), path


def test_entity_counts_only_readable(app):
    bob = client(app, "bob").get("/api/entry?logbook=Laser&mid=1").json
    alice = client(app, "alice").get("/api/entry?logbook=Laser&mid=1").json
    nb = next(e["entries"] for e in bob["entities"] if e["norm"] == "s179")
    na = next(e["entries"] for e in alice["entities"] if e["norm"] == "s179")
    assert na > nb


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/entry?logbook=Secret&mid=1", None),
    ("get", "/api/insights/series?logbook=Secret", None),
    ("post", "/api/digest", {"logbook": "Secret", "kind": "week", "date": "2026-10-05"}),
    ("post", "/api/thread/summary", {"logbook": "Secret", "mid": 2}),
    ("post", "/api/entry/analyze", {"logbook": "Secret", "mid": 1}),
    ("post", "/api/assist/suggest", {"logbook": "Secret", "text": "hello world, a long enough text"}),
    ("post", "/api/assist/polish", {"logbook": "Secret", "text": "x"}),
])
def test_bob_cannot_open_secret(app, method, path, body):
    c = client(app, "bob")
    r = c.get(path) if method == "get" else c.post(path, json=body)
    assert r.status_code == 404


def test_alice_sees_secret(app):
    c = client(app, "alice")
    assert any(r["logbook"] == "Secret" for r in c.get("/api/search?q=classified run").json["results"])
    assert any(r["name"] == "777" for r in c.get("/api/insights/runs?q=S-179").json)
    assert c.get("/api/entry?logbook=Secret&mid=2").json["thread"]["summary"]       # precomputed
    assert c.post("/api/digest", json={"logbook": "Secret", "kind": "week", "date": "2026-10-05"}).json["text"]


def test_assistant_only_uses_readable_entries(app):
    for q in ("What happened to sample S-179?", "Tell me about the classified run 777", "secret compressor plan"):
        r = client(app, "bob").post("/api/ask", json={"question": q, "logbooks": ["Secret", "Laser"]})
        evs = [json.loads(l) for l in r.data.decode().splitlines()]
        src = next(e for e in evs if e["type"] == "sources")
        assert all(s["logbook"] == "Laser" for s in src["sources"]), q
        assert not leaks([e for e in evs if e["type"] != "conversation"]), q
    # nor was any Secret entry text in what the model was given
    from fake_ollama import Handler
    prompts = json.dumps([c for c in Handler.calls if c[0] == "/api/chat" and
                          "You are the assistant" in json.dumps(c[1]["messages"][0])][-3:])
    assert "What happened to sample S-179" in prompts
    for secret_text in ("Cut S-179 for the", "secret compressor upgrade", "replacing the pump", "Secret #"):
        assert secret_text not in prompts


def test_enrichment_batches_never_mix_logbooks(app):
    from fake_ollama import Handler
    for path, body in Handler.calls:
        if path == "/api/chat" and "### id=" in json.dumps(body):
            text = body["messages"][-1]["content"]
            assert not ("[Secret #" in text and "[Laser #" in text)


def test_suggest_similar_only_readable(app):
    r = client(app, "bob").post("/api/assist/suggest", json={
        "logbook": "Laser", "text": "<p>Cut S-179 for the classified run, compressor broken afterwards.</p>"}).json
    assert not leaks(r)


def test_session_check_through_waitress(app):
    """The real server, not Flask's test client: X-Forwarded-For must reach the access check."""
    import threading
    import urllib.request

    from waitress import create_server

    from elogai.app import serve_options
    opts = serve_options({"host": "127.0.0.1", "port": 0})
    opts.pop("port")
    srv = create_server(app, port=0, **{k: v for k, v in opts.items() if k != "host"}, host="127.0.0.1")
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.effective_port}/api/me",
                                     headers={"Cookie": "sid=bob", "X-Forwarded-For": CLIENT_IP})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read())["login"] == "bob"
    finally:
        srv.close()
