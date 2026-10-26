"""Evaluation with real questions: does retrieval find the entries that answer them, does the
answer cite them, and does it contain the facts? Run before changing models or prompts:

    python -m elogai --env-file .env --eval eval/questions.jsonl [--eval-no-answer] [--eval-out report.json]

One JSON object per line: {"question": ..., "expect": ["Legend #1", ...], "keywords": ["stretcher", ...],
"logbooks": [optional scope]}. Runs with access to all logbooks, as an administrator would."""
from __future__ import annotations

import json
import re
import time

from . import assistant, db


def _norm_ref(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().strip("[]")).lower()


def run(ai, ix, path: str, answer: bool = True, out: str = "") -> dict:
    qs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip() and not l.lstrip().startswith("#")]
    allowed = set(ix.logbooks.values())
    names = list(ix.logbooks)
    results = []
    for q in qs:
        expect = {_norm_ref(e) for e in q.get("expect") or []}
        t0 = time.time()
        rows, _ = assistant.retrieve(ai, q["question"], allowed, {"logbooks": q.get("logbooks") or []}, names, [])
        t_ret = time.time() - t0
        refs = [_norm_ref(f"{r['logbook']} #{r['mid']}") for r in rows]
        found = [e for e in expect if e in refs]
        rank = min((refs.index(e) + 1 for e in found), default=None)
        res = {"question": q["question"], "expect": sorted(expect), "found": found,
               "recall": len(found) / len(expect) if expect else None, "rank": rank,
               "retrieval_s": round(t_ret, 2)}
        if answer and ai.chat_enabled:
            t1 = time.time()
            text = "".join(ev.get("text", "") for ev in assistant.ask(ai, "eval", q["question"], allowed,
                                                                      {"logbooks": q.get("logbooks") or []}, None, names)
                           if ev["type"] == "delta")
            cited = {_norm_ref(m) for m in re.findall(r"\[[^\]\[]+ #\d+\]", text)}
            kws = q.get("keywords") or []
            res.update({"answer_s": round(time.time() - t1, 2), "answer": text,
                        "cited_expected": sorted(cited & expect),
                        "keywords_found": [k for k in kws if k.lower() in text.lower()],
                        "keyword_rate": (sum(k.lower() in text.lower() for k in kws) / len(kws)) if kws else None})
        results.append(res)
    # evaluation conversations are not anybody's history
    db.run("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user = 'eval')")
    db.run("DELETE FROM conversations WHERE user = 'eval'")

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    summary = {
        "questions": len(results),
        "recall": mean(r["recall"] for r in results),
        "mrr": mean((1 / r["rank"]) if r["rank"] else (0 if r["expect"] else None) for r in results),
        "citation_rate": mean((len(r["cited_expected"]) / len(r["expect"])) if r.get("answer") is not None
                              and r["expect"] else None for r in results),
        "keyword_rate": mean(r.get("keyword_rate") for r in results),
        "retrieval_s": mean(r["retrieval_s"] for r in results),
        "answer_s": mean(r.get("answer_s") for r in results),
        "models": {"chat": ai.chat.model, "fast": ai.fast.model, "embed": ai.embed_model,
                   "rerank": ai.conf.get("rerank")},
    }
    report = {"summary": summary, "results": results}
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    return report


def print_report(report: dict) -> None:
    for r in report["results"]:
        rec = "-" if r["recall"] is None else f"{r['recall']:.0%}"
        kw = "" if r.get("keyword_rate") is None else f" keywords {r['keyword_rate']:.0%}"
        print(f"recall {rec:>4} rank {r['rank'] or '-':>3}{kw}  {r['question'][:80]}")
    print("\nsummary:", json.dumps(report["summary"], ensure_ascii=False))
