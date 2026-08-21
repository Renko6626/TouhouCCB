# 单写者内存状态机 · 阶段 4 实施计划（历史包 + nginx）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec § 7：每 outcome 内存多分辨率环形缓冲（ring）→ `/history/` 不可变分段端点（三道封存防线）→ SSE snapshot 携带尾巴 → nginx `proxy_cache` → 前端图表改为「封存段一次加载 + snapshot 尾巴 + tick 帧续写」，图表回源次数与在线人数解耦。

**Architecture:** 新增 `HistoryRing`（每 outcome 一份、4 档 OHLCV 桶，writer commit 后与 flusher 同源喂入）；`MarketState.rings` 持有并随 `_load_one`/`reload_state` 从 `OutcomeCandle` 镜像初始化。新路由 `/history/o/{outcome_id}/{interval}/{segment_epoch}.json` 挂在 `/api/v1` 之外（绕开 no-store 中间件），只吐已封存段并打 `immutable`：进行中段 404（防线 1）、ring 窗口内一律从 ring 供数（防线 2）、超窗段先验 flusher 高水位再查 DB + 进程内 LRU（防线 3）。nginx 对 `/history/` 叠 `proxy_cache`。前端新增列式解码 + `useCandleHistory` 组装层，图表数据源切换，失败回退老 `/api/v1/chart/candles`（老端点保留不动）。

**Tech Stack:** FastAPI + SQLAlchemy async（Postgres 生产 / SQLite 测试）+ asyncio；前端 Vue 3 + vitest + lightweight-charts。无新运行时依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-single-writer-design.md`（本计划实现其 § 7 全部 + § 8 阶段 4）

## Global Constraints

- 生产站在跑；全部工作在分支 `perf/2026-08-21-single-writer` 上，**不 push**（push 触发自动部署，完成后交用户决定）
- **前置依赖**：阶段 2（tick 帧 + `useMarketRealtime` 已吃 tick）与阶段 3（vitest 基建、`npm run test:unit`）已合入本分支——本计划的前端测试直接用 vitest，图表续写直接吃 tick 帧
- **单进程**是架构前提；ring/LRU 全部进程内，多 worker 会分裂（`Dockerfile --workers 1` 不变）
- **immutable 承诺是硬约束**：`/history/` 一旦对某段发过 200，该段内容永不可变——错一个 200 会被 nginx 钉 30 天、浏览器钉 1 年。三道防线（spec § 7.2）逐条要有测试
- ring 的写入只发生在 writer consumer（commit 之后、与 candle flusher 同一份 `candle_rows`）；`/history/` 与 snapshot 读 ring 与 writer 同在一个 event loop，读取期间无 await 打断即天然一致，**不加锁**
- 资金/份额 Decimal 6 位、价格 8 位（`quantize_cost` / `quantize_price`）；列式编码价格 = `round(float(price) * 1e8)` 整数定点
- 不改 schema（无新表新列）；`market_locks.py` 一行不动
- `deploy/nginx.conf` 是 CLAUDE.md 红线文件，spec § 7.6 已获用户明确授权修改；**nginx reload 与缓存目录创建是用户手动步骤**，计划内只改配置文件并输出部署清单
- 后端验证（每 task commit 前）：`python -m py_compile $(find app -name '*.py')` + `python -c "import app.main"` + 该 task pytest；收尾跑全量 `python -m pytest -q`
- 前端验证：`npm run type-check` + `npm run lint` + `npm run test:unit`；UI 改动浏览器实测，起不来则日志写「未实测 UI」
- commit 风格：`feat:/fix:/test:/perf:` + 中文；按文件 `git add <path>`；后端命令在 `backend/`、前端命令在 `thccb-frontend/` 下执行

## 分段与编码契约（全计划共用，先读这里）

| 档 | step | ring 桶数 | ring 窗口 | 段长（封存粒度） |
|---|---|---|---|---|
| 10s | 10 | 360 | 1 h | 600 s（10 min） |
| 1m | 60 | 1440 | 24 h | 3600 s（1 h） |
| 15m | 900 | 672 | 7 d | 86400 s（1 d） |
| 1h | 3600 | 2160 | 90 d | 604800 s（7 d） |

列式编码（段与尾巴同一格式；稀疏——只含有数据的桶）：

```json
{
  "t0": 1755734400,          // 段/尾巴起点 epoch（段：对齐段长；尾巴：最后封存边界）
  "step": 60,                // 桶宽秒
  "n_buckets": 60,           // 满段应有桶数（段长/step；尾巴为边界→now 的桶数上限）
  "t": [0, 3, 4],            // 有数据桶的相对序号：epoch = t0 + t[i]*step
  "o": [55000000, ...],      // open，8 位定点整数 = round(price*1e8)
  "h": [...], "l": [...], "c": [...],
  "v": [12.5, ...],          // volume_shares，float(quantize_cost(v))
  "trades": [3, ...]         // n_trades
}
```

---

### Task 1: HistoryRing 模块（4 档桶 + 合并 + 列式编码 + 段/尾巴读取）

**Files:**
- Create: `backend/app/services/history_ring.py`
- Test: `backend/tests/test_history_ring.py`

**Interfaces:**
- Consumes: `candle_writer.compute_candle_rows` 的行 dict 格式（`outcome_id/interval/bucket_start/open_price/high_price/low_price/close_price/volume_shares/n_trades/updated_at`）
- Produces（后续 task 依赖的确切签名）:
  - `RING_SPEC: dict[str, RingTier]`，`RingTier` 含 `step: int` / `buckets: int` / `segment: int`（值见上表）
  - `class HistoryRing`：
    - `merge_row(row: dict) -> None`（按 row["interval"] 路由到对应档；同桶合并语义与 flusher 一致：open 保留最早、close 取最新、high/low 极值、v/n 累加；插入后按窗口修剪）
    - `get_segment(interval: str, segment_epoch: int) -> dict`（该段列式编码；段内无数据返回空数组的合法编码，不返回 None）
    - `tail(interval: str, now_epoch: int) -> dict`（最后封存边界 → now 的列式编码）
    - `window_start(interval: str, now_epoch: int) -> int`（ring 可信覆盖下界 = `floor(now, step) - (buckets-1)*step`）
  - 模块级 `seal_boundary(interval: str, now_epoch: int) -> int`（`now - now % segment`）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_history_ring.py`：

```python
"""HistoryRing 纯内存单元测试（无 DB、无 asyncio）。"""
from datetime import datetime, timezone
from decimal import Decimal

from app.services.history_ring import RING_SPEC, HistoryRing, seal_boundary


def _row(interval="10s", epoch=1_755_734_400, o="0.5", h="0.6", l="0.5", c="0.6",
         v="1", n=1, oid=1):
    return {
        "outcome_id": oid, "interval": interval,
        "bucket_start": datetime.fromtimestamp(epoch, tz=timezone.utc),
        "open_price": Decimal(o), "high_price": Decimal(h),
        "low_price": Decimal(l), "close_price": Decimal(c),
        "volume_shares": Decimal(v), "n_trades": n,
        "updated_at": datetime.fromtimestamp(epoch, tz=timezone.utc),
    }


def test_ring_spec_matches_design():
    assert RING_SPEC["10s"].step == 10 and RING_SPEC["10s"].buckets == 360 and RING_SPEC["10s"].segment == 600
    assert RING_SPEC["1m"].step == 60 and RING_SPEC["1m"].buckets == 1440 and RING_SPEC["1m"].segment == 3600
    assert RING_SPEC["15m"].step == 900 and RING_SPEC["15m"].buckets == 672 and RING_SPEC["15m"].segment == 86400
    assert RING_SPEC["1h"].step == 3600 and RING_SPEC["1h"].buckets == 2160 and RING_SPEC["1h"].segment == 604800


def test_merge_same_bucket_open_first_close_last():
    r = HistoryRing()
    r.merge_row(_row(o="0.5", h="0.6", l="0.5", c="0.6", v="1", n=1))
    r.merge_row(_row(o="0.6", h="0.7", l="0.4", c="0.55", v="2", n=1))
    seg = r.get_segment("10s", 1_755_734_400)   # 1_755_734_400 % 600 == 0
    assert seg["t"] == [0]
    assert seg["o"] == [round(0.5 * 1e8)]
    assert seg["c"] == [round(0.55 * 1e8)]
    assert seg["h"] == [round(0.7 * 1e8)]
    assert seg["l"] == [round(0.4 * 1e8)]
    assert seg["v"] == [3.0]
    assert seg["trades"] == [2]


def test_segment_encoding_sparse_and_shape():
    r = HistoryRing()
    base = 1_755_734_400
    r.merge_row(_row(epoch=base))                # 桶 0
    r.merge_row(_row(epoch=base + 30))           # 桶 3
    seg = r.get_segment("10s", base)
    assert seg["t0"] == base and seg["step"] == 10 and seg["n_buckets"] == 60
    assert seg["t"] == [0, 3]
    assert len(seg["o"]) == len(seg["h"]) == len(seg["l"]) == len(seg["c"]) == len(seg["v"]) == len(seg["trades"]) == 2
    # 段外的桶不进本段
    r.merge_row(_row(epoch=base + 600))
    assert r.get_segment("10s", base)["t"] == [0, 3]


def test_empty_segment_is_valid_encoding_not_none():
    r = HistoryRing()
    seg = r.get_segment("1m", 1_755_734_400 - 1_755_734_400 % 3600)
    assert seg["t"] == [] and seg["o"] == []
    assert seg["n_buckets"] == 60


def test_window_pruning_drops_old_buckets():
    r = HistoryRing()
    base = 1_755_734_400
    r.merge_row(_row(epoch=base))
    # 推进超过 1h 窗口（360 桶 × 10s）
    r.merge_row(_row(epoch=base + 360 * 10))
    assert r.get_segment("10s", base)["t"] == []          # 老桶已被修剪
    # 其它档不受影响（各档独立窗口）
    r.merge_row(_row(interval="1h", epoch=base - base % 3600))
    assert r.get_segment("1h", (base - base % 3600) - (base - base % 3600) % 604800)["t"] != []


def test_tail_and_window_start_and_seal_boundary():
    r = HistoryRing()
    base = 1_755_734_400            # 对齐 600
    now = base + 250                # 段内进行中
    assert seal_boundary("10s", now) == base
    r.merge_row(_row(epoch=base + 240))
    t = r.tail("10s", now)
    assert t["t0"] == base and t["t"] == [24]
    assert r.window_start("10s", now) == (now - now % 10) - 359 * 10
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_history_ring.py -x -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.history_ring`

