# Wavelength 观众竞猜接入计划

**目标：** Wavelength 服务端通知题目、封盘与结果；THCCB 保存观众竞猜并按误差奖励金圆券。

**当前范围：** 完成 THCCB 后端、数据库迁移、管理员配置页，以及 Wavelength 控制器配置与通知发送端；观众竞猜页面另一步实施。

**现有技术：** FastAPI、SQLModel/SQLAlchemy、Alembic、PostgreSQL、现有账户通知及审计；不新增服务或依赖。

## 已讨论的规则

- 每道题独立竞猜；出题结束后开放，台上锁定拨杆时封盘，揭晓时结算。
- 玩家登录 THCCB 提交；封盘前允许修改，最后一次成功提交生效。
- 每轮首次提交投入 100 金圆券，成功扣款才算参与；封盘前修改位置不重复扣款，同一用户每轮只有一份竞猜。
- 结算到账总额依次为 300 / 200 / 150 / 75 / 0 金圆券，对应净盈亏 +200 / +100 / +50 / -25 / -100；不另退本金。
- 奖励进入普通现金，不主动还款，仍受现有强平规则约束。取消未结算轮次时退回已投入的 100；重复取消不重复退款。
- 隐藏答案只在结果通知中传给 THCCB，观众不连接 Wavelength 的 SSE。
- 同轮重复结果通知不能重复发钱。

## 已确认的具体选择

1. 接口、两张业务表、两端开关方案及先交付 THCCB 后端的顺序已获确认。
2. 投入 100 及结算到账金额 300 / 200 / 150 / 75 / 0 已确认。
3. 区间已确认固定为 1.6 / 5.6 / 10 / 15 个百分点，即误差上限 16 / 56 / 100 / 150（位置单位 0–1000）；边界取较高奖励。不随 Wavelength 色带设置变化。

## 开关与密钥（按用户新确认方案实施）

- 两端均在管理员/控制器页面配置开关、密钥及对方站点地址，默认关闭，保存立即生效，不使用联动环境变量。
- THCCB 复用私有 SiteConfig 数据项，密钥只保存 SHA-256 摘要，配置读取仅返回 secret_configured；独立管理员接口避免通用配置列表/日志泄露。Wavelength 地址仅用于管理页跳转。
- Wavelength 复用服务端 config.json 的 thccb 配置，保存明文外发密钥，必须拦截静态访问配置/会话/Git 等内部文件。现有共享密码非空且设备登录后才允许配置。
- 密钥至少 32 字符，留空保存保留原值；从不回传已保存密钥。两端使用相同密钥。
- 有未结束竞猜时禁止修改开关、地址和密钥，先结算或取消。THCCB 管理页提供取消并退款入口，复用已有 finish_round 服务供意外重启后的人工处理，不引入恢复状态机。

## 服务端通知接口

统一前缀 `/api/v1/wavelength`。位置采用与 Wavelength 一致的整数 0–1000，页面可转换为 0–100%；奖励按绝对位置误差计算，避免百分比四舍五入影响结果。

| 方法与路径 | 内容与行为 |
|---|---|
| `POST /rounds/start` | `round_key`（全局唯一字符串）、`game_key`、`round_number`、左右概念、当前队名；创建开放轮次，不接收答案 |
| `POST /rounds/{round_key}/close` | 停止接收或修改竞猜；重复关闭返回当前结果 |
| `POST /rounds/{round_key}/result` | `target_position`；首次通知结算，重复相同结果返回原结算摘要，不同结果返回 409，禁止自动改账 |

- 相同编号、相同题目信息的重复 start 返回原轮次；信息冲突返回 409。
- 同时只允许一个未结束轮次；不能静默覆盖旧轮次。
- 未封盘的轮次拒绝公布结果，提示先调用 close；不能隐式让漏掉的封盘步骤变成可继续提交的竞猜。
- 结果响应包含轮次状态、参与人数及奖励总额，不向 Wavelength 返回玩家账户明细。
- 每轮保存当轮奖励规则，避免改配置后已开始的题目使用另一套额度。
- `POST /rounds/{round_key}/cancel` 仅取消未结算轮次，同事务退回参与者投入；不设计撤销已发奖励的流程。

## THCCB 玩家查询与提交

- `GET /current`：当前或最近一轮公开信息；无轮次时返回空；未结算不返回答案。
- `GET /rounds/{round_key}/me`：本人竞猜位置、投入、是否结算、误差、结算到账金额及净盈亏。
- `PUT /rounds/{round_key}/prediction`：登录账号提交 `position`；行锁内确认仍开放，首次提交扣 100，后续仅修改位置；余额不足或既有现金支出风控不通过时不保存参与记录。
- `GET /stream`：仅通知轮次开始、封盘、取消和结算；不广播玩家竞猜或余额。
- 初次进入、SSE 重连后查询当前轮次和本人记录。订阅建立后补查一次状态，避免查询与订阅之间错过变更。
- 结算提交后才发送 SSE 和现有账户变化通知；掉线不影响发钱。

