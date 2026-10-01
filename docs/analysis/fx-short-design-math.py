"""FX 做空设计的可复算数学证据，不是已实现业务或永久 CI 测试。

从仓库根目录用 backend/venv/bin/python docs/analysis/fx-short-design-math.py 运行。
只调用现有纯数学报价和门槛，不访问数据库，不产生业务写入。
"""
from decimal import Decimal, ROUND_CEILING, localcontext
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.services.credit.thresholds import derive_thresholds
from app.services.fx.amm import quote_buy

UNIT = Decimal("0.000001")
ZERO = Decimal("0")


def round_up(amount):
    return amount.quantize(UNIT, rounding=ROUND_CEILING)


def weighted_risk(leverage, positive_assets, gold_debt, short_cover_cost):
    return max(leverage * gold_debt, (leverage - 1) * positive_assets) + (leverage - 1) * short_cover_cost


def verify_legacy_compatibility():
    generator = random.Random(20260930)
    for _ in range(20000):
        leverage = Decimal(generator.randrange(2, 21))
        gold_debt = Decimal(generator.randrange(1, 20000))
        positive_assets = Decimal(generator.randrange(0, 50000))
        cash = Decimal(generator.randrange(0, 50000))
        equity = cash + positive_assets - gold_debt
        maintenance = Decimal("0.025")
        old = derive_thresholds(leverage, maintenance)
        weighted = weighted_risk(leverage, positive_assets, gold_debt, ZERO)
        assert old.recovered(equity, gold_debt) == (leverage * (leverage - 1) * equity >= weighted)
        assert old.triggered(equity, gold_debt) == (leverage * equity < maintenance * weighted)
    return 20000


def verify_shared_margin_examples():
    examples = [
        ("long_10x", 0, 5000, 4500, 0, True),
        ("short_10x", 5500, 0, 0, 5000, True),
        ("two_full_10x", 5000, 5000, 4500, 5000, False),
        ("long_5x_short_5x", 2500, 2500, 2000, 2500, True),
        ("short_full_then_cash_loan", 10000, 0, 4500, 5000, False),
        ("funded_long_plus_short", 4500, 500, 0, 4500, True),
    ]
    result = []
    leverage = Decimal("10")
    for label, cash, assets, debt, cover, expected in examples:
        cash, assets, debt, cover = map(Decimal, (cash, assets, debt, cover))
        equity = cash + assets - debt - cover
        weighted = weighted_risk(leverage, assets, debt, cover)
        allowed = leverage * (leverage - 1) * equity >= weighted
        assert allowed == expected
        result.append({"case": label, "equity": str(equity), "risk_basis": str(weighted / leverage), "allowed": allowed})
    return result


def verify_exact_output_rounding():
    checked = 0
    with localcontext() as context:
        context.prec = 60
        for gold, foreign in [(Decimal("2000000"), Decimal("10000000")),
                              (Decimal("1000"), Decimal("100")),
                              (Decimal("1"), Decimal("1000000"))]:
            for quantity in (UNIT, foreign * Decimal("0.001"), foreign * Decimal("0.2"), foreign - UNIT):
                for fee_rate in map(Decimal, ("0", "0.0005", "0.002", "0.2")):
                    quantity = quantity.quantize(UNIT)
                    required_net = round_up(gold * quantity / (foreign - quantity))
                    gold_input = round_up(required_net / (1 - fee_rate))
                    actual = quote_buy(gold_input, gold, foreign, fee_rate)
                    assert actual.output_amount >= quantity
                    assert (gold + actual.net_input) * (foreign - quantity) >= gold * foreign
                    if gold_input > UNIT:
                        try:
                            smaller = quote_buy(gold_input - UNIT, gold, foreign, fee_rate)
                        except ValueError:
                            smaller = None
                        assert smaller is None or smaller.output_amount < quantity
                    checked += 1
    return checked


if __name__ == "__main__":
    report = {
        "legacy_compatibility_samples": verify_legacy_compatibility(),
        "exact_output_rounding_cases": verify_exact_output_rounding(),
        "margin_examples": verify_shared_margin_examples(),
        "limits": "Deterministic mathematics checks only; no product implementation, live database, concurrent execution, or workload simulation.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
