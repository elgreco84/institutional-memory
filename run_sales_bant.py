"""
Feed discovery-call transcripts to the Sales Call BANT Agent, one session per
transcript, with the CRM memory store attached.

Usage:
    python run_sales_bant.py                 # all calls in synthetic-data/sales-calls/
    python run_sales_bant.py 01 03           # only files starting with 01 and 03
"""

import os
import sys
from pathlib import Path

from anthropic import Anthropic


CALLS_DIR = Path("synthetic-data/sales-calls")


def process_call(client: Anthropic, path: Path, agent_id: str, env_id: str, store_id: str) -> None:
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=env_id,
        title=f"BANT — {path.stem}",
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": store_id,
                "access": "read_write",
                "instructions": (
                    "This is the sales CRM. Mounted at /mnt/memory/. One file per "
                    "deal under deals/. Follow your memory protocol."
                ),
            }
        ],
    )

    user_message = (
        "Process the discovery call transcript below. Update the CRM with BANT "
        "data for this deal only, then give me your short summary.\n\n"
        f"=====  TRANSCRIPT: {path.name}  =====\n{path.read_text()}"
    )

    with client.beta.sessions.events.stream(session.id) as stream:
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": user_message}]}],
        )
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if getattr(block, "type", None) == "text":
                        print(block.text, end="", flush=True)
            elif event.type == "agent.tool_use":
                inp = getattr(event, "input", {}) or {}
                target = inp.get("path") or inp.get("file_path") or inp.get("command") or ""
                if "/mnt/memory" in str(target):
                    print(f"\n  [memory: {event.name}  {target}]", flush=True)
            elif event.type == "session.status_idle":
                print("\n[agent finished]")
                break


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY before running.")

    ids = {}
    for key, fname in (("agent", ".bant_agent_id"), ("env", ".bant_environment_id"), ("store", ".bant_memory_store_id")):
        if not Path(fname).exists():
            raise SystemExit(f"Missing {fname}. Run create_sales_agent.py first.")
        ids[key] = Path(fname).read_text().strip()

    prefixes = sys.argv[1:]
    calls = sorted(CALLS_DIR.glob("*.md"))
    if prefixes:
        calls = [c for c in calls if any(c.name.startswith(p) for p in prefixes)]
    if not calls:
        raise SystemExit("No matching transcripts.")

    client = Anthropic()
    for call in calls:
        print(f"\n{'=' * 60}\n{call.name}\n{'=' * 60}")
        process_call(client, call, ids["agent"], ids["env"], ids["store"])

    print("\nDone. Inspect the CRM:  python inspect_memory.py --sales")


if __name__ == "__main__":
    main()