## 数据与资金边界

只增加两张业务表：

- `WavelengthRound`：唯一外部轮次编号、游戏/题目信息、状态、投入与奖励规则、答案、开始/关闭/结算时间。
- `WavelengthPrediction`：轮次、用户、最终猜测、投入、提交时间、误差、结算到账金额；轮次与用户联合唯一。取消是否退款由轮次状态及同事务资金记录确定，不新增恢复流程。

首次提交锁轮次及用户，在现有 `check_cash_spend` 规则下扣 100、记录投入与审计并更新经济版本。允许有债用户在满足现有支出风控时参与，不照搬兑换中心“有债禁止”的限制；不自动借款凑足投入。

初版按一轮一个数据库事务结算：锁轮次，按用户 ID 顺序锁参与账户，计算奖励并增加 `User.cash`，写预测奖励、资金流水和审计，递增经济版本后提交。复用既有经济写所有权保护。失败整轮回滚，操作者可重复通知；不增加任务队列或恢复状态机。

增加专用 `wavelength_stake`、`wavelength_reward`、`wavelength_refund` 流水与审计类型，并在审计重放中校验对应现金增减。只允许后端按已保存竞猜与规则算金额，外部通知不提供用户或发钱金额。零奖励保存竞猜结果但不写无金额资金流水。取消按用户 ID 顺序锁账户并原额退款，重复取消直接返回原状态。

## Wavelength 推荐调用位置

在 `server.js` 的 `/api/intent` 服务端处理中识别实际状态变化，不放在 `app.js` 按钮回调或 `game.js` 纯逻辑引擎里：

- `donePsychic` 成功进入 `dial`：选定题面已稳定，发送 start。
- `lock` 从 `dial` 进入 `guess`：发送 close。
- `reveal` 实际产生该轮结果：发送 result，目标取 `state.target.center`。
- 新游戏或跳过未结束的题目：取消旧竞猜，下一次出题结束时创建新的唯一轮次。

发送端实施时需保证同轮顺序；封盘未被 THCCB 确认前，控制器应得到明确失败提示并停止揭晓。采用超时提示和人工重试，不自动引入持久化补发队列。重试必须使用原 round_key，不能因重试生成新轮次。

## 实施任务

- [x] 数据与配置：新增 `backend/app/models/wavelength.py`、对应 Alembic 迁移；接入私有 `SiteConfig` 配置、模型注册与管理员接口。
- [x] 结算服务：新增 `backend/app/services/wavelength.py`；在 `models/ledger.py`、`models/audit.py`、`services/ledger_service.py`、`services/audit_replay.py` 接入奖励类型。
- [x] 接口及通知：新增 `backend/app/schemas/wavelength.py`、`backend/app/api/v1/wavelength.py`，在 `main.py` 挂载；使用现有 SSE 编码方式和账户提交后通知习惯。
- [x] 接入文档：记录完整请求响应、配置、调用时机、单位及 HTTP 错误，明确必须先封盘再公布结果。
- [x] 集中验证：复用现有数据库夹具，验证关闭/无密钥不能写、首次扣款与改猜不重复扣款、余额及支出风控、开放与封盘、奖励边界、重复结果不二次入账、取消只退一次、事务失败无部分发钱、资金审计与实时表一致。迁移检查升级与回滚，不固定迁移版本号。

不访问生产库，不部署，不执行浏览器自动交互。仅运行受影响的后端检查及必要迁移验证；页面实施时再安排构建和截图。

## 实施记录

- 已在 `feat/wavelength-predictions` 分支完成 THCCB 后端及管理员配置页；未部署。接入文档见 `docs/wavelength-integration.md`。
- 验证命令（backend 目录，独立测试库）：`DATABASE_URL=sqlite+aiosqlite:////tmp/thccb-wavelength-check.db pytest -q tests/test_wavelength.py tests/test_wavelength_migration.py tests/test_audit_events.py tests/test_fx_audit_replay.py tests/test_fx_short_audit_replay.py tests/test_account_events.py tests/test_season_reset.py`。结果：36 passed。
- “账户通知”复用现有 account_changed，仅提醒页面查询余额；不参与资金事务，也不新增资金恢复机制。
- 现已补充两端页面配置和 Wavelength 发送端；观众页面和生产迁移尚未执行。

### 页面配置与发送端收尾

- THCCB 使用 GET/PUT `/api/v1/admin/wavelength/config` 保存启用状态、密钥摘要和 Wavelength 地址；新增管理员取消退款入口。
- Wavelength 在 `feature/thccb-bridge` 分支完成控制器配置、4种通知、同轮手动重试与静态配置保护。
- 最终后端相关检查 39 passed（竞猜、页面配置、迁移、审计导出及重放、账户通知、赛季重置）；前端 `npm run build` 通过。Wavelength logic/dom/server 检查通过。
- 临时数据库下真实两端 HTTP 联调通过：配置、开盘、扣款、封盘、结果到账、取消退款。未运行浏览器自动交互，未触碰生产数据库。
