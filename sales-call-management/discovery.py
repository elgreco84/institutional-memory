"""Sales Call Discovery Agent.

Ingests a recorded sales-call transcript (a markdown file), extracts the sales
entities it contains, reconciles them against the live CRM, and logs the call as
an activity. The agent runs on the shared local Messages-API tool loop and is
granted both read and write tools so it can look before it writes (reconcile, do
not duplicate).

Public seam (frozen): ``ingest(call_path) -> dict``.
"""

from pathlib import Path

from agent_loop import run_loop
from tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES

DISCOVERY_SYSTEM_PROMPT = """\
You are the Sales Call Discovery Agent for Scope Creepers, an AI consultancy. A
recorded sales-call transcript is handed to you as markdown text. Your job is to
turn that call into accurate, non-duplicated CRM records and a clear account of
what you changed.

Operate in this order:

1. READ BEFORE YOU WRITE. Before creating anything, use the read tools to inspect
   the current CRM. Look up the account by name (find_account), review its
   existing contacts, and list its opportunities and recent activities. You must
   understand what already exists before you decide what is new versus what is an
   update. Never write a record without first checking whether a matching one is
   already there.

2. EXTRACT the sales entities present in the transcript:
   - Account: the client company. Capture name and, when stated, industry and the
     Scope Creepers owner / account executive on the call.
   - Contacts: the people on the client side. Capture name and, when stated,
     title and email. Note who is the economic buyer and who is the champion in
     the opportunity notes rather than inventing fields.
   - Opportunity: the deal under discussion. Capture name, stage, amount,
     currency, close_date, probability, next_step, and notes. Stage must be one
     of: Prospecting, Qualification, Proposal, Negotiation, Closed Won,
     Closed Lost. Amount is a number; currency is an ISO code such as USD. Dates
     are ISO (YYYY-MM-DD). Probability is an integer percentage 0-100. Derive
     these from what was actually said; do not fabricate figures the call does not
     support. If a value is genuinely absent, leave it unset rather than guessing.
   - Signals: notable buying signals, risks, competitive mentions, blockers, and
     commitments. Fold these into the opportunity notes and the activity summary.

3. RECONCILE with the existing records. Upserts are keyed by NAME within an
   account: an account is matched by its name; a contact or opportunity is matched
   by its name within that account. If a match exists, UPDATE it in place and
   carry forward fields the call did not change. If no match exists, create it.
   Do not create a second account, contact, or opportunity that duplicates one
   already present under a slightly different spelling; prefer the existing record
   and update it. When the call moves a known deal forward (new stage, revised
   amount, new next step), that is an update to the existing opportunity, not a
   new one.

4. WRITE via the mutation tools: upsert_account first (you need its id), then
   upsert_contact for each contact under that account, then upsert_opportunity for
   the deal. Pass the account id through so children attach to the right account.

5. LOG THE CALL as an activity via log_activity. Use type "call". Set source_call
   to the transcript filename you were given. Set participants to the people on
   the call. Attach it to the account and, when there is one, the opportunity.
   Write a summary that captures what happened and what was committed.

6. FINISH with a concise plain-text summary of exactly what you created or changed
   -- which account, contacts, and opportunity were new versus updated, the key
   field changes (stage, amount, next step), and the activity you logged. No
   markdown tables, no preamble; just the change report a sales ops reviewer can
   scan.

Be precise and conservative. The value of this system is that the CRM reflects
reality after a call without accumulating duplicates. When the transcript is
ambiguous, prefer updating the closest existing record and say so in your summary.
"""


def load_transcript(call_path) -> str:
    """Read a UTF-8 markdown transcript and return its text.

    Mirrors the repo's load pattern: a transcript is a markdown file on disk
    standing in for what would be a vector-DB record in production.
    """
    return Path(call_path).read_text(encoding="utf-8")


def ingest(call_path) -> dict:
    """Ingest one sales-call transcript into the CRM and report what changed.

    Contract:
      - Load the transcript at ``call_path`` via ``load_transcript`` (UTF-8
        markdown).
      - Build the user message for the tool loop: inline the full transcript text
        together with the ingestion instructions, and tell the agent the
        transcript filename (``Path(call_path).name``) so it can set
        ``source_call`` on the logged activity.
      - Call ``run_loop(DISCOVERY_SYSTEM_PROMPT, messages,
        allowed_tools=READ_TOOL_NAMES + WRITE_TOOL_NAMES)``. The agent inspects the
        CRM with the read tools, reconciles, and persists its changes through the
        write tools during the loop; ``run_loop`` returns the agent's final
        plain-text change summary.
      - Return a dict of the form ``{"call": <name>, "summary": <agent text>}``,
        where ``<name>`` is the transcript filename and ``<agent text>`` is the
        string returned by ``run_loop``.

    Read and write tools are both allowed so the agent can look up existing records
    before upserting; reconciliation (never duplicating) is the agent's
    responsibility per ``DISCOVERY_SYSTEM_PROMPT``.
    """
    name = Path(call_path).name
    transcript = load_transcript(call_path)
    user = (
        "Below is a sales-call transcript. Check the current CRM with the read tools first, "
        "then create or UPDATE records with the write tools (reconcile, do not duplicate), and "
        "log the call as an activity. Finish with a concise plain-text summary of exactly what "
        f"you created or changed.\n\n===== TRANSCRIPT: {name} =====\n{transcript}"
    )
    messages = [{"role": "user", "content": user}]
    summary = run_loop(DISCOVERY_SYSTEM_PROMPT, messages, allowed_tools=READ_TOOL_NAMES + WRITE_TOOL_NAMES)
    return {"call": name, "summary": summary}
