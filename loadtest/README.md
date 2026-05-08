# 压测套件 — 上线前最后一道闸

只在 prod 机器上跑（2C/4G/SSD/3MB·s⁻¹）。同机压测意味着 k6 + uvicorn + postgres
抢同一对 CPU 核心，**结果是延迟和吞吐的下限**——真实用户从外网打过来，少了 k6
的 CPU/内存争用，会更宽松一点点（但被 3MB·s⁻¹ 上行压回来）。

## 安装 k6（一次性）

```bash
sudo gpg -k
sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
    --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
    | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install -y k6
k6 version    # 验证
```

## 运行顺序（**严格按这个顺序**）

```bash
cd /home/deploy/TouhouCCB        # 或本地 dev 目录

# 1. 备份。失败立刻退出，不允许带病开测。
./loadtest/seed/backup_before.sh
#   -> backups/loadtest_pre_<TS>.sql.gz

# 2. seed 200 个 loadtest 用户 + 5 个 [LT] 市场
./loadtest/seed/seed_users.sh
./loadtest/seed/seed_markets.sh
#   抄下 [LT] HOT_2OPT 的 market.id 和两个 outcome.id

# 3. 容器内签 200 个 access token
./loadtest/seed/mint_tokens.sh
#   -> loadtest/tokens.txt

# 4. 把 HOT 的 ID 写到环境变量（也可以加到 ~/.bashrc 之类）
export BASE_URL=http://127.0.0.1:8004
export HOT_MARKET_ID=<上面的 market.id>
export HOT_OUTCOME_YES=<上面 YES 的 outcome.id>
export HOT_OUTCOME_NO=<上面 NO 的 outcome.id>

# 5. nginx 白名单生效（已经在 deploy/nginx.conf 改好；要 reload）
sudo nginx -t && sudo systemctl reload nginx

# 6. smoke：1 VU × 60s，确认所有端点都通
k6 run loadtest/scenarios/smoke.js
#   绿了再继续；任一红立刻停。

# 7. 启观测脚本（三个不同 terminal / tmux 窗口；都用同一 RUN_TAG）
RUN=trade_$(date -u +%Y%m%dT%H%M%SZ)
./loadtest/record/host.sh "$RUN"          &
./loadtest/record/pg.sh start "$RUN"
./loadtest/record/app_tail.sh "$RUN"      &

# 8. 主压测
k6 run --out json=loadtest/results/k6_${RUN}.json loadtest/scenarios/trade_ramp.js

# 9. 停观测
./loadtest/record/pg.sh stop "$RUN"
kill %1 %2     # host.sh 和 app_tail.sh

# 10. （可选）SSE fan-out 测试 —— 单独跑，不和 trade_ramp 同时
RUN=sse_$(date -u +%Y%m%dT%H%M%SZ)
./loadtest/record/host.sh "$RUN" &
./loadtest/record/pg.sh start "$RUN"
k6 run --out json=loadtest/results/k6_${RUN}.json loadtest/scenarios/sse_fanout.js
./loadtest/record/pg.sh stop "$RUN"
kill %1
```

## abort 阈值

k6 自带 thresholds 触发任一即自动结束，但**人也要看着**：

- `http_req_duration p(99) > 2000ms` 持续 30s → 立即 Ctrl-C
- `host.sh` 里 `load1 > 4` 持续 30s（2C 机 load1 大致是 CPU 数 × 2 警戒）
- `pg_active_count > 30`（DB 连接池上限是 `DB_POOL_SIZE=10 + DB_MAX_OVERFLOW=20 = 30`）
- 后端日志出现 `invariant violated`（loan_service 的兜底，意味着钱算错了）

## 收尾（**每次跑完必做**）

```bash
# 1. 删 token（含 SECRET_KEY 签出的 JWT，泄露 = 任意账户接管）
rm loadtest/tokens.txt

# 2. 清 loadtest 数据
./loadtest/cleanup/cleanup.sh
#   会问 yes 确认；只删 casdoor_id LIKE 'loadtest:%' 和 [LT]% 的市场

# 3. 真正上线**前**：移除 nginx 白名单
#    编辑 deploy/nginx.conf，把 geo $is_loadtest 块里的 127.0.0.1 那行删掉
#    sudo nginx -t && sudo systemctl reload nginx
```

## 翻车恢复

```bash
./loadtest/cleanup/restore.sh backups/loadtest_pre_<TS>.sql.gz
```

会停 backend、用 `--clean --if-exists` 模式覆盖整个库、重启 backend。**只在
cleanup.sql 也救不回来时用**。

## 文件清单

```
loadtest/
├── seed/
│   ├── seed_users.sh        # 200 个 loadtest_NNN 用户（cash=100000）
│   ├── seed_markets.sh      # 5 个 [LT] 市场覆盖热/冷/高 b/低 b/多选
│   ├── mint_tokens.py       # 容器内执行体：复用 create_access_token 离线签 JWT
│   ├── mint_tokens.sh       # docker cp + exec wrapper
│   └── backup_before.sh     # pg_dump 全库 → backups/loadtest_pre_*.sql.gz
├── scenarios/
│   ├── lib/auth.js          # SharedArray 加载 tokens.txt + VU→token 分发
│   ├── smoke.js             # 1 VU × 60s 端点验证
│   ├── trade_ramp.js        # 1→200 VU 阶梯，集中打 HOT_2OPT
│   ├── sse_fanout.js        # 200 SSE 订阅 + 50 background trader
│   └── .env.example
├── record/
│   ├── host.sh              # CPU/mem/iowait/load + docker stats 1Hz CSV
│   ├── pg.sh                # pg_stat_activity / pg_locks 周期；pg_stat_statements 跑前重置跑后 dump
│   └── app_tail.sh          # tail backend 日志，过滤 >500ms 和 5xx
├── cleanup/
│   ├── cleanup.sql          # 按 FK 顺序 DELETE 所有 loadtest 痕迹
│   ├── cleanup.sh           # 包装 + 确认提示
│   └── restore.sh           # 从 *.sql.gz 完整恢复（最后手段）
└── results/                 # 所有日志和 k6 JSON 输出
```

## 设计取舍

- **同机 k6**：用户选的，认知到这是「下限」；好处是消除网络变量，看到的吞吐就是 backend
  自身能吃下多少。
- **离线签 JWT**：不动 Casdoor。SECRET_KEY 是同一把，签出来的 token 后端 100% 接受。
- **集中打 HOT_2OPT**：真实生产里有热门事件（"东方深秘录新作几号发"）就是这种形态——
  所有人挤一个 outcome 的买卖，最坏锁竞争。分散打反而测不到瓶颈。
- **DELETE 不 TRUNCATE**：CLAUDE.md 红线。
- **白名单按 IP 不按 header**：nginx `limit_req` 不能条件 skip，只能让 key 为空。`geo`
  按源 IP 是最干净的写法，比改 `X-Loadtest` 头骚扰真实代理链稳。
