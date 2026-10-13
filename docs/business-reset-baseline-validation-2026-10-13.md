# 业务重置与新基线验证记录（2026-10-13）

本记录对应独立分支 `feat/business-reset-baseline`，首次集成检查起点 `3b46857`，修复后全套起点 `50c40be`。执行范围是隔离 worktree、现有后端检查和归档核对；没有执行生产预览、远端部署或生产数据库操作。政策与操作阶段见[业务重置操作单](business-reset.md)。

## 本轮集成检查

命令在 `backend/` 执行，使用已有 venv，Python 3.13.2；没有安装依赖。与 `.github/workflows/ci.yml` 保持相同 compile/import 模块范围、FX 独立入口和测试超时。

| 检查 | 实际命令或范围 | 结果 |
| --- | --- | --- |
| CI 编译 | `venv/bin/python -c`，按 CI 的 `glob.glob('app/**/*.py', recursive=True)` 逐文件 `py_compile.compile(..., doraise=True)` | 128 个文件编译成功，exit 0 |
| CI 导入 | `venv/bin/python -c`，导入 settings、OIDCClient、LMSR cost/quantize、User/Market/Outcome/Position/Transaction、交易及用户 schemas、auth/user/market/chart API | All module imports OK，exit 0 |
| 独立 FX HTTP | `timeout 180 venv/bin/python -m pytest -q --noconftest tests/test_fx_end_to_end.py` | 14 passed，2.55s，exit 0 |
| 后端全套（首次） | `timeout 300 venv/bin/python -m pytest -q` | 1 failed、1431 passed、1 skipped、41 deselected；243.44s，exit 1（原因及后续见下） |
| 后端全套（`50c40be` 后） | 同一命令，仅因真实日志修复复测一次 | 1432 passed、1 skipped、41 deselected、1 warning；241.87s，exit 0 |

本轮编译与导入的实际 Python payload（各以 `venv/bin/python -c` 执行）：

```python
import py_compile, glob
files = glob.glob('app/**/*.py', recursive=True)
[py_compile.compile(f, doraise=True) for f in sorted(files)]
print(f'All {len(files)} files compiled OK')
```

```python
from app.core.config import settings
from app.core.oidc import OIDCClient
from app.services.lmsr import calculate_lmsr_cost, quantize_cost, quantize_price
from app.models.base import User, Market, Outcome, Position, Transaction
from app.schemas.market import TradeRequest, QuoteRequest, ResolveRequest
from app.schemas.user import UserSummary, HoldingRead
from app.api.v1 import auth, user, market, chart
print('All module imports OK')
```

导入命令有开发配置缺少 SECRET_KEY/Casdoor 的提示，没有输出随机密钥或凭据。默认 pytest 配置排除 `pg`；全套通过不能替代真实 PostgreSQL 或生产验收。原始 stdout 留在忽略的私有 SDD 日志目录，不进入 Git。

## 集成失败的定位

首次全套唯一失败是 `tests/test_credit_execution.py::test_finite_version_conflicts_exhaust_without_mutation_then_resume` 的日志断言：预期四条 `economic_version_drift`，实际 caplog 为空；此前的 retry_exhausted 返回与尝试次数断言通过。

- `timeout 60 venv/bin/python -m pytest -q tests/test_credit_execution.py::test_finite_version_conflicts_exhaust_without_mutation_then_resume`：1 passed，0.40s，exit 0。
- `timeout 60 venv/bin/python -m pytest -q tests/test_admin_system.py::test_system_operations_require_admin tests/test_credit_execution.py::test_finite_version_conflicts_exhaust_without_mutation_then_resume`：1 passed、1 failed，0.63s，exit 1，复现相同原断言。
- 单独导入 execution 后调用 `logging.config.fileConfig('alembic.ini')`，实际 execution logger 的 `(disabled, propagate)` 从 `(False, True)` 变为 `(True, True)`。新增真实 Alembic client fixture 的注入 Connection 路径触发 env.py 全局 fileConfig，禁用已存在业务 logger。

