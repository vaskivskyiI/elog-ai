"""ELOG AI settings: environment variables, usually from one `.env` file.

Everything ELOG already knows (logbook directory, logbooks, password file, admins) is
read from elogd.cfg. The remaining settings:

    ELOG_DIR, ELOGD_CFG     where elogd.cfg is (default /usr/local/elog/elogd.cfg)
    ELOG_INTERNAL_URL       how this service reaches elogd, for access checks
                            (default http://127.0.0.1:<Port of elogd.cfg>)
    ELOGAI_STATE_DIR        database and caches (default <cfg dir>/elog-ai)
    ELOGAI_HOST, ELOGAI_PORT  listen address (default 127.0.0.1:8091)
    ELOGAI_AUTH             elogd (default): ELOG's own session decides | none: no checks (testing only)
    ELOGAI_ADMINS           extra admins (login or full names), besides [global] Admin user
    ELOGAI_API_TOKEN        bearer token for scripts (acts as an admin)
    ELOGAI_SCAN_INTERVAL    seconds between scans of the logbook files (default 20)

    AI_PROVIDER             ollama | openai | anthropic | gemini | none
    AI_BASE_URL, AI_API_KEY
    AI_CHAT_MODEL           answers, digests
    AI_FAST_MODEL           background enrichment (default: AI_CHAT_MODEL)
    AI_EMBED_PROVIDER, AI_EMBED_BASE_URL, AI_EMBED_API_KEY, AI_EMBED_MODEL
                            embeddings (default: the chat provider; Anthropic has none, use ollama)
    AI_VISION_MODEL         optional: describe image attachments (screenshots, plots)
    AI_ENRICH               all (default) | new | off
    AI_ENRICH_SINCE         only enrich entries after this date (YYYY-MM-DD)
    AI_ENRICH_BATCH         short entries per enrichment request (default 4)
    AI_PRIVATE_LOGBOOKS     logbooks whose text must never leave the server: they are
                            only sent to a local provider (ollama or a localhost URL)
    AI_LANGUAGE             language of answers and summaries (default English)
    AI_TIMEOUT              seconds per request (default 300)
"""
from __future__ import annotations

import os
import re

PATH_VARS = ("ELOG_DIR", "ELOGD_CFG", "ELOGAI_STATE_DIR")

DEFAULT_MODELS = {
    # provider: (chat, fast, embed)
    "ollama": ("gemma4:31b", "gemma4:latest", "qwen3-embedding:4b"),
    "openai": ("gpt-5-mini", "gpt-5-nano", "text-embedding-3-small"),
    "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5", ""),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"),
    "none": ("", "", ""),
}
DEFAULT_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "none": "",
}


def load_env_file(path: str) -> None:
    """Minimal .env reader: KEY=VALUE lines; the existing environment wins.

    Relative paths are resolved against the file's directory."""
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in PATH_VARS and value and not os.path.isabs(value):
                value = os.path.join(base, value)
            os.environ.setdefault(key, value)


def _list(value: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", value or "") if x.strip()]


def from_env() -> dict:
    env = os.environ
    cfg = env.get("ELOGD_CFG") or os.path.join(env.get("ELOG_DIR") or "/usr/local/elog", "elogd.cfg")
    provider = (env.get("AI_PROVIDER") or "ollama").strip().lower()
    if provider not in DEFAULT_MODELS:
        raise SetupError(f"AI_PROVIDER={provider}: use ollama, openai, anthropic, gemini or none")
    chat, fast, embed = DEFAULT_MODELS[provider]
    embed_provider = (env.get("AI_EMBED_PROVIDER") or ("ollama" if provider == "anthropic" else provider)).lower()
    if embed_provider not in DEFAULT_MODELS:
        raise SetupError(f"AI_EMBED_PROVIDER={embed_provider}: use ollama, openai, gemini or none")
    same = embed_provider == provider
    return {
        "elogd_cfg": os.path.abspath(cfg),
        "elog_internal_url": (env.get("ELOG_INTERNAL_URL") or "").rstrip("/"),
        "state_dir": env.get("ELOGAI_STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(cfg)), "elog-ai"),
        "host": env.get("ELOGAI_HOST") or "127.0.0.1",
        "port": int(env.get("ELOGAI_PORT") or 8091),
        "auth": (env.get("ELOGAI_AUTH") or "elogd").lower(),
        "admins": _list(env.get("ELOGAI_ADMINS")),
        "api_token": env.get("ELOGAI_API_TOKEN") or "",
        "scan_interval": float(env.get("ELOGAI_SCAN_INTERVAL") or 20),
        "chat": {
            "provider": provider,
            "base_url": (env.get("AI_BASE_URL") or DEFAULT_URLS[provider]).rstrip("/"),
            "api_key": env.get("AI_API_KEY") or "",
            "model": env.get("AI_CHAT_MODEL") or chat,
            "fast_model": env.get("AI_FAST_MODEL") or env.get("AI_CHAT_MODEL") or fast,
            "vision_model": env.get("AI_VISION_MODEL") or "",
        },
        "embed": {
            "provider": embed_provider,
            "base_url": (env.get("AI_EMBED_BASE_URL") or (env.get("AI_BASE_URL") if same else "")
                         or DEFAULT_URLS[embed_provider]).rstrip("/"),
            "api_key": env.get("AI_EMBED_API_KEY") or (env.get("AI_API_KEY") if same else "") or "",
            "model": env.get("AI_EMBED_MODEL") or DEFAULT_MODELS[embed_provider][2],
        },
        "enrich": (env.get("AI_ENRICH") or "all").lower(),
        "enrich_since": env.get("AI_ENRICH_SINCE") or "",
        "enrich_batch": max(1, int(env.get("AI_ENRICH_BATCH") or 4)),
        "private_logbooks": _list(env.get("AI_PRIVATE_LOGBOOKS")),
        "language": env.get("AI_LANGUAGE") or "English",
        "timeout": float(env.get("AI_TIMEOUT") or 300),
    }


class SetupError(Exception):
    """A configuration problem the operator has to fix; shown without a traceback."""
