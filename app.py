"""
Sales call memory — a small web UI over the Managed Agent + memory store pattern
used by the session scripts in this repo.

The agent reads discovery-call transcripts from synthetic-data/sales-calls/ and
keeps one memory file per prospect at prospects/<slug>.md in its own memory
store. The UI lists those files, shows them, and asks the agent to prep a call.

Usage:
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python create_agent.py      # once — the sales agent reuses its environment
    .venv/bin/uvicorn app:app --reload    # then open http://127.0.0.1:8000
"""

import hashlib
import html
import json
import re
import threading
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

CALLS_DIR = Path("synthetic-data/sales-calls")
CATEGORY = "**Category:** sales-discovery"

SALES_SYSTEM_PROMPT = """\
You are the Sales Memory Agent for Scope Creepers, an AI consultancy that sells
Claude/LLM implementations, app modernization, process automation and
enterprise AI rollouts.

You help sellers prepare for calls with PROSPECTS and spot opportunities to
expand an engagement beyond the first project.

# Memory protocol (mandatory)

Your persistent memory store survives across sessions. At the start of EVERY
session, list it and read anything relevant before doing anything else.

Keep exactly ONE file per prospect at `prospects/<slug>.md` inside your memory
store (slug = lowercase company name with hyphens, e.g. `ironbridge-freight`).
Each file uses these sections, in this order:

# <Company name>
**Industry:** ... | **AI need:** ... | **Stage:** ... | **Last call:** <date>

## BANT
| | Status | What we know | Source |
|---|---|---|---|
| Budget | Known / Partial / Unknown | ... | call date + timestamp |
| Authority | ... | who actually decides / signs | ... |
| Need | ... | ... | ... |
| Timeline | ... | ... | ... |
Only mark an item Known when a call established it — a guess or a hint is
Partial. When a later call corrects an item, update it and add "(corrected
<date>: was ...)".

## Summary
## People (name — role — stance)
## Pains (with numbers)
## Objections & risks
## Competitors & alternatives
## Next steps (owner, date)
## Open questions / our loose commitments
## Expansion opportunities
Each item: the opportunity, why it matters, and the supporting quote with its
transcript timestamp.

When a new call contradicts the file, UPDATE it (don't append) and note the
date of the change. Do not copy transcripts into memory.

# How to answer

Be concise and specific. Cite timestamps when you rely on a call.
"""


_ids_lock = threading.Lock()


def ids() -> tuple[str, str]:
    """Return (sales_agent_id, sales_memory_store_id), creating them on first use."""
    with _ids_lock:  # concurrent first requests must not create two agents
        return _ids()


def _ids() -> tuple[str, str]:
    agent_path, store_path = Path(".sales_agent_id"), Path(".sales_memory_store_id")
    client = Anthropic()
    # The id file holds "<agent_id> <prompt fingerprint>"; a changed prompt gets a fresh agent,
    # since an agent's system prompt is fixed when it's created.
    fingerprint = hashlib.sha256(SALES_SYSTEM_PROMPT.encode()).hexdigest()[:12]
    saved = agent_path.read_text().split() if agent_path.exists() else []
    if saved[1:] != [fingerprint]:
        agent = client.beta.agents.create(
            name="Sales Memory Agent",
            model="claude-sonnet-4-6",  # same model as create_agent.py
            system=SALES_SYSTEM_PROMPT,
            tools=[{"type": "agent_toolset_20260401"}],
            metadata={"hackathon": "partner-basecamp-2026", "track": "sales-memory"},
        )
        agent_path.write_text(f"{agent.id} {fingerprint}")
    if not store_path.exists():
        store = client.beta.memory_stores.create(
            name="Sales Memory",
            description=(
                "One file per sales prospect (prospects/<slug>.md): people, pains, "
                "budget, objections, next steps and expansion opportunities learned "
                "from discovery calls. Newer entries supersede older ones."
            ),
        )
        store_path.write_text(store.id)
    return agent_path.read_text().split()[0], store_path.read_text().strip()


def run_agent(title: str, message: str) -> str:
    """One session against the sales agent with its memory attached. Same loop as run_session_1.py."""
    env_path = Path(".environment_id")
    if not env_path.exists():
        raise RuntimeError("Missing .environment_id — run create_agent.py first.")
    agent_id, store_id = ids()
    client = Anthropic()
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=env_path.read_text().strip(),
        title=title,
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": store_id,
                "access": "read_write",
                "instructions": "Your persistent sales memory. One file per prospect under prospects/.",
            }
        ],
    )
    parts: list[str] = []
    with client.beta.sessions.events.stream(session.id) as stream:
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        )
        for event in stream:
            if event.type == "agent.message":
                parts.extend(b.text for b in event.content if getattr(b, "type", None) == "text")
            elif event.type == "session.status_idle":
                break
    return "".join(parts)