- [ ] **Step 3: 实现 history_ring.py**

```python
"""每 outcome 的内存多分辨率环形缓冲（spec § 7.1 / § 7.2 / § 7.3）。

writer 每笔 commit 后把 compute_candle_rows 的行 merge 进来（与 candle
flusher 同一份数据），/history/ 端点与 SSE snapshot 从这里读。全部操作
在同一个 event loop 内、读取期间无 await，不需要锁。

价格在编码边界转 8 位定点整数（round(float * 1e8)）——这是 spec § 7.1
的列式编码契约，客户端 ÷1e8 还原。桶内部保留 Decimal 精确合并。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict


@dataclass(frozen=True)
class RingTier:
    step: int      # 桶宽（秒）
    buckets: int   # ring 容量（桶数）
    segment: int   # 封存段长（秒），spec § 7.2

    @property
    def window(self) -> int:
        return self.step * self.buckets


RING_SPEC: dict[str, RingTier] = {
    "10s": RingTier(step=10, buckets=360, segment=600),
    "1m": RingTier(step=60, buckets=1440, segment=3600),
    "15m": RingTier(step=900, buckets=672, segment=86400),
    "1h": RingTier(step=3600, buckets=2160, segment=604800),
}


def seal_boundary(interval: str, now_epoch: int) -> int:
    """最近一个已过去的封存边界（<= now）。边界之前的段才不可变。"""
    seg = RING_SPEC[interval].segment
    return now_epoch - (now_epoch % seg)


_PRICE_FIXED = 1e8


class HistoryRing:
    """单 outcome 的 4 档 OHLCV 桶。桶存储：epoch → dict(o,h,l,c Decimal, v Decimal, n int)。"""

    def __init__(self) -> None:
        self._tiers: Dict[str, Dict[int, dict]] = {k: {} for k in RING_SPEC}

    def merge_row(self, row: dict) -> None:
        interval = row["interval"]
        tier = RING_SPEC.get(interval)
        if tier is None:
            return
        buckets = self._tiers[interval]
        epoch = int(row["bucket_start"].timestamp())
        b = buckets.get(epoch)
        if b is None:
            buckets[epoch] = {
                "o": row["open_price"], "h": row["high_price"],
                "l": row["low_price"], "c": row["close_price"],
                "v": row["volume_shares"], "n": int(row["n_trades"]),
            }
        else:
            # 同桶合并：open 保留最早（不动），close 取最新，h/l 极值，v/n 累加
            b["h"] = max(b["h"], row["high_price"])
            b["l"] = min(b["l"], row["low_price"])
            b["c"] = row["close_price"]
            b["v"] = b["v"] + row["volume_shares"]
            b["n"] = b["n"] + int(row["n_trades"])
        # 按窗口修剪：以本档最新桶为基准，丢弃滑出 ring 的老桶
        newest = max(buckets)
        floor = newest - (tier.buckets - 1) * tier.step
        if min(buckets) < floor:
            for e in [e for e in buckets if e < floor]:
                del buckets[e]

    def _encode(self, interval: str, t0: int, until_exclusive: int) -> dict:
        tier = RING_SPEC[interval]
        buckets = self._tiers[interval]
        epochs = sorted(e for e in buckets if t0 <= e < until_exclusive)
        out = {
            "t0": t0, "step": tier.step,
            "n_buckets": (until_exclusive - t0 + tier.step - 1) // tier.step,
            "t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "trades": [],
        }
        for e in epochs:
            b = buckets[e]
            out["t"].append((e - t0) // tier.step)
            out["o"].append(round(float(b["o"]) * _PRICE_FIXED))
            out["h"].append(round(float(b["h"]) * _PRICE_FIXED))
            out["l"].append(round(float(b["l"]) * _PRICE_FIXED))
            out["c"].append(round(float(b["c"]) * _PRICE_FIXED))
            out["v"].append(float(b["v"]))
            out["trades"].append(b["n"])
        return out

    def get_segment(self, interval: str, segment_epoch: int) -> dict:
        seg = RING_SPEC[interval].segment
        enc = self._encode(interval, segment_epoch, segment_epoch + seg)
        enc["n_buckets"] = seg // RING_SPEC[interval].step
        return enc

    def tail(self, interval: str, now_epoch: int) -> dict:
        t0 = seal_boundary(interval, now_epoch)
        step = RING_SPEC[interval].step
        return self._encode(interval, t0, (now_epoch - now_epoch % step) + step)

    def window_start(self, interval: str, now_epoch: int) -> int:
        tier = RING_SPEC[interval]
        return (now_epoch - now_epoch % tier.step) - (tier.buckets - 1) * tier.step
```

- [ ] **Step 4: 跑测确认通过**

Run: `python -m pytest tests/test_history_ring.py -q && python -m py_compile $(find app -name '*.py')`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/history_ring.py tests/test_history_ring.py
git commit -m "feat(history): HistoryRing 环形缓冲——4 档 OHLCV 桶、flusher 同源合并语义、列式编码段/尾巴"
```

---

### Task 2: CandleFlusher 高水位（防线 3 的依据）

**Files:**
- Modify: `backend/app/services/candle_flusher.py`
- Test: `backend/tests/test_candle_flusher.py`（追加用例）

**Interfaces:**
- Produces: `CANDLE_FLUSHER.oldest_pending_bucket() -> datetime | None`——pending 里最老的 `bucket_start`；空 pending 返回 None。**语义**：返回 None 或返回值 ≥ 某段末尾 ⇒ 该段时间范围的 flush 已完成，DB 可信。writer off 时 flusher 无 pending（老路径事务内同步写 candle）→ 恒返回 None → 防线恒通过。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_candle_flusher.py` 末尾追加（复用文件内既有 `_row` helper）：

```python
def test_oldest_pending_bucket_none_when_empty():
    assert CANDLE_FLUSHER.oldest_pending_bucket() is None


def test_oldest_pending_bucket_returns_min_bucket_start():
    from datetime import datetime, timezone
    early = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 8, 21, 0, 10, 0, tzinfo=timezone.utc)
    CANDLE_FLUSHER.merge([_row(1, bucket=late)])
    CANDLE_FLUSHER.merge([_row(1, bucket=early)])
    assert CANDLE_FLUSHER.oldest_pending_bucket() == early
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_candle_flusher.py -x -q`
Expected: FAIL —— `AttributeError: oldest_pending_bucket`

- [ ] **Step 3: 实现**

`candle_flusher.py` 的 `pending_count` 旁加：

```python
    def oldest_pending_bucket(self):
        """pending 里最老的 bucket_start；空返回 None。

        /history/ 防线 3 用作 flush 高水位：某段末尾 <= 本值（或本值为 None）
        才允许从 DB 吐该段——防"封存边界刚过、5s 批次未落库"的窗口把不完整段
        以 immutable 固化（spec § 7.2 防线 3）。
        """
        if not self._pending:
            return None
        return min(row["bucket_start"] for row in self._pending.values())
```

- [ ] **Step 4: 跑测确认通过 + Commit**

Run: `python -m pytest tests/test_candle_flusher.py -q`
Expected: 全 PASS

```bash
git add app/services/candle_flusher.py tests/test_candle_flusher.py
git commit -m "feat(candle): flusher 高水位 oldest_pending_bucket——/history/ 防线 3 的落库完成判据"
```

---

