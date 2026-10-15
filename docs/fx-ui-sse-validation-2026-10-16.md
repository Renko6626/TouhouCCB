# FX 页面和行情连接修复

## 页面

FX 机器人管理页沿用 LMSR 的面板、统计行、策略卡片、紧凑表单、中文状态标签和管理弹窗。保留自定义资产、参数、批量操作、注资、暂停清仓、退休及共享统计开关。

修复刷新后旧错误残留、读取日志重载并覆盖配置草稿、切换交易对仍显示旧账户，以及创建禁用时缺少原因的问题。局部覆盖 UnoCSS 同名 `.table` 工具类造成的收缩，窄屏表格保留横向滚动。

## 公共行情 SSE

- HTTP 快照失败后，当前有效币种仍会建立 SSE，避免没有重连主体却长期显示连接中。
- 初次加载、切换币种和卸载复用 generation 检查，旧请求不会重建连接或覆盖新状态。
- 切换币种会正确结束当前加载状态。
- 打开或恢复流时，缺失/失败快照补读一次；错误受快照请求 generation 保护。
- 已有快照时保留刷新失败降级提示和交易界面；缺少初始快照时才显示整页错误。
- 服务端断开时取消并等待队列和踢出事件的等待任务，释放订阅和 IP 配额。
- 流响应补 `X-Accel-Buffering: no`。API 全局原有 `Cache-Control: no-store` 仍生效，未将缓存断言为线上故障原因。

原有重连退避、限流、事件格式、行情一致性锁和健康公共流的成交刷新行为保持不变。用户明确选择不调整后台限流。

## 验证

- `venv/bin/python -m pytest tests/test_fx_stream.py tests/test_fx_publication_e2e.py -q`：14 passed。
- `npm run test:unit -- src/utils/__tests__/fxStream.spec.ts src/api/__tests__/tradingStream.spec.ts`：10 passed。
- `npm run type-check`、`npm run build` 通过；最后已有快照降级保护只追加类型检查和上述单测。
- `venv/bin/python -m pytest tests/test_fx_pve_engine.py -q`：3 passed。
- 页面和 SSE 独立代码审阅通过。桌面/390px 窄屏使用本地模拟 API 截图核对，无页面异常和额外文档横向溢出；未自动点击、填表或运行浏览器交互流程。

页面请求失败和卸载路径以代码审阅核对，未建立新的 Vue/DOM 测试框架；尚未测量线上首快照延迟，未验证全部线上断线原因。

## 机器人与部署

FX 机器人直接读取数据库储备价，不依赖网页 SSE。部署停机停止 tick，不补跑空 tick；账户与未完成轮次保存在数据库，重启从当前价预热内存窗口。死亡按实际现金与外币清算价值判断，缺失估值和取价异常不按零资产处理。

正常停机等待活跃 tick，但现有 Docker 停机宽限不保证任意慢 tick 全部排空。未执行的到达可以丢弃；已提交成交和轮次状态同事务保留，未提交事务回滚。未新增恢复队列或修改部署参数。

本轮另有[混合与纯随机对照实验](../research/fx/reversion/repeated/random20_comparison/report.md)，属于零手续费、无玩家的三种子探索性比较。
