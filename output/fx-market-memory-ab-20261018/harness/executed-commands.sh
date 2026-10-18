# Execution record. Paths/DBs now exist; do not directly rerun.
# Main agent started and owns isolated PG14 socket55489; this harness never starts/stops it.
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/prepare.py
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/prepare.py --after-commit 330f4e6
# First run failed at application startup before load; logs retained separately.
# After harness naming fix, new before/after_r1_test databases:
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/run.py
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/analyze.py
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-market-memory-ab-20261018/write_report.py
