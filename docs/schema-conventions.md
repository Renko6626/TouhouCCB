# Schema 与前后端契约工程约定

> 项目踩过的坑总结成规则。新加字段/接口前看一眼，避免重复犯错。

## 1. Decimal 字段在响应 schema 里必须用 `Money` / `Price`

### 背景

Pydantic v2 默认对 `Decimal` 字段 JSON 序列化为 **string** (`"0.2"`)，**不是 number**。
前端 TS 如果标 `number` 直接调 `.toFixed()` / 算术运算 → `TypeError: x.toFixed is not a function`。

真实事故：[`dfb4a4d`](https://github.com/Renko6626/TouhouCCB/commit/dfb4a4d) 修 `MarginCallBanner` 报错。
`UserSummary.margin_ratio: Optional[Decimal]` → JSON 输出 `"-0.399990"` → 前端 `formatRatio(r)`
里 `r.toFixed(3)` 在 string 上炸。

### 规则

后端 schema (`backend/app/schemas/`) 中所有 **响应给前端的 Decimal 字段**：

| 字段语义 | 推荐类型 | 序列化结果 |
|---|---|---|
| 资金、份额（6 位小数） | `Money` = `Annotated[Decimal, PlainSerializer(float)]` | JSON number |
| 价格（8 位小数） | `Price` | JSON number |
| 比率（margin_ratio 等） | `Money` 或新建 `Ratio` annotation | JSON number |
| 可空版本 | `Optional[Money]` (验证过 None → null) | number 或 null |

**不要直接用 `Decimal`** 除非：
1. 前端 TS 明确标 `string` 且**所有消费点都用 `Number()` 包裹**
2. 字段不显示给用户（例如内部 API、admin debug endpoint）

### 自检脚本

新加响应字段后：

```bash
# 1. 实际 JSON 输出
cd backend && python -c "
from app.schemas.user import UserSummary
from decimal import Decimal
# 构造一个含目标字段的实例
r = UserSummary(...)
print(r.model_dump_json())
"

# 2. 验证前端消费
grep -rn "summary\.<your_field>\|holding\.<your_field>" thccb-frontend/src/
# 看有没有直接 .toFixed / 算术运算的调用
```

### 已知 raw Decimal 字段（合规的）

以下 schema 字段是 raw `Decimal` 但**安全**（前端 TS 标 string + `Number()` 包裹消费）：

- `LoanQuotaResponse.cash/debt/net_worth/leverage_k/daily_rate/max_borrow`
- `LoanActionResponse.cash/debt/max_borrow/effective`

请求体 schema（`BorrowRequest.amount` 等）也是 raw Decimal，无前端消费风险。

## 2. 函数返回 dict 时所有早返回路径字段集必须一致

### 背景

`liquidation_sweep.run_liquidation_sweep_once()` 之前有个 bug：「无候选用户」早返回 dict 漏
了 `recovered_count` 和 `skipped_count` 字段。下游 ELK/Prometheus exporter 依赖这俩字段，
sweep 在某个空 tick 上会让 dashboard 报错或漏数据。

详见 commit 修复的 docstring。

### 规则

函数返回 dict 给监控/日志/前端时：

1. **在 docstring 里写明"完整字段集"**
2. **所有 return 路径**（成功、早返回、错误恢复）返回**相同字段集**
3. 数值字段缺省值用 `0` 而不是省略
4. 测试要覆盖每条早返回路径并断言字段集完整
5. 多个 return 点时考虑抽 `_empty_result()` helper 共享

### 反例

```python
async def sweep() -> dict:
    if not candidates:
        return {"triggered_count": 0, "duration_ms": x}   # ← 漏 recovered_count
    ...
    return {"triggered_count": t, "recovered_count": r,
            "skipped_count": s, "duration_ms": x}
```

### 正例

```python
def _empty_result(duration_ms: int) -> dict:
    return {"triggered_count": 0, "recovered_count": 0,
            "skipped_count": 0, "errors": 0, "deadlocks": 0,
            "soft_warning_count": 0, "sweep_duration_ms": duration_ms}

async def sweep() -> dict:
    if not candidates:
        return _empty_result(duration_ms)
    ...
```

## 3. 前后端口径分裂的字段命名

详见 `docs/holdings-value-semantics.md`。核心规则：

- 同一概念存在多种语义（如 MTM/LCV、账面/清算）时，**字段名带后缀显式标注**
- 主字段是用户最常看到的口径，副字段加 `_liquidation` / `_safe` / `_mtm` 等明确后缀
- 文档里给出"哪个场景用哪个"决策表

## 4. 错误捕获要精确，不要 `except Exception`

### 反例

```python
try:
    hard_thr = Decimal(cfg["liquidation_hard_threshold"])
    soft_thr = Decimal(cfg["liquidation_soft_threshold"])
    rate = Decimal(cfg["loan_daily_rate"])
except (KeyError, Exception):   # ← Exception 已包含 KeyError + 还吞所有
    return {"skipped": "config_parse_failed"}
```

`Exception` 会吞掉 `TypeError`、`AttributeError` 等编程 bug，让真问题变成"安静 skip"。

### 正例

```python
except (KeyError, InvalidOperation, ValueError):
    logger.exception("config_parse_failed", extra={...})
    return {"skipped": "config_parse_failed"}
```

只捕获你**预期**的失败类型。意外类型让它继续往上抛，监控/sentry 能看到。

## 5. 配置缺失要 ERROR 级别 log，不要静默 skip

如果一个本来该存在的 site_config 行/环境变量缺失，应该：

- **ERROR-level log** 让运维收到告警
- skip reason 写明具体哪个 key 缺（不是笼统 `"config_parse_failed"`）
- 跟"value 解析失败"分两种错误码

详见 round-2 review I-2 follow-up。

## 历史相关

- [`dfb4a4d`](https://github.com/Renko6626/TouhouCCB/commit/dfb4a4d) — Money fix 起源
- `docs/holdings-value-semantics.md` — 双口径分工详解
- `docs/liquidation-perf-options-2026-05-18.md` — perf 改造档案
