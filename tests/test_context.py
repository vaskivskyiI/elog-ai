"""Per-logbook notes (AI context) and related logbooks (AI related): what goes into which prompt,
re-analysis when notes change, and that related logbooks never widen what a user can see.

Test site: Laser (everybody) has `AI related = Sec*`, so Laser <-> Secret (alice only)."""
from __future__ import annotations

import json
import os
import time

import pytest

from elogai import db, digest, enrich
from fake_ollama import Handler
from test_elogai import CLIENT_IP, FakeElogd, client, elogd, make_app, ollama, write_site  # noqa: F401


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("ctx-elog"))
    write_site(root)
    return root


@pytest.fixture(scope="module")
def app(site, ollama, elogd, tmp_path_factory):
    FakeElogd.fork = True
    a = make_app(site, ollama, elogd, str(tmp_path_factory.mktemp("ctx-state")))
    yield a
    FakeElogd.fork = False


def ctx(app):
    return app.extensions["elogai"]["ai"].ctx


def calls_with(system_part: str) -> list:
    return [b for p, b in Handler.calls if p == "/api/chat" and system_part in json.dumps(b["messages"][0])]


def test_notes_and_relations(app):
    c = ctx(app)
    assert c.own("Laser").startswith("Moving the laser table")
    assert c.own("Secret") == "Pump replacements are the important events here."      # from the file
    assert c.related("Laser") == ["Secret"] and c.related("Secret") == ["Laser"]        # wildcard, both ways
    # shared results of Secret (alice) may use Laser (everybody), not the other way round
    assert c.safe("Secret", "Laser") and not c.safe("Laser", "Secret")
    assert c.shared_related("Laser") == [] and c.shared_related("Secret") == ["Laser"]


def test_analysis_uses_the_logbooks_notes(app):
    calls = calls_with("You index entries")
    laser = [json.dumps(b["messages"][0]) for b in calls if "[Laser #" in b["messages"][-1]["content"]]
    secret = [json.dumps(b["messages"][0]) for b in calls if "[Secret #" in b["messages"][-1]["content"]]
    assert laser and all("laser table" in m and "Pump replacements" not in m for m in laser)
    assert secret and all("Pump replacements" in m and "laser table" in m for m in secret)  # knows Laser's too


def test_changed_notes_redo_the_analysis(app, site):
    x = app.extensions["elogai"]
    assert not enrich.pending_enrich(x["ai"], x["conf"], 100)
    cfg = os.path.join(site, "elogd.cfg")
    text = open(cfg).read()
    with open(cfg, "w") as f:
        f.write(text.replace("what matters is output power", "what matters is output power and pulse length"))
    os.utime(cfg, (time.time() + 5, time.time() + 5))
    x["ix"].load_config()
    x["ai"].ctx.refresh()
    pending = {r["logbook"] for r in enrich.pending_enrich(x["ai"], x["conf"], 100)}
    assert pending == {"Laser", "Secret"}        # Secret's analysis includes Laser's notes too
    while True:
        rows = enrich.pending_enrich(x["ai"], x["conf"], 20)
        if not rows:
            break
        enrich.enrich_batch(x["ai"], x["ix"], rows)
    assert any("pulse length" in json.dumps(b["messages"][0]) for b in calls_with("You index entries"))


def test_digest_context_only_from_safe_logbooks(app):
    x = app.extensions["elogai"]
    from datetime import date
    Handler.calls.clear()
    digest.get(x["ai"], x["ix"].logbooks["Laser"], "week", date(2026, 10, 5))
    digest.get(x["ai"], x["ix"].logbooks["Secret"], "week", date(2026, 10, 5))
    laser, secret = [b["messages"][-1]["content"] for b in calls_with("summary of a period")]
    assert "Related logbooks" not in laser and "[Secret #" not in laser     # Laser readers can't all read Secret
    assert "Related logbooks" in secret and "[Laser #" in secret.split("Related logbooks", 1)[1]


def test_answers_use_related_logbooks_per_user(app):
    def sources(sid, q, scope=None):
        r = client(app, sid).post("/api/ask", json={"question": q, "logbooks": scope or []})
        return next(e for e in map(json.loads, r.data.decode().splitlines()) if e["type"] == "sources")["sources"]

    alice = sources("alice", "What happened with the pump replacement?")
    assert any(s["logbook"] == "Secret" for s in alice)
    assert any(s["logbook"] == "Laser" and s["related"] for s in alice)        # instrument state from the related logbook
    bob = sources("bob", "Why did the laser power drop?")
    assert bob and all(s["logbook"] == "Laser" for s in bob)                   # bob cannot read Secret
    only = sources("alice", "What happened with the pump replacement?", ["Secret"])
    assert all(s["logbook"] == "Secret" for s in only)                         # limited to one logbook
    prompt = json.dumps(calls_with("You are the assistant")[-3]["messages"][0])
    assert "Pump replacements" in prompt and "laser table" in prompt           # notes of both logbooks


def test_entry_panel_shows_related_logbooks_around_the_time(app):
    d = client(app, "alice").get("/api/entry?logbook=Secret&mid=2").json
    assert {r["logbook"] for r in d["around"]["entries"]} == {"Laser"} and len(d["around"]["entries"]) >= 2
    assert {v["name"] for v in d["around"]["values"]} == {"Power", "T"}
    assert client(app, "bob").get("/api/entry?logbook=Laser&mid=2").json["around"] == {"entries": [], "values": []}


def test_instrument_state_is_not_limited_to_a_day(app):
    # Laser #19 is from 16 Oct; Secret's last entries are from 5 Oct: still its state at that time
    d = client(app, "alice").get("/api/entry?logbook=Laser&mid=19").json
    assert [r["mid"] for r in d["around"]["entries"]][-1] == 4
    assert all(r["logbook"] == "Secret" for r in d["around"]["entries"])
