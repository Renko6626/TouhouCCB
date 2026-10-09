# FX 新开局数据库重建

本工具只迁移身份、称号和已购权益。旧交易、市场、行情、债务、钱包、锁金、强平状态与无关机器人数据留在旧库备份中。生产切换和旧库删除需另行确认具体环境。

## 预检与政策

连接分别使用 `REBUILD_SOURCE_DATABASE_URL` 和 `REBUILD_TARGET_DATABASE_URL`，不要把连接串写入命令日志。两者必须指向不同数据库，目标必须完全无表。导出通过只读一致事务读取，导出文件包含身份和兑换码，权限固定为 0600，应存于仓库外。

政策文件示例：

```json
{"balance": "10000.000000", "dependency_balance": "0"}
```

`balance` 是已确认的新开局金额 B，必须显式提供；可先只读查询源库 `siteconfig.initial_balance` 后确认，工具不猜默认值。默认保留所有非 bot 用户；可通过 `user_ids` 指定完整真人账号范围。称号授予者、权益批次创建者和核销操作者等必要依赖账号会保留原身份、标记停用，余额使用 `dependency_balance`。未识别的用户关联表会阻止导入；调查后才能在 `exclude_tables` 中明确排除。不要以该选项掩盖应保留的权益。

```bash
cd backend
venv/bin/python scripts/rebuild_user_database.py inspect --policy /secure/policy.json --file /secure/manifest.json
venv/bin/python scripts/rebuild_user_database.py export --policy /secure/policy.json --file /secure/export.json
venv/bin/python scripts/rebuild_user_database.py import --policy /secure/policy.json --file /secure/export.json
venv/bin/python scripts/rebuild_user_database.py verify --policy /secure/policy.json --file /secure/export.json
```

inspect 和 export 都生成敏感完整清单，stdout 只输出计数、明确金额和未识别表名。审核真人／依赖账号范围、称号、已领取称号码、已购码及核销备注、兑换购买记录、弹幕兑换记录和权益审计。未售库存不迁移。

## 空库初始化与核对

现有 Alembic 首 revision 是 stamp-only，不能在空库直接 upgrade 全历史。工具先确认目标完全无表，用当前注册模型建 schema，然后记录动态 Alembic head；不调用 `init_db`，不改旧迁移。默认配置来自现有播种列表，不复制旧全站配置；贷款、强平、FX、做空、统一信贷和 PvE 开关关闭，新增风险冻结。账户旧授信冻结清除，但原账号停用与管理员权限保留。

导入前校验字段白名单、账号范围、金额和外键依赖；用户、权益、配置及新开局审计在同一事务内写入。数据库错误会回滚行，SQLite 建表失败后可能保留空 schema，需换一个完全空目标重新执行。重复导入拒绝。

verify 比对身份权限与权益所有保留字段、零债务、初始经济版本、空旧业务表、开关、余额审计锚点和序列水位。PostgreSQL 导出读取序列 `last_value`，包含已删除市场的 ID；SQLite AUTOINCREMENT 表读取 `sqlite_sequence`，其他表只能读取现存最大 ID，无法推断过去已删除的 ID。新 SQLite 目标采用 AUTOINCREMENT 保留导出水位。若源 SQLite 关键表无 AUTOINCREMENT 且曾删除高 ID，导出会列入 `uncertain_sequences` 并阻止导入。核对备份／旧缓存后，通过政策 `sequence_highwater` 明确提供 market/outcome/fx_pair 的历史水位，再重新导出；不能假定最大现存 ID 足够。

## 正式切换顺序

1. 固定源库、目标库、代码 SHA、政策 B、权益清单；停止所有旧写入者。
2. 制作一致备份并在隔离环境验证恢复；完成只读导出。
3. 在空目标导入并 verify，核对部署环境门闸。首次切换核验期设置 `THCCB_READ_ONLY_INSTANCE=true`，保持 FX／贷款／强平／PvE 停止；不要只依赖站点 FX 开关。
4. 保留所有源序列高水位，清理 FX 与 LMSR history HTTP/nginx 缓存及客户端旧待确认订单，重启 writer/ring。旧 pair/outcome ID 不得指向新市场。
5. 切换应用到新库；单独准备新市场注资与开市。
6. 新库接受用户交易前可切回备份旧环境；接受新账后不能直接切回丢弃新账。旧库最终删除是单列操作，需确认恢复备份和具体库名。

不要让普通 deploy 的自动 upgrade 接触旧源库。清理 revision 的 downgrade 只恢复空结构，不能恢复删除的数据；数据恢复依赖备份。

## 已授权的生产 Actions 通道

专用工作流 `.github/workflows/fx-rebuild.yml` 使用现有部署 SSH secrets，在 `/home/deploy/TouhouCCB` 操作固定 `postgres:5432/thccb`。仅仓库所有者与执行者均为 `Renko6626` 时运行；源配置来自服务器挂载的 `.env`，不把连接串送回 runner。此通道包含已授权的旧库最终删除，完整 dump 与旧前端仍保留。

