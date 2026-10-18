"""Run existing behavior tests against the experimental ASGI/cadence wrapper."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:////tmp/touhouccb_capacity_validation_{os.getpid()}.db"
os.environ["FX_CAPACITY_VARIANT"] = "asgi_cadence8"
os.environ["FX_CAPACITY_OUTPUT"] = str(Path(__file__).resolve().parent)
sys.path[:0] = [str(ROOT/"backend"), str(ROOT/"loadtest")]
os.chdir(ROOT/"backend")

# Initialize the project's disposable test setup before importing the wrapper.
import tests.conftest  # noqa: F401, E402
import fx_capacity_server  # noqa: E402
import pytest  # noqa: E402

# Performance samplers are unnecessary during behavior tests; keep the real
# middleware and publisher experiment changes, with the existing test lifespan.
fx_capacity_server.app.router.lifespan_context = fx_capacity_server.original_lifespan
raise SystemExit(pytest.main([
    "-q", "-x", "tests/test_fx_loadtest_driver.py", "tests/test_trading_stream.py",
    "tests/test_credit_final_guards.py", "tests/test_fx_publisher.py",
    "tests/test_history_endpoint.py", "tests/test_fx_history.py",
]))
