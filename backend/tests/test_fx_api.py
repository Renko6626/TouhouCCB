def test_fx_router_exports_required_paths():
    from app.api.v1.fx import router
    paths = {route.path for route in router.routes}
    assert "/pairs" in paths
    assert "/pairs/{pair_id}/snapshot" in paths
    assert "/pairs/{pair_id}/quote" in paths
    assert "/pairs/{pair_id}/trades" in paths
    # I5 player reads: per-user wallet and personal trade history.
    assert "/pairs/{pair_id}/wallet" in paths
    assert "/pairs/{pair_id}/my-trades" in paths
    # Task 3c1 authenticated short writes (quote/current-position reads are 3c2).
    assert "/pairs/{pair_id}/short/open" in paths
    assert "/pairs/{pair_id}/short/cover" in paths


def test_admin_fx_router_exports_pair_read():
    from app.api.v1.admin_fx import router
    paths = {route.path for route in router.routes}
    assert "/pairs" in paths


def test_fx_request_schemas_enforce_six_decimal_places():
    from pydantic import ValidationError
    from app.schemas.fx import FxQuoteRequest, FxTradeRequest

    assert FxQuoteRequest(side="buy", amount="1.000000").amount == 1
    assert FxTradeRequest(side="buy", amount="1", min_out="0.000000", idempotency_key="k")
    for kwargs in ({"side": "buy", "amount": "1.0000001"},
                   {"side": "buy", "amount": "1", "min_out": "0.0000001", "idempotency_key": "k"}):
        with __import__("pytest").raises(ValidationError):
            (FxQuoteRequest(**kwargs) if "min_out" not in kwargs else FxTradeRequest(**kwargs))