### Task 3: MarketState.rings 接线（启动加载 + consumer 喂入 + 自愈重建）

**Files:**
- Modify: `backend/app/services/market_writer.py`（`MarketState` 加 `rings` 字段；`_load_one` 从 DB 初始化；`_consume` apply 后 merge；`reload_state` 换新 rings）
- Modify: `backend/app/main.py`（lifespan 注释：resync 先于 WRITER.start 的顺序依赖）
- Test: `backend/tests/test_writer_rings.py`

**Interfaces:**
- Consumes: Task 1 `HistoryRing` / `RING_SPEC`；既有 `OutcomeCandle` 模型；既有 `OpOutcome.candle_rows`
- Produces: `MarketState.rings: dict[int, HistoryRing]`（outcome_id → ring；spec § 4.1 的字段就位）。`_load_one` 返回的 state 带已从镜像回灌的 rings；writer consumer 在 `_merge_candles` 的同一位置把同一份 `candle_rows` 逐行 `merge_row` 进对应 outcome 的 ring

- [ ] **Step 1: 写失败测试**

`backend/tests/test_writer_rings.py`（`_fresh_db` / `_seed_market` 模式与 `tests/test_market_writer_state.py` 一致——拷贝进来，不跨文件共享 helper；`_seed_user` 拷贝自 `tests/test_writer_buy.py`）：

```python
"""MarketState.rings 接线测试：启动回灌 / 成交喂入 / reload 重建。"""
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User
from app.services.history_ring import seal_boundary
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd

# （此处粘贴 test_market_writer_state.py 的 _fresh_db fixture 与 _seed_market helper、
#   test_writer_buy.py 的 _seed_user helper，内容一致）


def _candle_row(oid: int, interval: str, epoch: int, price="0.5", v="1", n=1):
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return OutcomeCandle(
        outcome_id=oid, interval=interval, bucket_start=ts,
        open_price=Decimal(price), high_price=Decimal(price),
        low_price=Decimal(price), close_price=Decimal(price),
        volume_shares=Decimal(v), n_trades=n, updated_at=ts,
    )


@pytest.mark.asyncio
async def test_start_backfills_rings_from_mirror():
    mid, oids = await _seed_market()
    now = int(time.time())
    in_window = (now - now % 60) - 120            # 1m 档窗口内
    out_of_window = (now - now % 60) - 25 * 3600  # 超出 1m 档 24h 窗口
    async with async_session_maker() as s:
        s.add(_candle_row(oids[0], "1m", in_window))
        s.add(_candle_row(oids[0], "1m", out_of_window))
        await s.commit()
    await WRITER.start()
    ring = WRITER.get_state(mid).rings[oids[0]]
    seg_epoch = in_window - in_window % 3600
    assert (in_window - seg_epoch) // 60 in ring.get_segment("1m", seg_epoch)["t"]
    old_seg = out_of_window - out_of_window % 3600
    assert ring.get_segment("1m", old_seg)["t"] == []   # 超窗行不回灌


@pytest.mark.asyncio
async def test_buy_feeds_rings_all_outcomes():
    mid, oids = await _seed_market(shares=("0", "0"))
    uid = await _seed_user()
    await WRITER.start()
    await WRITER.submit(BuyCmd(
        market_id=mid, outcome_id=oids[0], user_id=uid, username="alice",
        shares=Decimal("10"), max_cost=None, max_slippage_bps=None,
        accept_any_slippage=True,
    ))
    st = WRITER.get_state(mid)
    now = int(time.time())
    for oid in oids:   # 被交易 outcome 与联动 outcome 都有价格桶
        t = st.rings[oid].tail("10s", now)
        assert t["t"], f"outcome {oid} ring 未被喂入"
    traded = st.rings[oids[0]].tail("10s", now)
    linked = st.rings[oids[1]].tail("10s", now)
    assert sum(traded["trades"]) == 1 and sum(traded["v"]) == 10.0
    assert sum(linked["trades"]) == 0 and sum(linked["v"]) == 0.0


@pytest.mark.asyncio
async def test_reload_state_rebuilds_rings_from_mirror():
    mid, oids = await _seed_market()
    await WRITER.start()
    now = int(time.time())
    epoch = now - now % 60
    async with async_session_maker() as s:
        s.add(_candle_row(oids[0], "1m", epoch, price="0.9"))
        await s.commit()
    await WRITER.reload_state(mid)
    ring = WRITER.get_state(mid).rings[oids[0]]
    seg = ring.get_segment("1m", epoch - epoch % 3600)
    assert round(0.9 * 1e8) in seg["c"]
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_writer_rings.py -x -q`
Expected: FAIL —— `AttributeError: 'MarketState' object has no attribute 'rings'`（或 KeyError）

- [ ] **Step 3: market_writer.py 改动**

1) `MarketState` 加字段（`unavailable` 之后）：

```python
    rings: dict[int, "HistoryRing"] = field(default_factory=dict)  # outcome_id → 环形缓冲（spec § 7.1）
```

顶部 import：`from app.services.history_ring import RING_SPEC, HistoryRing`，并 `from dataclasses import dataclass, field`（field 已在）。

2) `_load_one` 末尾、构造 `MarketState` 前回灌 rings（一次 SELECT，按各档窗口过滤在 Python 侧完成）：

```python
    # ── ring 回灌（spec § 7.1）：从镜像 OutcomeCandle 读回各档窗口内的桶 ──
    # 依赖 lifespan 顺序：_resync_recent_candles 先于 WRITER.start() 执行，
    # 崩溃丢失的 ≤5s candle 已被重放修复，镜像此刻可信。
    import time as _time
    from app.models.base import OutcomeCandle
    now_epoch = int(_time.time())
    max_window = max(t.window for t in RING_SPEC.values())
    from datetime import datetime as _dt, timezone as _tz
    cutoff = _dt.fromtimestamp(now_epoch - max_window, tz=_tz.utc)
    rings: dict[int, HistoryRing] = {int(o.id): HistoryRing() for o in outs}
    candle_rows = (await session.execute(
        select(OutcomeCandle).where(
            OutcomeCandle.outcome_id.in_([int(o.id) for o in outs]),
            OutcomeCandle.interval.in_(list(RING_SPEC.keys())),
            OutcomeCandle.bucket_start >= cutoff,
        )
    )).scalars().all()
    for c in candle_rows:
        bs = c.bucket_start if c.bucket_start.tzinfo else c.bucket_start.replace(tzinfo=_tz.utc)
        tier = RING_SPEC[c.interval]
        if int(bs.timestamp()) < now_epoch - tier.window:
            continue   # 该档窗口外（cutoff 用的是最长窗口 90d，短档要再过滤）
        rings[int(c.outcome_id)].merge_row({
            "outcome_id": int(c.outcome_id), "interval": c.interval,
            "bucket_start": bs,
            "open_price": c.open_price, "high_price": c.high_price,
            "low_price": c.low_price, "close_price": c.close_price,
            "volume_shares": c.volume_shares, "n_trades": c.n_trades,
            "updated_at": c.updated_at,
        })
```

并把 `rings=rings` 加进 `MarketState(...)` 构造参数。（import 就近写在函数体内是为了避免顶部环依赖膨胀；`select` 已在顶部。）

3) `_consume` 里 `if outcome.candle_rows:` 分支改为同时喂 ring：

```python
                if outcome.candle_rows:
                    self._merge_candles(outcome.candle_rows)
                    # ring 与 flusher 吃同一份行——两者永远一致（spec § 7.5）
                    for row in outcome.candle_rows:
                        ring = st.rings.get(int(row["outcome_id"]))
                        if ring is not None:
                            ring.merge_row(row)
```

4) `reload_state` 已整体拷贝 `st_new` 的字段——在拷贝行加上 rings：

```python
            st.q_dec, st.q, st.prices = st_new.q_dec, st_new.q, st_new.prices
            st.status, st.closes_at = st_new.status, st_new.closes_at
            st.rings = st_new.rings   # 自愈同样从镜像重建 ring（resync 保证镜像可信）
```

- [ ] **Step 4: main.py 顺序依赖注释**

`main.py` lifespan 中 `await _resync_recent_candles()` 的 try 块上方注释追加一行：

