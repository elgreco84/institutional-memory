# Sales Call Management System — "Scope Creepers"

A sales call management system. A sales call "comes in" as a recorded transcript (a markdown
file, standing in for what would be a vector DB in production). Two agents operate over a CRM
datastore (a JSON file, standing in for a real CRM), and a web UI shows a sales dashboard
plus a chat interface wired to the reporting agent.

- **Sales Call Discovery Agent** — parses a call transcript, extracts sales entities, and
  reconciles/updates the CRM.
- **Sales Reporting Agent** — a chat agent that queries the CRM live and answers questions
  with the freshest data.

## Architecture decisions (locked)

| Decision | Choice |
|---|---|
| Location | Inside the `institutional-memory` repo, this `sales-call-management/` subdir |
| Agent runtime | **Local Messages-API tool loop** (no Managed Agents / no cloud) |
| UI | **FastAPI + vanilla JS** (no React / no bundler) |
| CRM store | **JSON file** (`data/crm.json`, regenerated from `data/crm.seed.json`) |
| Domain | **Literal sales CRM**: accounts, contacts, opportunities, activities |
| Model | `claude-opus-5-5` for both agents (adaptive thinking on; no `budget_tokens`) |

**Synthetic world.** Vendor = *Scope Creepers* (an AI consultancy). Client accounts reuse the
universe referenced in the repo's 1:1 transcripts so everything is coherent: Northfold Mutual,
Meridian Freight, Tallgrass Outdoor Co., Pellworth Health, Brightwell Retail, Okonjo Legal.
Contacts include Renata Vasquez (Northfold), Hannah Wexler (Meridian), Gabe Ortiz (Tallgrass).
The 1:1 transcripts under `../synthetic-data/one-on-ones/` are **not** ingested — they are
peripheral context only.

## Layout

```
sales-call-management/
├── README.md               # this file
├── requirements.txt        # anthropic, fastapi, uvicorn[standard], python-dotenv
├── crm.py                  # CRM datastore: schema, atomic load/save/reset, reads, upserts
├── tools.py                # tool JSON schemas + dispatch over crm.py (read + write)
├── agent_loop.py           # shared manual, streaming-capable Messages-API tool loop
├── discovery.py            # Sales Call Discovery Agent  (ingest transcript -> update CRM)
├── reporting.py            # Sales Reporting Agent  (chat, read-only tools, streaming)
├── server.py               # FastAPI app + static mount
├── static/                 # index.html, app.js, styles.css  (dashboard + chat)
├── data/
│   ├── crm.seed.json       # committed seed (working crm.json is gitignored, regenerated)
│   └── calls/              # sales-call transcripts (.md)
└── scripts/reset_crm.py    # copy seed -> working data/crm.json
```

## Data contract — `data/crm.json`

Frozen. Both agents' tools and the dashboard aggregation read/write this shape. Coordinate on
the `#scope-creepers` channel before changing a field.

```jsonc
{
  "accounts":      [{ "id", "name", "industry", "owner", "created_at", "updated_at" }],
  "contacts":      [{ "id", "account_id", "name", "title", "email" }],
  "opportunities": [{ "id", "account_id", "name", "stage", "amount", "currency",
                      "close_date", "probability", "next_step", "notes", "updated_at" }],
  "activities":    [{ "id", "account_id", "opportunity_id", "type", "date", "summary",
                      "source_call", "participants" }]
}
```

Stages: `Prospecting | Qualification | Proposal | Negotiation | Closed Won | Closed Lost`.

## Module contracts (frozen seams)

These signatures are the integration boundaries. Build behind them in parallel; don't change a
public signature without a heads-up.

- **`crm.py`** — `load()`, `save(db)`, `reset()`; reads `list_accounts()`, `find_account(name)`,
  `get_opportunity(id)`, `list_opportunities(stage, account, min_amount)`,
  `list_activities(account, limit)`, `pipeline_summary()`; writes `upsert_account(...)`,
  `upsert_contact(...)`, `upsert_opportunity(...)`, `log_activity(...)`.
- **`tools.py`** — `TOOL_SCHEMAS` (list of Anthropic tool defs), `READ_TOOL_NAMES`,
  `WRITE_TOOL_NAMES`, `dispatch(name, tool_input) -> dict`.
- **`agent_loop.py`** — `MODEL`, `run_loop(system, messages, allowed_tools, stream_cb=None)`.
- **`discovery.py`** — `ingest(call_path) -> dict` (change summary).
- **`reporting.py`** — `answer(session_id, message, stream_cb=None) -> str`.
- **`server.py`** — `GET /api/dashboard`, `GET /api/calls`, `POST /api/calls/ingest`,
  `POST /api/chat` (SSE), static mount at `/`.

## Parallel work plan (ownership)

Dependency order flows down. `crm.py` is the shared foundation — land it first; everything else
can scaffold against its signatures immediately.

| # | Chunk | Files | Depends on | Suggested owner |
|---|---|---|---|---|
| 1 | CRM datastore | `crm.py`, `data/crm.seed.json`, `scripts/reset_crm.py` | — | |
| 2 | Call transcripts | `data/calls/*.md` | data contract | |
| 3 | Tools + agent loop | `tools.py`, `agent_loop.py` | 1 | |
| 4 | Discovery agent | `discovery.py` | 3 | |
| 5 | Reporting agent | `reporting.py` | 3 | |
| 6 | FastAPI backend | `server.py` | 1, 4, 5 | |
| 7 | Frontend | `static/*` | 6 (API shape) | |

Per-chunk loop (team convention): implement → code-review → fix → generate tests → run tests →
green → open PR. Read the `dataviz` skill before writing any chart code in chunk 7.

## Setup

```bash
cd sales-call-management
python -m venv ../.venv && source ../.venv/bin/activate   # repo's .venv is gitignored
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."   # or: ant auth login  (then no env var needed)
```

## Run

```bash
cd sales-call-management
python scripts/reset_crm.py              # fresh data/crm.json from the seed
uvicorn server:app --reload              # run from INSIDE this dir (the hyphenated
                                         # folder name is not an importable package)
# open http://127.0.0.1:8000
```

## Demo beat (the "freshest data" moment)

1. Dashboard renders from the seed.
2. Pick a call in the UI and **Ingest** it → the discovery agent updates the CRM.
3. Dashboard refreshes: a new/changed opportunity and a logged activity appear.
4. In chat, ask *"What's our total open pipeline and which deals are in Negotiation?"* and
   *"What changed on Northfold after the latest call?"* — the answer reflects the new data.

## Status

Scaffold only. Every module below `crm.py`'s data contract currently raises
`NotImplementedError` with its contract documented in the docstring — these are the interface
seams for parallel work, not finished code.

## Out of scope (time-bound)

Vector DB, auth, multi-user, real CRM integration, persistence beyond the JSON file.
