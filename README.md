# ELOG AI

An AI module for the [PSI ELOG](https://elog.psi.ch/elog/) (F7 edition): semantic search, an
assistant that answers questions from the logbooks with links to the entries, summaries of days,
weeks and threads, recognised problems, trends of numeric values, samples across logbooks, DAQ
runs, and help while writing an entry. Works with a remote (or local) Ollama, or with an
OpenAI-compatible, Anthropic or Gemini API.

## Why a separate service

ELOG keeps working exactly as before when this service is stopped, removed or broken. The AI part
is a sidecar next to elogd and the ELOG Manager:

```
 browser ── nginx ─┬─ /         elogd            (logbooks, unchanged)
                   ├─ /manage/  ELOG Manager
                   └─ /ai/      ELOG AI ──reads──> elogd.cfg, logbook files   (read-only)
                                    │  └─asks───> elogd: may this session read logbook X?
                                    └──────────> Ollama (remote GPU host) / API
```

* **Reads the files, never writes them.** elogd stores a logbook as one text file per day. The
  service parses them directly (no credentials needed, attachments visible) and re-reads only files
  whose size or mtime changed: indexing all 12,141 entries of f7-elog takes 2 s, a rescan 30 ms.
* **Access is decided by elogd** (see *Permissions*). No password or access rule is duplicated.
* **One line integrates it into ELOG**: `AI URL = /ai/` in `[global]` makes the F7 fork load
  `widget.js` on every logbook page and show an *AI* button on the start page. Remove the line and
  ELOG looks as before. (Any other ELOG: `Bottom text = <script src="/ai/widget.js" defer></script>`.)

An integration into elogd itself (C) was considered and rejected: elogd is single-threaded, so
model calls taking seconds to minutes would block every user; Python has the libraries; and the
module can be upgraded, restarted or switched off without touching ELOG.

## What it does

**In ELOG's pages** (`widget.js`):

| Page | |
|---|---|
| Entry | Panel under the entry: one-sentence summary, kind (problem, fix, measurement, alignment …), tags, readings with units, English translation of Slovenian entries, samples/tips/SNs and how many other entries mention them, DAQ run of the entry, *Summarise this thread*, **related entries** from all readable logbooks. An entry that has not been analysed yet is analysed first when somebody opens it. |
| List | **✦ Ask AI** in the top bar (Alt+A): a side panel that answers questions about this logbook (or all), with links. |
| Edit | **Suggest subject & fields** (from the text, using the logbook's allowed options; one click applies them), similar existing entries (reply instead of duplicating?), what a reader would miss (units, sample …), **Polish text** (spelling and clarity, every number kept). |

**Workspace** at `/ai/`:

| | |
|---|---|
| Overview | Activity of each logbook over 26 weeks, open problems, this week's digests |
| Ask | Conversations with the logbooks; dates ("last month", "in March 2025") and logbook names in the question become filters; follow-up questions work; sources and citations link to the entries; thumbs up/down are stored |
| Search | Hybrid search: exact words (FTS5/BM25, `S-179`, run numbers) plus meaning (embeddings), fused; filters by logbook, date, author, kind, sample; *similar…* per result |
| Digests | Per logbook and day/week/month. Yesterday's, last week's and last month's are prepared every morning for logbooks with entries; others on request. Cached until an entry of the period changes. |
| Problems | Problems the analysis found (and entries with Category *Problem*), grouped by what they affect, recurring ones first, marked fixed when a later entry of the thread reports a fix |
| Trends | Numeric attributes (Legend OUT, T, HUM, Mantis …) and readings found in the text (e.g. `P vitara = 516 mW`) as charts; values far from the preceding median are marked |
| Samples & things | Samples, tips, serial numbers, batches from attributes, run descriptions and the text, with their history across logbooks |
| Runs | DAQ runs from the `.ini` run descriptions attached to DAQ entries: sample, info, start/end, interrupted, device settings |
| Status | Admins: index progress, provider health, token usage per task, maintenance buttons |

## Permissions

Every user sees results only from logbooks they may open in ELOG; the work itself is done once,
for all logbooks, in the background.

* **Who and which logbooks.** The pages run under the same origin as ELOG, so the browser sends
  ELOG's session cookie (`sid`). The service passes it to elogd together with the browser's
  `X-Forwarded-For` (elogd binds every session to the client address, so without it every user
  would look logged out) and asks `/?access=1`. The F7 fork answers with the login and the logbooks
  this session may read: no password file, guest access (`Guest menu commands`), or an active
  account allowed by `Login user`/`Admin user`, the same rules as elogd's own login check. With an
  unmodified elogd the service instead asks every logbook for `?jcmd=Status`, which elogd answers
  only after that check. Results are cached for 2 minutes per session and address, so a changed
  `Login user` list applies within 2 minutes.
* **Computed globally, filtered per user.** Embeddings, entry analysis, entities, runs, digests and
  thread summaries are computed once per entry, logbook or thread, for everybody. Each request then
  only reads rows of the user's logbooks: search and semantic search, related entries, the
  assistant's sources (only those go into the model's prompt), problems, entities and their counts,
  runs, activity and digests. Logbooks a user names in a request but may not read are dropped.
  Requests for an entry, digest, series or thread of such a logbook answer 404.
* **No mixing in the model.** A digest or thread summary is made from one logbook only, and the
  background analysis never puts entries of different logbooks into one request, so a model that
  mixes up items cannot move text from a restricted logbook into another one.
* Conversations are private to their user. `tests/test_permissions.py` checks all of the above for
  two users against a fake elogd, with and without the fork's access endpoint.

## How the data is analysed and stored

One SQLite database (WAL) in `ELOGAI_STATE_DIR`; everything in it can be rebuilt from the logbook
files.

* **entries**: parsed entries (attributes, plain text without quoted replies, thread root),
  content hash. Drafts are skipped. **entries_fts**: FTS5 over subject, text, attributes and the AI
  summary/tags/translation, diacritics folded (č = c).
* **chunks**: embeddings, one per entry (long entries in overlapping pieces), normalised float16
  blobs (2 KB per entry with bge-m3). Searched in memory with numpy: exact, milliseconds.
* **enrich**: per entry summary, kind, tags, entities, measurements, problem, language, translation.
  Short entries are batched (`AI_ENRICH_BATCH`) into one request; trivial ones ("ok") are not sent.
* **entities, runs**: from attributes, `.ini` attachments and the analysis.
* **digests**: period and thread summaries keyed by a hash of their input.
* **conversations, messages, usage**.

Work is found by comparing hashes, not by a queue: an edited entry is re-embedded and re-analysed,
nothing else; a different embedding model re-embeds everything (*Redo* on the status page).
Background work is newest first, entries somebody opened first, and pauses while someone waits
for an answer (on a CPU-only server one model call at a time is all there is).

**Privacy.** With an external API, entry texts are sent to it. `AI_PRIVATE_LOGBOOKS` lists
logbooks that are only ever sent to Ollama or a local URL; the assistant then answers without them
and says how many matching entries it left out.

## Models (remote Ollama)

The ELOG server (16 CPU cores, no GPU) only runs this service; the models run on an Ollama server
elsewhere in the network:

```
AI_PROVIDER=ollama
AI_BASE_URL=http://gpu-host.example.org:11434  # embeddings use the same server unless AI_EMBED_BASE_URL is set
AI_API_KEY=...                                 # only if that Ollama sits behind a proxy that wants a bearer token
AI_CHAT_MODEL=qwen3:14b                        # answers, digests
AI_FAST_MODEL=qwen3:8b                         # background analysis of every entry
AI_EMBED_MODEL=bge-m3                          # multilingual (English and Slovenian)
```

On the Ollama host: `ollama pull bge-m3`, `ollama pull qwen3:8b`, `ollama pull qwen3:14b`, and let it
listen on the network (`OLLAMA_HOST=0.0.0.0`), firewalled to the ELOG server. The *Status* page shows
whether each model is reachable and pulled. Entry texts travel to that host: use a host inside the
institute, or put a TLS proxy in front. An Ollama server counts as self-hosted, so
`AI_PRIVATE_LOGBOOKS` (logbooks that must not go to an external API) does not exclude it.

Other providers work the same way (`openai` for any OpenAI-compatible server such as vLLM,
`anthropic`, `gemini`), also mixed, e.g. an API for answers and Ollama for embeddings.

## Install (f7-elog.ijs.si, without Docker)

```bash
# the service, as the elog user
sudo mkdir -p /opt/elog-ai && sudo chown elog: /opt/elog-ai
# copy this repository to /opt/elog-ai, then:
cd /opt/elog-ai
sudo -u elog python3 -m venv venv && sudo -u elog venv/bin/pip install -r requirements.txt
sudo -u elog cp .env.example .env          # AI_BASE_URL of the Ollama host, models ...
sudo -u elog venv/bin/python -m elogai --env-file .env --scan      # first index, prints counts
sudo cp deploy/elog-ai.service /etc/systemd/system/
sudo systemctl enable --now elog-ai

# nginx: add deploy/nginx-ai.conf inside the ELOG server block
sudo nginx -t && sudo systemctl reload nginx
```

Then in `elogd.cfg`, `[global]` (needs the F7 fork with `AI URL`):

```
AI URL = /ai/
```

elogd reloads the file by itself (`Watch config`). In nginx, the `/ai/` location must pass the same
`X-Forwarded-For` as the location of elogd (see `deploy/nginx-ai.conf`). Open `https://f7-elog.ijs.si/ai/` and, as an
admin, check *Status*.

With Docker: `docker compose up -d --build`, see `docker-compose.yml`.

## Develop

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest   # Linux: .venv/bin/
python -m pytest -q tests
python tests/dev_server.py path/to/elogd.cfg        # fake model, no login, http://127.0.0.1:8091
```

`tests/fake_ollama.py` imitates Ollama's API (hashed bag-of-words embeddings, canned answers), so
everything except answer quality can be tested without a model.

## Layout

```
elogai/elogsrc.py    elogd.cfg reader, logbook file parser, HTML to text, run .ini parser
elogai/indexer.py    incremental scan, entities, runs, threads, numeric attributes
elogai/db.py         SQLite schema (append-only migrations) and helpers
elogai/providers.py  Ollama, OpenAI-compatible, Anthropic, Gemini over plain HTTP
elogai/ai.py         configured providers, privacy rule, usage accounting, interactive gate
elogai/vectors.py    chunking, float16 storage, in-memory similarity search
elogai/enrich.py     embeddings, per-entry analysis, image descriptions
elogai/search.py     hybrid search, similar entries
elogai/assistant.py  question answering with retrieval and citations
elogai/digest.py     period and thread summaries, morning schedule
elogai/insights.py   problems, series and outliers, entities, runs, activity
elogai/auth.py       ELOG session → user and readable logbooks (asks elogd)
elogai/worker.py     background threads
elogai/app.py        Flask API, pages
elogai/static/       workspace (index.html, app.js, style.css) and widget.js/.css for ELOG pages
```
