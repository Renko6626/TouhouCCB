# 新赛季切换方案（2026-09-27）

本次范围已由用户确认：保留真人基本信息和称号，现金按当前 `siteconfig.initial_balance` 重置，借款清零；清空市场、选项、持仓、K 线和金融交易历史；清除机器人；保留兑换码、购买归属、核销状态及核销/撤销审计记录。

沿用上次 `backend/scripts/season_reset.py` 的单事务重置，不必换数据库连接、重建用户或修改 SSO。完整备份作为上一赛季的归档，恢复时使用该备份。

## 相对旧脚本的调整

- 清空 `bot_profile` 并关闭 `pve_enabled`。无保留记录引用的机器人账号删除；仍被称号、兑换码或核销审计引用的账号停用、取消管理员权限、资产归零，保留 ID 以保持历史归属。
- 兑换购买、弹幕激活码、核销及撤销的记录全部保留，包括 `redemption_transaction`、`danmuku_exchange`，以及 `redeem_purchase`、`danmuku_exchange`、`redeem_fulfill`、`redeem_fulfill_revoke` 审计；市场和借贷等其它财务审计清掉，再为保留账号写入新赛季资产锚点。兑换购买历史作为旧码凭证继续可查。
- 不重置自增序列。生产 PostgreSQL 的市场/选项 ID 继续递增，避免 `/history/o/{outcome_id}/…` 的浏览器一年缓存和 Nginx 30 天缓存串入新赛季。
- 审计重放自检移到事务提交前；失败则全部回滚。删除了 PostgreSQL 非事务性的 `setval` 操作，避免回滚数据却没有回滚序列。
- 新的运维调用传 `--expected-ruleset 2026-09-27`；旧镜像不支持这个参数会立即拒绝执行，防止误用旧脚本。

## 执行顺序

1. 发布更新后的脚本至生产镜像。代码部署本身不会清库。
2. 运行只读预览，得到实际初始金额、真人/机器人数量、每张清理表的行数及保留的核销审计数量。
3. 核对预览并进入短暂维护，停止后端，等待写入与后台任务退出。
4. 完整 `pg_dump -Fc` 归档，记录时间、文件大小和 SHA-256；在隔离验证库恢复备份并核对数据，验证成功才允许重置。
5. 执行新脚本，要求明确输入 `RESET`。全部数据库修改在同一事务，提交前验证审计与实际余额一致。
6. 重启后端，验证用户与称号、兑换/核销状态保持，活动表为空，机器人不能调度，并检查站点健康。
7. 新市场列表初始为空，之后管理员创建本赛季市场；历史市场与交易留在归档备份中。

## 预览命令（只读）

在生产项目目录：

```bash
docker compose run --rm --no-deps -T backend python scripts/season_reset.py --dry-run --expected-ruleset 2026-09-27
```

执行命令只应在后端已停止且备份验证成功之后运行：

```bash
docker compose run --rm --no-deps -T backend python scripts/season_reset.py --expected-ruleset 2026-09-27
```

脚本会要求输入 `RESET`；不要直接套用旧手册里序列重置的假设。不要用 `init_db.py` 或删除 Docker 数据卷替代换季。

## 当前状态

已在临时测试数据库验证本次保留和清理规则。尚未改动生产数据库，也未创建生产归档备份；正式操作前需要生产执行通道及预览结果。

生产目标为 PostgreSQL；SQLite 回归验证资金、称号、兑换归属和事务回滚，不代表 SQLite 普通 rowid 也能像 PostgreSQL 序列一样在删空表后继续递增。