```python
    # ── candle 表 race-window 兜底扫（spec § 6.3）──
    # 覆盖 migration→新代码上线之间可能漏的 buy/sell。
    # ★ 顺序依赖（阶段 4）：必须先于 WRITER.start()——writer 启动时从
    #   OutcomeCandle 回灌 HistoryRing，resync 先跑保证崩溃丢失的 ≤5s 已修复。
```

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_writer_rings.py tests/test_market_writer_state.py tests/test_writer_buy.py tests/test_writer_e2e.py -q && python -c "import app.main"`
Expected: 全 PASS（既有 writer 测试不回归）

- [ ] **Step 6: Commit**

```bash
git add app/services/market_writer.py app/main.py tests/test_writer_rings.py
git commit -m "feat(history): MarketState.rings 接线——启动从镜像回灌、成交与 flusher 同源喂入、自愈重建"
```

---

### Task 4: resync 确定性断言测试（immutable 承诺的前置）

**Files:**
- Test: `backend/tests/test_candle_resync_determinism.py`

**Interfaces:**
- Consumes: `app.main._resync_recent_candles`；既有 `Transaction` / `OutcomeCandle` 模型
- Produces: 无新代码——这是 spec § 7.5 要求的护栏测试：「同一批 Transaction 重放出逐字段相同的 OHLCV」。若不成立，崩溃重启后的 DB 段会与崩溃前从 ring 以 immutable 发出的段不一致，违反 immutable 承诺。

**已知豁免**：`updated_at` 不参与断言——`upsert_candles` 的 UPDATE 分支写 `func.now()`，天然非确定；它不进列式编码（Task 1 的 `_encode` 不读它），不影响 immutable 承诺。测试注释里写明这一点。

- [ ] **Step 1: 写测试（预期直接通过——现实现应当已是确定性的；若 FAIL 则是真 bug，停下修）**

```python
"""spec § 7.5：_resync_recent_candles 必须确定性——同一批 Transaction 重放
两次得到逐字段相同的 OutcomeCandle 行集。

updated_at 豁免：upsert_candles UPDATE 分支写 func.now()，非确定；但它不进
/history/ 的列式编码，不违反 immutable 承诺。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import SQLModel, select

from app.core.database import engine, async_session_maker
from app.main import _resync_recent_candles
from app.models.base import (
    Market, MarketStatus, Outcome, OutcomeCandle, Transaction, TransactionType, User,
)

# （粘贴 _fresh_db fixture，内容与 tests/test_market_writer_state.py 一致，
#   teardown 不需要 WRITER.stop()，去掉那行）


async def _seed_market_with_trades() -> list[int]:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o1 = Outcome(market_id=m.id, label="a", total_shares=Decimal("30"))
        o2 = Outcome(market_id=m.id, label="b", total_shares=Decimal("0"))
        s.add(o1); s.add(o2); await s.flush()
        u = User(username="alice", email="a@x.com", hashed_password="x",
                 cash=Decimal("1000"), is_active=True)
        s.add(u); await s.flush()
        for i, (shares, post) in enumerate([("10", [0.52, 0.48]), ("20", [0.56, 0.44])]):
            s.add(Transaction(
                user_id=u.id, outcome_id=o1.id, type=TransactionType.BUY,
                shares=Decimal(shares), cost=Decimal("5"), price=Decimal("0.5"),
                pre_market_price=Decimal("0.5"), post_market_price=Decimal(str(post[0])),
                gross=Decimal("5"), fee=Decimal("0"),
                market_prices_post=post,
                timestamp=now - timedelta(minutes=5) + timedelta(seconds=i * 7),
            ))
        await s.commit()
        return [int(o1.id), int(o2.id)]


def _fingerprint(rows: list[OutcomeCandle]) -> list[tuple]:
    return sorted(
        (r.outcome_id, r.interval, r.bucket_start.replace(tzinfo=None),
         r.open_price, r.high_price, r.low_price, r.close_price,
         r.volume_shares, r.n_trades)
        for r in rows
    )


@pytest.mark.asyncio
async def test_resync_twice_yields_identical_rows():
    oids = await _seed_market_with_trades()
    await _resync_recent_candles(window_hours=1)
    async with async_session_maker() as s:
        first = _fingerprint((await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oids))
        )).scalars().all())
    assert first, "resync 应产出 candle 行"
    await _resync_recent_candles(window_hours=1)   # 第二次：DELETE + 重放
    async with async_session_maker() as s:
        second = _fingerprint((await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id.in_(oids))
        )).scalars().all())
    assert first == second
```

- [ ] **Step 2: 跑测**

Run: `python -m pytest tests/test_candle_resync_determinism.py -x -q`
Expected: PASS。若 FAIL：说明 resync 存在非确定性来源（immutable 承诺被破坏的真 bug），**停止推进本计划后续任务**，先定位修复再回来。

- [ ] **Step 3: Commit**

```bash
git add tests/test_candle_resync_determinism.py
git commit -m "test(candle): resync 确定性断言——同批 Transaction 重放两次逐字段相同（immutable 前置，spec § 7.5）"
```

---

### Task 5: `/history/` 端点（三道防线 + LRU + immutable 头）

**Files:**
- Create: `backend/app/api/v1/history.py`
- Modify: `backend/app/main.py`（挂路由 + `_LOG_SKIP_PREFIXES` 加 `/history/`）
- Test: `backend/tests/test_history_endpoint.py`

**Interfaces:**
- Consumes: Task 1 `RING_SPEC`；Task 2 `CANDLE_FLUSHER.oldest_pending_bucket()`；Task 3 `WRITER.get_state(mid).rings`；`WRITER.market_id_for_outcome`
- Produces: `GET /history/o/{outcome_id}/{interval}/{segment_epoch}.json` → 列式编码 JSON + `Cache-Control: public, max-age=31536000, immutable`。**挂载前缀是 `/history`，不在 `/api/v1` 下**——`main.py::_set_no_store_for_api` 只匹配 `/api/v1/` 前缀，天然不打 no-store，中间件零改动。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_history_endpoint.py`（走 HTTP，用 conftest 的 `client` fixture 同款模式——本仓库集成测试用 `httpx.AsyncClient` + `ASGITransport`，参考 `tests/test_chart_endpoint.py` 的 client 构造方式，逐字拷贝其 fixture）：

```python
"""GET /history/ 三道防线 + 编码 + 缓存头测试。"""
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.market_writer import WRITER

# （粘贴 tests/test_chart_endpoint.py 的 client fixture 与 DB 重建 fixture；
#   teardown 追加 await WRITER.stop() 与 CANDLE_FLUSHER._pending.clear()）


def _seg(epoch: int, seg_len: int) -> int:
    return epoch - epoch % seg_len


async def _seed_outcome_with_candle(epoch: int, interval="1m") -> int:
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        oid = int(o.id)
        s.add(OutcomeCandle(
            outcome_id=oid, interval=interval, bucket_start=ts,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.5"), close_price=Decimal("0.6"),
            volume_shares=Decimal("2"), n_trades=1, updated_at=ts,
        ))
        await s.commit()
        return oid


@pytest.mark.asyncio
async def test_inflight_segment_404(client):
    """防线 1：段末尾在未来 → 404，绝不吐进行中的段。"""
    oid = await _seed_outcome_with_candle(int(time.time()) - 60)
    cur = _seg(int(time.time()), 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{cur}.json")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sealed_segment_from_db_200_immutable(client):
    """防线 3（writer off / 超窗）：已封存 + flusher 无 pending → DB 供数 + immutable。"""
    epoch = int(time.time()) - 2 * 3600           # 上上个 1h 段内
    epoch -= epoch % 60
    oid = await _seed_outcome_with_candle(epoch)
    seg = _seg(epoch, 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{seg}.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    body = resp.json()
    assert body["t0"] == seg and body["step"] == 60 and body["n_buckets"] == 60
    idx = (epoch - seg) // 60
    assert idx in body["t"]
    assert body["c"][body["t"].index(idx)] == round(0.6 * 1e8)


@pytest.mark.asyncio
async def test_unflushed_segment_404(client):
    """防线 3：flusher 高水位覆盖段范围 → 404（未落库的段绝不固化）。"""
    epoch = int(time.time()) - 2 * 3600
    epoch -= epoch % 60
    oid = await _seed_outcome_with_candle(epoch)
    seg = _seg(epoch, 3600)
    ts = datetime.fromtimestamp(seg + 60, tz=timezone.utc)
    CANDLE_FLUSHER.merge([{
        "outcome_id": oid, "interval": "1m", "bucket_start": ts,
        "open_price": Decimal("0.5"), "high_price": Decimal("0.5"),
        "low_price": Decimal("0.5"), "close_price": Decimal("0.5"),
        "volume_shares": Decimal("1"), "n_trades": 1, "updated_at": ts,
    }])
    resp = await client.get(f"/history/o/{oid}/1m/{seg}.json")
    assert resp.status_code == 404
    CANDLE_FLUSHER._pending.clear()


@pytest.mark.asyncio
async def test_ring_serves_in_window_segment_without_db(client):
    """防线 2：writer on 且段在 ring 窗口内 → 一律 ring 供数（哪怕 DB 是空的）。"""
    epoch = int(time.time()) - 700                # 上一个 10min 段内（10s 档）
    epoch -= epoch % 10
    oid = await _seed_outcome_with_candle(epoch, interval="10s")
    await WRITER.start()
    st = WRITER.get_state(WRITER.market_id_for_outcome(oid))
    assert st is not None and oid in st.rings     # Task 3 已回灌
    # 删掉 DB 行证明走的是 ring
    from sqlalchemy import delete
    async with async_session_maker() as s:
        await s.execute(delete(OutcomeCandle).where(OutcomeCandle.outcome_id == oid))
        await s.commit()
    seg = _seg(epoch, 600)
    resp = await client.get(f"/history/o/{oid}/10s/{seg}.json")
    assert resp.status_code == 200
    assert (epoch - seg) // 10 in resp.json()["t"]


@pytest.mark.asyncio
async def test_validation_404s(client):
    oid = await _seed_outcome_with_candle(int(time.time()) - 7200)
    past = _seg(int(time.time()) - 7200, 3600)
    assert (await client.get(f"/history/o/{oid}/5m/{past}.json")).status_code == 404       # interval 白名单
    assert (await client.get(f"/history/o/{oid}/1m/{past + 61}.json")).status_code == 404  # epoch 未对齐
    assert (await client.get(f"/history/o/999999/1m/{past}.json")).status_code == 404      # outcome 不存在


@pytest.mark.asyncio
async def test_empty_sealed_segment_is_200(client):
    """无成交的已封存段是合法 200（空数组编码）——空也是不可变事实。"""
    oid = await _seed_outcome_with_candle(int(time.time()) - 7200)
    older = _seg(int(time.time()) - 8 * 3600, 3600)
    resp = await client.get(f"/history/o/{oid}/1m/{older}.json")
    assert resp.status_code == 200
    assert resp.json()["t"] == []
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_history_endpoint.py -x -q`
Expected: FAIL —— 404 route not found（路由不存在）

- [ ] **Step 3: 实现 history.py**

```python
"""/history/ 不可变历史段端点（spec § 7.2 / § 7.4 / D4）。

