import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.audit_replay import FxPairState, FxShortState, Snapshot, UserState
from scripts.audit_export import _snap_dict


def test_snapshot_export_preserves_fx_balances_and_accrual_clocks():
    snapshot = Snapshot(
        last_event_id=12,
        users={1: UserState(Decimal("100.000001"), Decimal("20"), True, 12,
                            datetime(2026, 10, 19, 12, tzinfo=timezone(timedelta(hours=8))))},
        fx_pairs={2: FxPairState(Decimal("123.000001"), Decimal("456.000002"),
                                 Decimal("0.000003"), Decimal("0.000004"), True, 11)},
        fx_wallets={(1, 2): Decimal("7.000005")},
        fx_shorts={
            (1, 2): FxShortState(Decimal("8.000006"), Decimal("0.000007"), None,
                                 Decimal("9.000008"), datetime(2026, 10, 19, 4), 12),
            (3, 2): FxShortState(Decimal("1"), Decimal("0"), Decimal("2.000009"),
                                 Decimal("2"), None, 10),
        },
    )

    exported = json.loads(json.dumps(_snap_dict(snapshot)))

    assert exported["users"]["1"] == {
        "cash": "100.000001", "debt": "20", "anchored": True,
        "debt_last_accrued_at": "2026-10-19T04:00:00+00:00",
    }
    assert exported["fx_pairs"] == {"2": {
        "gold": "123.000001", "foreign": "456.000002",
        "treasury_gold": "0.000003", "treasury_foreign": "0.000004", "anchored": True,
    }}
    assert exported["fx_wallets"] == [{"user_id": 1, "pair_id": 2, "amount": "7.000005"}]
    assert exported["fx_shorts"] == [
        {"user_id": 1, "pair_id": 2, "principal_foreign": "8.000006",
         "interest_foreign": "0.000007", "restricted_gold": None,
         "proceeds_basis_gold": "9.000008", "interest_last_accrued_at": "2026-10-19T04:00:00+00:00"},
        {"user_id": 3, "pair_id": 2, "principal_foreign": "1", "interest_foreign": "0",
         "restricted_gold": "2.000009", "proceeds_basis_gold": "2", "interest_last_accrued_at": None},
    ]
