# 压测套件 — 上线前最后一道闸

两种运行模式，**推荐远程**：

| 模式 | k6 运行位置 | BASE_URL | 结果质量 | 适合场景 |
|------|------------|----------|----------|----------|
| **同机**（旧） | prod 服务器 | `http://127.0.0.1:8004` | 偏悲观（CPU 争抢） | 快速验证逻辑 |
| **远程**（推荐） | 你的本机/另一台机 | `https://thccb.secret-sealing.club` | 真实（经 nginx+TLS） | 容量规划 |

同机压测：k6 + uvicorn + postgres 抢同一对 CPU 核心，延迟虚高、吞吐虚低。远程压测消除这个干扰，结果反映后端真实极限。

---

## 安装 k6（一次性，在跑 k6 的机器上）

**有 conda（推荐，无需 sudo）：**
```bash
conda install -c conda-forge k6 -y
k6 version
```

**有 sudo（Ubuntu/Debian）：**
```bash
sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
    --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
    | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install -y k6
```

**macOS：** `brew install k6`

---

## 远程 k6 方案（推荐）

**前置条件**：k6 运行机能 SSH 到 prod + 能访问 `https://thccb.secret-sealing.club`。

> **开发服务器即是 k6 runner**：如果你从开发机打流量到生产，codebase 已经在
> `/data/sunyunbo/www/TouhouCCB`，`scenarios/` 不用传。只需：
> 1. prod 上跑 seed + mint → 开发机上 `PROD=deploy@<prod-ip> ./loadtest/seed/pull_tokens.sh`
> 2. prod nginx geo 白名单加 `162.105.151.134/32`（开发机公网 IPv4）
> 3. 开发机上 `export BASE_URL=https://thccb.secret-sealing.club` 再跑 k6

### Step 1 — prod 上：备份 + seed

```bash
# SSH 到 prod
ssh deploy@<prod-ip>
cd /home/deploy/TouhouCCB

./loadtest/seed/backup_before.sh
./loadtest/seed/seed_users.sh
./loadtest/seed/seed_markets.sh
# 记下 [LT] HOT_2OPT 的 market.id 和两个 outcome.id

./loadtest/seed/mint_tokens.sh
# -> loadtest/tokens.txt（含敏感 JWT，全程保密）
```

### Step 2 — 把 tokens.txt 取到 k6 机器

**如果 k6 运行在开发机**（codebase 已在本地，scenarios/ 不用传）：
```bash
# 在开发机上执行
PROD=deploy@<prod-ip> ./loadtest/seed/pull_tokens.sh
# -> loadtest/tokens.txt
```

**如果 k6 运行在其他机器**（先在 prod 上推过去）：
```bash
# 在 prod 上执行
REMOTE=<user@目标机> ./loadtest/seed/transfer_tokens.sh
# 传 tokens.txt + scenarios/ 到远端 ~/thccb-loadtest/
```

### Step 3 — prod nginx：把远程机 IP 加入白名单

编辑 `deploy/nginx.conf`，找到 `geo $loadtest_ip` 块，取消注释并填入远程机 IP：

```nginx
geo $loadtest_ip {
    default            0;
    127.0.0.1          1;
    1.2.3.4/32         1;   # ← 填你的压测机公网 IP
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

> 不加白名单会被 `api_trade` 限速（10r/s burst=20）打成一片 429，数据无意义。

### Step 4 — 远程机：设置环境变量，跑测试

```bash
# SSH 到远程机（或在本机终端）
cd ~/thccb-loadtest

export BASE_URL=https://thccb.secret-sealing.club
export HOT_MARKET_ID=23
export HOT_OUTCOME_YES=72
export HOT_OUTCOME_NO=73
# tokens.txt 已在 ~/thccb-loadtest/tokens.txt，auth.js 自动读取

# smoke 先验（1 VU × 60s）
k6 run scenarios/smoke.js
# 全绿再继续

# 主压测（结果存本地，事后可 scp 到 prod 归档）
RUN=trade_$(date -u +%Y%m%dT%H%M%SZ)
k6 run --out json=k6_${RUN}.json scenarios/trade_ramp.js
```

### Step 5 — prod 上：同时启观测脚本

```bash
# prod 上另一个 tmux 窗口
RUN=<和远程机相同的 RUN_TAG>
./loadtest/record/host.sh "$RUN" &
./loadtest/record/pg.sh start "$RUN"
./loadtest/record/app_tail.sh "$RUN" &
```

k6 跑完后停观测：
```bash
./loadtest/record/pg.sh stop "$RUN"
kill %1 %2
```

### Step 6 — 收尾

```bash
# 远程机：删 token
rm ~/thccb-loadtest/tokens.txt

# prod：删 token + 清数据
rm loadtest/tokens.txt
./loadtest/cleanup/cleanup.sh

# nginx：移除白名单 IP，reload
# 编辑 deploy/nginx.conf，注释掉 1.2.3.4/32 那行
sudo nginx -t && sudo systemctl reload nginx
```

---

## 同机方案（快速验证逻辑用）

```bash
cd /home/deploy/TouhouCCB

# 1. 备份
./loadtest/seed/backup_before.sh

# 2. seed
./loadtest/seed/seed_users.sh
./loadtest/seed/seed_markets.sh

# 3. 签 token
./loadtest/seed/mint_tokens.sh

