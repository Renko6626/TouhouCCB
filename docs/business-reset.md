# 业务重置操作单

本入口建立当前 Alembic 新基线，保留所有真人账号的身份、SSO、状态、权限及完整称号体系；称号引用的机器人只保留停用、零资产身份。真人现金采用停写源库 `siteconfig.initial_balance`，债务清零，经济版本加一。交易、市场、行情、旧流水和审计、机器人业务及全部兑换数据清空。新库只建立开户和配置审计锚点。

常规 `season_reset` 仍保留兑换权益；LMSR 修复工具继续保留。旧 schema 不直接升级新基线，旧工具仅供[归档查阅](archive/database-pre-baseline-2026-10-13/README.md)。

## 准备与预览

合并新基线前将仓库变量 `DATABASE_RESET_PENDING=true`，暂停普通 CI Deploy。旧暂停变量已退役。CI 继续构建镜像与前端。专用 `.github/workflows/business-reset.yml` 仅接受仓库 owner `Renko6626` 在 main 手工触发，与普通 CD 共用 `thccb-production` 并发锁。

选择当前 main 成功的 `CI/CD` push run，填入 `build_run_id`；workflow 校验仓库、工作流、分支、事件、成功状态及当前 main SHA，下载同一 run 的前端，使用完整 SHA 的镜像，并在远程执行前再次核对 main。

先选择 `action=preview`。`expected_initial_balance` 可选，只用于核对源库金额；预览输出政策、真人/依赖账号/称号数量、旧业务计数、源 revision、余额和摘要，不输出身份、称号码、兑换码或凭据，不停写或改库。预览目录不复用，重跑会使用新的 workflow run ID。

执行要求 `action=execute`、`confirmation=RESET`、`DATABASE_RESET_PENDING=true`，并填写预览确认的 `expected_initial_balance`。金额由源配置决定；此输入不能改写现金。固定源为 Compose `postgres:5432/thccb`。每次运行独占 `.business-reset-RUNID` stage 和 `backups/business-reset-RUNID` 私有目录，禁止复用库或覆盖导出。部署账户需要读取当前环境文件而不打印它，固定 PG 管理权限、Docker、非交互 sudo，以及对已配置 `/var/cache/nginx/thccb` 的精确清理权限。缓存路径和 sudo 命令在停写前预检；不扩大系统权限。

## 执行与验收

runner 保存旧镜像、Compose/deploy 配置和前端，拒绝 Compose 网络上的额外容器。停止 backend 后检查源库没有其他会话；不主动杀死未知 writer。完整 `pg_dump -Fc` 生成校验和，并在独立验证库实际 `pg_restore --exit-on-error`，成功后清理验证库。保留 dump、恢复日志及旧不可变镜像。

固定政策导出后创建完全空的 `thccb_reset_RUNID`，调用当前工具 import：实际 Alembic upgrade、恢复保留字段和 ID 高水位、生成审计锚点并严格核对。导入重新读取源快照，拒绝变化；切换前再 verify 目标并核对源余额。

目标应用角色由当前镜像配置读取，不能输出凭据。仅在该目标数据库设置角色 `default_transaction_read_only=on`，保存其精确原值和恢复 SQL；不修改全局角色或其他库。当前固定角色必须是普通 SQL 标识符，否则拒绝并转人工核查。`THCCB_READ_ONLY_INSTANCE=true` 停后台 writer，数据库级默认只读同时保护认证回调等路径。停写期间不运行设置事务只读为 off 的外部会话。

阻止源与目标新连接、再次确认无会话后，依次将旧源改名 `thccb_before_RUNID`、目标改名 `thccb`。旧库保持不可连接。清理固定 nginx 历史缓存、安装相同 SHA 的前端和 Compose，强制重建只读 backend，检查 schema/构建 SHA/健康，执行真实 audit_verify 与 after-cutover verify。严格空业务核对只在此停写只读阶段执行。

停止只读 backend、确认无残留会话，恢复目标角色原设置。强制重建 owner 使用新连接，检查健康。owner 启动后不再用“空业务”断言验收可变数据，也不自动切回旧库。owner 健康后才删除旧物理库。五项经济开关保持关闭、风险冻结开启；初始化市场和主动开市由管理员后续处理。成功后由操作人解除 `DATABASE_RESET_PENDING`，恢复普通 CD。

## 失败与恢复边界

私有 `stage` 文件记录准确阶段；结合真实数据库名和 `ALLOW_CONNECTIONS` 状态核对，不能只凭日志猜测。runner 不自动删除失败目标、dump、旧镜像或前端。

- 首次 rename 前失败：确认维护容器退出后，恢复源连接权限，原 owner 原本运行时重新启动旧容器；原本停机则保持停机。备份恢复失败不执行导入。无法确认容器退出或恢复源连接则保持停写，人工处理。
- 首次 rename 尝试及其后、owner 启动前失败：backend 保持停止，保留两个物理库及私有阶段文件。人工先核对实际库名、目标只读设置、部署文件与前端，再决定恢复；不能盲目执行反向 rename。旧 dump 用保存的旧 Git `04c9e7ad2081337677cd4a1e82515c4a9849f76e` 和旧镜像恢复，不能让新代码自动升级旧 schema。
- owner 启动尝试后失败：不自动停止/回滚 owner，不自动删除旧物理库。可能已有登录或新账，先保存当前数据与日志，按实况恢复健康；禁止切回旧库丢失新账。

旧物理库删除后保留完整 dump、校验和、旧镜像/代码标识、配置与前端。生产操作由主 agent 在审阅及预览通过后执行；隔离测试不构成生产权限或容量验收。

后端分阶段 CLI 在 `backend/` 下使用 `python scripts/business_reset.py config|preview|export|import|verify --run-id RUNID --directory PRIVATE_DIRECTORY [--expected-initial-balance B]`；`--after-cutover` 仅供 verify。CLI 不负责创建、交换或删除数据库，生产只通过专用 runner 编排。
