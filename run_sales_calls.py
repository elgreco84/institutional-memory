"""
Sales calls — act on the call transcripts with what memory already holds.

Same agent, same memory store, fresh session. Inlines the transcripts in
synthetic-data/sales-calls/. The agent should:
- Read memory first (`/mnt/memory/`), including what it learned from the
  one-on-ones (run_one_on_ones.py)
- Compare the calls against what the one-on-ones said
- UPDATE memory rather than appending
- Lead its answer with what changed and why

Usage:
    python run_sales_calls.py
"""

import os
from pathlib import Path

from anthropic import Anthropic


TEST_QUESTION = (
    "For each customer on these sales calls, what happened, what was "
    "committed, and what needs to happen next (owner and date)? Flag anything "
    "that conflicts with what the one-on-one debriefs told you."
)

DOCS_DIR = Path("synthetic-data/sales-calls")
OUTPUT_DIR = Path("outputs")


def load_docs_as_context(docs_dir: Path) -> str:
    blocks = []
    for path in sorted(docs_dir.glob("*.md")):
        print(f"  including {path.name}")
        blocks.append(f"=====  DOCUMENT: {path.name}  =====\n{path.read_text()}")
    return "\n\n".join(blocks)


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY before running.")

    for required in (".agent_id", ".environment_id", ".memory_store_id"):
        if not Path(required).exists():
            raise SystemExit(f"Missing {required}. Run create_agent.py first.")

    agent_id = Path(".agent_id").read_text().strip()
    environment_id = Path(".environment_id").read_text().strip()
    memory_store_id = Path(".memory_store_id").read_text().strip()

    client = Anthropic()

    print(f"Loading sales call docs from {DOCS_DIR}/...")
    context = load_docs_as_context(DOCS_DIR)

    print(f"\nStarting fresh session with same memory store {memory_store_id}...")
    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        title="Sales calls",
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": memory_store_id,
                "access": "read_write",
                "instructions": (
                    "This is your persistent institutional memory. Some entries "
                    "may be out of date — reconcile against the sales call "
                    "transcripts in this session and UPDATE existing entries "
                    "(don't just append)."
                ),
            }
        ],
    )

    user_message = (
        "I'm including sales call transcripts below. Some may contradict "
        "things you learned from the one-on-one debriefs in a previous "
        "session.\n\n"
        "Please:\n"
        "1. First, check your memory store at /mnt/memory/ to see what you "
        "already know.\n"
        "2. Read the sales call transcripts below.\n"
        "3. Reconcile conflicts — UPDATE memory entries to reflect the "
        "newer information. Note dates.\n"
        "4. Answer the question.\n"
        "5. If your answer differs from your previous answer, lead with what "
        "changed and why.\n\n"
        f"{context}\n\n"
        "==================================================\n"
        f"QUESTION: {TEST_QUESTION}"
    )

    final_text_parts: list[str] = []
    print("\nAgent working...\n")
    with client.beta.sessions.events.stream(session.id) as stream:
        client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": user_message}],
                }
            ],
        )
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if getattr(block, "type", None) == "text":
                        final_text_parts.append(block.text)
                        print(block.text, end="", flush=True)
            elif event.type == "agent.tool_use":
                name = getattr(event, "name", "?")
                inp = getattr(event, "input", {}) or {}
                target = inp.get("path") or inp.get("file_path") or inp.get("command") or ""
                if "/mnt/memory" in str(target):
                    print(f"\n  [memory: {name}  {target}]", flush=True)
                else:
                    print(f"\n  [{name}]", flush=True)
            elif event.type == "session.status_idle":
                print("\n\n[agent finished]")
                break

    final_text = "".join(final_text_parts)
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "sales_calls.txt"
    out.write_text(
        f"=== SALES CALLS ===\nQuestion: {TEST_QUESTION}\n\n--- ANSWER ---\n{final_text}\n"
    )
    print(f"\nSaved to {out}")
    print(f"Inspect updated memory:  python inspect_memory.py")


if __name__ == "__main__":
    main()
