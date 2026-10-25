"""Development server: the fake Ollama plus ELOG AI without access checks.

    python tests/dev_server.py <elogd.cfg> [state dir] [port]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_ollama  # noqa: E402

cfg = os.path.abspath(sys.argv[1])
state = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(os.path.dirname(cfg), "elog-ai")
port = sys.argv[3] if len(sys.argv) > 3 else "8091"
srv, url = fake_ollama.start()
os.environ.update({"ELOGD_CFG": cfg, "ELOGAI_STATE_DIR": state, "ELOGAI_AUTH": "none", "ELOGAI_PORT": port,
                   "AI_PROVIDER": "ollama", "AI_BASE_URL": url, "AI_CHAT_MODEL": "fake", "AI_FAST_MODEL": "fake",
                   "AI_EMBED_MODEL": "fake-embed"})
os.environ.setdefault("AI_ENRICH_SINCE", "2026-01-01")

from elogai.app import main  # noqa: E402

main(["--env-file", os.devnull])
