"""A stand-in for Ollama's HTTP API, for tests and UI development without a model.

    python tests/fake_ollama.py 11434

Embeddings are hashed bags of words (similar texts get similar vectors); chat answers are
canned but well-formed: enrichment JSON for every `### id=N` block, citations for questions."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

DIM = 256


def embed(text: str) -> list[float]:
    v = np.zeros(DIM, dtype=np.float32)
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) & 1 else -1.0
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


def answer(messages: list[dict], json_mode: bool) -> str:
    last = messages[-1]["content"]
    ids = re.findall(r"### id=(\d+)", last)
    if ids:
        items = []
        for i in ids:
            block = re.split(rf"### id={i}\n", last, maxsplit=1)[1].split("### id=", 1)[0]
            prob = re.search(r"misalign|broken|error|fail|problem|leak|low power", block, re.I)
            m = re.search(r"(\d+(?:\.\d+)?)\s*(mW|W|K|A|mbar)\b", block)
            items.append({"id": int(i), "summary": block.strip().splitlines()[-1][:80], "kind": "problem" if prob else "info",
                          "tags": ["test"], "entities": [{"type": "instrument", "value": "Legend"}] if "legend" in block.lower() else [],
                          "measurements": [{"quantity": "power", "value": float(m.group(1)), "unit": m.group(2)}] if m else [],
                          "problem": {"component": "compressor", "symptom": "misaligned", "severity": "medium",
                                      "resolved": False} if prob else None,
                          "lang": "en", "english": ""})
        return json.dumps({"items": items})
    if json_mode:
        if "standalone search query" in json.dumps(messages) or "Last question" in last:
            return json.dumps({"query": last.rsplit("Last question:", 1)[-1].strip(), "keywords": [], "since": None,
                               "until": None, "logbooks": []})
        return json.dumps({"subject": "Suggested subject", "attributes": {"Category": "Info"}, "missing": ["units"]})
    refs = re.findall(r"\[[^\]\n]+ #\d+\]", last)
    return "<think>hidden</think>Based on the entries " + " ".join(dict.fromkeys(refs[:3])) + " this is the answer."


class Handler(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": "fake:latest"}, {"name": "fake-embed:latest"}]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Handler.calls.append((self.path, body))
        if self.path == "/api/embed":
            inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return self._json({"embeddings": [embed(t) for t in inp]})
        if self.path == "/api/chat":
            text = answer(body["messages"], body.get("format") == "json")
            if not body.get("stream"):
                return self._json({"message": {"role": "assistant", "content": text}, "done": True,
                                   "prompt_eval_count": 100, "eval_count": 20})
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for piece in re.findall(r"\S+\s*", text):
                self.wfile.write((json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode())
                self.wfile.flush()
            self.wfile.write((json.dumps({"done": True, "prompt_eval_count": 100, "eval_count": 20}) + "\n").encode())
            return
        self._json({"error": "not found"}, 404)


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start(int(sys.argv[1]) if len(sys.argv) > 1 else 11434)
    print("fake ollama at", url)
    while True:
        time.sleep(3600)
