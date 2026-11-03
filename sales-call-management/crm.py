"""crm.py - the single source of truth for the CRM datastore shape.

Every other module in the sales-call-management project does ``import crm`` and
relies on this module to define, read, and write the CRM JSON document. The
on-disk shape is FROZEN and must not drift. It is a single JSON object with four
top-level lists:

    accounts:      [{id, name, industry, owner, created_at, updated_at}]
    contacts:      [{id, account_id, name, title, email}]
    opportunities: [{id, account_id, name, stage, amount, currency,
                     close_date, probability, next_step, notes, updated_at}]
    activities:    [{id, account_id, opportunity_id, type, date, summary,
                     source_call, participants}]

Opportunity ``stage`` is always one of ``STAGES`` (see below).

This module implements the generic, domain-agnostic file I/O for real
(``load``, ``save``, ``reset``, and the path/stage constants). The domain
query and mutation functions are declared here as the integration contract for
the rest of the team but are intentionally left as stubs that raise
``NotImplementedError``; their docstrings specify the exact return shapes so
colleagues can build against them in parallel.
"""

import json
import os
import tempfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED_PATH = DATA_DIR / "crm.seed.json"
CRM_PATH = DATA_DIR / "crm.json"

STAGES = [
    "Prospecting",
    "Qualification",
    "Proposal",
    "Negotiation",
    "Closed Won",
    "Closed Lost",
]


def load() -> dict:
    """Read and JSON-parse the working CRM document.

    If ``CRM_PATH`` does not exist yet, fall back to reading ``SEED_PATH`` so
    that a fresh checkout still has data to work with. The parsed dict is
    returned with the frozen shape documented at the top of this module.
    """
    path = CRM_PATH if CRM_PATH.exists() else SEED_PATH
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save(db: dict) -> None:
    """Atomically write ``db`` to ``CRM_PATH`` as pretty-printed JSON.

    The document is first written to a temporary file in ``DATA_DIR`` and then
    moved onto ``CRM_PATH`` with ``os.replace``, which is atomic on the same
    filesystem. This guarantees readers never observe a half-written file.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(DATA_DIR), prefix="crm.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(db, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, CRM_PATH)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def reset() -> dict:
    """Restore the working datastore from the committed seed.

    Reads ``SEED_PATH`` and writes its contents to ``CRM_PATH`` via ``save``,
    overwriting any working state, then returns the restored db dict.
    """
    with SEED_PATH.open("r", encoding="utf-8") as handle:
        db = json.load(handle)
    save(db)
    return db


def list_accounts() -> list[dict]:
    """Return all accounts.

    Returns a list of account dicts, each shaped as
    ``{id, name, industry, owner, created_at, updated_at}``. Returns an empty
    list when there are no accounts.
    """
    raise NotImplementedError(
        "chunk-1 owner: read the db via load() and return its 'accounts' list"
    )


def find_account(name: str) -> dict | None:
    """Return the account whose name matches ``name`` case-insensitively.

    Returns a single account dict shaped as
    ``{id, name, industry, owner, created_at, updated_at}``, or ``None`` when no
    account name matches (ignoring case).
    """
    raise NotImplementedError(
        "chunk-1 owner: case-insensitively match 'name' against accounts[].name"
    )


def get_opportunity(opp_id: str) -> dict | None:
    """Return the opportunity with id ``opp_id``.

    Returns a single opportunity dict shaped as
    ``{id, account_id, name, stage, amount, currency, close_date, probability,
    next_step, notes, updated_at}``, or ``None`` when no opportunity has that id.
    """
    raise NotImplementedError(
        "chunk-1 owner: look up an opportunity by exact id and return it or None"
    )


def list_opportunities(
    stage: str | None = None,
    account: str | None = None,
    min_amount: float | None = None,
) -> list[dict]:
    """Return opportunities, optionally filtered.

    Filters (all optional, combined with AND):
    - ``stage``: keep only opportunities whose ``stage`` equals this value.
    - ``account``: keep only opportunities under the account whose name matches
      this value case-insensitively.
    - ``min_amount``: keep only opportunities whose ``amount`` is greater than or
      equal to this value.

    Returns a list of opportunity dicts shaped as
    ``{id, account_id, name, stage, amount, currency, close_date, probability,
    next_step, notes, updated_at}``. Returns an empty list when nothing matches.
    """
    raise NotImplementedError(
        "chunk-1 owner: filter opportunities by stage, account name, and min_amount"
    )


def list_activities(
    account: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return activities, optionally filtered and capped.

    Filters:
    - ``account``: keep only activities under the account whose name matches
      this value case-insensitively.
    - ``limit``: return at most this many activities (most relevant/recent first
      per the owner's ordering decision).

    Returns a list of activity dicts shaped as
    ``{id, account_id, opportunity_id, type, date, summary, source_call,
    participants}``. Returns an empty list when nothing matches.
    """
    raise NotImplementedError(
        "chunk-1 owner: filter activities by account name and cap with limit"
    )


