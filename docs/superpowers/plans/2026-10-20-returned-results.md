# 逆转录者竞猜结果 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement the approved design inline.

**Goal:** 两边显示已结算轮次的真实目标及最接近的五位观众。

**Architecture:** 复用竞猜记录和结算后的 SSE；新增 GET `/api/v1/returned/rounds/{round_key}/results`，允许有效玩家 JWT 或现有服务端密钥。发送端在结果 POST 成功约 500ms 后查询，通过已有游戏状态同步到控制器和监视器。

**Tech Stack:** 现有 FastAPI / SQLModel、Vue / Naive UI、Node HTTP 与原生前端。

**Spec:** 本轮对话中已确认的方案。

## Global Constraints

- 不新增数据库表或字段，不改结算金额和审计通道；保持两边现有画风。
- 结果只在 settled 后返回，排名依次按误差、提交时间、用户 ID 升序，最多五人。
- 返回 round_key、target_position、top_five；每人 username、position、distance、payout。
- 密钥只供发送端服务端使用，浏览器不直接请求 THCCB；获取榜单失败不影响已完成结算。
- 不执行浏览器点击或填写交互；仅相关已有检查和必要新行为测试。

## Tasks

- [x] 后端：现有竞猜测试覆盖结果权限、未揭晓保护、前五排序和实际结算金额；实现 schema、查询服务及 GET。
- [x] THCCB 前端：结算 SSE 后读取结果，滑动条上方显示真实位置、个人竞猜及奖励等级，下方显示前五；扩展现有 SSE 用例。
- [x] 发送端：结果 POST 成功后延迟 GET，广播结果或简单错误提示，控制器及监视器揭晓时展示；扩展已有真实 HTTP 用例覆盖顺序和跨轮隔离。
- [x] 验证：相关后端测试、前端单测与构建、发送端 logic/DOM/server 检查；一次专项审阅、文档和两边 PR。

验证记录：后端相关 11 项、前端 264 项及构建、发送端 logic/DOM/server 均通过；专项审阅未发现重要问题。页面按静态夹具截图验证，无浏览器点击或填写。
