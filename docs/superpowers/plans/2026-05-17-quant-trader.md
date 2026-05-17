# Quant Trader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `quant/` 子目录下的实盘量化交易脚本：模块化单进程框架（client/broker/strategy/state 四层），内置 Grid + DCA 两个策略，强制风控与 kill switch，token 自动续期，部署用 tmux + watchdog + user cron。

**Architecture:** asyncio 单进程；strategy 注入 `StrategyContext(rest, broker, store, lmsr, logger, config)`；下单走 broker 强制过 risk；state 用 aiosqlite 三表持久化；REST polling 起步，SSE skeleton 留接口未启用。

**Tech Stack:** Python 3.11+ / httpx (async) / pydantic v2 / aiosqlite / PyYAML / python-dotenv / structlog / respx (test) / pytest-asyncio / setproctitle / uv (venv 管理)

**Spec:** `docs/superpowers/specs/2026-05-17-quant-trader-design.md`

---

## File Structure

```
quant/
├── README.md
├── run.sh
├── pyproject.toml
├── .env.example
├── config.example.yaml
├── thccb_quant/
│   ├── __init__.py
│   ├── __main__.py            # python -m thccb_quant
│   ├── trader.py              # 主 loop + kill switch + 信号
│   ├── errors.py
│   ├── logging_setup.py
│   ├── lmsr.py
│   ├── client/
│   │   ├── __init__.py
│   │   ├── auth.py            # TokenManager
│   │   ├── rest.py            # RestClient + rate limit
│   │   └── sse.py             # SseClient skeleton (not used at start)
│   ├── broker/
│   │   ├── __init__.py
│   │   ├── base.py            # Broker ABC
│   │   ├── risk.py            # RiskGuard
│   │   ├── live.py            # LiveBroker
│   │   └── dryrun.py          # DryRunBroker
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── base.py            # Strategy ABC + StrategyContext
│   │   ├── registry.py        # STRATEGY_REGISTRY
│   │   ├── grid.py
│   │   └── dca.py
│   └── state/
│       ├── __init__.py
│       ├── schema.sql
│       └── store.py           # aiosqlite 包装
└── tests/
    ├── __init__.py
    ├── conftest.py            # 共享 fixtures
    ├── test_lmsr.py
    ├── test_logging.py
    ├── test_store.py
    ├── test_auth.py
    ├── test_rest.py
    ├── test_risk.py
    ├── test_broker.py
    ├── test_strategy_dca.py
    └── test_strategy_grid.py
```

主仓 `.gitignore` 追加：
```
quant/.env
quant/config.yaml
quant/state/
quant/logs/
quant/.venv/
quant/__pycache__/
quant/**/__pycache__/
quant/*.db*
quant/.pytest_cache/
```

---

## Task 1: 项目骨架与依赖

**Files:**
- Create: `quant/pyproject.toml`
- Create: `quant/.env.example`
- Create: `quant/config.example.yaml`
- Create: `quant/thccb_quant/__init__.py`
- Create: `quant/tests/__init__.py`
- Create: `quant/tests/conftest.py`
- Modify: `.gitignore`（主仓根）

- [ ] **Step 1: 新建 `quant/pyproject.toml`**

```toml
[project]
name = "thccb-quant"
version = "0.1.0"
description = "TouhouCCB quant trader"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "aiosqlite>=0.20",
    "PyYAML>=6.0",
    "python-dotenv>=1.0",
    "structlog>=24.1",
    "setproctitle>=1.3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["thccb_quant*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 新建 `quant/.env.example`**

```
# 从浏览器登录 thccb.com 后 F12 Network 抓 Authorization header
THCCB_BASE_URL=http://127.0.0.1:8004
THCCB_ACCESS_TOKEN=eyJ_replace_me
THCCB_REFRESH_TOKEN=eyJ_replace_me
```

- [ ] **Step 3: 新建 `quant/config.example.yaml`**

```yaml
risk:
  single_order_cap_cny: 50
  daily_loss_cap_cny: 100
  daily_turnover_cap_cny: 2000
  max_slippage_bps: 300
  min_seconds_between_orders: 3

client:
  rate_limit_per_sec: 8
  request_timeout_sec: 10

strategies:
  - name: dca_market_1_outcome_yes
    type: dca
    enabled: false
    outcome_id: 1
    cny_per_buy: 5.0
    interval_hours: 6
    total_budget_cny: 200

  - name: grid_market_1_outcome_yes
    type: grid
    enabled: false
    market_id: 1
    outcome_id: 1
    price_low: 0.30
    price_high: 0.60
    grid_count: 6
    shares_per_grid: 2.0
    tick_interval_sec: 30
```

- [ ] **Step 4: 新建 `quant/thccb_quant/__init__.py` 和 `quant/tests/__init__.py`（空文件）**

```bash
: > quant/thccb_quant/__init__.py
: > quant/tests/__init__.py
```

- [ ] **Step 5: 新建 `quant/tests/conftest.py`**

```python
import asyncio
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
```

- [ ] **Step 6: 修改主仓根 `.gitignore`，追加 quant 块**

```bash
cat >> .gitignore <<'EOF'

