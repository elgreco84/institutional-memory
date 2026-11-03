"""server.py - FastAPI backend for the sales-call-management project.

IMPORTANT - run from inside the directory:
    The enclosing folder ``sales-call-management`` contains a hyphen, so it is
    NOT a valid Python package name and cannot be imported. The sibling modules
    (``crm``, ``discovery``, ``reporting``) are therefore imported as flat
    top-level modules. For that to resolve, the server must always be launched
    with the project directory as the current working directory, e.g.::

        cd sales-call-management && uvicorn server:app

    Running uvicorn from the parent directory (``sales-call-management.server``)
    will not work.

The app exposes a small JSON/SSE API consumed by the static frontend in
``static/`` and serves that frontend at the site root.
"""

import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import crm
import discovery
import reporting

BASE_DIR = Path(__file__).resolve().parent
CALLS_DIR = BASE_DIR / "data" / "calls"
STATIC_DIR = BASE_DIR / "static"

RECENT_ACTIVITY_LIMIT = 10
RECENT_ACTIVITY_DISPLAY = 8
CLOSED_STAGES = frozenset({"Closed Won", "Closed Lost"})

app = FastAPI(title="Sales Call Management API")


class IngestRequest(BaseModel):
    """Request body for ``POST /api/calls/ingest``.

    ``call`` is the bare filename of a transcript under ``data/calls`` (for
    example ``01-northfold-renewal-expansion.md``), not a path.
    """

    call: str


class ChatRequest(BaseModel):
    """Request body for ``POST /api/chat``.

    ``session_id`` groups messages into a single reporting conversation and
    ``message`` is the user's latest question for the reporting assistant.
    """

    session_id: str
    message: str


def build_dashboard() -> dict:
    """Aggregate the data the dashboard UI needs into a single dict.

    This is pure Python with no LLM involvement. It combines:

    - ``crm.pipeline_summary()`` for the stat tiles and the "Pipeline by stage"
      breakdown,
    - ``crm.list_activities(limit=RECENT_ACTIVITY_LIMIT)`` (newest first) for the
      "Recent activity" feed, trimmed to the most recent entries,
    - the open opportunities (every opportunity whose stage is not a closed
      stage) from ``crm.list_opportunities()`` for the "Open opportunities"
      table, sorted by amount descending.

    Account names are resolved by matching each ``account_id`` against
    ``crm.list_accounts()``. The returned dict is JSON-serializable and shaped
    as::

        {
            "stats": {
                "open_pipeline", "weighted_pipeline", "open_count", "win_rate"
            },
            "pipeline_by_stage": [<stage summary dict>, ...],
            "recent_activity": [<activity dict with "account_name">, ...],
            "open_opportunities": [<open opportunity dict>, ...]
        }
    """
    summary = crm.pipeline_summary()
    by_stage = summary["by_stage"]
    totals = summary["totals"]

    weighted_pipeline = sum(
        row["weighted_amount"]
        for row in by_stage
        if row["stage"] not in CLOSED_STAGES
    )

    account_names = {
        account["id"]: account["name"] for account in crm.list_accounts()
    }

    recent_activity = []
    for activity in crm.list_activities(limit=RECENT_ACTIVITY_LIMIT)[:RECENT_ACTIVITY_DISPLAY]:
        entry = dict(activity)
        entry["account_name"] = account_names.get(activity.get("account_id"))
        recent_activity.append(entry)

    open_opportunities = [
        {
            "account_name": account_names.get(opp.get("account_id")),
            "name": opp["name"],
            "stage": opp["stage"],
            "amount": opp["amount"],
            "currency": opp["currency"],
            "close_date": opp["close_date"],
            "probability": opp["probability"],
            "next_step": opp["next_step"],
        }
        for opp in crm.list_opportunities()
        if opp["stage"] not in CLOSED_STAGES
    ]
    open_opportunities.sort(key=lambda opp: opp["amount"], reverse=True)

    return {
        "stats": {
            "open_pipeline": totals["open_amount"],
            "weighted_pipeline": weighted_pipeline,
            "open_count": totals["open_count"],
            "win_rate": totals["win_rate"],
        },
        "pipeline_by_stage": by_stage,
        "recent_activity": recent_activity,
        "open_opportunities": open_opportunities,
    }


@app.get("/api/dashboard")
def get_dashboard() -> dict:
    """Return the aggregated dashboard payload built by ``build_dashboard``."""
    return build_dashboard()


@app.get("/api/calls")
def list_calls() -> list[str]:
    """Return the sorted list of transcript filenames under ``data/calls``.

    Only ``*.md`` files are reported, as bare filenames (no directory part).
    Returns an empty list when the directory is absent or holds no transcripts.
    """
    if not CALLS_DIR.is_dir():
        return []
    return sorted(path.name for path in CALLS_DIR.glob("*.md"))


@app.post("/api/calls/ingest")
def ingest_call(body: IngestRequest) -> dict:
    """Resolve ``body.call`` to a transcript and run ``discovery.ingest`` on it.

    The filename is validated to be a plain ``*.md`` filename that resolves to a
    real file directly inside ``data/calls``. Path traversal or any attempt to
    escape the calls directory yields HTTP 400; a syntactically valid name that
    does not exist yields HTTP 404.
    """
    name = body.call
    if not name or "/" in name or "\\" in name or Path(name).name != name:
        raise HTTPException(status_code=400, detail="Invalid call filename")
    if not name.endswith(".md"):
        raise HTTPException(status_code=400, detail="Call must be a .md transcript")

    target = (CALLS_DIR / name).resolve()
    calls_root = CALLS_DIR.resolve()
    if calls_root != target.parent:
        raise HTTPException(status_code=400, detail="Resolved path escapes calls directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Call transcript not found")

    return discovery.ingest(str(target))


@app.post("/api/chat")
def chat(body: ChatRequest) -> StreamingResponse:
    """Stream the reporting assistant's answer as Server-Sent Events.

    ``reporting.answer`` is synchronous and delivers incremental text through a
    ``stream_cb`` callback. To surface those chunks as an SSE response we run
    ``reporting.answer`` on a background thread and have its callback push chunks
    onto a thread-safe ``queue.Queue``. The response generator (driven by the
    event loop) drains that queue, emitting each chunk as a ``data: <chunk>``
    SSE frame, and terminates with a ``data: [DONE]`` frame once the worker
    thread has finished. A sentinel object marks end-of-stream on the queue.
    """

    chunks: "queue.Queue[object]" = queue.Queue()
    sentinel = object()

    def stream_cb(chunk: str) -> None:
        chunks.put(chunk)

    def worker() -> None:
        try:
            reporting.answer(body.session_id, body.message, stream_cb=stream_cb)
        except Exception as exc:  # surface worker failure to the client stream
            chunks.put(exc)
        finally:
            chunks.put(sentinel)

    def event_stream():
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            item = chunks.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                yield f"data: [ERROR] {item}\n\n"
                break
            yield f"data: {item}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
