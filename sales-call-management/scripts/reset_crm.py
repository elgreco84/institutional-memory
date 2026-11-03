"""Reset the working CRM datastore from the committed seed.

Usage (from inside sales-call-management/):
    python scripts/reset_crm.py

Copies data/crm.seed.json -> data/crm.json, overwriting any working state. Run this before a
demo to get a clean, known starting point.
"""

import sys
from pathlib import Path

# Allow running as `python scripts/reset_crm.py` from the project root by putting the project
# dir (the parent of scripts/) on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crm


def main() -> None:
    db = crm.reset()
    counts = {key: len(db.get(key, [])) for key in ("accounts", "contacts", "opportunities", "activities")}
    print(f"Reset {crm.CRM_PATH} from seed.")
    print("  " + ", ".join(f"{n} {k}" for k, n in counts.items()))


if __name__ == "__main__":
    main()
