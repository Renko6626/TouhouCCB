# FX 公告实现计划

**目标：** 管理员追加无标题的纯文本公告，FX 交易页只显示最新一条，可关闭并重新查看。

**已确认方案：** 新增小型公告表；一个管理员 POST 写接口、一个公开 GET 最新公告接口；复用 FX SSE 提醒重新读取。关闭记录按账号保存在本地，新公告重新显示。没有编辑、删除、自动注资或历史列表功能。

**实现方式：** 沿用 FastAPI、SQLModel、Alembic、Vue 与现有 FX 页面结构，本次在当前工作区局部修改，保留已有未提交内容。直接在本会话执行；初次实现仅在本地，后续用户已授权阶段性提交、推送 main 并触发自动部署。

## 任务

- [x] 后端：`models/fx.py` 增加公告正文、发布人及时间；增量迁移；`schemas/fx.py` 定义创建与公开响应；`api/v1/fx.py` 提供 `GET /announcements/latest`（无公告返回 null）；`api/v1/admin_fx.py` 提供管理员 `POST /announcements`。按递增 ID 选择最新记录，提交后向已有 FX 货币对广播 `fx_announcement` 更新提醒。
- [x] 前端：独立 `api/fxAnnouncement.ts` 定义公告类型与接口；`FxStream` 接收更新提醒，在订阅完成的 snapshot 后刷新公告。`useFxAnnouncement.ts` 管理最新公告和账号本地关闭记录；独立横幅组件集成到 `Fx.vue` 顶部；独立管理员发布组件集成到 FX 管理页。
- [x] 验证：复用 `test_admin_fx.py` 的真实数据库及管理员 fixture，覆盖权限、追加/最新读取、已有记录保留、正文校验与实际广播；在现有 `fxStream.spec.ts` 覆盖公告提醒及 snapshot；新增简短 composable 行为测试覆盖关闭、重入、新公告及账号区分。运行相关后端/前端检查、前端构建，并在临时数据库验证迁移升级与回滚。

## 实现细节

- 正文最多 280 字符，合并换行等空白为一行；管理员输入框提示限制。桌面自然排版，窄屏允许换行，不截断内容。
- 公告读取失败仅提示重试，不阻塞行情或交易；公告发布失败显示错误，管理员可自行核对后重试。
- 初次进入页面读取一次；SSE snapshot 在后端订阅建立后触发读取，避免首连/重连期间漏掉公告。无单独轮询、消息队列或跨设备已读同步。
- 复用当前单写实例与进程内 FX SSE 部署方式，不引入外部依赖。

## 验证关注点

公告独立于货币对选择，暂停/无行情也可读取；关闭后保留查看入口；新公告重新展示；发布人不出现在公开响应；纯文本渲染，已有行情帧不被公告事件修改。

## 执行与验证记录

初次实现完成时尚未提交或部署。正文上限采用 280 字符，属于本次常规实现细节。

- 后端（在 `backend/`）：`python -m pytest tests/test_admin_fx.py tests/test_fx_api.py tests/test_fx_stream.py tests/test_credit_migration.py -q` → 56 passed。含临时 SQLite 数据库上的真实迁移升级、回滚与模型兼容性检查。
- 前端（在 `thccb-frontend/`）：`npm run test:unit -- src/composables/__tests__/useFxAnnouncement.spec.ts src/composables/__tests__/useFxResource.spec.ts src/utils/__tests__/fxStream.spec.ts src/utils/__tests__/fx.spec.ts` → 48 passed。
- 前端：`npm run build` → 成功，含 TypeScript 检查。构建仍提示现有分包体积及混合静态/动态导入警告。
- `git diff --check` → 通过。
- 按 requesting-code-review 技能进行一次限定范围的只读审阅，未发现需修改的问题，未扩展功能。
- Chromium 仅加载含真实组件和静态公告数据的临时预览并截图；桌面 1280×800、手机 390×844。已查看 `output/fx-announcements/desktop.png` 与 `mobile.png`，正文、关闭按钮及管理表单可读；临时预览已移除。
- 未进行浏览器点击、填写或提交，未进行完整交易页面交互验收，未执行生产数据库迁移或部署。

### 同步云端后的复核

按用户要求 fetch 并快进同步 `origin/main`，从 `8f47b75` 更新到 `ab0e83c`（21 个提交）。保留本地公告改动及其他未提交工作；右轴五位有效数字和手机紧凑布局采用云端已有实现，刚才基于旧代码的试改已备份。同步前的 patch、相关文件和云端新增路径上的本地原文件保存在 `/tmp/sunyunbo/touhouccb-before-cloud-sync-k1cedxy9/`，stash `codex-before-cloud-sync-20261016` 也保留作恢复参考。

公告迁移的前驱调整为云端新增的 `fx_pve_profiles_20261016`。`python -m alembic heads` 确认只有 `fx_announcements_20261016` 一个 head；`python -m pytest tests/test_credit_migration.py tests/test_admin_fx.py -q` → 43 passed；`npm run type-check` → 通过；`git diff --check` → 通过。示例账户的实际页面截图保存在 `output/fx-cloud-synced/`，手机 390px 下顶部黑块约 153px，相比旧图的约 305px 已明显缩短；无横向溢出、无 pageerror、无写请求。只导航和截图，未做交互测试。

### 阶段性提交前检查

按用户逐项要求更新交易页：手机图表无外框并铺满屏宽，周期选择独占图表上方一行；实时连接状态并入标题行，更新时间和刷新入口紧凑排列，移除图表底部灰字；手机购买区四个操作同排，持仓卡和表单间距收紧；桌面与手机的操作标题和规模倍数同排。下单参数及滑条算法沿用原实现。

发布前只读审阅发现按外币数量开空时标题倍数可能沿用旧金额，已改为以当前数量乘参考价计算显示规模。此调整仅影响显示；回补沿用已有报价成本。未增加恢复状态或额外接口。

- 后端：`python -m pytest tests/test_admin_fx.py tests/test_fx_api.py tests/test_fx_stream.py tests/test_credit_migration.py -q` → 56 passed。
- 前端：`npm run test:unit -- src/composables/__tests__/useFxAnnouncement.spec.ts src/composables/__tests__/useFxResource.spec.ts src/utils/__tests__/fxStream.spec.ts src/utils/__tests__/fx.spec.ts src/utils/__tests__/fxPresentation.spec.ts` → 79 passed。
- 前端：`npm run build` → 成功；保留既有分包体积提示。
- `git diff --check` → 通过。桌面和手机截图仅用于查看布局，未自动点击、填写或提交。样图保存在本机 `output/fx-order-heading-inline/` 等目录；未以截图代替交互验收。
- 本次提交只包含公告与上述 FX 排版改动。既有榜单、规则书、研究和其他未提交文件继续留在工作区。
- 按现有 `.github/workflows/ci.yml`，推送 main 后执行现有 CI/CD；未新增或扩大工作流。未设置 `DATABASE_RESET_PENDING` 仓库变量，部署 job 的该条件允许执行。