不挂在 /api/v1 下：main.py 的 _set_no_store_for_api 只对 /api/v1/ 打
no-store，本路由的 immutable 缓存头因此不被覆盖。nginx 对 /history/ 叠
proxy_cache（deploy/nginx.conf），回源次数 ≈ 段数，与在线人数无关。

三道封存防线——错一个 200 会被 nginx 钉 30 天、浏览器钉 1 年：
  1. 段末尾 > now → 404（进行中的段永不吐，尾巴走 SSE snapshot）
  2. writer on 且段在 ring 窗口内 → 一律 ring 供数，不读 DB（ring 是
     writer 实时写的，跨过封存边界即完整，无需等 flusher）
  3. 超窗 / writer off → 先验 flusher 高水位（oldest_pending_bucket），
     未覆盖段范围才从 DB 聚合，并进程内 LRU 缓存
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Outcome, OutcomeCandle
from app.services.candle_flusher import CANDLE_FLUSHER
from app.services.history_ring import RING_SPEC, HistoryRing
from app.services.market_writer import WRITER

router = APIRouter()

IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

# 进程内 LRU：key=(outcome_id, interval, segment_epoch) → 编码 dict。
# 段内容不可变，缓存永不失效；上限控内存，满了淘汰最久未用。
_LRU_MAX = 1024
_lru: OrderedDict[tuple[int, str, int], dict] = OrderedDict()


def _lru_get(key):
    enc = _lru.get(key)
    if enc is not None:
        _lru.move_to_end(key)
    return enc


def _lru_put(key, enc) -> None:
    _lru[key] = enc
    _lru.move_to_end(key)
    if len(_lru) > _LRU_MAX:
        _lru.popitem(last=False)


def _json_immutable(enc: dict) -> Response:
    return Response(
        content=json.dumps(enc, separators=(",", ":")),
        media_type="application/json",
        headers=IMMUTABLE_HEADERS,
    )


def _encode_db_rows(interval: str, segment_epoch: int, rows) -> dict:
    """DB 行 → 与 HistoryRing._encode 相同的列式编码（复用 ring 保证格式恒一致）。"""
    ring = HistoryRing()
    for c in rows:
        bs = c.bucket_start if c.bucket_start.tzinfo else c.bucket_start.replace(tzinfo=timezone.utc)
        ring.merge_row({
            "outcome_id": int(c.outcome_id), "interval": c.interval, "bucket_start": bs,
            "open_price": c.open_price, "high_price": c.high_price,
            "low_price": c.low_price, "close_price": c.close_price,
            "volume_shares": c.volume_shares, "n_trades": c.n_trades,
            "updated_at": c.updated_at,
        })
    return ring.get_segment(interval, segment_epoch)


@router.get("/o/{outcome_id}/{interval}/{segment_epoch}.json", summary="不可变历史段（列式 OHLCV）")
async def get_history_segment(outcome_id: int, interval: str, segment_epoch: int):
    tier = RING_SPEC.get(interval)
    if tier is None:
        raise HTTPException(status_code=404, detail="不支持的 interval")
    if segment_epoch < 0 or segment_epoch % tier.segment != 0:
        raise HTTPException(status_code=404, detail="segment_epoch 未对齐段长")

    now = int(time.time())
    seg_end = segment_epoch + tier.segment
    if seg_end > now:
        # 防线 1：进行中的段永不吐（尾巴数据只走 SSE snapshot，spec § 7.3）
        raise HTTPException(status_code=404, detail="段尚未封存")

    # 防线 2：ring 窗口内一律从 ring 供数（writer 实时写，跨过边界即完整）
    if WRITER.enabled:
        mid = WRITER.market_id_for_outcome(outcome_id)
        st = WRITER.get_state(mid) if mid is not None else None
        ring = st.rings.get(outcome_id) if st is not None else None
        if ring is not None and segment_epoch >= ring.window_start(interval, now):
            return _json_immutable(ring.get_segment(interval, segment_epoch))

    # 防线 3：超窗 / writer off → DB。段不可变，命中 LRU 直接回
    key = (outcome_id, interval, segment_epoch)
    cached = _lru_get(key)
    if cached is not None:
        return _json_immutable(cached)

    oldest_pending = CANDLE_FLUSHER.oldest_pending_bucket()
    if oldest_pending is not None:
        op = oldest_pending if oldest_pending.tzinfo else oldest_pending.replace(tzinfo=timezone.utc)
        if int(op.timestamp()) < seg_end:
            # 该段范围的 flush 尚未完成——不完整段绝不以 immutable 固化
            raise HTTPException(status_code=404, detail="段落库未完成，请稍后重试")

    async with async_session_maker() as s:
        if (await s.execute(select(Outcome.id).where(Outcome.id == outcome_id))).scalars().first() is None:
            raise HTTPException(status_code=404, detail="选项不存在")
        rows = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == outcome_id,
                OutcomeCandle.interval == interval,
                OutcomeCandle.bucket_start >= datetime.fromtimestamp(segment_epoch, tz=timezone.utc),
                OutcomeCandle.bucket_start < datetime.fromtimestamp(seg_end, tz=timezone.utc),
            ).order_by(OutcomeCandle.bucket_start.asc())
        )).scalars().all()

    enc = _encode_db_rows(interval, segment_epoch, rows)
    _lru_put(key, enc)
    return _json_immutable(enc)
```

- [ ] **Step 4: main.py 挂路由 + 日志跳过**

- `_LOG_SKIP_PREFIXES` 元组追加一行 `"/history/",`（注释：`# 阶段 4：不可变历史段，nginx 缓存回源为主，量大且无审计价值`）
- router 注册区（`app.include_router(stream.router, ...)` 之后）加：

```python
from app.api.v1 import history as history_api
app.include_router(history_api.router, prefix="/history", tags=["History"])  # 不在 /api/v1 下：绕开 no-store 中间件（见 history.py 模块注释）
```

- [ ] **Step 5: 跑测确认通过**

Run: `python -m pytest tests/test_history_endpoint.py -q && python -c "import app.main" && python -m py_compile $(find app -name '*.py')`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/history.py app/main.py tests/test_history_endpoint.py
git commit -m "feat(history): /history/ 不可变段端点——三道封存防线 + 进程内 LRU + immutable 缓存头"
```

---

### Task 6: SSE snapshot 携带 history_tail

**Files:**
- Modify: `backend/app/api/v1/stream.py`（`_build_snapshot` 加 `history_tail`）
- Test: `backend/tests/test_stream_history_tail.py`

**Interfaces:**
- Consumes: Task 1 `HistoryRing.tail` / `seal_boundary`；Task 3 `WRITER.get_state(mid).rings`
- Produces: snapshot `data` 新增字段 `history_tail: {str(outcome_id): {interval: 列式编码}}`——最后封存边界 → now 的桶，4 档全给。**纯新增字段**：老前端 / bot 不读它，契约兼容（sse-contract.md 的 snapshot 字段是「至少包含」语义，阶段 2 已同步）。尾巴与后续 tick 帧共享同一条 seq 流（snapshot 锚点机制不变），gap 检测天然覆盖尾巴→实时的接缝。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_stream_history_tail.py`（client fixture 拷贝自 `tests/test_history_endpoint.py` 同款；SSE 流测试参考仓库既有 `tests/test_stream_*.py` 的「读首包 snapshot 后断开」模式，逐字拷贝其读取 helper）：

