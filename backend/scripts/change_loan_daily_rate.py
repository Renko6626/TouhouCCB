"""Stop all economic writers first; settle old-rate debt and switch atomically.

Usage: python scripts/change_loan_daily_rate.py --rate 0.02 --operator-user-id 1
Requires migrated PostgreSQL. Never initializes or migrates the database.
"""
import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.credit.rate_maintenance import change_loan_daily_rate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=Decimal, required=True)
    parser.add_argument("--operator-user-id", type=int, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(change_loan_daily_rate(new_rate=args.rate, operator_user_id=args.operator_user_id))
    except Exception as exc:
        # Do not print database connection strings or credentials from driver errors.
        parser.exit(1, f"Rate maintenance failed; transaction not committed ({type(exc).__name__}).\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
