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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

load_dotenv()

CALLS_DIR = Path("synthetic-data/sales-calls")
CATEGORY = "**Category:** sales-discovery"

SALES_MODEL = "claude-haiku-5-5"  # fast; "claude-sonnet-4-6" picks up more nuance but ~2x slower

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
| | Status | Headline | Detail | Source |
|---|---|---|---|---|
| Budget | Known / Partial / Unknown | ≤6 words, e.g. "~$550K available" | one sentence | call date + timestamp |
| Authority | ... | who actually decides, e.g. "Linda Brandt, CFO — signs" | ... | ... |
| Need | ... | ... | ... | ... |
| Timeline | ... | ... | ... | ... |
Status is exactly one word. Only mark an item Known when a call established
it — a guess or a hint is Partial. When a later call corrects an item, update
it and start the Detail with "Corrected <Mon D, YYYY> — previously ...".

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

# Speed (important — the seller is waiting)

- Keep the file tight: at most ~5 bullets per section, one line each, and
  under ~6,000 characters overall. Prefer numbers and names over prose.
- Save with exactly ONE write of the whole file per session (new or
  existing prospect). Do not make multiple edits — each tool call is a slow
  round trip. Carry forward everything still true from the old file.
- Don't re-read a file you just wrote. Don't explore other prospects' files.

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
    # The id file holds "<agent_id> <fingerprint>"; a changed model or prompt gets a fresh agent,
    # since both are fixed when an agent is created.
    fingerprint = hashlib.sha256((SALES_MODEL + SALES_SYSTEM_PROMPT).encode()).hexdigest()[:12]
    saved = agent_path.read_text().split() if agent_path.exists() else []
    if saved[1:] != [fingerprint]:
        agent = client.beta.agents.create(
            name="Sales Memory Agent",
            model=SALES_MODEL,
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

NAV = [("/", "Prospects"), ("/opportunities", "Opportunities"), ("/calls", "Process calls"), ("/architecture", "Architecture")]
ARCHITECTURE = Path("sales-call-management/static/architecture.html")


def call_bar() -> str:
    """Square buttons 1..n: green+check once processed, pink for the next one, plus 'Process call N'."""
    calls, done = sales_calls(), processed_calls()
    nxt = next((i for i, p in enumerate(calls, 1) if p.name not in done), None)
    buttons = ""
    for i, p in enumerate(calls, 1):
        state = "done" if p.name in done else "next" if i == nxt else "todo"
        label = ICON_CHECK if state == "done" else str(i)
        tip = f"Call {i}: {p.name}" + (" — processed, click to run again" if state == "done" else "")
        buttons += (f'<form method="post" action="/calls/{p.name}/process">'
                    f'<button class="sm-call sm-call-{state}" title="{html.escape(tip)}" aria-label="{html.escape(tip)}">{label}</button></form>')
    cta = (f'<form method="post" action="/calls/{calls[nxt - 1].name}/process">'
           f'<button class="sm-btn-dark">{ICON_ARROW}Process call {nxt}</button></form>') if nxt else ""
    reset = ('<form method="post" action="/reset" onsubmit="return confirm(\'Clear all prospect memory and mark every call unprocessed?\')">'
             '<button class="sm-btn-outline" style="font-family:inherit;cursor:pointer" title="Delete everything the agent remembers so you can rerun the calls">Clear memory</button></form>')
    return (f'<div class="sm-calls-meta"><span class="sm-label">CALLS</span>'
            f'<span style="font-size:13px"><b style="font-weight:600">{len(done & {p.name for p in calls})}</b> of {len(calls)} processed</span></div>'
            f'<div class="sm-call-list">{buttons}</div><div style="display:flex;gap:8px">{reset}{cta}</div>')


def page(title: str, body: str, active: str = "/", refresh: bool = False, wide: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="4">' if refresh else ""
    nav = "".join(
        f'<a href="{href}" class="sm-tab"{" aria-current=page" if href == active else ""}>{label}</a>'
        for href, label in NAV
    )
    width = "sm-wrap"
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>{html.escape(title)} · Sales Memory</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/basecoat-css@1.0.2/dist/basecoat.cdn.min.css">
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.1.11/dist/index.global.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.2.6/purify.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  /* Design tokens from "Sales Memory — Opportunities" mock (stone palette, pink accent). */
  :root{{--sm-bg:#fafaf9;--sm-fg:#0c0a09;--sm-muted:#57534e;--sm-subtle:#78716c;--sm-line:#e7e5e4;--sm-soft:#f5f5f4;
         --sm-ok:#15803d;--sm-warn:#d97706;--sm-accent:#FF5C8F}}
  body{{background:var(--sm-bg);color:var(--sm-fg);font-family:Inter,system-ui,sans-serif;font-size:14px;line-height:1.6;font-feature-settings:"tnum"}}
  a:focus-visible,button:focus-visible{{outline:2px solid var(--sm-accent);outline-offset:2px}}
  .sm-wrap{{max-width:1280px;margin:0 auto;padding:0 32px}} .sm-narrow{{max-width:1024px}}
  .sm-bar{{background:#fff;border-bottom:1px solid var(--sm-line)}}
  .sm-top{{min-height:64px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 40px}}
  .sm-brand{{display:flex;align-items:baseline;gap:8px;text-decoration:none;color:var(--sm-fg)}}
  .sm-brand b{{font-size:16px;font-weight:700;letter-spacing:-.01em}} .sm-brand span{{color:var(--sm-subtle)}}
  .sm-nav{{display:flex;align-self:stretch;gap:4px}}
  .sm-tab{{display:flex;align-items:center;padding:0 12px;font-weight:500;color:var(--sm-muted);text-decoration:none;border-bottom:2px solid transparent}}
  .sm-tab:hover{{color:var(--sm-fg)}} .sm-tab[aria-current]{{font-weight:600;color:var(--sm-fg);border-bottom-color:var(--sm-accent)}}
  .sm-calls{{padding-top:14px;padding-bottom:14px;display:flex;flex-wrap:wrap;align-items:center;gap:12px 24px}}
  .sm-calls-meta{{display:flex;flex-direction:column;gap:2px;min-width:150px}}
  .sm-label{{font-size:11px;font-weight:600;letter-spacing:1.5px;color:var(--sm-subtle)}}
  .sm-call-list{{display:flex;flex-wrap:wrap;gap:6px;flex:1 1 300px;min-width:0}}
  .sm-call{{width:36px;height:36px;border-radius:6px;display:flex;align-items:center;justify-content:center;font:500 13px Inter,sans-serif;cursor:pointer;border:1px solid var(--sm-line);background:#fff;color:var(--sm-muted)}}
  .sm-call-todo:hover{{border-color:var(--sm-muted)}}
  .sm-call-done{{background:var(--sm-ok);border-color:var(--sm-ok);color:#fff}} .sm-call-done:hover{{background:#166534}}
  .sm-call-next{{border:1.5px solid var(--sm-accent);background:#fff1f5;color:var(--sm-fg);font-weight:600}}
  .sm-btn-dark{{height:36px;padding:0 14px;border-radius:6px;border:1px solid var(--sm-fg);background:var(--sm-fg);color:#fff;font:500 14px Inter,sans-serif;display:flex;align-items:center;gap:8px;cursor:pointer}}
  .sm-btn-dark:hover{{background:#292524}}
  .sm-btn-outline{{height:36px;padding:0 12px;border-radius:6px;border:1px solid var(--sm-line);background:#fff;font-weight:500;text-decoration:none;color:var(--sm-fg);display:flex;align-items:center;gap:6px}}
  .sm-btn-outline:hover{{background:var(--sm-soft)}}
  .sm-main{{padding-top:40px;padding-bottom:48px;display:flex;flex-direction:column;gap:24px}}
  .sm-titlebar{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px}}
  .sm-h1{{margin:0;font-size:30px;line-height:1.2;font-weight:700;letter-spacing:-.02em}} .sm-sub{{margin:0;color:var(--sm-muted)}}
  .sm-legend{{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;color:var(--sm-muted)}} .sm-legend>span{{display:flex;align-items:center;gap:6px}}
  .sm-dot{{width:8px;height:8px;border-radius:999px;display:inline-block}}
  .sm-card{{background:#fff;border:1px solid var(--sm-line);border-radius:8px;overflow:hidden}}
  .sm-card-head{{padding:18px 20px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid var(--sm-line)}}
  .sm-who{{display:flex;align-items:center;gap:14px;min-width:0}}
  .sm-avatar{{width:40px;height:40px;border-radius:8px;background:var(--sm-soft);border:1px solid var(--sm-line);display:flex;align-items:center;justify-content:center;font-weight:600;color:#44403c}}
  .sm-name{{margin:0;font-size:18px;line-height:1.3;font-weight:600}}
  .sm-progress{{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;font-size:13px;color:var(--sm-muted)}} .sm-progress b{{font-weight:600;color:var(--sm-fg)}}
  .sm-segs{{display:flex;gap:3px}} .sm-seg{{width:18px;height:6px;border-radius:3px;background:var(--sm-line)}}
  .sm-seg-known{{background:var(--sm-ok)}} .sm-seg-partial{{background:#f59e0b}}
  .sm-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1px;background:var(--sm-line)}}
  .sm-cell{{background:#fff;padding:18px 20px 20px;display:flex;flex-direction:column;gap:8px}}
  .sm-cell-top{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
  .sm-pill{{display:inline-flex;align-items:center;gap:4px;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;border:1px solid}}
  .sm-pill-ok{{background:#f0fdf4;border-color:#bbf7d0;color:var(--sm-ok)}} .sm-pill-partial{{background:#fffbeb;border-color:#fde68a;color:#b45309}}
  .sm-pill-unknown{{background:var(--sm-soft);border-color:var(--sm-line);color:var(--sm-subtle)}}
  .sm-head-lg{{font-size:20px;line-height:1.3;font-weight:600;letter-spacing:-.01em}} .sm-head{{font-size:16px;line-height:1.4;font-weight:600}}
  .sm-detail{{margin:0;font-size:13px;color:var(--sm-muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
  .sm-head-lg,.sm-head{{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
  @media (max-width:640px){{.sm-wrap{{padding:0 16px}}}}
  @layer base{{*,::before,::after{{border-color:var(--border)}}}}
  .md h1{{font-size:1.5rem;font-weight:600;letter-spacing:-.01em;margin:0 0 .5rem}}
  .md h2{{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted-foreground);margin:2rem 0 .6rem}}
  .md h3{{font-weight:600;margin:1rem 0 .25rem}} .md p{{margin:.5rem 0;line-height:1.6}}
  .md ul{{list-style:disc;padding-left:1.25rem;margin:.5rem 0}} .md ol{{list-style:decimal;padding-left:1.25rem}}
  .md li{{margin:.3rem 0;line-height:1.55}} .md code{{background:var(--muted);padding:0 .3rem;border-radius:.25rem;font-size:.85em}}
  .md blockquote{{border-left:3px solid var(--border);padding-left:.75rem;color:var(--muted-foreground)}}
  .md td,.md th{{white-space:normal;vertical-align:top}}
  .md .table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius);margin:.5rem 0}}
</style></head>
<body class="min-h-screen">
<header class="sm-bar"><div class="sm-wrap sm-top">
  <a href="/" class="sm-brand"><b>Scope Creepers</b><span>Sales Memory</span></a>
  <nav class="sm-nav" aria-label="Primary">{nav}</nav>
</div></header>
<section class="sm-bar" aria-label="Calls"><div class="sm-wrap sm-calls">{call_bar()}</div></section>
<main class="{width} {"sm-main" if wide else ""}" style="{"" if wide else "padding-top:32px;padding-bottom:48px"}">{body}</main>
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
    """item -> (status, headline, detail) from rows '| Budget | Known | headline | detail | source |'."""
    out = {}
    for item in BANT:
        cells = [c.strip() for c in first_line(content.replace("**", ""), f"| {item} |").split("|")]
        if len(cells) < 5:
            out[item] = ("unknown", "", "")
            continue
        status = (cells[2].split() or ["unknown"])[0].lower()
        if len(cells) >= 7:  # | item | status | headline | detail | source |
            out[item] = (status, cells[3], cells[4])
        else:  # older | item | status | what we know | source |
            out[item] = (status, cells[3][:70], cells[3])
    return out


def bant_badges(content: str) -> str:
    out = [f'<span class="badge" data-variant="{BADGE.get(s, "outline")}" title="{item}: {s}">{item}</span>'
           for item, (s, _, _) in bant(content).items()]
    return f'<div class="flex flex-wrap gap-1.5">{"".join(out)}</div>'


ICON_CHECK = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>'
ICON_INFO = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg>'
ICON_CHEVRON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>'
ICON_ARROW = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M12 5l7 7-7 7"/></svg>'

PILL = {
    "known": ("sm-pill-ok", ICON_CHECK, "Confirmed"),
    "partial": ("sm-pill-partial", ICON_INFO, "Partial"),
}


def initials(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", name) if w.lower() not in {"llp", "inc", "and", "the"}]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def opportunity_card(slug: str, content: str) -> str:
    name = first_line(content, "# ").lstrip("# ") or slug
    b = bant(content)
    confirmed = [k for k, (s, _, _) in b.items() if s == "known"]
    open_items = [k.lower() for k in BANT if k not in confirmed]
    segs = "".join(f'<span class="sm-seg sm-seg-{s}"></span>' for s, _, _ in b.values())
    summary = f'<b>{len(confirmed)} of 4</b> confirmed' + (f' · {", ".join(open_items)} open' if open_items else "")
    cells = ""
    for item, (status, headline, detail) in b.items():
        cls, icon, label = PILL.get(status, ("sm-pill-unknown", "", "Unknown"))
        size = "sm-head-lg" if item == "Budget" else "sm-head"
        cells += (f'<div class="sm-cell"><div class="sm-cell-top"><span class="sm-label">{item.upper()}</span>'
                  f'<span class="sm-pill {cls}">{icon}{label}</span></div>'
                  f'<div class="{size}" title="{html.escape(headline)}">{html.escape(headline) or "—"}</div>'
                  + (f'<p class="sm-detail" title="{html.escape(detail)}">{html.escape(detail)}</p>' if detail and detail != headline else "")
                  + '</div>')
    return (f'<article class="sm-card"><header class="sm-card-head"><div class="sm-who">'
            f'<div class="sm-avatar" aria-hidden="true">{html.escape(initials(name))}</div>'
            f'<div><h2 class="sm-name">{html.escape(name)}</h2>'
            f'<div class="sm-progress"><span class="sm-segs" aria-hidden="true">{segs}</span><span>{summary}</span></div></div></div>'
            f'<a class="sm-btn-outline" href="/prospects/{slug}">View prospect{ICON_CHEVRON}</a></header>'
            f'<div class="sm-grid">{cells}</div></article>')


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities():
    cards = "".join(opportunity_card(slug, c) for slug, c in prospects().items())
    head = ('<div class="sm-titlebar"><div><h1 class="sm-h1">Opportunities</h1>'
            '<p class="sm-sub">BANT per prospect, built up from processed calls.</p></div>'
            '<div class="sm-legend"><span><span class="sm-dot" style="background:#15803d"></span>Confirmed on a call</span>'
            '<span><span class="sm-dot" style="background:#d97706"></span>Partial</span></div></div>')
    empty = '<div class="sm-card" style="padding:20px">No prospects yet. Process call 1 to start.</div>'
    return page("Opportunities", head + (cards or empty), active="/opportunities", wide=True)


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
                    "the prospect's file in prospects/ following your protocol. Then reply with at most 5 "
                    f"short bullets: what changed in BANT and anything corrected.\n\n===== TRANSCRIPT: {name} =====\n{path.read_text()}",
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


@app.get("/architecture", response_class=HTMLResponse)
def architecture():
    # The diagram is a self-contained page with its own (dark) styles, so embed it rather than restyle it.
    if not ARCHITECTURE.exists():
        raise HTTPException(404, f"{ARCHITECTURE} not found — pull main")
    return page("Architecture",
                # Size the frame to the diagram's own height (same origin) so there's no inner scrollbar.
                '<iframe id="arch" src="/architecture/diagram" title="System architecture diagram" scrolling="no" '
                'style="display:block;width:100%;height:80vh;border:0;border-radius:8px;background:#0f172a"></iframe>'
                '<script>(() => {'
                'const f = document.getElementById("arch");'
                'const fit = () => { const d = f.contentDocument; if (d && d.body) {'
                ' d.documentElement.style.overflow = "hidden";'
                # the diagram's body has min-height:100vh, which would track the frame height; measure content instead
                ' d.body.style.minHeight = "0";'
                ' f.style.height = Math.ceil(d.body.getBoundingClientRect().height) + "px"; } };'
                'f.addEventListener("load", () => { fit();'
                ' new ResizeObserver(fit).observe(f.contentDocument.body); });'
                'addEventListener("resize", fit);'
                '})();</script>',
                active="/architecture", wide=True)


@app.get("/architecture/diagram")
def architecture_diagram():
    return FileResponse(ARCHITECTURE, media_type="text/html")


@app.post("/reset")
def reset():
    """Delete every memory in the current store and its processed-call list. Keeps the store and agent."""
    if any(j["status"] == "running" for j in JOBS.values()):
        raise HTTPException(409, "An agent run is still in progress — wait for it to finish, then clear.")
    _, store_id = ids()
    client = Anthropic()
    for item in client.beta.memory_stores.memories.list(store_id, path_prefix="/").data:
        if item.type == "memory":
            client.beta.memory_stores.memories.delete(item.id, memory_store_id=store_id)
    with _processed_lock:
        try:
            data = json.loads(PROCESSED_PATH.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data.pop(store_id, None)
        PROCESSED_PATH.write_text(json.dumps(data, indent=2))
    return RedirectResponse("/opportunities", status_code=303)