```python
"""snapshot.history_tail：writer on 走 ring / writer off 走 DB，两路都要有。"""
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.history_ring import seal_boundary
from app.services.market_writer import WRITER

# （粘贴 client + DB fixture；teardown 含 await WRITER.stop()）


async def _read_snapshot(client, market_id: int) -> dict:
    """打开 SSE 流读第一条 snapshot 的 data 后断开。"""
    async with client.stream("GET", f"/api/v1/stream/market/{market_id}") as resp:
        assert resp.status_code == 200
        buf = ""
        async for line in resp.aiter_lines():
            buf += line + "\n"
            if line == "" and "data: " in buf:
                data_line = next(l for l in buf.split("\n") if l.startswith("data: "))
                payload = json.loads(data_line[len("data: "):])
                assert payload["type"] == "snapshot"
                return payload["data"]
    raise AssertionError("未读到 snapshot")


async def _seed(now_epoch: int):
    # 注意：seed 与请求之间若恰好跨过 1h 封存边界会 flaky（概率 ~1/3600）。
    # 边界紧邻时把种子桶放到"新边界后"仍成立，这里接受该极小概率重跑。
    boundary = seal_boundary("1m", now_epoch)
    ts = datetime.fromtimestamp(boundary + 60, tz=timezone.utc)   # 边界之后 → 属于尾巴
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal("0"))
        s.add(o); await s.flush()
        s.add(OutcomeCandle(
            outcome_id=o.id, interval="1m", bucket_start=ts,
            open_price=Decimal("0.5"), high_price=Decimal("0.6"),
            low_price=Decimal("0.5"), close_price=Decimal("0.6"),
            volume_shares=Decimal("2"), n_trades=1, updated_at=ts,
        ))
        await s.commit()
        return int(m.id), int(o.id), boundary


@pytest.mark.asyncio
async def test_tail_from_db_when_writer_off(client):
    mid, oid, boundary = await _seed(int(time.time()))
    snap = await _read_snapshot(client, mid)
    tail = snap["history_tail"][str(oid)]["1m"]
    assert tail["t0"] == boundary
    assert 1 in tail["t"]                       # boundary+60 → 桶序号 1
    assert set(snap["history_tail"][str(oid)].keys()) == {"10s", "1m", "15m", "1h"}


@pytest.mark.asyncio
async def test_tail_from_ring_when_writer_on(client):
    mid, oid, boundary = await _seed(int(time.time()))
    await WRITER.start()                        # Task 3：启动回灌 ring
    snap = await _read_snapshot(client, mid)
    tail = snap["history_tail"][str(oid)]["1m"]
    assert tail["t0"] == boundary and 1 in tail["t"]
```

- [ ] **Step 2: 跑测确认失败**

Run: `python -m pytest tests/test_stream_history_tail.py -x -q`
Expected: FAIL —— `KeyError: 'history_tail'`

- [ ] **Step 3: 实现 stream.py 改动**

顶部 import 调整：既有 `from app.models.base import Market, Outcome` 追加 `OutcomeCandle`，另加两行：

```python
from app.services.history_ring import RING_SPEC, HistoryRing, seal_boundary
from app.services.market_writer import WRITER
```

`_build_snapshot` 的 `return {...}` 前加，并在 dict 里加 `"history_tail": history_tail,`：

```python
    # ── 尾巴（spec § 7.3）：最后封存边界 → now，随 snapshot 首包下发 ──
    # 与后续 tick 帧共享同一条 seq 流（snapshot 锚点原子性不变），gap 检测
    # 天然覆盖"尾巴 → 实时"的接缝；/history/ 永不吐进行中的段（防线 1）。
    import time as _time
    now_epoch = int(_time.time())
    history_tail: dict = {}
    st = WRITER.get_state(int(market.id)) if WRITER.enabled else None
    if st is not None:
        for o in outcomes:
            ring = st.rings.get(int(o.id))
            if ring is not None:
                history_tail[str(o.id)] = {
                    iv: ring.tail(iv, now_epoch) for iv in RING_SPEC
                }
    if not history_tail:
        # writer off（旧路径）：从 DB 组装。每档一次范围查询（snapshot 本就
        # 是 per-connection DB 读，这里 4 次小查询可接受；writer on 时零 DB）。
        oid_list = [int(o.id) for o in outcomes]
        rings = {oid: HistoryRing() for oid in oid_list}
        for iv in RING_SPEC:
            boundary = seal_boundary(iv, now_epoch)
            rows = (await db.execute(
                select(OutcomeCandle).where(
                    OutcomeCandle.outcome_id.in_(oid_list),
                    OutcomeCandle.interval == iv,
                    OutcomeCandle.bucket_start >= datetime.fromtimestamp(boundary, timezone.utc),
                )
            )).scalars().all()
            for c in rows:
                bs = c.bucket_start if c.bucket_start.tzinfo else c.bucket_start.replace(tzinfo=timezone.utc)
                rings[int(c.outcome_id)].merge_row({
                    "outcome_id": int(c.outcome_id), "interval": c.interval, "bucket_start": bs,
                    "open_price": c.open_price, "high_price": c.high_price,
                    "low_price": c.low_price, "close_price": c.close_price,
                    "volume_shares": c.volume_shares, "n_trades": c.n_trades,
                    "updated_at": c.updated_at,
                })
        history_tail = {
            str(oid): {iv: rings[oid].tail(iv, now_epoch) for iv in RING_SPEC}
            for oid in oid_list
        }
```

（`datetime` / `timezone` 已在 stream.py 顶部 import。）

- [ ] **Step 4: 跑测确认通过**

Run: `python -m pytest tests/test_stream_history_tail.py tests/test_realtime_broker.py -q && python -c "import app.main"`
Expected: 全 PASS（既有 SSE 测试不回归——snapshot 是纯加字段）

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/stream.py tests/test_stream_history_tail.py
git commit -m "feat(history): snapshot 携带 history_tail——封存边界后的尾巴随首包下发，与 tick 帧共享 seq 流"
```

---

### Task 7: nginx proxy_cache（红线文件，spec 已授权）

**Files:**
- Modify: `deploy/nginx.conf`

**Interfaces:**
- Consumes: Task 5 的 `/history/` 端点（immutable 200 / 404）
- Produces: nginx 层 30 天段缓存；回源次数 ≈ 段数。**404 不缓存**（未配 `proxy_cache_valid 404`，nginx 默认只缓存配置过的状态码）——进行中段 / 未落库段的 404 每次回源，封存后第一次 200 即被钉住，语义正确。

- [ ] **Step 1: 改 nginx.conf**

`limit_req_zone` 区域末尾（`zone=api_trade_user` 行之后）追加：

```nginx
# ── /history/ 不可变历史段缓存（spec § 7.6）──
# 段内容 immutable：URL 含段起点，永不变化。回源次数 ≈ 段数，与在线人数无关。
proxy_cache_path /var/cache/nginx/thccb levels=1:2 keys_zone=thccb_hist:10m
                 max_size=1g inactive=30d;
