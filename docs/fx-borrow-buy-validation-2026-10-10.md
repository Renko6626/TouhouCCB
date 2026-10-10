# FX 借款并买入验证记录（2026-10-10）

最终集成基线：`origin/main` 的 `7d46d7c`。初始工作区是较旧的 `b296972`；部署准备时将本次补丁移入独立主线目录，保留纯 AMM、统一信贷唯一模式、现有行情与后台修复。

## 功能与边界

- 新增已认证融资报价和原子成交接口，金额明确拆成现金和新增本金，借款与买入一次提交。
- 复用统一信贷完整交易后准入，融资用估值结果直接投影，不再独立重复估值；普通无债现金路径保持。
- FX 原有请求编号唯一约束扩展到融资用途，原请求回放不重复借款、买入或计息。新本金从本次操作时点开始计息。
- 真实 FX 做多页提供融资开关、本金输入、成交后债务/风险、回执及融资历史；未知结果保留原编号，恢复前阻止新交易。
- 融资请求绑定发起账号，切换账号后禁止自动刷新重发；旧共享认证刷新不能覆盖新账号凭证。
- 迁移前驱为 `unified_credit_only_20261010`，有融资历史时拒绝 downgrade，保留成交身份。提交后沿用主线行情通知和发布机制。

## 最终检查

| 检查 | 实际结果 |
| --- | --- |
| 最新主线融资/迁移/FX/信贷相关回归 | 69 passed，11.72 秒 |
| 前端完整单测 | 16 文件，174 passed |
| 前端生产构建 | exit 0；保留既有大 chunk 提示 |
| PostgreSQL 并发 | 11 passed，8.14 秒 |
| 完整后端默认回归 | 1406 passed，1 skipped，34 deselected，226.43 秒；1 条既有连接回收警告 |

命令在 `/tmp/sunyunbo/thccb-fx-borrow-buy-integration` 下执行：

```bash
# backend，SQLite 路径均为独立临时库
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-borrow-main-tests.db venv/bin/python -m pytest -q tests/test_fx_borrow_buy.py tests/test_fx_migration.py tests/test_fx_trading_service.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py tests/test_credit_risk.py
DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-fx-borrow-main-full.db venv/bin/python -m pytest -q
TEST_PG_DATABASE_URL=postgresql+asyncpg://sunyunbo@127.0.0.1:55463/thccb_fx_borrow_main_test venv/bin/python -m pytest -q -m pg tests/pg/test_pg_fx_short_concurrency.py
venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py
# thccb-frontend
npm run test:unit
npm run build
```

真实数据库覆盖成功资金拆分、旧债计息、风控/滑点/冻结/锁金/同币种空头失败后完整回滚、同编号并发回放和不同编号竞争剩余保证金。HTTP 覆盖金额校验、字符串金额响应、个人融资历史和公开流隐私。前端覆盖请求持久化恢复、存储失败不发送、账号切换的旧响应和共享认证重试。

初始工作区的定向只读 ESLint 通过；共享 `api/index.ts` 保留 10 项原有 no-explicit-any 错误，与原版按规则/消息/严重程度比较无新增。未为清理这些旧问题扩展改动。

## 初始方案性能测量（历史，不代表最终主线生产指标）

在初始工作分支上，用独立 PostgreSQL 14 可丢弃库比较旧两步提交和新原子提交。每路径预热 5 次、测 30 次，直接调用真实 handler/service；不含 HTTP、认证网络或生产流量。

| 路径 | SQL 中位数 | 中位数 ms | 样本 P95 ms |
| --- | ---: | ---: | ---: |
| 两步借款再买入，无空头 | 43 | 32.860 | 37.151 |
| 原子借款买入，无空头 | 29 | 20.558 | 23.686 |
| 两步借款再买入，3 个空头 | 45 | 40.890 | 48.627 |
| 原子借款买入，3 个空头 | 30 | 22.073 | 24.362 |
| 融资报价，无/3 空头 | 14 / 15 | 9.297 / 10.339 | 10.362 / 10.775 |
| 普通现金买入，原/新 | 12 / 12 | 9.529 / 9.480 | 10.310 / 9.669 |

同 pair 连续 30 笔现金交易，叠加 30 笔融资报价和 30 笔融资成交：两步/原子流程下，现金样本 P95 为 31.022/40.166ms，现金门闩等待 P95 为 15.138/20.974ms；完成全部操作的墙钟时间为 1780.047/1380.968ms。融资整体少了独立提交，但单次独占锁时间较长，现金请求等待增加。不能据此宣称所有混合负载均无退化。

脚本和原始数据在 `/tmp/sunyunbo/fx-borrow-buy-perf.py`、`fx-borrow-buy-baseline-trading.py`、`fx-borrow-buy-perf-result.json`。该对照发生在初始分支，主线已含其他性能改动；没有把这些样本冒充最终部署版本的 P95 或容量证明。

## 未验证范围与发布

未自动点击按钮、填写表单或执行浏览器交易流程；未进行真实 SSO 或生产账户资金操作，也未做生产规模全站压测。测试通过不等于这些场景已验收。

用户明确授权提交、推送及自动部署。沿用 main 的现有 CI/CD；发布结果以本次提交对应的 Actions 运行和远程健康检查为准。
