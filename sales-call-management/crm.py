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

import datetime
import json
import os
import re
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


def _slug(s: str) -> str:
    """Slugify ``s``: lowercase, non-alphanumeric runs to single hyphen, trimmed.

    Collapses every run of non-alphanumeric characters into a single hyphen and
    strips leading/trailing hyphens, producing a stable, id-friendly token.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _today() -> str:
    """Return today's date as an ISO date string (YYYY-MM-DD)."""
    return datetime.date.today().isoformat()


def _resolve_account(db: dict, account: str) -> dict | None:
    """Resolve an account within ``db`` by case-insensitive name or exact id."""
    key = str(account).lower()
    for acc in db.get("accounts", []):
        if acc.get("name", "").lower() == key or acc.get("id") == account:
            return acc
    return None


def list_accounts() -> list[dict]:
    """Return all accounts.

    Returns a list of account dicts, each shaped as
    ``{id, name, industry, owner, created_at, updated_at}``. Returns an empty
    list when there are no accounts.
    """
    return load().get("accounts", [])


def find_account(name: str) -> dict | None:
    """Return the account whose name matches ``name`` case-insensitively.

    Returns a single account dict shaped as
    ``{id, name, industry, owner, created_at, updated_at}``, or ``None`` when no
    account name matches (ignoring case).
    """
    key = str(name).lower()
    for acc in load().get("accounts", []):
        if acc.get("name", "").lower() == key:
            return acc
    return None


def get_opportunity(opp_id: str) -> dict | None:
    """Return the opportunity with id ``opp_id``.

    Returns a single opportunity dict shaped as
    ``{id, account_id, name, stage, amount, currency, close_date, probability,
    next_step, notes, updated_at}``, or ``None`` when no opportunity has that id.
    """
    for opp in load().get("opportunities", []):
        if opp.get("id") == opp_id:
            return opp
    return None


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
    db = load()
    account_id = None
    if account is not None:
        acc = _resolve_account(db, account)
        if acc is None:
            return []
        account_id = acc.get("id")

    result = []
    for opp in db.get("opportunities", []):
        if stage is not None and opp.get("stage") != stage:
            continue
        if account_id is not None and opp.get("account_id") != account_id:
            continue
        if min_amount is not None and not (opp.get("amount") or 0) >= min_amount:
            continue
        result.append(opp)
    return result


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
    db = load()
    account_id = None
    if account is not None:
        acc = _resolve_account(db, account)
        if acc is None:
            return []
        account_id = acc.get("id")

    acts = [
        a
        for a in db.get("activities", [])
        if account_id is None or a.get("account_id") == account_id
    ]
    acts.sort(key=lambda a: a.get("date") or "", reverse=True)
    if limit is not None:
        acts = acts[:limit]
    return acts