```

server 块内、`location /api/v1/ {` 之前加（前缀 `/history/` 比 `/` 长，最长前缀优先，不会被 SPA `try_files` 吃掉）：

```nginx
    # ── 不可变历史段（阶段 4）：后端吐 immutable，nginx 叠 30d 磁盘缓存 ──
    location /history/ {
        proxy_pass http://127.0.0.1:8004;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache thccb_hist;
        proxy_cache_valid 200 30d;
        proxy_cache_use_stale error timeout updating;
        add_header X-Cache-Status $upstream_cache_status;
    }
```

- [ ] **Step 2: 本地语法自查（无法真实 reload）**

Run: `nginx -t -c /data/sunyunbo/www/TouhouCCB/deploy/nginx.conf 2>&1 || true`
Expected: 本机多半没有 nginx / 证书路径不存在导致报错——**这不算验证失败**。在收尾报告写明：「nginx 配置未在本机验证，生产 reload 前必须 `sudo nginx -t`」。

- [ ] **Step 3: Commit（附部署清单）**

```bash
git add deploy/nginx.conf
git commit -m "feat(deploy): nginx /history/ proxy_cache——30d 段缓存，immutable 回源与在线人数解耦（spec § 7.6，已授权）"
```

**部署清单（用户手动步骤，写进收尾报告）**：
1. `sudo mkdir -p /var/cache/nginx/thccb && sudo chown www-data:www-data /var/cache/nginx/thccb`（owner 按实际 nginx worker 用户）
2. `sudo nginx -t && sudo systemctl reload nginx`
3. 验证：`curl -sI https://<域名>/history/o/<某oid>/1m/<已封存epoch>.json | grep -i x-cache-status`——第一次 MISS、第二次 HIT
4. **顺序**：本清单必须先于前端新图表版本部署完成（Task 8/9 的产物）——否则 `/history/` 请求会被 SPA `try_files` 兜走返回 index.html（前端有回退，但会静默降级到老 chart 端点）

---

### Task 8: 前端 history API 层（列式解码 + 段 epoch 计算 + 客户端 fill）+ vitest 单测

**Files:**
- Create: `thccb-frontend/src/api/history.ts`
- Test: `thccb-frontend/src/api/__tests__/history.spec.ts`

**Interfaces:**
- Consumes: Task 5 的 `/history/` 响应格式；`@/types/api` 的 `Candle` / `ChartInterval`
- Produces（Task 9 依赖的确切签名）:
  - `interface EncodedSegment { t0: number; step: number; n_buckets: number; t: number[]; o: number[]; h: number[]; l: number[]; c: number[]; v: number[]; trades: number[] }`
  - `type HistoryTailMap = Record<string, Record<string, EncodedSegment>>`（snapshot.history_tail 的形状）
  - `SEGMENT_SECONDS: Record<ChartInterval, number>`（600/3600/86400/604800）
  - `decodeSegment(seg: EncodedSegment): Candle[]`（定点 ÷1e8，t → ISO）
  - `sealedSegmentEpochs(interval: ChartInterval, fromSec: number, nowSec: number): number[]`（覆盖 `[fromSec, 最后封存边界)` 的对齐段起点列表）
  - `fetchSegment(outcomeId: number, interval: ChartInterval, epoch: number): Promise<EncodedSegment | null>`（404 → null；其余非 2xx 抛错）
  - `fillCandles(candles: Candle[], stepSec: number, fromSec: number, toSecExclusive: number): Candle[]`（缺桶用 prev_close 平推，语义对齐 chart.py `fill=true`：窗口前无数据用首根 open 反向回填）

- [ ] **Step 1: 写失败测试**

`thccb-frontend/src/api/__tests__/history.spec.ts`：

```typescript
import { describe, expect, it } from 'vitest'
import {
  decodeSegment, fillCandles, sealedSegmentEpochs,
  SEGMENT_SECONDS, type EncodedSegment,
} from '../history'

const seg: EncodedSegment = {
  t0: 1755734400, step: 60, n_buckets: 60,
  t: [0, 3],
  o: [50000000, 60000000], h: [70000000, 60000000],
  l: [50000000, 40000000], c: [60000000, 55000000],
  v: [1.5, 2], trades: [1, 2],
}

describe('decodeSegment', () => {
  it('定点 ÷1e8、稀疏桶按 t0+t[i]*step 定位', () => {
    const candles = decodeSegment(seg)
    expect(candles).toHaveLength(2)
    expect(candles[0]).toEqual({
      t: new Date(1755734400 * 1000).toISOString(),
      o: 0.5, h: 0.7, l: 0.5, c: 0.6, v: 1.5, n: 1,
    })
    expect(candles[1]!.t).toBe(new Date((1755734400 + 180) * 1000).toISOString())
    expect(candles[1]!.c).toBe(0.55)
  })
  it('空段解码为空数组', () => {
    expect(decodeSegment({ ...seg, t: [], o: [], h: [], l: [], c: [], v: [], trades: [] })).toEqual([])
  })
})

describe('sealedSegmentEpochs', () => {
  it('只含完全封存的段，覆盖 lookback 起点所在段', () => {
    expect(SEGMENT_SECONDS['1m']).toBe(3600)
    const now = 1755734400 + 3600 + 120        // 当前 1h 段进行中
    const from = 1755734400 - 1800             // 上上段中部
    const epochs = sealedSegmentEpochs('1m', from, now)
    expect(epochs).toEqual([1755734400 - 3600, 1755734400])   // 进行中段不含
  })
  it('lookback 全在当前段内时返回空', () => {
    const now = 1755734400 + 300
    expect(sealedSegmentEpochs('1m', 1755734400 + 60, now)).toEqual([])
  })
})

describe('fillCandles', () => {
  const c = (epoch: number, price: number, v = 1, n = 1) => ({
    t: new Date(epoch * 1000).toISOString(),
    o: price, h: price, l: price, c: price, v, n,
  })
  it('缺桶用 prev_close 平推，v=0 n=0（对齐后端 fill=true）', () => {
    const out = fillCandles([c(1000000020, 0.6)], 10, 1000000000, 1000000050)
    expect(out).toHaveLength(5)
    expect(out[0]).toMatchObject({ o: 0.6, c: 0.6, v: 0, n: 0 })   // 前置空桶用首根 open 回填
    expect(out[2]).toMatchObject({ o: 0.6, c: 0.6, v: 1 })
    expect(out[3]).toMatchObject({ o: 0.6, c: 0.6, v: 0, n: 0 })
  })
  it('完全无数据返回空（无 prev_close 可推）', () => {
    expect(fillCandles([], 10, 1000000000, 1000000050)).toEqual([])
  })
})
```

- [ ] **Step 2: 跑测确认失败**

Run: `npm run test:unit -- src/api/__tests__/history.spec.ts`
Expected: FAIL —— 找不到模块 `../history`

- [ ] **Step 3: 实现 history.ts**

```typescript
// /history/ 不可变段的取数与解码（阶段 4，spec § 7）。
// 段 URL 含起点 epoch，内容永不变化：浏览器 immutable + nginx proxy_cache
// 双层缓存，重复进入图表几乎零请求。进行中的尾巴不走这里——由 SSE
// snapshot.history_tail 携带（useCandleHistory 组装）。
import type { Candle, ChartInterval } from '@/types/api'

export interface EncodedSegment {
  t0: number; step: number; n_buckets: number
  t: number[]; o: number[]; h: number[]; l: number[]; c: number[]
  v: number[]; trades: number[]
}

/** snapshot.history_tail 的形状：outcome_id(str) → interval → 列式尾巴 */
export type HistoryTailMap = Record<string, Record<string, EncodedSegment>>

/** 封存段长（秒），与后端 RING_SPEC.segment 一致 */
export const SEGMENT_SECONDS: Record<ChartInterval, number> = {
  '10s': 600, '1m': 3600, '15m': 86400, '1h': 604800,
}

const PRICE_FIXED = 1e8

const baseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004').replace(/\/$/, '')

export function decodeSegment(seg: EncodedSegment): Candle[] {
  const out: Candle[] = []
  for (let i = 0; i < seg.t.length; i++) {
    out.push({
      t: new Date((seg.t0 + seg.t[i]! * seg.step) * 1000).toISOString(),
      o: seg.o[i]! / PRICE_FIXED, h: seg.h[i]! / PRICE_FIXED,
      l: seg.l[i]! / PRICE_FIXED, c: seg.c[i]! / PRICE_FIXED,
      v: seg.v[i]!, n: seg.trades[i]!,
    })
  }
  return out
}

/** 覆盖 [fromSec, 最后封存边界) 的段起点列表（对齐段长；进行中的段不含） */
export function sealedSegmentEpochs(interval: ChartInterval, fromSec: number, nowSec: number): number[] {
  const seg = SEGMENT_SECONDS[interval]
  const boundary = nowSec - (nowSec % seg)          // 最后封存边界
  let cur = fromSec - (fromSec % seg)
  const epochs: number[] = []
  for (; cur < boundary; cur += seg) epochs.push(cur)
  return epochs
}

/** 取一个封存段；404（理论上只有未封存/未落库的竞态窗口）→ null，调用方跳过 */
export async function fetchSegment(
  outcomeId: number, interval: ChartInterval, epoch: number,
): Promise<EncodedSegment | null> {
  const resp = await fetch(`${baseUrl}/history/o/${outcomeId}/${interval}/${epoch}.json`)
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error(`history segment ${epoch} failed: ${resp.status}`)
  return (await resp.json()) as EncodedSegment
}

/** 客户端 fill：缺桶用 prev_close 平推（v=0 n=0），语义对齐后端 chart.py fill=true。
 *  窗口前无数据时用首根的 open 反向回填，让前置空桶显示横线。 */
export function fillCandles(
  candles: Candle[], stepSec: number, fromSec: number, toSecExclusive: number,
): Candle[] {
  const byEpoch = new Map<number, Candle>()
  for (const c of candles) byEpoch.set(Math.floor(new Date(c.t).getTime() / 1000), c)
  const first = candles[0]
  let prevClose: number | null = first ? first.o : null
  if (prevClose === null) return []
  const out: Candle[] = []
  for (let cur = fromSec - (fromSec % stepSec); cur < toSecExclusive; cur += stepSec) {
    const c = byEpoch.get(cur)
    if (c) {
      out.push(c)
      prevClose = c.c
    } else {
      out.push({
        t: new Date(cur * 1000).toISOString(),
        o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0, n: 0,
      })
    }
  }
  return out
}
```

- [ ] **Step 4: 跑测确认通过**

Run: `npm run test:unit -- src/api/__tests__/history.spec.ts && npm run type-check`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/history.ts src/api/__tests__/history.spec.ts
git commit -m "feat(chart): history 段取数层——列式解码/封存段 epoch 计算/客户端 fill，vitest 单测"
```

---

### Task 9: useCandleHistory 组装 + 图表数据源切换 + 收尾验证

**Files:**
- Create: `thccb-frontend/src/composables/useCandleHistory.ts`
- Modify: `thccb-frontend/src/composables/useMarketRealtime.ts`（暴露 `historyTail`）
- Modify: `thccb-frontend/src/components/chart/CandleChart.vue`、`thccb-frontend/src/components/chart/PriceChart.vue`（`loadFull` 换数据源）

**Interfaces:**
- Consumes: Task 8 全部导出；Task 6 的 snapshot `history_tail`；阶段 2 的 tick 帧增量逻辑（图表已有，**本 task 不动它**）
- Produces:
  - `useMarketRealtime` 返回值新增 `historyTail: Ref<HistoryTailMap | null>`（snapshot 到达时更新；切市场清 null）
  - `loadHistoryCandles(outcomeId: number, interval: ChartInterval, lookbackMinutes: number, tail: HistoryTailMap | null): Promise<Candle[]>`（`useCandleHistory.ts` 导出）——封存段（并发 fetch，双层缓存）+ 尾巴（snapshot tail 新鲜则用之；缺失/过期则回退拉老 `/api/v1/chart/candles` 只补尾巴窗口）+ 排序 + 客户端 fill 到 now；任何段请求失败 → 整体回退老端点全量拉取（保可用性）

- [ ] **Step 1: useMarketRealtime 暴露 historyTail**

`useMarketRealtime.ts`：

- import 加 `import type { HistoryTailMap } from '@/api/history'`
- `UseMarketRealtimeReturn` 接口加：

```typescript
  // snapshot 首包携带的历史尾巴（最后封存边界 → now），图表初始化用（阶段 4）
  historyTail: Ref<HistoryTailMap | null>
```

- 函数体加 `const historyTail = ref<HistoryTailMap | null>(null)`；`handleSnapshot` 内（`snapshotToken.value += 1` 之前）加：

```typescript
    historyTail.value = (evt.data as { history_tail?: HistoryTailMap }).history_tail ?? null
```

- 切市场重置块（`latestMarketStatus.value = null` 处）加 `historyTail.value = null`；return 对象加 `historyTail,`

- [ ] **Step 2: 实现 useCandleHistory.ts**

```typescript
// 图表初始数据组装（阶段 4，spec § 7.4）：
//   封存段：/history/ 不可变段（浏览器 immutable + nginx proxy_cache 双层缓存）
//   尾巴：SSE snapshot.history_tail（零额外请求）；缺失/过期回退老 chart 端点只补尾巴
//   实时：tick 帧续写由图表组件自己的增量逻辑负责（阶段 2 已有，这里不管）
// 任何 /history/ 请求失败 → 整体回退老 /api/v1/chart/candles 全量（保可用性：
// 例如 nginx 尚未上线 /history/ 转发时，SPA try_files 会把请求兜给 index.html，
// resp.json() 解析失败走 catch）。
import { chartApi } from '@/api/chart'
import {
  decodeSegment, fetchSegment, fillCandles, sealedSegmentEpochs,
  SEGMENT_SECONDS, type HistoryTailMap,
} from '@/api/history'
import type { Candle, ChartInterval } from '@/types/api'

const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '1m': 60, '15m': 900, '1h': 3600,
}