def pipeline_summary() -> dict:
    """Summarize the opportunity pipeline by stage and overall.

    Returns a dict with one entry per stage in ``STAGES`` plus overall totals.
    The expected shape is::

        {
            "by_stage": {
                "<stage name>": {
                    "count": int,            # number of opportunities in stage
                    "amount": float,         # sum of amount in stage
                    "weighted_amount": float # sum of amount * probability/100
                },
                ...  # one key for every stage in STAGES
            },
            "totals": {
                "count": int,            # total opportunities
                "amount": float,         # total amount across all stages
                "weighted_amount": float # total probability-weighted amount
            }
        }
    """
    raise NotImplementedError(
        "chunk-1 owner: aggregate per-stage count, amount, and weighted_amount, plus totals"
    )


def upsert_account(
    name: str,
    industry: str | None = None,
    owner: str | None = None,
) -> dict:
    """Create or update an account, keyed by ``name`` (case-insensitive).

    This is an idempotent upsert: if an account with the given name already
    exists it is updated in place (only provided, non-None fields overwrite),
    otherwise a new account is created with a fresh id and ``created_at``. In all
    cases ``updated_at`` is stamped and the db is persisted via ``save``.

    Returns the created or updated account dict shaped as
    ``{id, name, industry, owner, created_at, updated_at}``.
    """
    raise NotImplementedError(
        "chunk-1 owner: idempotent upsert of an account keyed by name, stamping updated_at"
    )


def upsert_contact(
    account: str,
    name: str,
    title: str | None = None,
    email: str | None = None,
) -> dict:
    """Create or update a contact under an account, keyed by contact ``name``.

    The ``account`` is resolved by case-insensitive name. The upsert is
    idempotent and keyed by contact ``name`` within that account: an existing
    contact is updated in place (only provided, non-None fields overwrite),
    otherwise a new contact is created with a fresh id and ``account_id``. The
    db is persisted via ``save``.

    Returns the created or updated contact dict shaped as
    ``{id, account_id, name, title, email}``.
    """
    raise NotImplementedError(
        "chunk-1 owner: idempotent upsert of a contact keyed by name within an account"
    )


def upsert_opportunity(
    account: str,
    name: str,
    stage: str | None = None,
    amount: float | None = None,
    close_date: str | None = None,
    probability: int | None = None,
    next_step: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create or update an opportunity under an account, keyed by ``name``.

    The ``account`` is resolved by case-insensitive name. The upsert is
    idempotent and keyed by opportunity ``name`` within that account: an existing
    opportunity is updated in place (only provided, non-None fields overwrite),
    otherwise a new one is created with a fresh id and ``account_id``. When
    provided, ``stage`` must be a member of ``STAGES``. ``updated_at`` is stamped
    and the db is persisted via ``save``.

    Returns the created or updated opportunity dict shaped as
    ``{id, account_id, name, stage, amount, currency, close_date, probability,
    next_step, notes, updated_at}``.
    """
    raise NotImplementedError(
        "chunk-1 owner: idempotent upsert of an opportunity keyed by name within an account, "
        "validating stage against STAGES and stamping updated_at"
    )


def log_activity(
    account: str,
    type: str,
    date: str,
    summary: str,
    source_call: str | None = None,
    opportunity: str | None = None,
    participants: list[str] | None = None,
) -> dict:
    """Append an activity record under an account (and optional opportunity).

    The ``account`` is resolved by case-insensitive name; when ``opportunity`` is
    given it is resolved by case-insensitive name within that account and its id
    stored as ``opportunity_id`` (otherwise ``opportunity_id`` is ``None``). A new
    activity is created with a fresh id; the db is persisted via ``save``.

    Returns the created activity dict shaped as
    ``{id, account_id, opportunity_id, type, date, summary, source_call,
    participants}``.
    """
    raise NotImplementedError(
        "chunk-1 owner: append an activity resolving account and optional opportunity by name"
    )
