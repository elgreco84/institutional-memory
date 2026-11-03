"""Tool surface shared by the two Claude agents in this project.

Both agents expose their capabilities to the model as Anthropic tool-use
definitions (`TOOL_SCHEMAS`) and execute the model's chosen calls through a
single thin router (`dispatch`). Every tool is a thin wrapper over a function
in the sibling `crm` module.

Two capability tiers are served from the same schema list:

- The reporting agent is granted only the READ tools (`READ_TOOL_NAMES`) so it
  can summarize pipeline and activity without mutating the CRM.
- The discovery agent is granted READ and WRITE tools
  (`READ_TOOL_NAMES + WRITE_TOOL_NAMES`) so it can record accounts, contacts,
  opportunities, and activities it extracts from sales calls.

The agent loop (see `agent_loop.run_loop`) filters `TOOL_SCHEMAS` down to the
names a given agent is allowed to use before each Messages API request, and
routes any `tool_use` block back through `dispatch`.
"""

import crm

TOOL_SCHEMAS = [
    {
        "name": "list_accounts",
        "description": (
            "List every account in the CRM. Use this to get the full book of "
            "business before summarizing coverage or picking an account to "
            "drill into. Takes no arguments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "find_account",
        "description": (
            "Look up a single account by its name (exact or close match). Use "
            "this to resolve an account mentioned on a call before reading or "
            "writing related opportunities, contacts, or activities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Account (company) name to search for.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_opportunity",
        "description": (
            "Fetch the full detail of a single opportunity (deal) by its "
            "identifier, including stage, amount, close date, probability, and "
            "next step. Use after listing opportunities to inspect one deal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "opp_id": {
                    "type": "string",
                    "description": "Unique identifier of the opportunity to retrieve.",
                },
            },
            "required": ["opp_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_opportunities",
        "description": (
            "List opportunities (deals) in the pipeline, optionally filtered by "
            "stage, owning account, and/or a minimum dollar amount. Use to build "
            "pipeline views, find deals in a given stage, or isolate large deals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Restrict to opportunities in this sales stage (e.g. 'Prospecting', 'Negotiation').",
                },
                "account": {
                    "type": "string",
                    "description": "Restrict to opportunities owned by this account name.",
                },
                "min_amount": {
                    "type": "number",
                    "description": "Restrict to opportunities with amount greater than or equal to this value.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_activities",
        "description": (
            "List logged activities (calls, meetings, emails, notes), optionally "
            "scoped to one account and/or capped to the most recent N entries. "
            "Use to review recent engagement history before reporting or follow-up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Restrict to activities for this account name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of activities to return (most recent first).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "pipeline_summary",
        "description": (
            "Return an aggregated summary of the sales pipeline: totals and "
            "counts rolled up by stage. Use for executive-style reporting without "
            "pulling every individual deal. Takes no arguments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "upsert_account",
        "description": (
            "Create a new account or update an existing one (matched by name). "
            "Use to record a company surfaced on a call, optionally tagging its "
            "industry and the owning sales rep."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Account (company) name. Serves as the match key for upsert.",
                },
                "industry": {
                    "type": "string",
                    "description": "Industry or vertical the account operates in.",
                },
                "owner": {
                    "type": "string",
                    "description": "Name of the sales rep who owns this account.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "upsert_contact",
        "description": (
            "Create or update a contact (person) on an account (matched by "
            "account plus contact name). Use to record a stakeholder met on a "
            "call, optionally with their title and email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Name of the account this contact belongs to.",
                },
                "name": {
                    "type": "string",
                    "description": "Full name of the contact.",
                },
                "title": {
                    "type": "string",
                    "description": "Job title or role of the contact.",
                },
                "email": {
                    "type": "string",
                    "description": "Email address of the contact.",
                },
            },
            "required": ["account", "name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "upsert_opportunity",
        "description": (
            "Create or update an opportunity (deal) on an account (matched by "
            "account plus opportunity name). Use to record or advance a deal "
            "discussed on a call, setting stage, amount, close date, probability, "
            "next step, and notes as learned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Name of the account this opportunity belongs to.",
                },
                "name": {
                    "type": "string",
                    "description": "Name of the opportunity (deal).",
                },
                "stage": {
                    "type": "string",
                    "description": "Current sales stage (e.g. 'Prospecting', 'Proposal', 'Closed Won').",
                },
                "amount": {
                    "type": "number",
                    "description": "Deal value in dollars.",
                },
                "close_date": {
                    "type": "string",
                    "description": "Expected close date in ISO 8601 format (YYYY-MM-DD).",
                },
                "probability": {
                    "type": "number",
                    "description": "Probability of winning the deal, as a percentage from 0 to 100.",
                },
                "next_step": {
                    "type": "string",
                    "description": "The agreed next action to advance the deal.",
                },
                "notes": {
                    "type": "string",
                    "description": "Free-form notes or context about the deal.",
                },
            },
            "required": ["account", "name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "log_activity",
        "description": (
            "Log an activity against an account (a call, meeting, email, or "
            "note). Use to record engagement from a sales call, optionally "
            "linking the source call recording, a related opportunity, and the "
            "participants involved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Name of the account this activity relates to.",
                },
                "type": {
                    "type": "string",
                    "description": "Kind of activity (e.g. 'call', 'meeting', 'email', 'note').",
                },
                "date": {
                    "type": "string",
                    "description": "Date the activity occurred, in ISO 8601 format (YYYY-MM-DD).",
                },
                "summary": {
                    "type": "string",
                    "description": "Short summary of what happened during the activity.",
                },
                "source_call": {
                    "type": "string",
                    "description": "Identifier or reference of the source call recording, if any.",
                },
                "opportunity": {
                    "type": "string",
                    "description": "Name of a related opportunity this activity advanced, if any.",
                },
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of the people who took part in the activity.",
                },
            },
            "required": ["account", "type", "date", "summary"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

READ_TOOL_NAMES = [
    "list_accounts",
    "find_account",
    "get_opportunity",
    "list_opportunities",
    "list_activities",
    "pipeline_summary",
]

WRITE_TOOL_NAMES = [
    "upsert_account",
    "upsert_contact",
    "upsert_opportunity",
    "log_activity",
]

_DISPATCH_TABLE = {
    "list_accounts": crm.list_accounts,
    "find_account": crm.find_account,
    "get_opportunity": crm.get_opportunity,
    "list_opportunities": crm.list_opportunities,
    "list_activities": crm.list_activities,
    "pipeline_summary": crm.pipeline_summary,
    "upsert_account": crm.upsert_account,
    "upsert_contact": crm.upsert_contact,
    "upsert_opportunity": crm.upsert_opportunity,
    "log_activity": crm.log_activity,
}


def dispatch(name: str, tool_input: dict) -> dict:
    """Route a single tool call to its matching crm function and return a result.

    Looks up `name` in the dispatch table and calls the corresponding
    `crm.<fn>(**tool_input)`. The return value is normalized to a
    JSON-serializable dict so it can be placed directly into a `tool_result`
    content block:

    - a dict return is passed through unchanged;
    - any other return (list, None, scalar) is wrapped as `{"result": <value>}`.

    Raises `ValueError` for an unknown tool name. Any exception raised by the
    underlying crm function (including `NotImplementedError` while crm.py is
    still being built) propagates to the caller, which is responsible for
    turning it into an error `tool_result`.
    """
    fn = _DISPATCH_TABLE.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: {name!r}")

    result = fn(**tool_input)
    if isinstance(result, dict):
        return result
    return {"result": result}