async function tailCandles(
  outcomeId: number, interval: ChartInterval, boundary: number, nowSec: number,
  tail: HistoryTailMap | null,
): Promise<Candle[]> {
  const enc = tail?.[String(outcomeId)]?.[interval]
  if (enc && enc.t0 === boundary) return decodeSegment(enc)
  // snapshot 尾巴缺失（SSE 未连上）或过期（跨过了封存边界）→ 只补尾巴窗口
  const resp = await chartApi.getCandles(
    outcomeId, interval,
    new Date(boundary * 1000).toISOString(), new Date(nowSec * 1000).toISOString(),
    false, 5000,
  )
  return resp?.candles ?? []
}

export async function loadHistoryCandles(
  outcomeId: number, interval: ChartInterval, lookbackMinutes: number,
  tail: HistoryTailMap | null,
): Promise<Candle[]> {
  const nowSec = Math.floor(Date.now() / 1000)
  const fromSec = nowSec - lookbackMinutes * 60
  const step = INTERVAL_SECONDS[interval]
  const boundary = nowSec - (nowSec % SEGMENT_SECONDS[interval])
  try {
    const epochs = sealedSegmentEpochs(interval, fromSec, nowSec)
    const segs = await Promise.all(epochs.map(e => fetchSegment(outcomeId, interval, e)))
    const sealed = segs.filter((s): s is NonNullable<typeof s> => s !== null).flatMap(decodeSegment)
    const tailPart = await tailCandles(outcomeId, interval, boundary, nowSec, tail)
    const all = [...sealed, ...tailPart].sort(
      (a, b) => new Date(a.t).getTime() - new Date(b.t).getTime(),
    )
    return fillCandles(all, step, fromSec, nowSec + step)   // 填到当前进行中的桶
  } catch (err) {
    console.warn('[useCandleHistory] /history/ 加载失败，回退老 chart 端点:', err)
    const resp = await chartApi.getCandles(
      outcomeId, interval,
      new Date(fromSec * 1000).toISOString(), new Date(nowSec * 1000).toISOString(),
      true, Math.max(50, Math.ceil((lookbackMinutes * 60) / step) + 8),
    )
    return resp?.candles ?? []
  }
}
```

- [ ] **Step 3: CandleChart.vue 切数据源**

`loadFull` 中从 `const resp = await chartApi.getCandles(` 到 `if (!resp || !resp.candles) { candleCount.value = 0; return }` 及其后 `[...resp.candles].sort(...)` 的取数三行替换为：

```typescript
    const raw = await loadHistoryCandles(
      props.outcomeId, props.interval, Math.max(5, lookbackMinutes.value),
      realtime?.historyTail?.value ?? null,
    )
    const candles = [...raw].sort((a, b) => toUtcTimestamp(a.t) - toUtcTimestamp(b.t))
```

import 区把 `import { chartApi } from '@/api/chart'` 换成 `import { loadHistoryCandles } from '@/composables/useCandleHistory'`（chartApi 若无其他引用则删）。`loadFull` 其余逻辑（renderFull / forming candle / 可视窗）一行不动；tick 帧增量 `applyTrade` 与 `gapToken → loadFull()` 不动——gap reconcile 重跑 `loadFull` 时封存段全部命中浏览器缓存，实际只回源尾巴。

- [ ] **Step 4: PriceChart.vue 同款切换**

`loadFull` 中 `const resp = await chartApi.getCandles(...)` 与 `const candles = resp.candles` 替换为：

```typescript
    const candles = await loadHistoryCandles(
      props.outcomeId, props.interval, lookbackMin,
      realtime?.historyTail?.value ?? null,
    )
    if (!candles.length) { pointCount.value = 0; return }
```

（`lookbackMin` 变量已有；import 同 Step 3 调整。）后续 `pointCount/firstPrice/lastPrice/data 组装` 逻辑不动。

- [ ] **Step 5: 前端全套验证**

Run（`thccb-frontend/`）: `npm run test:unit && npm run type-check && npm run lint`
Expected: 全 PASS

- [ ] **Step 6: 浏览器实测（UI 改动必跑）**

起本地后端（writer flag 开）+ `npm run dev`，实测并记录：
1. 图表初载：K 线 / 价格走势正常渲染，Network 面板可见 `/history/o/...json` 请求 + snapshot 尾巴无额外请求
2. 切 interval（10s/1m/15m/1h）与切 outcome：数据正确重载
3. 成交后 tick 续写：forming candle 实时更新
4. 断线重连（DevTools offline 再恢复）：gap reconcile 后图表恢复，封存段走缓存（from disk cache）
5. `/history/` 不可达时（临时改 baseUrl 端口模拟）：图表回退老端点仍可用，console 有降级 warning
6. 移动端宽度 + 空数据市场边界态
环境起不来 → 在收尾报告写「未实测 UI」，不得谎称通过。

- [ ] **Step 7: 后端全量回归 + Commit**

Run（`backend/`）: `python -m pytest -q`
Expected: 全 PASS（含既有 1 个已知过期 fail 则按 memory 记录说明）

```bash
git add src/composables/useCandleHistory.ts src/composables/useMarketRealtime.ts src/components/chart/CandleChart.vue src/components/chart/PriceChart.vue
git commit -m "feat(chart): 图表改吃不可变段+snapshot尾巴+tick续写——/history/ 失败回退老端点"
```

---

## 收尾清单

- [ ] 后端全量 `python -m pytest -q` + 前端 `test:unit` / `type-check` / `lint` 全绿
- [ ] 浏览器实测记录（Task 9 Step 6 六项）或「未实测 UI」声明
- [ ] 输出部署清单（Task 7：缓存目录、nginx -t + reload、顺序约束）
- [ ] 一句话汇报：改了什么 / 分支 `perf/2026-08-21-single-writer` / 验证结果 / 未决风险（nginx 未本机验证、部署顺序约束）
