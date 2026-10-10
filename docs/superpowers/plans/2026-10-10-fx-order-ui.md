# FX 正式页面交互调整

> 使用 superpowers:executing-plans 在当前工作区执行。用户已认可 `/fx-preview` 并要求修改正式前端，按已授权的交互推进。

**Goal:** 新用户可从看涨 / 看跌开仓，从持仓减仓和平仓，默认只阅读与本次操作有关的信息。

**Architecture:** 保留 Vue 3、现有 FX 页面状态、报价及提交 API。局部调整模板、样式和金额展示辅助函数，不调整账户模型、信贷规则或后端接口。

**Tech Stack:** Vue 3 / TypeScript / Naive UI，现有 Decimal 字符串工具。

**Spec:** 用户认可的 `thccb-frontend/src/pages/fx-preview/README.md` 及对话中的截图；正式资金口径以 `docs/fx.md`、当前接口和 `fxPresentation.ts` 为准。

## Global Constraints

- 不使用 Superdesign；浏览器只做截图，不自动点击、填表或交易。
- 金额使用现有十进制工具；加载失败和缺失数据保持未知，不展示假零或假风险等级。
- 不把账户统一担保描述为单笔隔离保证金，不自行连接尚未实现的借款并买入接口。
- 保留只减仓、过期报价、切币隔离和待确认空头订单恢复流程。
- 不新增样式、文案和模板结构测试；仅为资金拆分的真实金额行为补充已有测试。

## Review Focus

- 平仓入口填数量并获取报价，始终由用户明确确认提交。
- 空头全部平仓继续使用 cover_all，包含成交时新计利息。
- 待确认订单不被方向选择、切币、重新输入覆盖。
- 现金拆分不能把锁定卖出所得算成可花现金。
- 共用手机入口保持预测市场默认文字及行为。

## Tasks

- [x] 1. 在 `fxPresentation.ts` 添加 `fxFundingPreview(amount, summary)`，返回现金投入和还需资金；在已有 `fxPresentation.spec.ts` 覆盖锁定资金排除、六位精度、未知现金。运行对应单测。
- [x] 2. 调整 `Fx.vue`：看涨 / 看跌方向选择、单层平仓状态、独立多空持仓、折叠费用与风控细节、真实资金预览、简短涨跌示意。保留原报价和提交函数。
- [x] 3. 调整 FX 桌面与手机排列；共用 `MobileTradeDock.vue` 增加可选文字及隐藏卖出入口，默认保持预测市场行为。
- [x] 4. 运行现有单测、类型检查、局部 lint、构建和 diff 检查。用只读模拟 API 截图正式页面；审查实际差异，记录验证及接口限制。

## Implementation notes

- 实际 FX 买入投入已包含手续费，不在投入上重复加手续费。
- “还需资金”只是现金差额，不是可借额度；借款仍去现有页面操作。
- 做多只展示当前账户风险；做空报价有成交后风险字段，可展示其权威等级。
- 当前阶段不引入无真实接口支撑的账户杠杆目标滑块。

## Verification

- `npm run test:unit -- src/utils/__tests__/fxPresentation.spec.ts`：新增资金拆分行为先 3 failed，再 14 passed。
- `npm run test:unit`：138 passed；样式和模板不新增永久测试。
- `npm run type-check`：exit 0。
- `node node_modules/eslint/bin/eslint.js src/pages/Fx.vue src/components/market/MobileTradeDock.vue src/utils/fxPresentation.ts src/utils/__tests__/fxPresentation.spec.ts`：exit 0。
- `npm run build`：exit 0，保留既有空 charts chunk、auth 导入重合和大 chunk 提示。
- `git diff --check`：exit 0。
- 只读截图：桌面做多、空头全部平仓、手机做多、空头快照读取失败；API 数据为临时截图夹具，全部 API 请求被拦截，不访问真实账户或执行真实交易。均无页面运行时异常或横向溢出。截图目录 `output/fx-ui/`。
- 独立静态审查发现并修复 `action=cover` 被重置开仓、首次空头读取失败缺少全部平仓入口两项回归；截图确认回补链接仍展示平仓及自动报价。
- 未运行浏览器点击、填表或交易流程测试，未部署、推送或提交。真实借款并买入原子接口尚未实现。

## 主线集成与阶段性发布

用户已授权提交、推送并触发自动部署。原工作区基于较早的信贷修复分支，UI 原始提交为 `b296972`；发布版本在最新 `origin/main` 上集成，保留原工作区及其余未跟踪文档。

- 保留主线 `onEnvelope` / `chartEnvelope` 历史与成交管线，不回退为旧版 tick 图表。
- 沿用主线始终统一信贷的账户契约，不恢复已删除的 `unified_credit_enabled` 或传统信贷逻辑。
- 主线纯 AMM 已移除新闻 API，正式页面随主线不再显示新闻区域；截图保留为前期 UI 审阅记录。
- 集成后的 `npm run test:unit` 为 163 passed，`npm run type-check`、局部 ESLint、`npm run build`、`git diff --cached --check` 均通过。独立只读审查未发现重要的主线兼容问题；保留既有打包提示。
- 本阶段仅发布前端 UI、模拟样稿和验证记录；未引入自动借款、精确外币数量买入或新的后端接口。
