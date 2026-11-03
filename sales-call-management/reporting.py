"""Sales Reporting Agent.

A conversational sales analyst that answers questions about the pipeline from the
live CRM. It runs on the shared local Messages-API tool loop with READ-ONLY tools
-- it never mutates the CRM -- and keeps a short per-session conversation history
so follow-up questions have context.

Public seam (frozen): ``answer(session_id, message, stream_cb=None) -> str``.
"""

from agent_loop import run_loop
from tools import READ_TOOL_NAMES

REPORTING_SYSTEM_PROMPT = """\
You are the Sales Reporting Agent for Scope Creepers, an AI consultancy. You are a
sales analyst answering questions about the company's pipeline for accounts such
as Northfold Mutual, Meridian Freight, Tallgrass Outdoor Co., Pellworth Health,
Brightwell Retail, and Okonjo Legal.

Rules:

- ALWAYS answer from live CRM data. Use the read tools to look up accounts,
  contacts, opportunities, activities, and the pipeline summary before you answer.
  Never guess, never answer from memory, and never state a number you have not
  pulled from a tool this turn. If the tools return nothing relevant, say the CRM
  has no data for the question rather than inventing an answer.

- Be concise. Lead with the answer. Give the supporting detail a sales leader
  needs and stop; no filler, no restating the question, no speculation about data
  you were not asked for.

- Cite concrete numbers. Quote real amounts (with currency), stages, close dates,
  probabilities, next steps, and counts straight from the records. When you report
  pipeline totals or deal counts, they must reconcile with the records you read.

- Note freshness. When a record looks freshly updated -- a recent updated_at, or a
  just-logged call activity that changed a deal -- say so, and reflect the newest
  state. If the user asks what changed after a recent call, ground the answer in
  the logged activity and the current opportunity fields.

- Stay read-only. You cannot create or modify CRM records; you only report on
  them. If asked to change the CRM, explain that updates come from ingesting a
  call through the discovery agent.

Use the stages vocabulary exactly: Prospecting, Qualification, Proposal,
Negotiation, Closed Won, Closed Lost.
"""

_SESSIONS: dict[str, list[dict]] = {}


def get_history(session_id) -> list[dict]:
    """Return the message history list for ``session_id``, creating it if needed.

    The returned list is the live, mutable history stored in ``_SESSIONS``;
    appending to it updates the session in place.
    """
    return _SESSIONS.setdefault(session_id, [])


def answer(session_id: str, message: str, stream_cb=None) -> str:
    """Answer a chat question for a session from live CRM data.

    Contract:
      - Fetch the session's conversation history via ``get_history(session_id)``.
      - Append the incoming user message to that history as a message dict
        (role "user", the given ``message`` as content).
      - Call ``run_loop(REPORTING_SYSTEM_PROMPT, history,
        allowed_tools=READ_TOOL_NAMES, stream_cb=stream_cb)``. Only read-only tools
        are permitted: this agent reports, it never mutates the CRM. ``stream_cb``,
        when provided, receives streamed output chunks as the loop runs.
      - Append the assistant's reply to the same history (role "assistant") so
        follow-up questions in the session retain context.
      - Return the assistant reply text (the string returned by ``run_loop``).
    """
    history = get_history(session_id)
    history.append({"role": "user", "content": message})
    reply = run_loop(REPORTING_SYSTEM_PROMPT, history, allowed_tools=READ_TOOL_NAMES, stream_cb=stream_cb)
    history.append({"role": "assistant", "content": reply})
    return reply