# quant trader
quant/.env
quant/config.yaml
quant/state/
quant/logs/
quant/.venv/
quant/__pycache__/
quant/**/__pycache__/
quant/*.db*
quant/.pytest_cache/
EOF
```

- [ ] **Step 7: 安装依赖并验证**

```bash
cd quant/
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
python -c "import httpx, pydantic, aiosqlite, yaml, dotenv, structlog, setproctitle; print('ok')"
```
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
cd ..   # 回主仓根
git add quant/pyproject.toml quant/.env.example quant/config.example.yaml \
  quant/thccb_quant/__init__.py quant/tests/__init__.py quant/tests/conftest.py \
  .gitignore
git commit -m "feat(quant): 初始化项目骨架与依赖"
```

---

## Task 2: 异常类

**Files:**
- Create: `quant/thccb_quant/errors.py`

- [ ] **Step 1: 新建 `quant/thccb_quant/errors.py`**

```python
"""量化交易脚本异常分类。spec §8。"""


class QuantError(Exception):
    """所有量化脚本异常基类。"""


class FatalAuthError(QuantError):
    """Refresh token 失效或刷新失败，全局停机。"""


class RiskRejected(QuantError):
    """风控规则拒绝下单，策略 catch 后跳过本轮。"""


class TransientError(QuantError):
    """5xx / 网络超时，rest 已重试过仍失败。"""


class BusinessError(QuantError):
    """4xx 业务错（余额不足、份额不足、市场已关等）。"""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class StrategyError(QuantError):
    """策略自身 bug，runner 隔离单策略停其他继续。"""
```

- [ ] **Step 2: 验证 import**

```bash
cd quant/ && source .venv/bin/activate
python -c "from thccb_quant.errors import QuantError, FatalAuthError, RiskRejected, TransientError, BusinessError, StrategyError; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd ..
git add quant/thccb_quant/errors.py
git commit -m "feat(quant): 加异常分类"
```

---

## Task 3: LMSR 本地复现（TDD，对照后端）

**Files:**
- Create: `quant/tests/test_lmsr.py`
- Create: `quant/thccb_quant/lmsr.py`

后端参考: `backend/app/services/lmsr.py:27` `calculate_lmsr_cost`、`:38` `get_current_price`。

- [ ] **Step 1: 写失败测试 `quant/tests/test_lmsr.py`**

```python
"""与后端 backend/app/services/lmsr.py 公式对照。差异 < 1e-8 才通过。"""
import math

import pytest

from thccb_quant.lmsr import (
    calculate_lmsr_cost,
    get_current_price,
    calculate_lmsr_with_prices,
)


def _ref_cost(shares, b):
    if not shares:
        return 0.0
    max_q = max(shares)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares)
    return b * (math.log(sum_exp) + (max_q / b))


def _ref_price(shares, idx, b):
    max_q = max(shares)
    exps = [math.exp((q - max_q) / b) for q in shares]
    return exps[idx] / sum(exps)


@pytest.mark.parametrize(
    "shares,b",
    [
        ([0.0, 0.0], 100.0),
        ([10.0, 5.0], 100.0),
        ([100.0, 50.0], 100.0),
        ([1000.0, 800.0, 500.0], 100.0),
        ([0.5, 0.5], 50.0),
    ],
)
def test_cost_matches_backend(shares, b):
    assert abs(calculate_lmsr_cost(shares, b) - _ref_cost(shares, b)) < 1e-8


@pytest.mark.parametrize(
    "shares,b,idx",
    [
        ([10.0, 5.0], 100.0, 0),
        ([10.0, 5.0], 100.0, 1),
        ([1000.0, 800.0, 500.0], 100.0, 2),
    ],
)
def test_price_matches_backend(shares, b, idx):
    assert abs(get_current_price(shares, idx, b) - _ref_price(shares, idx, b)) < 1e-8


def test_empty_shares_returns_zero():
    assert calculate_lmsr_cost([], 100.0) == 0.0


def test_with_prices_combined():
    shares = [10.0, 5.0]
    b = 100.0
    cost, prices = calculate_lmsr_with_prices(shares, b)
    assert abs(cost - _ref_cost(shares, b)) < 1e-8
    assert abs(prices[0] - _ref_price(shares, 0, b)) < 1e-8
    assert abs(sum(prices) - 1.0) < 1e-9
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_lmsr.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'thccb_quant.lmsr'`

- [ ] **Step 3: 写实现 `quant/thccb_quant/lmsr.py`**

```python
"""本地复现后端 LMSR 报价，用于策略本地预判。

数学严格对齐 backend/app/services/lmsr.py，差异 < 1e-8。
"""
import math
from typing import List, Tuple


def calculate_lmsr_cost(shares_list: List[float], b: float) -> float:
    if not shares_list:
        return 0.0
    max_q = max(shares_list)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares_list)
    return b * (math.log(sum_exp) + (max_q / b))


def get_current_price(shares_list: List[float], target_index: int, b: float) -> float:
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    return exponents[target_index] / sum(exponents)


def calculate_lmsr_with_prices(
    shares_list: List[float], b: float
) -> Tuple[float, List[float]]:
    if not shares_list:
        return 0.0, []
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    sum_exp = sum(exponents)
    cost = b * (math.log(sum_exp) + (max_q / b))
    prices = [e / sum_exp for e in exponents]
    return cost, prices


def estimate_buy_cost(
    shares_list: List[float], target_index: int, delta_shares: float, b: float
) -> float:
    """模拟买入 delta_shares 后的成本差（即用户实付，未含手续费）。"""
    before = calculate_lmsr_cost(shares_list, b)
    after = list(shares_list)
    after[target_index] += delta_shares
    return calculate_lmsr_cost(after, b) - before


def estimate_sell_proceeds(
    shares_list: List[float], target_index: int, delta_shares: float, b: float
) -> float:
    """模拟卖出 delta_shares 后用户获得的金额（未扣手续费）。"""
    before = calculate_lmsr_cost(shares_list, b)
    after = list(shares_list)
    after[target_index] -= delta_shares
    return before - calculate_lmsr_cost(after, b)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_lmsr.py -v
```
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/lmsr.py quant/tests/test_lmsr.py
git commit -m "feat(quant): 本地 LMSR 复现，对照后端公式 < 1e-8"
```

---

## Task 4: structlog 日志配置

**Files:**
- Create: `quant/thccb_quant/logging_setup.py`
- Create: `quant/tests/test_logging.py`

- [ ] **Step 1: 写失败测试 `quant/tests/test_logging.py`**

```python
import json
from pathlib import Path

from thccb_quant.logging_setup import setup_logging, get_logger


def test_log_emits_json_with_fields(tmp_path: Path):
    log_dir = tmp_path / "logs"
    setup_logging(log_dir, "system")
    log = get_logger("test", strategy="grid_x", outcome_id=42)
    log.info("hello", extra_field="v")

    files = list(log_dir.glob("*.jsonl"))
    assert len(files) >= 1
    line = files[0].read_text().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["strategy"] == "grid_x"
    assert payload["outcome_id"] == 42
    assert payload["extra_field"] == "v"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_logging.py -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 写实现 `quant/thccb_quant/logging_setup.py`**

```python
"""structlog JSON Lines 日志配置。spec §9。"""
import logging
from pathlib import Path
from typing import Any

import structlog


def setup_logging(log_dir: Path, channel: str = "system") -> None:
    """初始化 structlog，写入 <log_dir>/<channel>.jsonl。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{channel}.jsonl"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **bind: Any) -> structlog.BoundLogger:
    return structlog.get_logger(name).bind(**bind)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_logging.py -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/logging_setup.py quant/tests/test_logging.py
git commit -m "feat(quant): structlog JSON 日志配置"
```

---

## Task 5: SQLite State 层

**Files:**
- Create: `quant/thccb_quant/state/__init__.py`
- Create: `quant/thccb_quant/state/schema.sql`
- Create: `quant/thccb_quant/state/store.py`
- Create: `quant/tests/test_store.py`

- [ ] **Step 1: 写 `quant/thccb_quant/state/schema.sql`**

```sql
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  shares TEXT NOT NULL,
  price TEXT,
  cost TEXT,
  status TEXT NOT NULL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_dedup
  ON orders (strategy, outcome_id, side, shares, ts);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome_id INTEGER,
  action TEXT NOT NULL,
  reason TEXT,
  snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
  date TEXT PRIMARY KEY,
  gross_turnover TEXT NOT NULL DEFAULT '0',
  net_pnl TEXT NOT NULL DEFAULT '0'
);
```

- [ ] **Step 2: 新建 `quant/thccb_quant/state/__init__.py`（空）**

```bash
: > quant/thccb_quant/state/__init__.py
```

- [ ] **Step 3: 写失败测试 `quant/tests/test_store.py`**

```python
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    db_path = tmp_path / "test.db"
    s = await Store.open(db_path)
    yield s
    await s.close()


async def test_log_order_success(store: Store):
    await store.log_order(
        strategy="grid_x",
        outcome_id=1,
        side="buy",
        shares=Decimal("2.5"),
        price=Decimal("0.42"),
        cost=Decimal("1.05"),
        status="success",
    )
    rows = await store.recent_orders(strategy="grid_x", limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["shares"] == "2.5"


async def test_has_recent_duplicate_window(store: Store):
    await store.log_order(
        strategy="g", outcome_id=1, side="buy",
        shares=Decimal("2.5"), price=Decimal("0.42"),
        cost=Decimal("1.05"), status="success",
    )
    assert await store.has_recent_duplicate(
        "g", 1, "buy", Decimal("2.5"), within_sec=5, statuses=("success", "dryrun")
    )
    # failed 单不算
    assert not await store.has_recent_duplicate(
        "g", 1, "buy", Decimal("3.0"), within_sec=5, statuses=("success", "dryrun")
    )


async def test_daily_stats_accumulate(store: Store):
    today = datetime.now(timezone.utc).date().isoformat()
    await store.add_turnover(Decimal("10.5"))
    await store.add_turnover(Decimal("4.5"))
    stats = await store.get_daily_stats(today)
    assert Decimal(stats["gross_turnover"]) == Decimal("15.0")


async def test_log_decision(store: Store):
    await store.log_decision(
        strategy="g", outcome_id=1, action="skip", reason="below threshold",
        snapshot={"price": 0.42},
    )
    rows = await store.recent_decisions(strategy="g", limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "skip"
```

- [ ] **Step 4: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_store.py -v
```
Expected: FAIL — module not found

- [ ] **Step 5: 写实现 `quant/thccb_quant/state/store.py`**

```python
"""aiosqlite 封装：orders / decisions / daily_stats。spec §7。"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, db_path: Path) -> "Store":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        with _SCHEMA_PATH.open() as f:
            await conn.executescript(f.read())
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def log_order(
        self,
        *,
        strategy: str,
        outcome_id: int,
        side: str,
        shares: Decimal,
        price: Optional[Decimal] = None,
        cost: Optional[Decimal] = None,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO orders (ts, strategy, outcome_id, side, shares, price, cost, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow_iso(),
                strategy,
                outcome_id,
                side,
                str(shares),
                str(price) if price is not None else None,
                str(cost) if cost is not None else None,
                status,
                error,
            ),
        )
        await self._conn.commit()

    async def has_recent_duplicate(
        self,
        strategy: str,
        outcome_id: int,
        side: str,
        shares: Decimal,
        *,
        within_sec: int,
        statuses: tuple,
    ) -> bool:
        cutoff = datetime.now(timezone.utc).timestamp() - within_sec
        placeholders = ",".join("?" * len(statuses))
        cur = await self._conn.execute(
            f"SELECT ts FROM orders WHERE strategy = ? AND outcome_id = ? AND side = ? "
            f"AND shares = ? AND status IN ({placeholders})",
            (strategy, outcome_id, side, str(shares), *statuses),
        )
        rows = await cur.fetchall()
        for row in rows:
            ts = datetime.fromisoformat(row["ts"]).timestamp()
            if ts >= cutoff:
                return True
        return False

    async def recent_orders(self, *, strategy: Optional[str] = None, limit: int = 50) -> list[dict]:
        if strategy:
            cur = await self._conn.execute(
                "SELECT * FROM orders WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    async def log_decision(
        self,
        *,
        strategy: str,
        outcome_id: Optional[int],
        action: str,
        reason: Optional[str] = None,
        snapshot: Optional[dict] = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO decisions (ts, strategy, outcome_id, action, reason, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utcnow_iso(),
                strategy,
                outcome_id,
                action,
                reason,
                json.dumps(snapshot) if snapshot else None,
            ),
        )
        await self._conn.commit()

    async def recent_decisions(self, *, strategy: Optional[str] = None, limit: int = 50) -> list[dict]:
        if strategy:
            cur = await self._conn.execute(
                "SELECT * FROM decisions WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    async def add_turnover(self, amount: Decimal) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self._conn.execute(
            "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, ?, '0') "
            "ON CONFLICT(date) DO UPDATE SET gross_turnover = "
            "CAST((CAST(gross_turnover AS REAL) + ?) AS TEXT)",
            (today, str(amount), float(amount)),
        )
        await self._conn.commit()

    async def add_pnl(self, amount: Decimal) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self._conn.execute(
            "INSERT INTO daily_stats (date, gross_turnover, net_pnl) VALUES (?, '0', ?) "
            "ON CONFLICT(date) DO UPDATE SET net_pnl = "
            "CAST((CAST(net_pnl AS REAL) + ?) AS TEXT)",
            (today, str(amount), float(amount)),
        )
        await self._conn.commit()

    async def get_daily_stats(self, date: str) -> dict[str, Any]:
        cur = await self._conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (date,)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {"date": date, "gross_turnover": "0", "net_pnl": "0"}
```

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest tests/test_store.py -v
```
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
cd ..
git add quant/thccb_quant/state/ quant/tests/test_store.py
git commit -m "feat(quant): SQLite state 层（orders/decisions/daily_stats）"
```

---

## Task 6: TokenManager（auth.py）

**Files:**
- Create: `quant/thccb_quant/client/__init__.py`
- Create: `quant/thccb_quant/client/auth.py`
- Create: `quant/tests/test_auth.py`

- [ ] **Step 1: 新建空 `quant/thccb_quant/client/__init__.py`**

```bash
: > quant/thccb_quant/client/__init__.py
```

- [ ] **Step 2: 写失败测试 `quant/tests/test_auth.py`**

```python
import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from thccb_quant.client.auth import TokenManager
from thccb_quant.errors import FatalAuthError


def _mk_jwt(exp_in_sec: int) -> str:
    """构造一个能被 jwt_decode_exp 读到的 JWT（不验签）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload_obj = {"sub": "42", "exp": int(time.time()) + exp_in_sec}
    payload = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@pytest.fixture
def env_file(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("")
    return p


@respx.mock
async def test_no_refresh_when_token_fresh(env_file: Path):
    fresh = _mk_jwt(3600)
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=fresh,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        token = await mgr.get_valid_access()
    assert token == fresh


@respx.mock
async def test_refresh_when_expiring(env_file: Path):
    expiring = _mk_jwt(60)  # < 5 min
    new_token = _mk_jwt(3600)
    route = respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": new_token, "token_type": "bearer"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        token = await mgr.get_valid_access()
    assert token == new_token
    assert route.called
    assert "THCCB_ACCESS_TOKEN" in env_file.read_text()


@respx.mock
async def test_concurrent_refresh_only_once(env_file: Path):
    expiring = _mk_jwt(60)
    new_token = _mk_jwt(3600)
    route = respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": new_token, "token_type": "bearer"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        results = await asyncio.gather(*[mgr.get_valid_access() for _ in range(10)])
    assert all(t == new_token for t in results)
    assert route.call_count == 1


@respx.mock
async def test_refresh_401_raises_fatal(env_file: Path):
    expiring = _mk_jwt(60)
    respx.post("http://x/api/v1/auth/refresh").mock(
        return_value=httpx.Response(401, json={"detail": "expired"})
    )
    async with httpx.AsyncClient(base_url="http://x") as client:
        mgr = TokenManager(
            base_url="http://x",
            access_token=expiring,
            refresh_token=_mk_jwt(86400 * 6),
            env_path=env_file,
            raw_client=client,
        )
        with pytest.raises(FatalAuthError):
            await mgr.get_valid_access()
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_auth.py -v
```
Expected: FAIL — module not found

- [ ] **Step 4: 写实现 `quant/thccb_quant/client/auth.py`**

```python
"""TokenManager: JWT 解析、自动 refresh、写回 .env。spec §4.1。

后端 /auth/refresh 只返回新 access_token，不轮换 refresh_token，所以这里
不更新 refresh。
"""
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import set_key

from thccb_quant.errors import FatalAuthError


def jwt_decode_exp(token: str) -> int:
    """从 JWT payload 取 exp（不验签）。"""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("invalid jwt")
    payload_b64 = parts[1]
    # padding
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return int(payload["exp"])


class TokenManager:
    REFRESH_BUFFER_SEC = 300  # 剩 < 5 min 触发刷新

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        refresh_token: str,
        env_path: Path,
        raw_client: httpx.AsyncClient,
    ):
        self._base_url = base_url
        self._access = access_token
        self._refresh = refresh_token
        self._env_path = env_path
        self._client = raw_client
        self._exp = jwt_decode_exp(access_token)
        self._refresh_exp = jwt_decode_exp(refresh_token)
        self._lock = asyncio.Lock()

    @property
    def refresh_exp_ts(self) -> int:
        return self._refresh_exp

    async def get_valid_access(self) -> str:
        if self._exp - time.time() < self.REFRESH_BUFFER_SEC:
            async with self._lock:
                if self._exp - time.time() < self.REFRESH_BUFFER_SEC:
                    await self._refresh_token()
        return self._access

    async def _refresh_token(self) -> None:
        resp = await self._client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": self._refresh},
        )
        if resp.status_code != 200:
            raise FatalAuthError(
                f"refresh failed: {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        self._access = data["access_token"]
        self._exp = jwt_decode_exp(self._access)
        set_key(str(self._env_path), "THCCB_ACCESS_TOKEN", self._access)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/test_auth.py -v
```
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd ..
git add quant/thccb_quant/client/__init__.py quant/thccb_quant/client/auth.py \
  quant/tests/test_auth.py
git commit -m "feat(quant): TokenManager 自动续期与 .env 写回"
```

---

## Task 7: RestClient（API 封装 + 限速）

**Files:**
- Create: `quant/thccb_quant/client/rest.py`
- Create: `quant/tests/test_rest.py`

后端端点参考: `backend/app/api/v1/market.py` `:925 /quote`、`:451 /buy`、`:606 /sell`、`:272 /list`、`:355 /{id}`；`backend/app/api/v1/user.py` `:38 /summary`、`:103 /holdings`；`backend/app/api/v1/auth.py` `:225 /me`。

- [ ] **Step 1: 写失败测试 `quant/tests/test_rest.py`**

```python
import asyncio
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.errors import BusinessError, TransientError


def _jwt(exp_secs: int = 3600):
    import base64, json
    payload = {"exp": int(time.time()) + exp_secs}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@pytest.fixture
async def rest_pair(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    async with httpx.AsyncClient(base_url="http://x") as raw:
        mgr = TokenManager(
            base_url="http://x",
            access_token=_jwt(),
            refresh_token=_jwt(86400),
            env_path=env,
            raw_client=raw,
        )
        async with httpx.AsyncClient(base_url="http://x") as client:
            rest = RestClient(client=client, token_manager=mgr, rate_limit_per_sec=100)
            yield rest


@respx.mock
async def test_quote_returns_parsed(rest_pair: RestClient):
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "outcome_id": 1, "side": "buy", "shares": "2.5",
                "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
                "after_prices": [],
            },
        )
    )
    q = await rest_pair.quote(outcome_id=1, shares=Decimal("2.5"), side="buy")
    assert q.gross == Decimal("1.05")


@respx.mock
async def test_buy_4xx_raises_business(rest_pair: RestClient):
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(400, json={"detail": "现金不足"})
    )
    with pytest.raises(BusinessError) as ei:
        await rest_pair.buy(outcome_id=1, shares=Decimal("2.5"), max_slippage_bps=300)
    assert ei.value.status == 400


@respx.mock
async def test_5xx_retries_then_transient(rest_pair: RestClient):
    route = respx.get("http://x/api/v1/market/1").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(TransientError):
        await rest_pair.get_market(1)
    assert route.call_count >= 2  # 重试过


@respx.mock
async def test_rate_limit_throttles(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    respx.get("http://x/api/v1/market/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1, "title": "t", "status": "trading", "liquidity_b": 100.0,
                "outcomes": [], "last_trade_at": None,
            },
        )
    )
    async with httpx.AsyncClient(base_url="http://x") as raw:
        mgr = TokenManager(
            base_url="http://x",
            access_token=_jwt(),
            refresh_token=_jwt(86400),
            env_path=env,
            raw_client=raw,
        )
        async with httpx.AsyncClient(base_url="http://x") as client:
            rest = RestClient(client=client, token_manager=mgr, rate_limit_per_sec=4)
            start = time.monotonic()
            await asyncio.gather(*[rest.get_market(1) for _ in range(8)])
            elapsed = time.monotonic() - start
    # 8 个请求 / 4 r/s 至少要 ~1s
    assert elapsed > 0.9
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_rest.py -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 写实现 `quant/thccb_quant/client/rest.py`**

```python
"""RestClient: 封装所有 thccb API 端点 + 自限速 + 错误分类。spec §4.2。"""
import asyncio
import time
from decimal import Decimal
from typing import Any, List, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from thccb_quant.client.auth import TokenManager
from thccb_quant.errors import BusinessError, TransientError


# ── Response models ────────────────────────────────────────────

class OutcomePriceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    label: str
    shares: float
    current_price: float


class QuoteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome_id: int
    side: str
    shares: Decimal
    avg_price: Decimal
    gross: Decimal
    fee: Decimal
    net: Decimal
    after_prices: List[OutcomePriceItem] = []


class OrderResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    shares: Decimal
    cost: Decimal
    new_cash: Decimal
    message: Optional[str] = None


class OutcomeDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    label: str
    total_shares: Decimal
    current_price: Decimal


class MarketDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    status: str
    liquidity_b: float
    outcomes: List[OutcomeDetail]


class MarketSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    status: str


class HoldingRead(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome_id: int
    outcome_label: str
    market_id: int
    market_title: str
    amount: Decimal
    cost_basis: Decimal
    avg_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


class UserSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    cash: Decimal
    debt: Decimal
    holdings_value: Decimal
    net_worth: Decimal


# ── RestClient ──────────────────────────────────────────────────

class _TokenBucket:
    """简易 token bucket：每秒补 rate 个 token，capacity = rate。"""

    def __init__(self, rate: float):
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class RestClient:
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.2

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_manager: TokenManager,
        rate_limit_per_sec: float = 8.0,
    ):
        self._c = client
        self._tm = token_manager
        self._bucket = _TokenBucket(rate_limit_per_sec)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self._bucket.acquire()
        token = await self._tm.get_valid_access()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        last_exc: Optional[Exception] = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self._c.request(method, path, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                await asyncio.sleep(self.BACKOFF_BASE * (2 ** attempt))
                continue

            if resp.status_code < 400:
                return resp
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                raise BusinessError(resp.status_code, str(detail))
            # 5xx / 429 → retry
            last_exc = TransientError(f"{resp.status_code} {resp.text[:200]}")
            await asyncio.sleep(self.BACKOFF_BASE * (2 ** attempt))

        if last_exc:
            raise TransientError(str(last_exc))
        raise TransientError("unknown")

    # ── Market endpoints ──

    async def quote(
        self, *, outcome_id: int, shares: Decimal, side: Literal["buy", "sell"]
    ) -> QuoteResponse:
        r = await self._request(
            "POST", "/api/v1/market/quote",
            json={"outcome_id": outcome_id, "shares": str(shares), "side": side},
        )
        return QuoteResponse.model_validate(r.json())

    async def buy(
        self, *, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse:
        r = await self._request(
            "POST", "/api/v1/market/buy",
            json={
                "outcome_id": outcome_id,
                "shares": str(shares),
                "max_slippage_bps": max_slippage_bps,
                "accept_any_slippage": False,
            },
        )
        return OrderResponse.model_validate(r.json())

    async def sell(
        self, *, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse:
        r = await self._request(
            "POST", "/api/v1/market/sell",
            json={
                "outcome_id": outcome_id,
                "shares": str(shares),
                "max_slippage_bps": max_slippage_bps,
                "accept_any_slippage": False,
            },
        )
        return OrderResponse.model_validate(r.json())

    async def list_markets(self) -> List[MarketSummary]:
        r = await self._request("GET", "/api/v1/market/list")
        return [MarketSummary.model_validate(x) for x in r.json()]

    async def get_market(self, market_id: int) -> MarketDetail:
        r = await self._request("GET", f"/api/v1/market/{market_id}")
        return MarketDetail.model_validate(r.json())

    # ── User endpoints ──

    async def get_holdings(self) -> List[HoldingRead]:
        r = await self._request("GET", "/api/v1/user/holdings")
        return [HoldingRead.model_validate(x) for x in r.json()]

    async def get_user_summary(self) -> UserSummary:
        r = await self._request("GET", "/api/v1/user/summary")
        return UserSummary.model_validate(r.json())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_rest.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/client/rest.py quant/tests/test_rest.py
git commit -m "feat(quant): RestClient 封装 + token bucket 限速 + 错误分类"
```

---

## Task 8: RiskGuard

**Files:**
- Create: `quant/thccb_quant/broker/__init__.py`
- Create: `quant/thccb_quant/broker/risk.py`
- Create: `quant/tests/test_risk.py`

- [ ] **Step 1: 新建 `quant/thccb_quant/broker/__init__.py`（空）**

```bash
: > quant/thccb_quant/broker/__init__.py
```

- [ ] **Step 2: 写失败测试 `quant/tests/test_risk.py`**

```python
import time
from decimal import Decimal
from pathlib import Path

import pytest

from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.errors import RiskRejected


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(
        single_order_cap_cny=Decimal("50"),
        daily_loss_cap_cny=Decimal("100"),
        daily_turnover_cap_cny=Decimal("2000"),
        max_slippage_bps=300,
        min_seconds_between_orders=3,
    )


@pytest.fixture
def state_dir(tmp_path: Path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_pass_normal(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.check(outcome_id=1, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))


def test_kill_switch_blocks(cfg, state_dir):
    (state_dir / "KILL").touch()
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="kill"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_single_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="single"):
        g.check(outcome_id=1, side="buy", cost=Decimal("51"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_slippage_over_config(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="slippage"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=400, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))


def test_daily_loss_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="loss"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("-100"))


def test_daily_turnover_cap(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    with pytest.raises(RiskRejected, match="turnover"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("1999"),
                net_pnl_today=Decimal("0"))


def test_cooldown(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.mark_order(outcome_id=1)
    with pytest.raises(RiskRejected, match="cooldown"):
        g.check(outcome_id=1, side="buy", cost=Decimal("10"),
                max_slippage_bps=200, turnover_today=Decimal("0"),
                net_pnl_today=Decimal("0"))
    time.sleep(3.1)
    g.check(outcome_id=1, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))


def test_cooldown_per_outcome(cfg, state_dir):
    g = RiskGuard(cfg, state_dir)
    g.mark_order(outcome_id=1)
    # 其他 outcome 不受冷却限制
    g.check(outcome_id=2, side="buy", cost=Decimal("10"),
            max_slippage_bps=200, turnover_today=Decimal("0"),
            net_pnl_today=Decimal("0"))
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_risk.py -v
```
Expected: FAIL — module not found

- [ ] **Step 4: 写实现 `quant/thccb_quant/broker/risk.py`**

```python
"""RiskGuard: 下单前最后一道闸。spec §5.1-§5.2。"""
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from thccb_quant.errors import RiskRejected


@dataclass
class RiskConfig:
    single_order_cap_cny: Decimal
    daily_loss_cap_cny: Decimal
    daily_turnover_cap_cny: Decimal
    max_slippage_bps: int
    min_seconds_between_orders: int


class RiskGuard:
    def __init__(self, config: RiskConfig, state_dir: Path):
        self._cfg = config
        self._state_dir = state_dir
        self._last_order_ts: dict[int, float] = {}

    def _kill_switch_active(self) -> bool:
        return (self._state_dir / "KILL").exists()

    def check(
        self,
        *,
        outcome_id: int,
        side: str,
        cost: Decimal,
        max_slippage_bps: int,
        turnover_today: Decimal,
        net_pnl_today: Decimal,
    ) -> None:
        if self._kill_switch_active():
            raise RiskRejected("kill switch active")

        if max_slippage_bps > self._cfg.max_slippage_bps:
            raise RiskRejected(
                f"slippage {max_slippage_bps} bps > config {self._cfg.max_slippage_bps}"
            )

        if cost.copy_abs() > self._cfg.single_order_cap_cny:
            raise RiskRejected(
                f"single order cost {cost} > cap {self._cfg.single_order_cap_cny}"
            )

        if net_pnl_today <= -self._cfg.daily_loss_cap_cny:
            raise RiskRejected(
                f"daily loss {net_pnl_today} reached cap {-self._cfg.daily_loss_cap_cny}"
            )

        if turnover_today + cost.copy_abs() > self._cfg.daily_turnover_cap_cny:
            raise RiskRejected(
                f"daily turnover would exceed cap {self._cfg.daily_turnover_cap_cny}"
            )

        last = self._last_order_ts.get(outcome_id)
        if last is not None and time.monotonic() - last < self._cfg.min_seconds_between_orders:
            raise RiskRejected(
                f"cooldown: last order on outcome {outcome_id} too recent"
            )

    def mark_order(self, *, outcome_id: int) -> None:
        self._last_order_ts[outcome_id] = time.monotonic()
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/test_risk.py -v
```
Expected: 8 passed (含一个 3 秒 sleep 的 cooldown 测试)

- [ ] **Step 6: Commit**

```bash
cd ..
git add quant/thccb_quant/broker/__init__.py quant/thccb_quant/broker/risk.py \
  quant/tests/test_risk.py
git commit -m "feat(quant): RiskGuard 单笔/日亏/日流水/滑点/冷却 + kill switch"
```

---

## Task 9: Broker（base/live/dryrun）

**Files:**
- Create: `quant/thccb_quant/broker/base.py`
- Create: `quant/thccb_quant/broker/live.py`
- Create: `quant/thccb_quant/broker/dryrun.py`
- Create: `quant/tests/test_broker.py`

- [ ] **Step 1: 写 `quant/thccb_quant/broker/base.py`**

```python
"""Broker ABC：spec §5。"""
from abc import ABC, abstractmethod
from decimal import Decimal

from thccb_quant.client.rest import OrderResponse


class Broker(ABC):
    @abstractmethod
    async def buy(
        self, *, strategy: str, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse: ...

    @abstractmethod
    async def sell(
        self, *, strategy: str, outcome_id: int, shares: Decimal, max_slippage_bps: int
    ) -> OrderResponse: ...
```

- [ ] **Step 2: 写失败测试 `quant/tests/test_broker.py`**

```python
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from thccb_quant.broker.live import LiveBroker
from thccb_quant.broker.dryrun import DryRunBroker
from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.errors import BusinessError, RiskRejected
from thccb_quant.state.store import Store


def _jwt(exp=3600):
    import base64, json
    p = {"exp": int(time.time()) + exp}
    return (
        base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
        + ".sig"
    )


@pytest.fixture
async def deps(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = await Store.open(state_dir / "test.db")
    cfg = RiskConfig(
        single_order_cap_cny=Decimal("50"),
        daily_loss_cap_cny=Decimal("100"),
        daily_turnover_cap_cny=Decimal("2000"),
        max_slippage_bps=300,
        min_seconds_between_orders=0,  # 测试不要冷却干扰
    )
    risk = RiskGuard(cfg, state_dir)
    async with httpx.AsyncClient(base_url="http://x") as raw:
        mgr = TokenManager(
            base_url="http://x",
            access_token=_jwt(),
            refresh_token=_jwt(86400),
            env_path=env,
            raw_client=raw,
        )
        async with httpx.AsyncClient(base_url="http://x") as client:
            rest = RestClient(client=client, token_manager=mgr, rate_limit_per_sec=100)
            yield rest, risk, store
    await store.close()


@respx.mock
async def test_live_buy_success_logs_order(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(200, json={
            "shares": 2.5, "cost": 1.05, "new_cash": 498.95, "message": "ok",
        })
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    resp = await broker.buy(
        strategy="g", outcome_id=1, shares=Decimal("2.5"), max_slippage_bps=200
    )
    assert resp.cost == Decimal("1.05")
    orders = await store.recent_orders(strategy="g", limit=10)
    assert len(orders) == 1
    assert orders[0]["status"] == "success"


@respx.mock
async def test_live_buy_400_logs_failed(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(400, json={"detail": "余额不足"})
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    with pytest.raises(BusinessError):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)
    orders = await store.recent_orders(strategy="g", limit=10)
    assert len(orders) == 1
    assert orders[0]["status"] == "failed"


@respx.mock
async def test_quote_fail_does_not_call_buy(deps):
    rest, risk, store = deps
    quote_route = respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(500)
    )
    buy_route = respx.post("http://x/api/v1/market/buy")
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    with pytest.raises(Exception):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)
    assert quote_route.called
    assert not buy_route.called


@respx.mock
async def test_idempotent_duplicate_within_5s(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    respx.post("http://x/api/v1/market/buy").mock(
        return_value=httpx.Response(200, json={
            "shares": 2.5, "cost": 1.05, "new_cash": 498.95, "message": "ok",
        })
    )
    broker = LiveBroker(rest=rest, risk=risk, store=store)
    await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                     max_slippage_bps=200)
    with pytest.raises(RiskRejected, match="duplicate"):
        await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                         max_slippage_bps=200)


@respx.mock
async def test_dryrun_does_not_call_buy(deps):
    rest, risk, store = deps
    respx.post("http://x/api/v1/market/quote").mock(
        return_value=httpx.Response(200, json={
            "outcome_id": 1, "side": "buy", "shares": "2.5",
            "avg_price": "0.42", "gross": "1.05", "fee": "0", "net": "1.05",
            "after_prices": [],
        })
    )
    buy_route = respx.post("http://x/api/v1/market/buy")
    broker = DryRunBroker(rest=rest, risk=risk, store=store)
    resp = await broker.buy(strategy="g", outcome_id=1, shares=Decimal("2.5"),
                            max_slippage_bps=200)
    assert resp.cost == Decimal("1.05")
    assert not buy_route.called
    orders = await store.recent_orders(strategy="g", limit=10)
    assert orders[0]["status"] == "dryrun"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_broker.py -v
```
Expected: FAIL — live.py / dryrun.py 缺失

- [ ] **Step 4: 写 `quant/thccb_quant/broker/live.py`**

```python
"""LiveBroker: 真实下单 + 风控 + 幂等。spec §5.3。"""
from datetime import datetime, timezone
from decimal import Decimal

from thccb_quant.broker.base import Broker
from thccb_quant.broker.risk import RiskGuard
from thccb_quant.client.rest import RestClient, OrderResponse
from thccb_quant.errors import BusinessError, RiskRejected
from thccb_quant.state.store import Store


class LiveBroker(Broker):
    def __init__(self, *, rest: RestClient, risk: RiskGuard, store: Store):
        self._rest = rest
        self._risk = risk
        self._store = store

    async def _common(
        self, *, strategy: str, outcome_id: int, shares: Decimal,
        side: str, max_slippage_bps: int,
    ) -> OrderResponse:
        quote = await self._rest.quote(outcome_id=outcome_id, shares=shares, side=side)
        cost = quote.net

        today = datetime.now(timezone.utc).date().isoformat()
        stats = await self._store.get_daily_stats(today)
        self._risk.check(
            outcome_id=outcome_id, side=side, cost=cost,
            max_slippage_bps=max_slippage_bps,
            turnover_today=Decimal(stats["gross_turnover"]),
            net_pnl_today=Decimal(stats["net_pnl"]),
        )

        if await self._store.has_recent_duplicate(
            strategy, outcome_id, side, shares,
            within_sec=5, statuses=("success", "dryrun"),
        ):
            raise RiskRejected("duplicate within 5s")

        try:
            if side == "buy":
                resp = await self._rest.buy(
                    outcome_id=outcome_id, shares=shares,
                    max_slippage_bps=max_slippage_bps,
                )
            else:
                resp = await self._rest.sell(
                    outcome_id=outcome_id, shares=shares,
                    max_slippage_bps=max_slippage_bps,
                )
        except BusinessError as e:
            await self._store.log_order(
                strategy=strategy, outcome_id=outcome_id, side=side,
                shares=shares, status="failed", error=str(e),
            )
            raise

        await self._store.log_order(
            strategy=strategy, outcome_id=outcome_id, side=side,
            shares=shares, price=quote.avg_price, cost=resp.cost,
            status="success",
        )
        await self._store.add_turnover(resp.cost.copy_abs())
        self._risk.mark_order(outcome_id=outcome_id)
        return resp

    async def buy(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="buy", max_slippage_bps=max_slippage_bps,
        )

    async def sell(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="sell", max_slippage_bps=max_slippage_bps,
        )
```

- [ ] **Step 5: 写 `quant/thccb_quant/broker/dryrun.py`**

```python
"""DryRunBroker: 走通所有路径但不真下单。spec §5.4。"""
from datetime import datetime, timezone
from decimal import Decimal

from thccb_quant.broker.base import Broker
from thccb_quant.broker.risk import RiskGuard
from thccb_quant.client.rest import RestClient, OrderResponse
from thccb_quant.errors import RiskRejected
from thccb_quant.state.store import Store


class DryRunBroker(Broker):
    def __init__(self, *, rest: RestClient, risk: RiskGuard, store: Store):
        self._rest = rest
        self._risk = risk
        self._store = store

    async def _common(
        self, *, strategy: str, outcome_id: int, shares: Decimal,
        side: str, max_slippage_bps: int,
    ) -> OrderResponse:
        quote = await self._rest.quote(outcome_id=outcome_id, shares=shares, side=side)
        cost = quote.net

        today = datetime.now(timezone.utc).date().isoformat()
        stats = await self._store.get_daily_stats(today)
        self._risk.check(
            outcome_id=outcome_id, side=side, cost=cost,
            max_slippage_bps=max_slippage_bps,
            turnover_today=Decimal(stats["gross_turnover"]),
            net_pnl_today=Decimal(stats["net_pnl"]),
        )

        if await self._store.has_recent_duplicate(
            strategy, outcome_id, side, shares,
            within_sec=5, statuses=("success", "dryrun"),
        ):
            raise RiskRejected("duplicate within 5s")

        await self._store.log_order(
            strategy=strategy, outcome_id=outcome_id, side=side,
            shares=shares, price=quote.avg_price, cost=cost,
            status="dryrun",
        )
        self._risk.mark_order(outcome_id=outcome_id)
        return OrderResponse(
            shares=shares, cost=cost, new_cash=Decimal("0"),
            message="dryrun",
        )

    async def buy(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="buy", max_slippage_bps=max_slippage_bps,
        )

    async def sell(self, *, strategy, outcome_id, shares, max_slippage_bps):
        return await self._common(
            strategy=strategy, outcome_id=outcome_id, shares=shares,
            side="sell", max_slippage_bps=max_slippage_bps,
        )
```

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest tests/test_broker.py -v
```
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
cd ..
git add quant/thccb_quant/broker/base.py quant/thccb_quant/broker/live.py \
  quant/thccb_quant/broker/dryrun.py quant/tests/test_broker.py
git commit -m "feat(quant): Broker（Live + DryRun）+ 风控/幂等/落账"
```

---

## Task 10: Strategy 基础（ABC + Registry）

**Files:**
- Create: `quant/thccb_quant/strategy/__init__.py`
- Create: `quant/thccb_quant/strategy/base.py`
- Create: `quant/thccb_quant/strategy/registry.py`

- [ ] **Step 1: 新建空 `quant/thccb_quant/strategy/__init__.py`**

```bash
: > quant/thccb_quant/strategy/__init__.py
```

- [ ] **Step 2: 写 `quant/thccb_quant/strategy/base.py`**

```python
"""Strategy ABC + StrategyContext。spec §6.1。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog

from thccb_quant.broker.base import Broker
from thccb_quant.client.rest import RestClient
from thccb_quant.state.store import Store


@dataclass
class StrategyContext:
    rest: RestClient
    broker: Broker
    store: Store
    logger: structlog.BoundLogger
    config: dict


class Strategy(ABC):
    name: str
    tick_interval_sec: int = 30

    def __init__(self, name: str, config: dict):
        self.name = name
        self._config = config

    @abstractmethod
    async def setup(self, ctx: StrategyContext) -> None: ...

    @abstractmethod
    async def tick(self) -> None: ...

    async def on_sse_event(self, event: Any) -> None:
        """默认 no-op，需要实时反应的策略覆盖。"""

    async def teardown(self) -> None:
        """优雅停机钩子，默认 no-op。"""
```

- [ ] **Step 3: 写 `quant/thccb_quant/strategy/registry.py`**

```python
"""策略注册表：config.yaml 里 type 字段映射到类。"""
from typing import Dict, Type

from thccb_quant.strategy.base import Strategy

STRATEGY_REGISTRY: Dict[str, Type[Strategy]] = {}


def register(type_name: str):
    def deco(cls: Type[Strategy]) -> Type[Strategy]:
        STRATEGY_REGISTRY[type_name] = cls
        return cls
    return deco


def get_strategy_class(type_name: str) -> Type[Strategy]:
    if type_name not in STRATEGY_REGISTRY:
        raise KeyError(f"unknown strategy type: {type_name}")
    return STRATEGY_REGISTRY[type_name]
```

- [ ] **Step 4: 验证 import**

```bash
cd quant/ && source .venv/bin/activate
python -c "from thccb_quant.strategy.base import Strategy, StrategyContext; from thccb_quant.strategy.registry import STRATEGY_REGISTRY, register; print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/strategy/__init__.py quant/thccb_quant/strategy/base.py \
  quant/thccb_quant/strategy/registry.py
git commit -m "feat(quant): Strategy ABC + 注册表"
```

---

## Task 11: DcaStrategy

**Files:**
- Create: `quant/thccb_quant/strategy/dca.py`
- Create: `quant/tests/test_strategy_dca.py`

- [ ] **Step 1: 写失败测试 `quant/tests/test_strategy_dca.py`**

```python
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.strategy.dca import DcaStrategy
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.client.rest import OrderResponse, QuoteResponse
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def _make_ctx(store: Store, broker_buy_resp=None) -> StrategyContext:
    rest = MagicMock()
    rest.quote = AsyncMock(return_value=QuoteResponse(
        outcome_id=1, side="buy", shares=Decimal("10"),
        avg_price=Decimal("0.5"), gross=Decimal("5"), fee=Decimal("0"),
        net=Decimal("5"),
    ))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=broker_buy_resp or OrderResponse(
        shares=Decimal("10"), cost=Decimal("5"), new_cash=Decimal("495"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"), config={},
    )


async def test_dca_buys_at_interval(store: Store):
    cfg = {
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 6, "total_budget_cny": 200,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count == 1


async def test_dca_respects_interval(store: Store):
    cfg = {
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 6, "total_budget_cny": 200,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    await s.tick()  # 立刻第二次，应该跳过
    assert ctx.broker.buy.call_count == 1


async def test_dca_respects_total_budget(store: Store):
    cfg = {
        "outcome_id": 1, "cny_per_buy": 5.0,
        "interval_hours": 0,  # 间隔为 0，纯靠预算限制
        "total_budget_cny": 10,
    }
    s = DcaStrategy(name="d", config=cfg)
    ctx = await _make_ctx(store)
    await s.setup(ctx)
    await s.tick()
    await s.tick()
    await s.tick()  # 第三次应该被预算挡住
    assert ctx.broker.buy.call_count == 2
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_strategy_dca.py -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 写实现 `quant/thccb_quant/strategy/dca.py`**

```python
"""DCA 定投策略。spec §6.2。

每 interval_hours 用 quote 估算需要多少 shares 才能花到 cny_per_buy，
然后下买单。total_budget_cny 是该策略总花销上限。
"""
import time
from decimal import Decimal

from thccb_quant.errors import RiskRejected, BusinessError, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


@register("dca")
class DcaStrategy(Strategy):
    tick_interval_sec = 60  # 主循环每分钟来一次，自己判断要不要下单

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._outcome_id: int = int(config["outcome_id"])
        self._cny_per_buy = Decimal(str(config["cny_per_buy"]))
        self._interval_sec = float(config["interval_hours"]) * 3600
        self._total_budget = Decimal(str(config["total_budget_cny"]))
        self._spent = Decimal("0")
        self._last_buy_ts: float = 0.0
        self._ctx: StrategyContext | None = None

    async def setup(self, ctx: StrategyContext) -> None:
        self._ctx = ctx
        # 从历史 orders 还原 _spent
        rows = await ctx.store.recent_orders(strategy=self.name, limit=10000)
        for r in rows:
            if r["status"] == "success" and r["side"] == "buy":
                self._spent += Decimal(r["cost"])

    async def tick(self) -> None:
        assert self._ctx is not None
        now = time.monotonic()
        if self._last_buy_ts and (now - self._last_buy_ts) < self._interval_sec:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason="interval not reached",
            )
            return
        if self._spent + self._cny_per_buy > self._total_budget:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason="total budget exhausted",
            )
            return

        # 估算需要多少 shares 才大致花到 cny_per_buy：先 quote 1 share 拿 avg_price
        probe = await self._ctx.rest.quote(
            outcome_id=self._outcome_id, shares=Decimal("1"), side="buy",
        )
        if probe.avg_price <= 0:
            return
        target_shares = (self._cny_per_buy / probe.avg_price).quantize(Decimal("0.000001"))

        try:
            resp = await self._ctx.broker.buy(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=target_shares,
                max_slippage_bps=int(self._ctx.config.get("max_slippage_bps", 300)),
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"order failed: {e}",
            )
            return

        self._spent += resp.cost
        self._last_buy_ts = now
        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="buy", reason="dca tick",
            snapshot={"cost": str(resp.cost), "shares": str(resp.shares),
                      "spent_total": str(self._spent)},
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_strategy_dca.py -v
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/strategy/dca.py quant/tests/test_strategy_dca.py
git commit -m "feat(quant): DCA 定投策略"
```

---

## Task 12: GridStrategy

**Files:**
- Create: `quant/thccb_quant/strategy/grid.py`
- Create: `quant/tests/test_strategy_grid.py`

策略说明：把 `[price_low, price_high]` 分成 `grid_count` 个格点。每次
tick 调 `get_market` 拿当前价 `p`；若 `p` 低于某个尚未持仓的格点 → 买
`shares_per_grid`；若 `p` 高于某个已持仓的格点 → 卖 `shares_per_grid`。
格点状态保存在 store decisions 表里（重启时从 orders 表 replay 还原）。

- [ ] **Step 1: 写失败测试 `quant/tests/test_strategy_grid.py`**

```python
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.strategy.grid import GridStrategy
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.client.rest import (
    MarketDetail, OrderResponse, OutcomeDetail, QuoteResponse,
)
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


def _market(price: float) -> MarketDetail:
    return MarketDetail(
        id=1, title="t", status="trading", liquidity_b=100.0,
        outcomes=[
            OutcomeDetail(id=1, label="yes", total_shares=Decimal("10"),
                          current_price=Decimal(str(price))),
            OutcomeDetail(id=2, label="no", total_shares=Decimal("5"),
                          current_price=Decimal(str(1 - price))),
        ],
    )


async def _make_ctx(store: Store, current_price: float) -> StrategyContext:
    rest = MagicMock()
    rest.get_market = AsyncMock(return_value=_market(current_price))
    rest.quote = AsyncMock(return_value=QuoteResponse(
        outcome_id=1, side="buy", shares=Decimal("2"),
        avg_price=Decimal(str(current_price)),
        gross=Decimal(str(current_price * 2)), fee=Decimal("0"),
        net=Decimal(str(current_price * 2)),
    ))
    broker = MagicMock()
    broker.buy = AsyncMock(return_value=OrderResponse(
        shares=Decimal("2"), cost=Decimal(str(current_price * 2)),
        new_cash=Decimal("500"),
    ))
    broker.sell = AsyncMock(return_value=OrderResponse(
        shares=Decimal("2"), cost=Decimal(str(-current_price * 2)),
        new_cash=Decimal("510"),
    ))
    return StrategyContext(
        rest=rest, broker=broker, store=store,
        logger=structlog.get_logger("test"), config={},
    )


def _cfg():
    return {
        "market_id": 1, "outcome_id": 1,
        "price_low": 0.30, "price_high": 0.60,
        "grid_count": 4, "shares_per_grid": 2.0,
    }


async def test_grid_buys_when_below_grid_point(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count >= 1


async def test_grid_no_action_above_high(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.70)
    await s.setup(ctx)
    await s.tick()
    assert ctx.broker.buy.call_count == 0


async def test_grid_no_double_buy_same_grid(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    first = ctx.broker.buy.call_count
    await s.tick()  # 价格没变，同一格点不应再触发
    assert ctx.broker.buy.call_count == first


async def test_grid_sells_after_rebound(store: Store):
    s = GridStrategy(name="g", config=_cfg())
    ctx = await _make_ctx(store, current_price=0.32)
    await s.setup(ctx)
    await s.tick()
    # 模拟价格回升到 0.55，应触发卖出
    ctx.rest.get_market = AsyncMock(return_value=_market(0.55))
    await s.tick()
    assert ctx.broker.sell.call_count >= 1
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd quant/ && source .venv/bin/activate
pytest tests/test_strategy_grid.py -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 写实现 `quant/thccb_quant/strategy/grid.py`**

```python
"""网格策略。spec §6.2。

把 [price_low, price_high] 分成 grid_count 个格点。
- 价格 ≤ 某未持仓格点 → 买 shares_per_grid，标记该格点已持仓
- 价格 ≥ 某已持仓格点 → 卖 shares_per_grid，标记空仓

格点状态保存在内存（重启从 orders 表 replay）。
"""
from decimal import Decimal

from thccb_quant.errors import BusinessError, RiskRejected, TransientError
from thccb_quant.strategy.base import Strategy, StrategyContext
from thccb_quant.strategy.registry import register


@register("grid")
class GridStrategy(Strategy):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._market_id = int(config["market_id"])
        self._outcome_id = int(config["outcome_id"])
        self._low = float(config["price_low"])
        self._high = float(config["price_high"])
        self._count = int(config["grid_count"])
        self._shares_per_grid = Decimal(str(config["shares_per_grid"]))
        self.tick_interval_sec = int(config.get("tick_interval_sec", 30))

        # 等距格点（包含端点）：count+1 个点
        step = (self._high - self._low) / self._count
        self._grids = [self._low + i * step for i in range(self._count + 1)]
        # 每个格点：True=该格已持有一份等待卖出
        self._held: list[bool] = [False] * len(self._grids)
        self._ctx: StrategyContext | None = None

    async def setup(self, ctx: StrategyContext) -> None:
        self._ctx = ctx
        # replay：根据历史 success 单还原 _held
        rows = await ctx.store.recent_orders(strategy=self.name, limit=10000)
        # 按时间正序处理（recent_orders 是 DESC）
        for r in reversed(rows):
            if r["status"] != "success" or int(r["outcome_id"]) != self._outcome_id:
                continue
            price = float(r["price"]) if r["price"] else None
            if price is None:
                continue
            idx = self._nearest_grid(price)
            if r["side"] == "buy":
                self._held[idx] = True
            elif r["side"] == "sell":
                self._held[idx] = False

    def _nearest_grid(self, price: float) -> int:
        return min(range(len(self._grids)), key=lambda i: abs(self._grids[i] - price))

    async def tick(self) -> None:
        assert self._ctx is not None
        market = await self._ctx.rest.get_market(self._market_id)
        outcome = next(
            (o for o in market.outcomes if o.id == self._outcome_id), None
        )
        if outcome is None:
            return
        price = float(outcome.current_price)

        if price > self._high or price < self._low:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"price {price} outside [{self._low}, {self._high}]",
            )
            return

        # 找到该价格下方最近的未持仓格点 → 买
        for i, gp in enumerate(self._grids):
            if price <= gp and not self._held[i]:
                await self._try_buy(i, gp)
                return
        # 找到该价格上方最近的已持仓格点 → 卖
        for i in range(len(self._grids) - 1, -1, -1):
            if price >= self._grids[i] and self._held[i]:
                await self._try_sell(i, self._grids[i])
                return

        await self._ctx.store.log_decision(
            strategy=self.name, outcome_id=self._outcome_id,
            action="skip", reason="no grid crossed",
        )

    async def _try_buy(self, grid_idx: int, grid_price: float) -> None:
        assert self._ctx is not None
        try:
            await self._ctx.broker.buy(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=self._shares_per_grid,
                max_slippage_bps=int(self._ctx.config.get("max_slippage_bps", 300)),
            )
            self._held[grid_idx] = True
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="buy", reason=f"crossed grid {grid_price:.4f}",
                snapshot={"grid_idx": grid_idx},
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"buy failed: {e}",
            )

    async def _try_sell(self, grid_idx: int, grid_price: float) -> None:
        assert self._ctx is not None
        try:
            await self._ctx.broker.sell(
                strategy=self.name, outcome_id=self._outcome_id,
                shares=self._shares_per_grid,
                max_slippage_bps=int(self._ctx.config.get("max_slippage_bps", 300)),
            )
            self._held[grid_idx] = False
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="sell", reason=f"crossed grid {grid_price:.4f}",
                snapshot={"grid_idx": grid_idx},
            )
        except (RiskRejected, BusinessError, TransientError) as e:
            await self._ctx.store.log_decision(
                strategy=self.name, outcome_id=self._outcome_id,
                action="skip", reason=f"sell failed: {e}",
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_strategy_grid.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/strategy/grid.py quant/tests/test_strategy_grid.py
git commit -m "feat(quant): 网格策略 + 格点状态 replay"
```

---

## Task 13: SseClient skeleton（不启用）

**Files:**
- Create: `quant/thccb_quant/client/sse.py`

spec §4.3 说起步不启用 SSE，但要预留接口供未来策略覆盖 `on_sse_event`。

- [ ] **Step 1: 写 skeleton `quant/thccb_quant/client/sse.py`**

```python
"""SseClient: spec §4.3。起步不启用，预留给未来动量类策略。

实现要点（实际启用时）：
- httpx.AsyncClient.stream("GET", f"/api/v1/stream/market/{market_id}")
- aiter_lines 按 "data:" 前缀解析 JSON
- 25s 心跳监测，连续 2 个 ping 间隔无事件 → 重连
- 后端 1 小时强断，到点前主动重连
- 重连后用 last_seq 锚点请求增量（如后端支持）
"""
from typing import AsyncIterator, Any


class SseClient:
    """占位：当前不启用。"""

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "SseClient skeleton — implement when first SSE-driven strategy lands"
        )

    async def subscribe(self, market_id: int) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError
        yield  # pragma: no cover
```

- [ ] **Step 2: 验证不破坏 import**

```bash
cd quant/ && source .venv/bin/activate
python -c "from thccb_quant.client.sse import SseClient; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd ..
git add quant/thccb_quant/client/sse.py
git commit -m "feat(quant): SseClient skeleton 预留接口"
```

---

## Task 14: Trader 主入口（asyncio loop + signal + kill switch）

**Files:**
- Create: `quant/thccb_quant/trader.py`
- Create: `quant/thccb_quant/__main__.py`

- [ ] **Step 1: 写 `quant/thccb_quant/trader.py`**

```python
"""Trader 主入口：asyncio loop + 信号 + kill switch + 策略调度。spec §2/§5.5/§11。"""
import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import structlog
import yaml
from dotenv import dotenv_values

# 触发策略注册副作用
import thccb_quant.strategy.dca  # noqa: F401
import thccb_quant.strategy.grid  # noqa: F401
from thccb_quant.broker.dryrun import DryRunBroker
from thccb_quant.broker.live import LiveBroker
from thccb_quant.broker.risk import RiskConfig, RiskGuard
from thccb_quant.client.auth import TokenManager
from thccb_quant.client.rest import RestClient
from thccb_quant.errors import FatalAuthError, StrategyError
from thccb_quant.logging_setup import setup_logging, get_logger
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import StrategyContext
from thccb_quant.strategy.registry import get_strategy_class

QUANT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = QUANT_ROOT / ".env"
CONFIG_PATH = QUANT_ROOT / "config.yaml"
STATE_DIR = QUANT_ROOT / "state"
LOG_DIR = QUANT_ROOT / "logs"
KILL_FILE = STATE_DIR / "KILL"


_stop_event = asyncio.Event()


def _install_signal_handlers():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop_event.set)


async def _kill_switch_watcher(logger):
    while not _stop_event.is_set():
        if KILL_FILE.exists():
            logger.warning("kill_switch_detected")
            _stop_event.set()
            return
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def _run_strategy(strategy, ctx: StrategyContext, logger):
    try:
        await strategy.setup(ctx)
        while not _stop_event.is_set():
            try:
                await strategy.tick()
            except Exception as e:
                logger.exception("strategy_tick_failed", error=str(e))
                # 单 tick 失败不停整个策略，继续下一轮
            try:
                await asyncio.wait_for(
                    _stop_event.wait(), timeout=strategy.tick_interval_sec
                )
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await strategy.teardown()
        except Exception:
            logger.exception("strategy_teardown_failed")


async def _refresh_token_warner(token_mgr: TokenManager, logger):
    """Refresh token 到期前 1 天打 ERROR 提示手动重登。"""
    while not _stop_event.is_set():
        exp = token_mgr.refresh_exp_ts
        days_left = (exp - datetime.now(timezone.utc).timestamp()) / 86400
        if days_left < 1.0:
            logger.error(
                "refresh_token_expiring_soon",
                days_left=days_left,
                msg="please re-login in browser and update .env",
            )
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found, copy from config.example.yaml"
        )
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def _load_env() -> dict:
    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f"{ENV_PATH} not found, copy from .env.example and fill tokens"
        )
    return dotenv_values(ENV_PATH)


async def main_async(dry_run: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_DIR, "system")
    logger = get_logger("trader")

    if KILL_FILE.exists():
        logger.error("startup_blocked_by_kill_switch", path=str(KILL_FILE))
        return 1

    try:
        env = _load_env()
        config = _load_config()
    except FileNotFoundError as e:
        logger.error("missing_file", error=str(e))
        return 1

    try:
        import setproctitle
        setproctitle.setproctitle("thccb-quant" + (" [dryrun]" if dry_run else ""))
    except Exception:
        pass

    base_url = env["THCCB_BASE_URL"]

    raw_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
    api_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    token_mgr = TokenManager(
        base_url=base_url,
        access_token=env["THCCB_ACCESS_TOKEN"],
        refresh_token=env["THCCB_REFRESH_TOKEN"],
        env_path=ENV_PATH,
        raw_client=raw_client,
    )

    risk = RiskGuard(
        RiskConfig(
            single_order_cap_cny=Decimal(str(config["risk"]["single_order_cap_cny"])),
            daily_loss_cap_cny=Decimal(str(config["risk"]["daily_loss_cap_cny"])),
            daily_turnover_cap_cny=Decimal(str(config["risk"]["daily_turnover_cap_cny"])),
            max_slippage_bps=int(config["risk"]["max_slippage_bps"]),
            min_seconds_between_orders=int(config["risk"]["min_seconds_between_orders"]),
        ),
        STATE_DIR,
    )

    rest = RestClient(
        client=api_client,
        token_manager=token_mgr,
        rate_limit_per_sec=float(config["client"]["rate_limit_per_sec"]),
    )

    store = await Store.open(STATE_DIR / "quant.db")

    broker = (
        DryRunBroker(rest=rest, risk=risk, store=store)
        if dry_run else
        LiveBroker(rest=rest, risk=risk, store=store)
    )

    logger.info("startup",
                base_url=base_url, dry_run=dry_run,
                strategies_count=len(config["strategies"]))

    _install_signal_handlers()

    tasks = [
        asyncio.create_task(_kill_switch_watcher(logger)),
        asyncio.create_task(_refresh_token_warner(token_mgr, logger)),
    ]

    # 实例化策略并起 task
    for s_cfg in config["strategies"]:
        if not s_cfg.get("enabled", False):
            continue
        s_type = s_cfg["type"]
        cls = get_strategy_class(s_type)
        strat = cls(name=s_cfg["name"], config=s_cfg)
        ctx = StrategyContext(
            rest=rest, broker=broker, store=store,
            logger=get_logger(s_cfg["name"], strategy=s_cfg["name"]),
            config={**config["risk"], **s_cfg},
        )
        tasks.append(asyncio.create_task(
            _run_strategy(strat, ctx, get_logger(s_cfg["name"], strategy=s_cfg["name"]))
        ))

    try:
        await _stop_event.wait()
    except FatalAuthError:
        logger.error("fatal_auth_error_stopping")

    logger.info("shutting_down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await store.close()
    await api_client.aclose()
    await raw_client.aclose()
    logger.info("stopped_clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="thccb-quant")
    parser.add_argument("--dry-run", action="store_true",
                        help="走通所有路径但不真下单（DryRunBroker）")
    args = parser.parse_args()
    return asyncio.run(main_async(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 写 `quant/thccb_quant/__main__.py`**

```python
"""支持 python -m thccb_quant。"""
from thccb_quant.trader import main
import sys

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Smoke-import 检查**

```bash
cd quant/ && source .venv/bin/activate
python -c "from thccb_quant.trader import main_async; print('ok')"
python -m thccb_quant --help
```
Expected: 第一行 `ok`，第二行 argparse 输出 `--dry-run` 选项

- [ ] **Step 4: 全套测试回归**

```bash
pytest -x
```
Expected: 所有先前任务的测试全过

- [ ] **Step 5: Commit**

```bash
cd ..
git add quant/thccb_quant/trader.py quant/thccb_quant/__main__.py
git commit -m "feat(quant): 主入口 trader.py + asyncio loop + kill switch + 信号"
```

---

## Task 15: run.sh + README + 启动手册

**Files:**
- Create: `quant/run.sh`
- Create: `quant/README.md`

- [ ] **Step 1: 写 `quant/run.sh`**

```bash
#!/usr/bin/env bash
# 看门狗：崩了 30s 后重拉；exit 0（kill switch / FatalAuth）不重拉
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

while true; do
  echo "[$(date '+%F %T')] starting trader"
  python -m thccb_quant
  EXIT=$?
  if [ $EXIT -eq 0 ]; then
    echo "[$(date '+%F %T')] clean exit, watchdog quitting"
    break
  fi
  echo "[$(date '+%F %T')] crashed exit=$EXIT, restart in 30s"
  sleep 30
done
```

```bash
chmod +x quant/run.sh
```

- [ ] **Step 2: 写 `quant/README.md`**

````markdown
# thccb-quant

TouhouCCB 量化交易脚本（实盘小额）。spec：
`docs/superpowers/specs/2026-05-17-quant-trader-design.md`。

## 起步

```bash
cd quant/
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

cp config.example.yaml config.yaml && $EDITOR config.yaml
cp .env.example .env && $EDITOR .env   # 填浏览器抓的 token

pytest -x                              # 全过才能跑
python -m thccb_quant --dry-run        # 演练 24h
python -m thccb_quant                  # 实盘（或下面的 run.sh 包装）
```

## Token 获取

浏览器登 thccb.com → F12 → Network → 找任意 `/api/v1/*` 请求 → 复制
`Authorization: Bearer <access>` 的 access token；refresh token 在
登录回调响应里。填到 `.env`：

```
THCCB_BASE_URL=http://127.0.0.1:8004
THCCB_ACCESS_TOKEN=eyJ...
THCCB_REFRESH_TOKEN=eyJ...
```

Refresh token 寿命 7 天，**到期需手动重新从浏览器拿**（后端
`/auth/refresh` 不轮换 refresh token）。日志会在到期前 1 天打
`refresh_token_expiring_soon` ERROR。

## 长跑（公用机无 sudo）

```bash
tmux new -d -s quant 'bash $(pwd)/run.sh'
tmux attach -t quant    # 看，Ctrl-b d 退出不影响进程
```

开机自启（用户 crontab）：

```cron
@reboot tmux new -d -s quant 'bash /data/sunyunbo/www/TouhouCCB/quant/run.sh' >> /data/sunyunbo/www/TouhouCCB/quant/logs/cron.log 2>&1
```

## Kill Switch

```bash
touch quant/state/KILL     # 优雅停（看门狗也退）
# 硬停：
tmux kill-session -t quant
```

启动时若 `state/KILL` 存在则拒绝启动，要手工 `rm` 才能重跑。

## 加新策略

1. 临时停 watchdog：`touch state/KILL`
2. 前台 dry-run：`python -m thccb_quant --dry-run` 跑 24h
3. 看 `logs/decisions.jsonl` 和 `logs/system.jsonl` 没异常
4. 改 `config.yaml` 把新策略 `enabled: true`
5. `rm state/KILL` 重启 watchdog

写新策略类：继承 `thccb_quant.strategy.base.Strategy`，用
`@register("your_type")` 注册，在 `trader.py` 顶部加 import 触发注册副
作用。

## 风控参数

见 `config.yaml` 的 `risk:` 段。默认值（spec §5.1）：

| 参数 | 默认 |
|---|---|
| single_order_cap_cny | 50 |
| daily_loss_cap_cny | 100 |
| daily_turnover_cap_cny | 2000 |
| max_slippage_bps | 300 |
| min_seconds_between_orders | 3 |

## 日志位置

- `logs/orders.jsonl` —— 所有下单尝试（成功/失败）
- `logs/decisions.jsonl` —— 所有策略决策（含未下单）
- `logs/system.jsonl` —— token 续期、kill switch、风控告警

复盘示例：

```bash
jq 'select(.strategy=="dca_x" and .status=="success") | .cost' logs/orders.jsonl
```
````

- [ ] **Step 3: 验证 run.sh 语法**

```bash
bash -n quant/run.sh && echo "syntax ok"
```
Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add quant/run.sh quant/README.md
git commit -m "docs(quant): run.sh 看门狗 + README 启动手册"
```

---

## Task 16: 端到端 Smoke 测试（手工）

**Files:** 无新增

- [ ] **Step 1: 跑全量 pytest 回归**

```bash
cd quant/ && source .venv/bin/activate
pytest -x -v
```
Expected: 所有测试全过（共 ~35 个）

- [ ] **Step 2: 跑 dry-run 30 秒看是否能启动并优雅停**

```bash
# 准备最小 config（单笔 1 元、日累计 5 元，新增一个 dca 策略 enabled=true）
cp config.example.yaml config.yaml
$EDITOR config.yaml   # 改 risk 上限到 1/5，dca 策略 enabled=true 指向真实 outcome_id
$EDITOR .env          # 填真实 token

# 跑 dry-run
timeout 30 python -m thccb_quant --dry-run || true

# 看输出
tail -20 logs/system.jsonl
tail -20 logs/orders.jsonl   # 应有 status=dryrun 行
```
Expected: system.jsonl 有 `startup` / `stopped_clean`；orders.jsonl 有 dryrun 行

- [ ] **Step 3: 实盘 smoke（最小金额、跑 5 分钟）**

```bash
# 配置已经是最小（单笔 1 元）
python -m thccb_quant &
PID=$!
sleep 60
touch state/KILL
wait $PID
```
Expected: 进程在 5s 内 clean exit

- [ ] **Step 4: 核对真实下单**

打开 thccb.com 看持仓页是否多了对应 outcome 的小仓位；手动平仓清理。

- [ ] **Step 5: 切回正常 config 并 commit**

```bash
$EDITOR config.yaml   # 把 risk 上限改回 50/100/2000，关掉 smoke 策略 enabled=false
rm state/KILL          # 后续可用 tmux 长跑
```

(此步不产生 commit，因 `config.yaml` 在 gitignore 中。)

---

## Spec Coverage Check

| Spec 章节 | 实现任务 |
|---|---|
| §2 整体架构 | Task 14 trader.py |
| §3 目录结构 + gitignore | Task 1 |
| §4.1 TokenManager | Task 6 |
| §4.2 RestClient | Task 7 |
| §4.3 SseClient skeleton | Task 13 |
| §5.1 RiskConfig | Task 8 |
| §5.2 Risk check | Task 8 |
| §5.3 LiveBroker | Task 9 |
| §5.4 DryRunBroker + 流程 | Task 9 / Task 15 README |
| §5.5 Kill switch | Task 14（watcher + 启动检测） |
| §6.1 Strategy ABC | Task 10 |
| §6.2 Grid + DCA | Task 11 / Task 12 |
| §6.3 LMSR 本地复现 | Task 3 |
| §7 State 三表 | Task 5 |
| §8 异常分类 | Task 2 |
| §9 structlog 日志 | Task 4 |
| §10 测试套件 | Task 3/5/6/7/8/9/11/12 |
| §11 部署 | Task 15 + Task 16 |
