"""Reranking: search finds candidates quickly; a model then judges which of them actually help
answer the question. AI_RERANK=llm (default) scores all candidates in one request to the fast
model; AI_RERANK=<model> with a dedicated reranker (e.g. Qwen3-Reranker) asks yes/no per
candidate; AI_RERANK=off keeps the search order."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from . import prompts, providers
from .ai import AI

log = logging.getLogger("elogai.rerank")

POINTWISE_SYSTEM = ('Judge whether the Document meets the requirements based on the Query and the Instruct '
                    'provided. Note that the answer can only be "yes" or "no".')


def _doc(r: dict) -> str:
    attrs = "; ".join(f"{k}: {v}" for k, v in (r.get("attrs") or {}).items() if v and k != "Author")
    text = r.get("full_text") or r.get("snippet") or ""
    return f"[{r['logbook']} #{r['mid']}] {r.get('date', '')} {attrs}\nSubject: {r.get('subject') or ''}\n{text}"[:1200]


def rerank(ai: AI, query: str, rows: list[dict], keep: int) -> list[dict]:
    mode = (ai.conf.get("rerank") or "off").strip()
    if mode == "off" or len(rows) <= 2 or not ai.chat_enabled:
        return rows[:keep]
    # entries of private logbooks are not sent to the reranker; they stay in the list, so the
    # assistant can say how many matching entries it had to leave out
    private = [r for r in rows if not ai.may_send([r["logbook"]], ai.fast)]
    rows = [r for r in rows if ai.may_send([r["logbook"]], ai.fast)]
    try:
        scores = _pointwise(ai, mode, query, rows) if mode != "llm" else _listwise(ai, query, rows)
    except (providers.ProviderError, ValueError, TypeError, AttributeError) as e:
        log.warning("rerank skipped: %s", e)
        return rows[:keep] + private
    order = sorted(range(len(rows)), key=lambda i: (-scores.get(i, 0), i))
    return [rows[i] for i in order[:keep]] + private


def _listwise(ai: AI, query: str, rows: list[dict]) -> dict[int, float]:
    docs = "\n\n".join(f"### {i}\n{_doc(r)[:700]}" for i, r in enumerate(rows))
    out = providers.parse_json(ai.fast.chat(
        [{"role": "user", "content": f"Question: {query}\n\nEntries:\n\n{docs}"}],
        system=prompts.rerank_system(), json_mode=True, max_tokens=40 * len(rows) + 100, temperature=0, task="rerank"))
    scores = {}
    for s in out.get("scores", []) if isinstance(out, dict) else []:
        try:
            scores[int(s["i"])] = float(s["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return scores


def _pointwise(ai: AI, model: str, query: str, rows: list[dict]) -> dict[int, float]:
    p = providers.make({**ai.conf["chat"], "model": model}, "model", ai.conf["timeout"])

    def judge(i):
        msg = (f"<Instruct>: Given a question about laboratory logbooks, judge whether the logbook entry helps "
               f"answer it\n<Query>: {query}\n<Document>: {_doc(rows[i])}")
        ans = p.chat([{"role": "user", "content": msg}], system=POINTWISE_SYSTEM, max_tokens=4, temperature=0,
                     task="rerank")
        return i, 1.0 if ans.strip().lower().startswith("yes") else 0.0

    with ThreadPoolExecutor(4) as pool:
        return dict(pool.map(judge, range(len(rows))))
