# 开发协作指南

## 技术栈约束（硬约束）

- 前端必须使用：Vue 3 + TypeScript + Naive UI + UnoCSS + Pinia + Axios
- 不引入与当前栈冲突的新框架
- 所有接口交互经过 `src/api/` 封装
- 页面状态放 `stores/` 或 `composables/`，避免散落在页面中
- 新增类型落在 `src/types/`，禁止大量 `any`
- 不修改后端业务逻辑（除非明确说明）

## 代码组织原则

- `src/api/*.ts`：处理请求细节和参数转换
- `stores/` / `composables/`：处理业务状态与副作用
- 页面组件只做展示与交互编排（保持"薄"）
- 抽离可复用业务组件到 `components/`
- 对后端不稳定字段做兜底处理（默认值、空态）

## 开发流程

1. 明确任务目标、涉及页面、涉及 API、验收标准
2. 只读和任务相关的文件
3. 实施改动，优先小步修改（先通路，后优化）
4. 执行 `type-check` 和 `lint` 验证
5. 更新 `docs/README.md` 中的完成状态（如有变化）

## 完成标准

以下条件全部满足才算完成：

- 功能满足任务描述且关键路径可用
- `npm run type-check` 通过（无新增 TS 错误）
- `npm run lint` 无新增严重问题
- 改动文件有清晰职责边界

## 环境变量

```bash
# thccb-frontend/.env.development
VITE_API_BASE_URL=http://localhost:8000
VITE_APP_TITLE=东方炒炒币
```

## 权威信息源（按优先级）

1. `docs/api.md` — 后端 API 规范
2. `docs/README.md` — 项目状态与功能清单
3. 当前代码实现（与文档不一致时以代码为准）
