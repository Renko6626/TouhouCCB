#!/usr/bin/env bash
# Prepared only. Run after the main agent authorizes final-code validation and starts the isolated PG.
set -euo pipefail
# Main agent already created fx_market_memory_test; coordinate before these destructive per-test schema fixtures.
cd /tmp/sunyunbo/fx-market-memory-ab-20261018/after/backend
TEST_PG_DATABASE_URL='postgresql+asyncpg://sunyunbo@/fx_market_memory_test?host=/tmp/sunyunbo/fx-pool-incident-20261017/socket&port=55489' /data/sunyunbo/www/TouhouCCB/backend/venv/bin/python -m pytest -q -m pg tests/pg/test_pg_trading_context.py tests/pg/test_pg_fx_history_locking.py tests/pg/test_pg_fx_trigger_orders.py
