from __future__ import annotations

import argparse
import json
from datetime import date

from app.db.session import get_session_factory
from app.services.historical_metrics import repair_recent_ixspy_daily_metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore derived metrics from complete, current IXSPY daily snapshots."
    )
    parser.add_argument(
        "--product-id",
        type=int,
        action="append",
        required=True,
        help="Limit the repair to one local product record.",
    )
    parser.add_argument("--target-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the corrections. Without this flag, changes are rolled back.",
    )
    args = parser.parse_args()

    with get_session_factory()() as session:
        result = repair_recent_ixspy_daily_metrics(
            session,
            product_ids=args.product_id,
            target_date=args.target_date,
            commit=args.apply,
        )
        if not args.apply:
            session.rollback()

    print(json.dumps({**result, "applied": args.apply}))


if __name__ == "__main__":
    main()