# 4. 环境变量（同机直连 uvicorn，不过 nginx）
export BASE_URL=http://127.0.0.1:8004
export HOT_MARKET_ID=<id>
export HOT_OUTCOME_YES=<id>
export HOT_OUTCOME_NO=<id>

# 5. smoke
k6 run loadtest/scenarios/smoke.js

# 6. 观测（三个 tmux 窗口）
RUN=trade_$(date -u +%Y%m%dT%H%M%SZ)
./loadtest/record/host.sh "$RUN" &
./loadtest/record/pg.sh start "$RUN"
./loadtest/record/app_tail.sh "$RUN" &

# 7. 主压测
k6 run --out json=loadtest/results/k6_${RUN}.json loadtest/scenarios/trade_ramp.js

# 8. 停观测
./loadtest/record/pg.sh stop "$RUN"
kill %1 %2

# 9. （可选）SSE fan-out
RUN=sse_$(date -u +%Y%m%dT%H%M%SZ)
./loadtest/record/host.sh "$RUN" &
./loadtest/record/pg.sh start "$RUN"
k6 run --out json=loadtest/results/k6_${RUN}.json loadtest/scenarios/sse_fanout.js
./loadtest/record/pg.sh stop "$RUN"
kill %1
```

---

## abort 阈值

k6 自带 thresholds 触发任一即自动结束，但**人也要看着**：

- `http_req_duration p(99) > 2000ms` 持续 30s → 立即 Ctrl-C
- `host.sh` 里 `load1 > 4` 持续 30s（2C 机 load1 大致是 CPU 数 × 2 警戒）
- `pg_active_count > 30`（DB 连接池上限是 `DB_POOL_SIZE=10 + DB_MAX_OVERFLOW=20 = 30`）
- 后端日志出现 `invariant violated`（loan_service 的兜底，意味着钱算错了）

---

## 收尾（**每次跑完必做**）

```bash
# 1. 删 token（含 SECRET_KEY 签出的 JWT，泄露 = 任意账户接管）
rm loadtest/tokens.txt
# 远程机器也要删：ssh <remote> rm ~/thccb-loadtest/tokens.txt

# 2. 清 loadtest 数据
./loadtest/cleanup/cleanup.sh
#   会问 yes 确认；只删 casdoor_id LIKE 'loadtest:%' 和 [LT]% 的市场

# 3. 移除 nginx 白名单 IP
#    编辑 deploy/nginx.conf，注释掉 geo $loadtest_ip 块里的压测机 IP
#    sudo nginx -t && sudo systemctl reload nginx
```

## 翻车恢复

```bash
./loadtest/cleanup/restore.sh backups/loadtest_pre_<TS>.sql.gz
```

会停 backend、用 `--clean --if-exists` 模式覆盖整个库、重启 backend。**只在
cleanup.sql 也救不回来时用**。

---

## 文件清单

```
loadtest/
├── seed/
│   ├── seed_users.sh          # 200 个 loadtest_NNN 用户（cash=100000）
│   ├── seed_markets.sh        # 5 个 [LT] 市场覆盖热/冷/高 b/低 b/多选
│   ├── mint_tokens.py         # 容器内执行体：复用 create_access_token 离线签 JWT
│   ├── mint_tokens.sh         # docker cp + exec wrapper
│   ├── transfer_tokens.sh     # 从 prod 推 tokens.txt + scenarios/ 到远程机
│   ├── pull_tokens.sh         # 从 prod 拉 tokens.txt 到本机（开发机作为 k6 runner）
│   └── backup_before.sh       # pg_dump 全库 → backups/loadtest_pre_*.sql.gz
├── scenarios/
│   ├── lib/auth.js            # SharedArray 加载 tokens.txt + VU→token 分发
│   ├── smoke.js               # 1 VU × 60s 端点验证
│   ├── trade_ramp.js          # 1→200 VU 阶梯，集中打 HOT_2OPT
│   ├── sse_fanout.js          # 200 SSE 订阅 + 50 background trader
│   └── .env.example
├── record/
│   ├── host.sh                # CPU/mem/iowait/load + docker stats 1Hz CSV
│   ├── pg.sh                  # pg_stat_activity / pg_locks 周期；pg_stat_statements 跑前重置跑后 dump
│   └── app_tail.sh            # tail backend 日志，过滤 >500ms 和 5xx
├── cleanup/
│   ├── cleanup.sql            # 按 FK 顺序 DELETE 所有 loadtest 痕迹
│   ├── cleanup.sh             # 包装 + 确认提示
│   └── restore.sh             # 从 *.sql.gz 完整恢复（最后手段）
└── results/                   # 所有日志和 k6 JSON 输出
```

---

## 设计说明

- **远程 k6 + nginx geo 白名单**：`$rate_key` 对白名单 IP 为空字符串，nginx `limit_req` 用空 key 时请求不进入任何限速桶，等同于无限速。比改 header 或 location 条件更干净。
- **离线签 JWT**：不动 Casdoor。SECRET_KEY 是同一把，签出来的 token 后端 100% 接受。
- **集中打 HOT_2OPT**：真实生产里有热门事件（"东方深秘录新作几号发"）就是这种形态——所有人挤一个 outcome 的买卖，最坏锁竞争。分散打反而测不到瓶颈。
- **DELETE 不 TRUNCATE**：CLAUDE.md 红线。
- **白名单按 IP 不按 header**：nginx `limit_req` 不能条件 skip，只能让 key 为空。`geo` 按源 IP 是最干净的写法，比改 `X-Loadtest` 头骚扰真实代理链稳。