def pipeline_summary() -> dict:
    """Summarize the opportunity pipeline by stage and overall.

    Returns a dict with one ``by_stage`` entry per stage in ``STAGES`` (in
    ``STAGES`` order) plus overall ``totals``. Money and weighted amounts are
    rounded to two decimals. ``weighted_amount`` for an opportunity is
    ``amount * probability / 100``. "Open" means a stage not in
    ("Closed Won", "Closed Lost"). ``win_rate`` is
    ``won_count / (won_count + lost_count)`` as a float in ``[0, 1]``, or ``0``
    when the denominator is zero.
    """
    db = load()

    by_stage = []
    stage_acc = {s: {"count": 0, "amount": 0.0, "weighted_amount": 0.0} for s in STAGES}

    total_count = 0
    total_amount = 0.0
    total_weighted = 0.0
    open_count = 0
    open_amount = 0.0
    won_count = 0
    won_amount = 0.0
    lost_count = 0

    closed = ("Closed Won", "Closed Lost")

    for opp in db.get("opportunities", []):
        stage = opp.get("stage")
        amount = opp.get("amount") or 0
        probability = opp.get("probability") or 0
        weighted = amount * (probability / 100)

        if stage in stage_acc:
            stage_acc[stage]["count"] += 1
            stage_acc[stage]["amount"] += amount
            stage_acc[stage]["weighted_amount"] += weighted

        total_count += 1
        total_amount += amount
        total_weighted += weighted

        if stage not in closed:
            open_count += 1
            open_amount += amount
        if stage == "Closed Won":
            won_count += 1
            won_amount += amount
        elif stage == "Closed Lost":
            lost_count += 1

    for s in STAGES:
        entry = stage_acc[s]
        by_stage.append(
            {
                "stage": s,
                "count": entry["count"],
                "amount": round(entry["amount"], 2),
                "weighted_amount": round(entry["weighted_amount"], 2),
            }
        )

    denom = won_count + lost_count
    win_rate = (won_count / denom) if denom else 0

    return {
        "by_stage": by_stage,
        "totals": {
            "count": total_count,
            "amount": round(total_amount, 2),
            "weighted_amount": round(total_weighted, 2),
            "open_count": open_count,
            "open_amount": round(open_amount, 2),
            "won_count": won_count,
            "won_amount": round(won_amount, 2),
            "lost_count": lost_count,
            "win_rate": win_rate,
        },
    }


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
    db = load()
    today = _today()
    key = str(name).lower()

    acc = None
    for existing in db.get("accounts", []):
        if existing.get("name", "").lower() == key:
            acc = existing
            break

    if acc is None:
        acc = {
            "id": "acc-" + _slug(name),
            "name": name,
            "industry": None,
            "owner": None,
            "created_at": today,
            "updated_at": today,
        }
        db.setdefault("accounts", []).append(acc)

    if industry is not None:
        acc["industry"] = industry
    if owner is not None:
        acc["owner"] = owner
    acc["updated_at"] = today

    save(db)
    return acc


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
    db = load()
    acc = _resolve_account(db, account)
    if acc is None:
        acc = upsert_account(account)
        db = load()
        acc = _resolve_account(db, acc["id"])

    account_id = acc["id"]
    key = str(name).lower()

    contact = None
    for existing in db.get("contacts", []):
        if existing.get("account_id") == account_id and existing.get("name", "").lower() == key:
            contact = existing
            break

    if contact is None:
        account_slug = account_id[len("acc-"):] if account_id.startswith("acc-") else _slug(acc["name"])
        contact = {
            "id": "con-" + account_slug + "-" + _slug(name),
            "account_id": account_id,
            "name": name,
            "title": None,
            "email": None,
        }
        db.setdefault("contacts", []).append(contact)

    if title is not None:
        contact["title"] = title
    if email is not None:
        contact["email"] = email

    save(db)
    return contact


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
    if stage is not None and stage not in STAGES:
        raise ValueError(f"invalid stage: {stage!r}; must be one of {STAGES}")

    db = load()
    today = _today()
    acc = _resolve_account(db, account)
    if acc is None:
        acc = upsert_account(account)
        db = load()
        acc = _resolve_account(db, acc["id"])

    account_id = acc["id"]
    key = str(name).lower()

    opp = None
    for existing in db.get("opportunities", []):
        if existing.get("account_id") == account_id and existing.get("name", "").lower() == key:
            opp = existing
            break

    if opp is None:
        account_slug = account_id[len("acc-"):] if account_id.startswith("acc-") else _slug(acc["name"])
        opp = {
            "id": "opp-" + account_slug + "-" + _slug(name),
            "account_id": account_id,
            "name": name,
            "stage": None,
            "amount": None,
            "currency": "USD",
            "close_date": None,
            "probability": None,
            "next_step": None,
            "notes": None,
            "updated_at": today,
        }
        db.setdefault("opportunities", []).append(opp)

    if stage is not None:
        opp["stage"] = stage
    if amount is not None:
        opp["amount"] = amount
    if close_date is not None:
        opp["close_date"] = close_date
    if probability is not None:
        opp["probability"] = probability
    if next_step is not None:
        opp["next_step"] = next_step
    if notes is not None:
        opp["notes"] = notes
    opp["updated_at"] = today

    save(db)
    return opp


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
    given it is resolved by id or case-insensitive name within that account and
    its id stored as ``opportunity_id`` (otherwise ``opportunity_id`` is
    ``None``). A new activity is always appended with a fresh zero-padded
    sequential id; the db is persisted via ``save``.

    Returns the created activity dict shaped as
    ``{id, account_id, opportunity_id, type, date, summary, source_call,
    participants}``.
    """
    db = load()
    acc = _resolve_account(db, account)
    if acc is None:
        acc = upsert_account(account)
        db = load()
        acc = _resolve_account(db, acc["id"])

    account_id = acc["id"]

    opportunity_id = None
    if opportunity is not None:
        opp_key = str(opportunity).lower()
        for opp in db.get("opportunities", []):
            if opp.get("account_id") != account_id:
                continue
            if opp.get("id") == opportunity or opp.get("name", "").lower() == opp_key:
                opportunity_id = opp.get("id")
                break

    seq = len(db.get("activities", [])) + 1
    activity = {
        "id": "act-" + str(seq).zfill(4),
        "account_id": account_id,
        "opportunity_id": opportunity_id,
        "type": type,
        "date": date,
        "summary": summary,
        "source_call": source_call,
        "participants": participants if participants is not None else [],
    }
    db.setdefault("activities", []).append(activity)

    save(db)
    return activity
