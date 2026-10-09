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
