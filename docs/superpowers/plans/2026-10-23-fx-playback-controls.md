# FX 播放页优化实施计划

**Goal:** 保留公开播放用途，在页面内选择币对、周期并隐藏工具栏。

**Architecture:** 仅修改现有 FxPlayback.vue，复用公开币对列表、FxStream 和 FxCandleChart；不修改接口、后端权限或行情实现。

**Tech Stack:** 现有 Vue 3、TypeScript、Vue Router。

**Spec:** 用户已确认本轮会话中的简短方案。采用紧凑工具栏，较单独选择页更便于播放中切换。

## 约束与检查重点

- 保留免登录和独立全屏布局，不增加导航入口。
- URL 保留 pair、interval，并用 controls=0 保存隐藏工具栏设置。
- 默认优先选择交易中的币对；指定 ID 必须属于公开列表。
- 无效 ID、空列表和加载失败有明确提示，可通过选择或重试恢复。
- 切换周期或工具栏时不重建 SSE；切换币对继续清理旧连接。
- 不运行浏览器自动点击，不新增冗余永久测试。

## 实施步骤

- [x] 修改 `thccb-frontend/src/pages/FxPlayback.vue`：加载公开列表，解析 URL，增加工具栏、恢复按钮及自适应布局。
- [x] 更新 `docs/fx.md`：记录选择方式、默认币对和隐藏工具栏参数，说明前端可选范围不等于后端访问限制。
- [x] 运行前端构建、现有 FX 行情相关单元测试及 `git diff --check`，检查实际变更。

验证结果：`npm run build` 通过；`npm run test:unit -- src/utils/__tests__/fxStream.spec.ts src/utils/__tests__/fxCandle.spec.ts src/utils/__tests__/fxHistory.spec.ts` 通过（47 项）；`git diff --check` 通过。未进行浏览器截图或交互验证，未部署。
