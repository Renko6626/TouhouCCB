# FX 百人实机压测

服务器通过手动脚本或 GitHub Action 准备和清理数据，开发机通过网站 HTTPS 入口运行压测。无需向压测机提供服务器密钥、数据库密码或 JWT 签名密钥。

本套件覆盖页面初始读取、成交后刷新、图表历史/补尾、现金买卖、融资还款、开空回补、少量跨零、挂单及两类 SSE。百人同时登录、系统干预、PvE 混合和强平不在本轮默认场景内。HTTP 结果也不代表现场手机渲染和 Wi-Fi 性能。

## 通过 Action 建号并下载加密凭证（推荐）

先将本分支合入 main 并完成正常部署。新 workflow 必须存在于 main；服务器镜像的 `python -m scripts.fx_loadtest --version` 必须返回 `fx-loadtest-2`，并已有 `deploy/fx_loadtest.sh`。Action 不会替你部署应用或修改 Nginx。

工作流复用 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_KEY`，仅仓库 owner Renko6626 在 main 手动触发，与部署共用生产互斥。开发机需有已登录的 `gh` 和现有 backend Python 环境。先完成后面第 1 节的限流发布，再执行：

```bash
# 在开发机仓库根目录；私钥目录必须在仓库之外。
backend/venv/bin/python loadtest/fx_credentials.py keygen \
  --directory "$HOME/.local/share/thccb-loadtest"

# 确认实际 active pair ID。下面仍以 8 为例；run ID 每轮换一个。
backend/venv/bin/python loadtest/fx_credentials.py request \
  --directory "$HOME/.local/share/thccb-loadtest" \
  --operation prepare --run-id fx100_20261017b --source-pair 8 --confirm PREPARE

gh run list -R Renko6626/TouhouCCB --workflow fx-loadtest.yml --limit 5
```

Action 在服务器备份、停写并准备 100 个账户和专用市场，恢复原先运行的服务后签发 60 分钟 token，用你的 RSA 公钥和 AES-GCM 加密，再删除服务器明文 token 文件。artifact 仅包含 `credentials.enc.json`，保留 1 天；私钥始终留在开发机，日志不打印 token 或签名密钥。

运行成功后把 `GITHUB_RUN_ID` 换成刚才的 Actions run ID：

```bash
backend/venv/bin/python loadtest/fx_credentials.py download \
  --directory "$HOME/.local/share/thccb-loadtest" \
  --run-id fx100_20261017b --github-run GITHUB_RUN_ID
```

命令会打印解密后的私有 token 文件路径，第 4 节 `--tokens` 使用该路径。目录 0700、文件 0600。artifact 可公开下载也无法在没有私钥时还原凭证；不要把私钥提交 Git。错过 60 分钟后用同一个目录/公钥请求 `--operation mint --run-id fx100_20261017b`，下载新 run 的 artifact，不重新建号。

只看服务器数据用 `--operation inspect`；结束后用下面命令清理（会备份、停写，并只删除该轮数据）：

```bash
backend/venv/bin/python loadtest/fx_credentials.py request \
  --directory "$HOME/.local/share/thccb-loadtest" \
  --operation cleanup --run-id fx100_20261017b --confirm CLEANUP