Task 2 所有者在 `50c40be` 修复：有注入 Connection 时保留 caller 全部 logging 配置；独立 Alembic CLI 继续按 ini 配置日志。定向 medium review Approved。Task 5 未改金融代码或放宽日志断言。

修复者执行相同有序命令 2 passed、0.56s；`venv/bin/python -m pytest tests/test_credit_migration.py tests/test_credit_startup_gating.py -q` 22 passed、5.41s；实际内存 baseline 升级前后每个已有 logger 的 disabled/level/handlers/propagate 完全不变，普通 SQLite Alembic CLI 仍有正常 INFO 升级日志，均 exit 0。Task 5 直接因这项真实改动进行一次全套复测，不重复上述定向检查。

修复后 `timeout 300 venv/bin/python -m pytest -q`：1432 passed、1 skipped、41 deselected、1 warning，241.87s，exit 0；没有触发 300 秒外层超时。原始首次失败与修复后完整 stdout 分别保留，不覆盖。全套另有 `test_fx_short_player_admission.py::test_foreign_only_short_blocks_lmsr_writer_buy` 的非归还 aiosqlite connection GC warning，未观察到对应行为失败；未扩展到无关连接生命周期重构。

## 真实数据库证据

以下是 Tasks 2–4 的既有实际执行证据，本轮未重复运行 PostgreSQL 矩阵。数据库均为父任务临时本机 PostgreSQL 集群的明确一次性测试库；此文档不保存连接身份或保留账号清单。

- Task 2 SQLite：`backend/venv/bin/python -m pytest -c backend/pytest.ini backend/tests/test_credit_migration.py backend/tests/test_credit_startup_gating.py backend/tests/test_credit_audit.py backend/tests/test_site_config.py -q`，35 passed，9.31s，exit 0。实际标准 Alembic baseline、空库 roundtrip、模型写入、外键/约束、非空 downgrade 拒绝和数据保留；临时未来增量 revision 实际升级，旧 head 启动拒绝、升级后启动接受。
- Task 2 PostgreSQL：显式一次性 `TEST_PG_DATABASE_URL` 下 `venv/bin/python -m pytest -m pg tests/pg/test_credit_pg.py -q`，4 passed，4.47s，exit 0。实际完整 baseline 的共享 Connection 升级、空库 roundtrip、ORM/FK/约束行为和非空 downgrade 保留资金。正常 settings-based SQLite `python -m app.core.schema` → empty、`alembic upgrade head` → 成功、schema CLI → upgradeable，均 exit 0。
- Task 3 最终 SQLite：`venv/bin/python -m pytest tests/test_business_reset.py -q`，2 passed、1 deselected，5.21s，exit 0。真实 baseline 导入后逐字段核对身份/完整称号、源 initial_balance、零债务、经济版本递增、五项关闭开关与冻结开关、兑换及其他业务表清空、历史整数 allocator 高水位、新审计锚点及 replay/live 一致；同时覆盖来源/目标混同、非空及 view-only 目标、保留表未知字段/缺失 FK、金额/版本错误、私有导出与事务中断回滚拒绝。
- Task 3 最终 PostgreSQL：显式一次性 `RESET_TEST_PG_SOURCE` / `RESET_TEST_PG_TARGET` 下 `venv/bin/python -m pytest -m pg tests/test_business_reset.py -q`，1 passed、2 deselected，3.07s，exit 0。实际 PG 空 baseline、完整导入/保留/清空/sequence 水位和审计核对，包含修正后的空 schema 与严格 retained-schema 守卫。
- Task 4 独立真实 PG probe：仅对新建一次性目标设置数据库级 `default_transaction_read_only`，改名后新连接仍为 on，实际 INSERT 被 `ReadOnlySQLTransactionError` 拒绝；管理数据库未改变。恢复原数据库级设置后，新 owner 连接 INSERT 成功。两个 probe 库清理，父集群保持运行。
- Task 4 对 Task 3 实际目标运行 `venv/bin/python scripts/audit_verify.py`（显式测试 DATABASE_URL）：events=30、last_id=129、users=2（anchored=2）、markets=0、positions=0，OK，exit 0。同一个 async wrapper 完成校验和 dispose，原跨 event-loop 清理异常消失。

