# FX 兼容代码清理计划

**目标：** 删除用户已批准的五类废弃／重复实现，保留当前交易与真实成交行情行为。

**范围与约束：** 沿用现有 Vue/TypeScript 与 Python 服务；不改接口、存储、订单恢复策略或模块边界。保留仍使用的独立回补入口及旧订单恢复。用户现有未提交内容不纳入本次修改。不新增证明删除的测试，不执行浏览器交互。

- [x] 删除图表 tick prop、监听器、引擎 applyPrice 及仅覆盖旧链的测试。
- [x] 删除历史点数组兼容包装和未使用的有状态 composable；保留带 coverage 的加载入口及其测试。
- [x] 删除无生产调用的报价 hooks、融资表单、旧提交器及对应测试；将仍使用的短报价类型留在页面。
- [x] 删除 FxStream 接收函数的重复解析 fallback，保留公开解析器及字段过滤。
- [x] 开空账务复用已有 `_settle_interest_at_T`，保留审计快照和目标仓位起息基点重置。
- [x] 集中运行相关已有前端测试、构建及后端 short/net 交易测试，检查 diff。
- [x] 提出三套订单恢复类的简化共用方案；用户确认后进入下述恢复合并。

**验收：** 真实成交 K 线、coverage 历史加载、现行本地交易预览、回补与原订单恢复入口保留；旧路径无残留调用；既有账务断言保持。

## 本次验证

在 `thccb-frontend` 执行：

```sh
npm run test:unit -- src/utils/__tests__/fx.spec.ts src/utils/__tests__/fxCandle.spec.ts src/utils/__tests__/fxHistory.spec.ts src/utils/__tests__/fxStream.spec.ts src/api/__tests__/fxBorrowBuy.spec.ts src/stores/__tests__/tradingSession.spec.ts
npm run build
```

6 个测试文件通过；构建通过，保留空 charts chunk、auth 静态/动态导入及大 chunk 提示。

在 `backend` 执行：

```sh
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/thccb-fx-compat-cleanup-tests.db venv/bin/python -m pytest tests/test_fx_short_trading.py tests/test_fx_net_trading.py -q
```

首次在沙箱内初始化 SQLite 超时，未执行交易断言；获准在沙箱外重跑同一命令后全部通过。测试使用 `/tmp` 隔离库；未验证 PostgreSQL 并发或生产数据。

`git diff --check` 通过。独立只读复核未发现生产引用残留、真实成交／coverage 路径回归或利息审计语义变化。未新增永久测试、运行浏览器交互、提交或部署。

## 已确认的恢复合并

用户确认一个共用实现、三个薄适配器，以及核对成交和账户后清除本地记录的入口。共用 `api/fxPendingOrder.ts` 负责保存、提交、手动重试、清理和账号切换；各适配器保留请求校验、旧存储前缀与路由。状态收敛为 pending、busy、storageError；不增加服务端接口、队列或自动重试。

- [x] 以现有 net 恢复测试覆盖新增人工清除：损坏记录解除、清除失败继续阻塞、成功回执不被清理错误掩盖、在途请求不允许清除。先运行，确认四项失败后实施。
- [x] 三适配器复用共用类，保留旧请求形状和同编号重试；页面统一投影待确认状态。
- [x] 页面加入记录链接、核对勾选和清除入口；账号变化清空勾选，清除失败提示恢复存储。
- [x] 运行相关前端已有检查和构建，完成独立复核。

在 `thccb-frontend` 执行：

```sh
npm run test:unit -- src/api/__tests__/fxBorrowBuy.spec.ts src/utils/__tests__/fx.spec.ts src/api/__tests__/userBoundRequest.spec.ts
npm run build
```

相关测试通过；最终模板修改后构建再次通过。`git diff --check` 通过。独立只读复核未发现旧请求兼容、清除策略或回执展示的实际回归。未执行浏览器交互、修改后端恢复接口、提交或部署。