```

成功下载不代表已测容量；Action 只管理账户，下面的 HTTP 压测仍由开发机运行。失败时先查看该 workflow 日志和数据 preview，不自动重试或清空现场。以下第 2、3、6 节保留手动服务器操作方式，与 Action 二选一；同一轮不要重复 prepare。

## 1. 部署本版本和限流调整

先按现有部署流程发布包含下列文件的后端镜像及服务器脚本：

- `backend/scripts/fx_loadtest.py` 和 `backend/scripts/fx_loadtest_credentials.py`
- `backend/app/services/realtime.py`：同 IP、同公开市场最多 200 条 SSE，市场总上限仍为 500。
- `deploy/fx_loadtest.sh`：放在服务器实际项目的 `deploy/` 下。

不要把 `deploy/nginx.conf` 原样覆盖线上站点：它含占位域名、证书路径。对实际站点副本应用本次差异：

- 新增 `$authenticated_rate_key` map：无 Authorization 时回退 `$client_ip_key`，其余按 Authorization 分桶。
- `$rate_key_user` 非白名单分支使用 `$authenticated_rate_key`。
- 新增 `api_general_user` zone，20 r/s；通用 API 和精确 `/api/v1/auth/me` 使用它，burst 40。
- FX 报价/成交嵌套路由及挂单使用现有 `api_trade_user`，10 r/s，burst 20。
- 保留管理、登录/OAuth/refresh 的现有限流；保留两类 SSE 的关闭缓冲与长连接设置。不要给压测机加白名单。

服务器执行配置校验与 reload，然后按正常流程重启/部署后端：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

若线上有 CDN/ESA，先核对真实 IP 传递。Nginx 白名单不能解除后端 SSE IP 限制；本轮不伪造转发头来绕过限制。token 桶按 token 字符串区分，JWT 有效性仍由后端校验。

## 2. 服务器准备（项目根目录）

以下脚本针对当前 Docker Compose 单 backend 部署。先确认没有其他经济写实例，选择维护窗口；此时普通玩家不应参与测试市场。准备会暂时暂停原 FX 市场，新建池子规模与费率相同的专用市场，创建 100 个初始现金为 100000 的普通账户。它们不是机器人、没有管理员权限；仅测试账户预置 TOS 接受时间。

```bash
cd /home/deploy/TouhouCCB
curl -fsS https://thccb.secret-sealing.club/api/v1/fx/pairs
# 先核对实际 ID；下面以本次检查时的 8 为例。
bash deploy/fx_loadtest.sh prepare fx100_20261017 8
# 输入 PREPARE
```

脚本先检查镜像中工具存在，确认后停止 backend、备份、运行一次性维护容器，再恢复原先运行的 backend。已有同 run 或其他压测账户时拒绝覆盖。stdout 只显示市场 ID、人数和 manifest 路径。

```bash
docker compose ps
docker compose logs --tail 40 backend
curl -fsS https://thccb.secret-sealing.club/api/v1/fx/pairs
bash deploy/fx_loadtest.sh inspect fx100_20261017
bash deploy/fx_loadtest.sh mint fx100_20261017
```

`mint` 不需要停服，只给本轮普通账户签发 **60 分钟** access token，不发 refresh token，不修改全站有效期。临近正式压测再执行，过期可重跑 mint。服务器文件：

```text
backups/fx-loadtest/fx100_20261017/manifest.json
backups/fx-loadtest/fx100_20261017/tokens.json
```

目录 0700、文件 0600。token 泄露会暴露这些测试账户；它不含服务器签名密钥。不要贴聊天、提交 Git 或放公开下载地址。备份留在服务器，清理脚本不自动恢复整库。

原市场的系统事件、PvE 等配置不复制给新市场。manifest 记录准备时的 `fx_*` 配置；无对应专用市场配置时，不将这次结果说成系统干预/机器人混合压测。需要这些混合场景时先配置明确负载再另跑。

## 3. 将凭证复制到开发机

在开发机执行，`YOUR_SERVER_SSH_ALIAS` 换成你使用的 SSH 地址/别名；命令中的目标私有目录应在仓库之外：

```bash
mkdir -p "$HOME/.local/share/thccb-loadtest"
chmod 700 "$HOME/.local/share/thccb-loadtest"
scp YOUR_SERVER_SSH_ALIAS:/home/deploy/TouhouCCB/backups/fx-loadtest/fx100_20261017/tokens.json \
  "$HOME/.local/share/thccb-loadtest/fx100_20261017-tokens.json"
chmod 600 "$HOME/.local/share/thccb-loadtest/fx100_20261017-tokens.json"
```

也可以通过你自己的 SFTP 客户端下载。将开发机文件路径交给执行压测的人即可。

## 4. 开发机运行

使用已安装 backend requirements 的 Python 环境（含 httpx 0.28.1），从仓库根目录运行。套件校验 token 中的专用市场标记与线上 snapshot，拒绝向原市场发测试成交。

```bash
backend/venv/bin/python loadtest/fx_http.py \
  --tokens "$HOME/.local/share/thccb-loadtest/fx100_20261017-tokens.json" \
  --mode smoke
```

默认 1 人 60 秒，要求真实买卖至少两笔成功并收到公开 snapshot 和私人 sync，出现失败不会进入大流量。确认结果后运行：

```bash
backend/venv/bin/python loadtest/fx_http.py \
  --tokens "$HOME/.local/share/thccb-loadtest/fx100_20261017-tokens.json" \
  --mode full --users 100