schema 测试覆盖真实 DDL、模型兼容、索引/FK/约束和关键 credit defaults 行为。现有 metadata helper 未开启全量 server-default 比较，check-constraint 循环只比名称；Task 2 medium review 已检查当前 DDL 并将这一精度改进列为非阻塞 Minor。本记录不将该 helper 描述为逐表达式、全 default 的法证 schema 等价验证。启动/部署 classifier 检查 revision、祖先链及最低表列完整性，也不承担任意索引/约束损坏审计。

## 当前入口与归档

本轮 `rg` 检查 `backend/app`、active Alembic、scripts、init_db、deploy、workflows 与 compose 的退休配置/转换/重建名称。没有旧工具 import 或调用；命中的 `last_trade_id` 是 `FxCandle` 的当前成交来源字段和对应 frozen DDL，按设计保留。当前 FxTrade、成交 receipt、API trade 引用和池事件行情仍在。历史计划、验证记录及归档中的旧名称用于追踪，不属于当前执行入口。

本轮 Python 校验将归档中的 37 个完整源文件逐字节与旧生产 SHA `04c9e7ad2081337677cd4a1e82515c4a9849f76e` 比较，并确认对应 active 文件缺席：全部通过，exit 0。包含 25 个历史 revisions、旧配置模块、8 个旧 scripts、旧 workflow/shell/runbook。历史测试 `.py.txt` 的原文及摘录来源在 Tasks 1/2/4 另行核对；不在 pytest 收集路径。旧库恢复使用完整旧代码与不可变镜像，不能用新 baseline 自动升级旧 revision。

[当前 scripts README](../backend/scripts/README.md) 有 9 项真实工具，含 business_reset；[文档索引](README.md)、[归档索引](archive/database-pre-baseline-2026-10-13/README.md)和 scripts README 的本地相对链接均经本轮解析存在，exit 0。常规 season_reset 与 LMSR 两个修复工具仍保留。

Task 4 `backend/venv/bin/python -m pytest backend/tests/test_business_reset_runner.py -q` 最终 8 passed，10.19s，exit 0；`bash -n deploy/business_reset.sh` 与 diff 检查 exit 0。shell 用例运行实际生产 shell、用假 Docker/sudo/curl 验证外部协议与失败阶段边界；它们不能证明远端 Docker/Compose、恢复或生产切库成功。workflow YAML/内嵌 Python 已解析通过，actionlint 未安装，未声称运行。

## 前端、审阅与执行限制

本轮 `git diff --name-only 663ddad..HEAD -- thccb-frontend` 无输出，前端没有变化。复用[上一轮前端验证](fx-admin-events-validation-2026-10-13.md)：实际 `npm run type-check`、`npm run build` 均 exit 0，相关事件/蜡烛/流单元命令 53 passed；其 build 保留 empty charts、auth 混合导入及大 chunk 提示。此轮未运行前端构建或浏览器；实际 PR CI 仍须运行 frontend job，不能以旧证据声称未来 CI 已通过。

Tasks 1/2 medium review Approved，Tasks 3/4 修正后 medium re-review Approved，Task 2 集成日志修复的定向 medium review Approved，无未处理 Critical/Important；Task 2 上述 Minor 已说明。Task 5 和整分支 medium review 由父任务随后执行，此记录不能替代它们。

尚未执行实际 Docker image pull/save/run、Compose 停写/健康/数据库改名/pg_dump+pg_restore、远端 SSH/artifact、真实 sudo 缓存权限、生产预览与生产重置，也未运行正常 psycopg2 PG Alembic CLI（现有 venv 缺该 driver，真实 PG 验证走受支持的 asyncpg shared Connection）。生产仍须核对唯一 writer、源金额、不可变构建 SHA、恢复备份和目标验收；任何不一致按操作单中止。未读取生产环境文件、凭据、身份清单或兑换码；没有 push、merge、部署或原用户工作区修改。