def prospects() -> dict[str, str]:
    """slug -> memory content for every prospects/*.md in the sales store."""
    _, store_id = ids()
    client = Anthropic()
    out = {}
    # order_by isn't honored by this endpoint (see inspect_memory.py) — sort ourselves.
    for item in sorted(client.beta.memory_stores.memories.list(store_id, path_prefix="/").data, key=lambda i: i.path):
        if item.type == "memory" and "/prospects/" in item.path:
            content = client.beta.memory_stores.memories.retrieve(item.id, memory_store_id=store_id).content or ""
            out[Path(item.path).stem] = content
    return out


# ponytail: jobs live in process memory and vanish on restart — fine for a local demo.
JOBS: dict[str, dict] = {}


def start_job(label: str, back: str, title: str, message: str, on_done=None) -> str:
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"label": label, "back": back, "status": "running", "result": ""}

    def work() -> None:
        try:
            JOBS[job_id].update(status="done", result=run_agent(title, message))
            if on_done:
                on_done()
        except Exception as e:  # surface API/setup errors on the job page
            JOBS[job_id].update(status="error", result=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return job_id


# Which calls have been processed, per memory store, so a fresh store starts all-grey.
PROCESSED_PATH = Path(".processed_calls.json")
_processed_lock = threading.Lock()


def processed_calls() -> set[str]:
    _, store_id = ids()
    try:
        return set(json.loads(PROCESSED_PATH.read_text()).get(store_id, []))
    except (FileNotFoundError, ValueError):
        return set()


def mark_processed(name: str) -> None:
    _, store_id = ids()
    with _processed_lock:
        try:
            data = json.loads(PROCESSED_PATH.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data[store_id] = sorted(set(data.get(store_id, [])) | {name})
        PROCESSED_PATH.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------- web
# UI: Basecoat (shadcn look, plain HTML classes) + Tailwind v4 browser build, both from CDN.

app = FastAPI()

NAV = [("/", "Prospects"), ("/opportunities", "Opportunities"), ("/calls", "Process calls")]


def call_bar() -> str:
    """Buttons 1..n, one per sales call in order; green once processed into the current memory store."""
    done = processed_calls()
    return "".join(
        f'<form method="post" action="/calls/{p.name}/process" class="inline">'
        f'<button class="btn{" bg-green-600 border-green-600 text-white hover:bg-green-700" if p.name in done else ""}" '
        f'data-size="sm" data-variant="{"primary" if p.name in done else "outline"}" '
        f'title="{"Processed — run again: " if p.name in done else "Process "}{p.name}">{i}</button></form>'
        for i, p in enumerate(sales_calls(), 1)
    )


def page(title: str, body: str, active: str = "/", refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="4">' if refresh else ""
    nav = "".join(
        f'<a href="{href}" class="btn" data-size="sm" data-variant="{"secondary" if href == active else "ghost"}">{label}</a>'
        for href, label in NAV
    )
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>{html.escape(title)} · Sales Memory</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/basecoat-css@1.0.2/dist/basecoat.cdn.min.css">
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.1.11/dist/index.global.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.2.6/purify.min.js"></script>
<style>
  @layer base{{*,::before,::after{{border-color:var(--border)}}}}
  .md h1{{font-size:1.5rem;font-weight:600;letter-spacing:-.01em;margin:0 0 .5rem}}
  .md h2{{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted-foreground);margin:2rem 0 .6rem}}
  .md h3{{font-weight:600;margin:1rem 0 .25rem}} .md p{{margin:.5rem 0;line-height:1.6}}
  .md ul{{list-style:disc;padding-left:1.25rem;margin:.5rem 0}} .md ol{{list-style:decimal;padding-left:1.25rem}}
  .md li{{margin:.3rem 0;line-height:1.55}} .md code{{background:var(--muted);padding:0 .3rem;border-radius:.25rem;font-size:.85em}}
  .md blockquote{{border-left:3px solid var(--border);padding-left:.75rem;color:var(--muted-foreground)}}
  .md .table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius);margin:.5rem 0}}
</style></head>
<body class="min-h-screen bg-muted/40">
<header class="border-b bg-background">
  <div class="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
    <a href="/" class="font-semibold tracking-tight mr-2">Scope Creepers <span class="text-muted-foreground font-normal">· Sales Memory</span></a>
    <nav class="flex gap-1">{nav}</nav>
  </div>
  <div class="border-t bg-muted/30"><div class="max-w-5xl mx-auto px-4 py-2 flex flex-wrap items-center gap-1.5"><span class="text-xs text-muted-foreground mr-1">Calls</span>{call_bar()}</div></div>