合并前由操作者设置部署暂停变量，防止普通 CD 的 rsync／迁移接触旧库；构建仍正常进行。指定的 CI/CD run 必须是当前 main 的成功 push 构建，不能用 PR 构建或过期 SHA。两个部署工作流共用 `thccb-production` 并发锁。

```bash
gh variable set FX_REBUILD_PENDING --body true
# 合并后找到成功的 main CI/CD run；读取其 ID 后显式填入下面命令。
gh run list --workflow ci.yml --branch main --event push --limit 5
gh workflow run fx-rebuild.yml --ref main -f action=preview -f build_run_id=BUILD_RUN_ID
# 查看 preview 的汇总，确认 source_initial_balance／真人数／权益数和新余额 B。
gh run list --workflow fx-rebuild.yml --limit 5
gh run view PREVIEW_RUN_ID --log
# B 为已确认的新开局金额；不能保留占位符，不能从脚本默认值推断。
gh workflow run fx-rebuild.yml --ref main -f action=execute -f build_run_id=BUILD_RUN_ID -f balance=B -f confirmation=REBUILD
gh run view EXECUTE_RUN_ID --log
# 仅在完成全部步骤且日志报告成功后解除普通 CD 暂停。
gh variable delete FX_REBUILD_PENDING
```

preview 不停服务、不创建／切换数据库。它拉取新镜像，用只读事务输出源库汇总；未填写预览余额时清单使用 0，仅用于查看源状态，不代表授权的新余额。preview 和 execute 是独立 run，execute 会在停写后重新导出。每次 run 在 `.fx-rebuild-RUN_ID` 暂存部署文件，私有文件存于 `backups/fx-rebuild-RUN_ID`；同一 run 的 rerun 拒绝覆盖已有目录，失败后调查再发起新 run。

execute 先保存旧 backend 容器 ID／镜像、compose、部署脚本和前端；停止 backend 并拒绝额外网络容器与活动源连接。备份为 0600 `source.dump`，记录 SHA256 并实际恢复到独立校验库，恢复成功后才导出与新建空目标。目标导入和 verify 不调用旧源的 Alembic。未知引用、未确定水位、余额溢出或不合政策均中止。

固定 `PG_DB=thccb` 不变：源改名为 `thccb_before_RUN_ID`，新库改为 `thccb`。只有接近切换时才交换前端。新 backend 先以容器环境强制 `THCCB_READ_ONLY_INSTANCE=true` 启动，核对健康、`APP_BUILD_SHA`、审计回放和重建 verify。随后以显式 false 启动 owner，门闸仍关闭，再核对健康与版本；全部成功才删除旧物理数据库。备份与旧前端保留，摘要只输出计数、B、版本与备份路径。脚本有文件锁，远端执行限制 30 分钟，超时清除本次 one-off 容器。不会自动开市。

旧 JWT／会话身份绑定可以继续使用。保留 FX pair 与 LMSR outcome／market 序列水位及新版行情版本机制；用户应清除浏览器旧待确认订单和页面缓存后刷新，不能重新提交旧 quote。开市前另行检查 HTTP/nginx 行情缓存并准备新市场。

### 失败后的人工恢复

先保持 `FX_REBUILD_PENDING=true`，查看工作流日志与 `backups/fx-rebuild-RUN_ID`，不要直接运行普通 deploy。不要在排查时打印 `.env`、manifest 或兑换码；`old-backend.txt` 保存旧容器／镜像 ID。

切换前失败，脚本重启原先停止的旧 backend 容器。目标和备份不自动删除，新的 execute 用新的 Actions run。若重启未成功，操作者确认源名仍为 `thccb` 且允许连接后再 `docker compose start backend`。

首次禁止连接或改名后失败，脚本保持 backend 停止并保留数据库。使用 `docker compose exec -T postgres psql -U thccb -d postgres` 查看 `pg_database` 的 `datname, datallowconn`，再确定失败位置。仅在确认新 owner 尚未产生任何新账时，才可手动恢复旧数据库名称：若源仍名 `thccb`，恢复其 `ALLOW_CONNECTIONS true`；若源已名 `thccb_before_RUN_ID` 且新目标仍名 `thccb_rebuild_RUN_ID`，把旧库改回 `thccb` 并允许连接；若新库已名 `thccb`，先保持停写，将新库改为独立保留名称，再把旧库改回 `thccb`。所有 SQL 使用已核实的完整数据库名与双引号。

回退还须从备份恢复旧 compose／deploy／前端，使用 `old-backend.txt` 的旧不可变镜像创建容器，不能用新镜像自动迁移旧库。新 owner 已启动后不自动回退；须先审查新写入，避免丢账。最终旧库已删除时只能从经过恢复验证的 `source.dump` 重建到独立数据库后按上述停写流程切换。备份 SHA256 文件用于核验 dump 完整性，不能替代实际恢复检查。

本适配器的静态／单元检查不等于生产演练；没有本地 Docker 时不得声称已验证容器、SSH、生产 restore 或切换。生产执行中的 restore、只读 verify、审计与健康结果才是本次操作证据。
