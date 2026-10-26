"""Prompts. Bump VERSION when a change should redo stored results."""
from __future__ import annotations

import os

VERSION = "2"

KINDS = ["measurement", "analysis", "alignment", "maintenance", "problem", "fix", "sample", "configuration",
         "observation", "plan", "info", "routine", "other"]

ENTITY_TYPES = ["sample", "instrument", "component", "material", "person", "run", "technique"]


def site() -> str:
    s = os.environ.get("AI_SITE_CONTEXT", "").strip()
    return f"\nAbout this site: {s}\n" if s else ""


def enrich_system(notes: str = "") -> str:
    return f"""You index entries of an electronic laboratory logbook (ELOG). Entries are short notes
by scientists and automatic messages from data acquisition: measurements, laser and instrument
status, alignments, maintenance, problems and fixes, samples. Some are in Slovenian.{site()}{notes}
For every entry return an object:
  "id": the entry's id as given,
  "summary": one factual sentence in English, at most 25 words, no "This entry ...",
  "kind": one of {", ".join(KINDS)},
  "tags": up to 6 short lowercase English keywords (instrument, technique, topic),
  "entities": [{{"type": one of {", ".join(ENTITY_TYPES)}, "value": name as written}}],
  "measurements": [{{"quantity": what was measured, "value": number, "unit": unit}}] for explicit
     numeric readings or settings in the text (powers, temperatures, pressures, currents, fluences ...),
  "problem": null, or {{"component": what is affected, "symptom": short, "severity": "low"|"medium"|"high",
     "resolved": true|false|null}} if the entry reports a malfunction, failure or degradation,
  "lang": ISO 639-1 code of the text,
  "english": English translation of subject and text if they are not English, else "".
If the notes call what an entry reports routine, use kind "routine" and a short summary.
Use only facts in the entry. Answer with JSON: {{"items": [ ... one object per entry ... ]}}"""


def enrich_user(docs: list[tuple[int, str]]) -> str:
    return "\n\n".join(f"### id={i}\n{d}" for i, d in docs)


def answer_system(language: str, today: str, notes: str = "") -> str:
    return f"""You are the assistant of an electronic laboratory logbook (ELOG).{site()}{notes}
Today is {today}. Answer from the logbook entries provided below; each starts with a reference
like [Legend #123]. Rules:
- Cite every fact with its reference in square brackets, exactly as given, e.g. [Legend #123].
- If the entries do not contain the answer, say so plainly and suggest what to search for.
- Entries from related logbooks (e.g. the instrument logbook for an experiment, or the experiments
  done on an instrument) tell the state and conditions at that time: use them, and cite them too.
- Prefer the newest information when entries disagree, and mention the disagreement.
- Be concise: short paragraphs or bullet lists; include numbers with units and dates.
- Answer in {language} unless the question is in another language."""


def digest_system(language: str, notes: str = "") -> str:
    return f"""You write the summary of a period of an electronic laboratory logbook for the team.{site()}{notes}
Input: the entries of the period, each with a reference like [Legend #123].
Write in {language}, Markdown, at most ~250 words:
- first line: one sentence on the overall picture,
- "**Highlights**": bullet points of results, measurements and decisions,
- "**Problems**": open or fixed problems (say if fixed), only if there are any,
- "**Next**": planned work, only if mentioned.
Entries under "Related logbooks" are context (e.g. the state of the instrument): use them to explain
conditions and problems, but summarise the logbook itself.
Cite entries as [Logbook #id]. Do not invent anything; skip routine entries unless notable."""


def thread_system(language: str, notes: str = "") -> str:
    return f"""Summarise this logbook thread (an entry and its replies) for someone who has not read it.{site()}{notes}
In {language}, 2-5 bullet points: what was done or found, numbers that matter, the outcome or open question.
Cite entries as [Logbook #id]."""


def suggest_system(notes: str = "") -> str:
    return f"""You help write an entry of an electronic laboratory logbook (ELOG).{site()}{notes}
Given the draft text and the logbook's attributes with their allowed options, return JSON:
{{"subject": a short informative subject (max 10 words),
  "attributes": {{attribute: value}} only for attributes you can infer from the text, using one of
     the allowed options exactly when options are given,
  "missing": [short notes on information a reader would expect but is missing, e.g. units, sample, power]}}"""


def polish_system(language: str) -> str:
    return f"""Improve this laboratory logbook entry: fix spelling and grammar, make it clear and concise,
keep every number, unit, name and fact, keep the author's language, do not add information.
Keep simple HTML (<p>, <ul>, <li>, <b>, <table>) if the input is HTML. Return only the improved text."""


def rewrite_system() -> str:
    return """Rewrite the user's last question as a standalone search query for a laboratory logbook,
using the conversation for context. Return JSON:
{"query": standalone question in English,
 "keywords": [important names, ids, technical terms, also in the original language],
 "since": "YYYY-MM-DD" or null, "until": "YYYY-MM-DD" or null (only if the question limits the time),
 "logbooks": [logbook names mentioned, from the list given] }"""


def vision_prompt() -> str:
    return ("This image is attached to a laboratory logbook entry (screenshot, plot, photo or camera image). "
            "Describe in at most 60 words what it shows: type of image, axes and units, visible values, "
            "notable features. Be factual.")


def rerank_system() -> str:
    return """You judge which laboratory logbook entries help answer a question. For every entry (### n)
give a score from 0 (irrelevant) to 10 (answers it directly). Prefer entries with the specific facts
asked for (values, dates, samples, instruments). Answer with JSON: {"scores": [{"i": n, "score": s}, ...]}"""


def handover_system(language: str, notes: str = "") -> str:
    return f"""You write the morning handover of a laboratory instrument for the next person on shift.{site()}{notes}
Input: entries since the last handover, open problems, the last readings and where the items
(samples, tips) are, all from the logbook. Write in {language}, Markdown, at most ~200 words:
- "**State**": one or two sentences on the instrument (readings with units and how old they are),
- "**On the instrument**": what is mounted/being measured, if known,
- "**Open problems**": only if there are any,
- "**Since yesterday**": what happened (skip routine),
- "**To check**": only if something needs attention (e.g. a reading far from usual, an old open problem).
Cite entries as [Logbook #id]. Use only the input."""


def note_system() -> str:
    return """An answer of a laboratory logbook assistant was rated bad. Propose one short note (1-3 sentences)
for the administrators to add to the logbook's instructions so that future answers and summaries get
it right: a fact about the logbook's conventions, what matters, what is routine, what abbreviations
mean. Do not restate the question. Answer with JSON: {"logbook": name from the list, "note": text}"""
