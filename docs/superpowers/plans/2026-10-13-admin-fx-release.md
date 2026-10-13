# 管理员 FX 发布衔接

用户已授权创建 PR、合并 main 并触发既有自动部署；另确认“补齐停写历史转换后合并”。沿用现有备份、停写、Alembic、审计校验、启动及健康检查流程。

## Task 1: 停写历史转换

在 schema 升级完成后、审计校验和 backend 重启前调用一个薄脚本。脚本只选择仍存在、有成交/资金事件来源且 history 未就绪的市场；无历史草稿和已经就绪市场跳过。复用既有 backfill 工具先校验所有选中市场，再执行转换；仍获取真实 ownership 和 pair gate。不得修改余额、旧成交或资金审计、伪造来源、吞错、删除历史或加入磁盘 staging。失败保持 backend 停止，现有部署 cleanup 负责。

覆盖真实新增不变量：pending 历史转换后 ready、资金与来源不变、重复部署跳过 ready、无来源草稿跳过、来源链坏时不执行转换；复用原 history 测试/隔离 DB，不搭建新测试项目。文档说明自动步骤和失败恢复。

## Task 2: CI 兼容修正

读取 PR CI 的具体失败，最小适配既有用例/显式 schema/同步 adapter 到已确认的 event 合同，保留有效行为断言。真实产品故障须修复并报告；不可通过删断言或放宽预期换通过。不扩展无关清理。运行相应检查；PR 的既有 CI full-suite 为合并前要求。

## Task 3: 审阅和合并

Sol low 实施、Sol medium 审阅。父 agent 检查实际产物与证据，推送更新，等待 PR backend/frontend CI 通过；合并到 main，不触碰主工作区的用户未提交文件。

## Task 4: 自动部署验证

跟进 main 对应 commit 的 CI/CD，包括镜像构建、部署、历史转换与健康检查。失败调查具体原因，沿用批准流程恢复；不得声称 merely 合并就部署成功。记录 PR、merge SHA、workflow 和最终状态。