```

full 先跑单人冒烟，再以约 0.6 秒间隔让其余账户入场。每账户维持公开行情和私人账户各一条 SSE。默认角色为 60% 现金、20% 融资、20% 开空/回补，空头角色每四人选一人覆盖跨零；先用真实成交验证角色可用，准备单单列。

| 阶段 | 100 人安排 | 名义成交尝试/s | 时长 |
| --- | --- | ---: | ---: |
| readonly | 全员看行情及账户版本 | 0 | 120s |
| normal | 40 人每 15–25s 一笔、30 人每 45–75s 一笔 | 2.5 | 300s |
| active | 70 人每 8–12s 一笔、20 人每 30–50s 一笔 | 7.5 | 300s |
| stress | 全员每 3–5s 一笔 | 25 | 120s |
| burst | 准备真实卖出持仓并预取报价，买卖各一半在目标 2 秒内提交 | 短时 50 | 再观察 60s |
| orders | 最多 20 个现金账户各建一张单，一半撤销、一半等待真实扫描执行 | 独立报告 | 120s |

人数少于 100 时按比例缩放。每账户最多一笔在途，不追赶补发，服务变慢时实际频率可能低于目标。burst 报告实际客户端提交窗口；预取报价和准备持仓不计入窗口。

`--amount` 默认 10，每次随机采用 1–5 倍买入预算（10–50 金圆券），融资借入其中一半；卖出和回补使用真实持仓/债务。每笔真实 quote 返回账户版本，请求编号唯一，没有自动重试。混合场景要求现有 `loan_enabled` 和 `fx_short_enabled` 已打开，不会替你修改开关或杠杆；若只想明确测现货基线，使用 `--cash-only`，报告会标明缺少融资/空头角色。

成功成交后调用 context、snapshot 和个人成交记录刷新；私人账户事件在 200ms 窗口合并，价格流已覆盖的版本不重复取价格修复。图表先读封存段、复用有效 SSE 尾段，需要时请求 `/chart` 补尾；少量用户在 active 前切换一次周期。新市场历史较浅，不能据此宣称成熟大历史容量通过。每笔仍多一次服务端 quote，主报告注明相比本地报价页面增加的 HTTP 负载。

需要时设置 `--orders-seconds 0` 明确跳过挂单；挂单扫描间隔维持服务器原配置，观察期内没有实际触发会在 coverage 中标记未覆盖，不算成测试通过。初始挂单列表和结果刷新同样测量，创建/撤销不算成交吞吐。

只在隔离调试时缩短 `--smoke-seconds`、`--readonly-seconds`、`--normal-seconds`、`--active-seconds`、`--stress-seconds`、`--burst-observe-seconds`、`--orders-seconds`；正式保留默认观察时长。`--min-interval`/`--max-interval` 仅调整单人冒烟节奏。Ctrl-C 停测会保存已有结果。

稳态滚动 30 秒内 429/5xx/网络错误/超时超过 5%，或成功成交 P99 超过 2 秒，会停止增加请求并排空。401/403 立即停测；行情订阅断开也停止升压。集中下单单独报告，不套用稳态延迟中止阈值。失败不自动删除测试数据。先检查普通/活跃阶段通过再解释加压结果；加压失败不等于正常人数不能使用。

## 5. 查看结果

默认输出在 `loadtest/results/<run-id>_<时间戳>/`，可用 `--output <新目录>` 指定：

- `summary.md`：便于直接查看的阶段吞吐与各交易类型 P95/P99 表。
- `summary.json`：完整阶段、角色、实际活跃人数、连接数、SSE 帧数/负载字节、成功成交/s、实际循环/s、HTTP 完成/s、节奏延迟和 P50/P95/P99；成功、失败、实际覆盖与未覆盖分开。
- `requests.jsonl`：请求完成时间、阶段、耗时和分类，不含 token、Authorization 或账户响应体。
- 控制台每 5 秒给出当前阶段、连接数和近 30 秒请求数。

SSE 报告有合法首帧耗时、帧间隔、事件年龄和异常断连；不自动重连。关联到成交 ID 时，另报从请求开始到第一个固定观察者收到该成交的时间；它包含成交处理时间，只代表该观察者，不代表全部 100 人。未匹配事件单列。事件年龄依赖服务器和开发机时钟，不能当精确网络延迟。

本套件没有服务器 CPU/内存/数据库锁数据；如有现成监控可同时查看。不使用该域名 `/health` 的 200 判断后端存活，因为本次检查该路径返回前端 HTML。

## 6. 服务器收尾

先停止开发机压测，再回服务器执行：

```bash
cd /home/deploy/TouhouCCB
bash deploy/fx_loadtest.sh cleanup fx100_20261017
# 先显示待删除行数，核对后输入 CLEANUP。
docker compose ps
curl -fsS https://thccb.secret-sealing.club/api/v1/fx/pairs
```

cleanup 再备份、停写，事务删除 manifest 指定的本轮账户、专用市场及依赖，并恢复原市场的交易状态。发现其他玩家参与、测试账户用于其他业务或原市场状态已改变时拒绝删除；查看日志后人工处理，不扩大清理范围。manifest 和备份保留，服务器本轮 token 文件删除。若 manifest 写成后数据库提交失败，保留文件并 inspect，不自行删 manifest 强行重复准备。

最后在开发机删除下载的 token 文件：

```bash
rm "$HOME/.local/share/thccb-loadtest/fx100_20261017-tokens.json"
```

不要使用旧 `loadtest/cleanup/cleanup.sql` 清 FX，不使用全库恢复撤销正常业务。只有服务器脚本输出与恢复后的接口结果都核对过，才算完成服务器收尾。
