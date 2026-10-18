# Execution record only; existing directories/databases prevent direct rerun.
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-stress-profile-20261018/prepare.py
/usr/lib/postgresql/14/bin/pg_ctl -D /tmp/sunyunbo/fx-pool-incident-20261017/pgdata -l /tmp/sunyunbo/fx-stress-profile-20261018/postgres.log -o "-k /tmp/sunyunbo/fx-pool-incident-20261017/socket -p 55489 -h '' -c shared_preload_libraries=pg_stat_statements -c compute_query_id=on -c track_io_timing=on -c track_wal_io_timing=on" start
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-stress-profile-20261018/run.py
/usr/lib/postgresql/14/bin/pg_ctl -D /tmp/sunyunbo/fx-pool-incident-20261017/pgdata -m fast stop
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-stress-profile-20261018/analyze.py
/data/sunyunbo/www/TouhouCCB/backend/venv/bin/python /tmp/sunyunbo/fx-stress-profile-20261018/write_report.py