</header>
<main class="max-w-5xl mx-auto px-4 py-8">{body}</main>
<script>
  const STATUS = {{known: "", partial: "secondary", unknown: "outline"}};
  document.querySelectorAll('.md').forEach(el => {{
    el.innerHTML = DOMPurify.sanitize(marked.parse(el.textContent));
    el.querySelectorAll('table').forEach(t => {{
      t.classList.add('table');
      const wrap = document.createElement('div'); wrap.className = 'table-wrap';
      t.replaceWith(wrap); wrap.appendChild(t);
    }});
    // BANT status cells -> badges
    el.querySelectorAll('td').forEach(td => {{
      const v = STATUS[td.textContent.trim().toLowerCase()];
      if (v !== undefined) td.innerHTML = `<span class="badge" data-variant="${{v}}">${{td.textContent.trim()}}</span>`;
    }});
  }});
</script>
</body></html>""")


def md(text: str) -> str:
    """Markdown rendered client-side (marked + DOMPurify); escaped here so it arrives as text."""
    return f'<div class="md">{html.escape(text)}</div>'


def button(action: str, label: str, attrs: str = "") -> str:
    return f'<form method="post" action="{action}" class="inline"><button class="btn" {attrs}>{label}</button></form>'


def first_line(content: str, prefix: str) -> str:
    return next((l for l in content.splitlines() if l.startswith(prefix)), "")


BANT = ("Budget", "Authority", "Need", "Timeline")
BADGE = {"known": "", "partial": "secondary"}


def bant(content: str) -> dict[str, tuple[str, str, str]]:
    """item -> (status, what we know, correction note), from the BANT table rows: '| Budget | Known | ... | source |'."""
    out = {}
    for item in BANT:
        cells = [c.strip() for c in first_line(content.replace("**", ""), f"| {item} |").split("|")]
        status, _, extra = cells[2].partition(" ") if len(cells) > 3 else ("unknown", "", "")
        out[item] = (status.lower(), cells[3] if len(cells) > 3 else "", extra.strip(" ()"))
    return out


def bant_badges(content: str) -> str:
    out = [f'<span class="badge" data-variant="{BADGE.get(s, "outline")}" title="{item}: {s}">{item}</span>'
           for item, (s, _, _) in bant(content).items()]
    return f'<div class="flex flex-wrap gap-1.5">{"".join(out)}</div>'


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities():
    rows = ""
    for slug, c in prospects().items():
        cells = ""
        for item, (status, what, extra) in bant(c).items():
            # ponytail: only "known" ticks the box; "partial" shows as a muted label next to an empty box.
            box = f'<input type="checkbox" class="input mt-0.5" disabled {"checked" if status == "known" else ""} aria-label="{item} {status}">'
            note = " · ".join(x for x in ("" if status == "known" else status, extra) if x)
            note = f'<div class="text-xs text-muted-foreground mt-1 whitespace-normal">{html.escape(note)}</div>' if note else ""
            cells += (f'<td class="align-top"><div class="flex items-start gap-2">{box}<div class="min-w-0">'
                      f'<div class="text-sm line-clamp-3 whitespace-normal" title="{html.escape(what)}">{html.escape(what) or "—"}</div>{note}</div></div></td>')
        name = first_line(c, "# ").lstrip("# ") or slug
        rows += (f'<tr><td class="align-top font-medium whitespace-normal"><a class="underline-offset-4 hover:underline" '
                 f'href="/prospects/{slug}">{html.escape(name)}</a></td>{cells}</tr>')
    body = rows or '<tr><td colspan="5" class="text-sm text-muted-foreground">No prospects yet — process some calls.</td></tr>'
    return page("Opportunities",
                '<div class="mb-6"><h1 class="text-2xl font-semibold tracking-tight">Opportunities</h1>'
                '<p class="text-muted-foreground text-sm">BANT per prospect. A ticked box means a call established it.</p></div>'
                '<div class="card p-0 overflow-x-auto"><table class="table table-fixed w-full"><thead><tr><th class="w-40">Client</th>'
                + "".join(f'<th>{b}</th>' for b in BANT)
                + f'</tr></thead><tbody>{body}</tbody></table></div>', active="/opportunities")


@app.get("/", response_class=HTMLResponse)
def home():
    items = prospects()
    head = ('<div class="mb-6"><h1 class="text-2xl font-semibold tracking-tight">Prospects</h1>'
            '<p class="text-muted-foreground text-sm">What the agent remembers from discovery calls. '
            'Filled badges are BANT items established on a call.</p></div>')
    if not items:
        return page("Prospects", head + '<div class="card"><section class="text-sm">No prospects in memory yet. '
                    '<a class="underline" href="/calls">Process a call</a> to start.</section></div>')
    cards = "".join(
        f'<a href="/prospects/{slug}" class="card hover:shadow-md transition-shadow">'
        f'<header><h2>{html.escape(first_line(c, "# ").lstrip("# ") or slug)}</h2>'
        f'<p>{html.escape(first_line(c, "**Industry").replace("**", ""))}</p></header>'
        f'<section>{bant_badges(c)}</section></a>'
        for slug, c in items.items()
    )
    return page("Prospects", head + f'<div class="grid gap-4 sm:grid-cols-2">{cards}</div>')


@app.get("/prospects/{slug}", response_class=HTMLResponse)
def prospect(slug: str):
    content = prospects().get(slug)
    if content is None:
        raise HTTPException(404, "Prospect not in memory")
    return page(slug,
                f'<div class="flex items-center justify-between gap-4 mb-4">'
                f'<a href="/" class="btn" data-size="sm" data-variant="ghost">← Prospects</a>'
                f'{button(f"/prospects/{slug}/prep", "Prep me for the next call")}</div>'
                f'<div class="card"><section>{md(content)}</section></div>')


@app.post("/prospects/{slug}/prep")
def prep(slug: str):
    job = start_job(f"Call prep: {slug}", f"/prospects/{slug}", f"Prep — {slug}",
                    f"Read your memory file prospects/{slug}.md. I have the next call with this prospect "
                    "tomorrow. Give me: the goal for the call, which BANT gaps to close and the exact questions "
                    "to ask, how to handle each known objection, the loose commitments we owe them, and which "
                    "expansion opportunity to plant and how. Do not change memory unless something is wrong.")
    return RedirectResponse(f"/jobs/{job}", status_code=303)


def sales_calls() -> list[Path]:
    # Only transcripts tagged as sales discovery — 1:1s etc. are ignored.
    return [p for p in sorted(CALLS_DIR.glob("*.md")) if CATEGORY in p.read_text()]


@app.get("/calls", response_class=HTMLResponse)
def calls():
    rows = "".join(
        f'<tr><td><div class="font-medium">{html.escape(first_line(p.read_text(), "# ").lstrip("# ").removeprefix("Discovery Call: ") or p.name)}</div>'
        f'<div class="text-xs text-muted-foreground">{p.name}</div></td>'
        f'<td class="text-sm text-muted-foreground whitespace-nowrap">{html.escape(first_line(p.read_text(), "- **Date:**").replace("- **Date:**", "").strip())}</td>'
        f'<td class="text-right">{button(f"/calls/{p.name}/process", "Process", 'data-size="sm" data-variant="outline"')}</td></tr>'
        for p in sales_calls()
    )
    return page("Process calls",
                '<div class="mb-6"><h1 class="text-2xl font-semibold tracking-tight">Process calls</h1>'
                f'<p class="text-muted-foreground text-sm">Transcripts in {CALLS_DIR}/. Process a prospect\'s calls in order — '
                'each run takes a minute or two.</p></div>'
                f'<div class="card p-0 overflow-x-auto"><table class="table"><thead><tr><th>Call</th><th>Date</th><th></th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>', active="/calls")


@app.post("/calls/{name}/process")
def process(name: str):
    path = next((p for p in sales_calls() if p.name == name), None)  # whitelist — no path traversal
    if path is None:
        raise HTTPException(404, "Unknown transcript")
    job = start_job(f"Processing {name}", "/", f"Process — {name}",
                    "Below is a discovery-call transcript. Check your memory first, then create or UPDATE "
                    "the prospect's file in prospects/ following your protocol. Reply with a short summary "
                    f"of what you saved or changed.\n\n===== TRANSCRIPT: {name} =====\n{path.read_text()}",
                    on_done=lambda: mark_processed(name))
    return RedirectResponse(f"/jobs/{job}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job(job_id: str):
    j = JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Unknown job (the server may have restarted)")
    head = f'<h1 class="text-2xl font-semibold tracking-tight mb-4">{html.escape(j["label"])}</h1>'
    if j["status"] == "running":
        return page(j["label"], head + '<div class="card"><section class="flex items-center gap-3 text-sm">'
                    '<span class="size-3 rounded-full bg-primary animate-pulse"></span>'
                    'Agent working… this page refreshes itself.</section></div>', refresh=True)
    if j["status"] == "done":
        body = f'<div class="card"><section>{md(j["result"])}</section></div>'
    else:
        body = (f'<div class="alert" data-variant="destructive"><h2>Agent run failed</h2>'
                f'<section><pre class="whitespace-pre-wrap text-sm">{html.escape(j["result"])}</pre></section></div>')
    return page(j["label"], head + body + f'<a class="btn mt-4" data-variant="outline" href="{j["back"]}">Back</a>')
