# FX 百人实机压测

服务器手动准备和清理数据，开发机通过网站 HTTPS 入口运行压测。无需向压测机提供服务器密钥、数据库密码或 JWT 签名密钥。

本套件测普通资金买卖、报价、账户读取和两类 SSE。融资、跨零开空、回补、挂单和百人同时登录尚未覆盖。HTTP 结果也不代表现场手机渲染和 Wi-Fi 性能。

## 1. 部署本版本和限流调整

先按现有部署流程发布包含下列文件的后端镜像及服务器脚本：

- `backend/scripts/fx_loadtest.py`
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

full 自带单人冒烟，通过后以约 0.3 秒间隔接入其余用户。每个账户维持 1 条公开 FX SSE 和 1 条私人 SSE；接着进行 10/30/100 人交易各 60 秒、100 人稳态 300 秒，最后 100 人在约 2 秒内各发起一轮报价和买入并观察恢复 60 秒。这里 2 秒是交易循环起始窗口，实际提交时间还受 context 和 quote 延迟影响，样本时间与排队延迟会记录；不声称所有写请求必定在 2 秒内抵达服务端。

每账户最多一笔在途成交，默认每 5–10 秒循环一次，买入预算 10、下一次卖出真实余额；账户版本用真实报价结果，每笔请求编号唯一，没有自动重试。默认 100 人平均约 13.3 次成交循环/秒，报告同时列实际值。每次循环调用 context 和服务器 quote，当前 UI 可本地报价，因此该场景对 HTTP 读取施加了额外负载。

`--amount` 可调整买入预算。只在隔离环境调试时缩短 `--smoke-seconds`、`--ramp-seconds`、`--steady-seconds` 和 `--burst-observe-seconds`；正式测试保留默认观察时长。Ctrl-C 可停测并保留已收集报告。

稳态滚动 30 秒内 429/5xx/网络错误/超时超过 5%，或成功成交 P99 超过 2 秒，会停止增加请求并排空。401/403 立即停测；行情订阅断开也停止升压。集中下单单独报告，不套用稳态延迟中止阈值。失败不会触发自动删除测试数据。

## 5. 查看结果

默认输出在 `loadtest/results/<run-id>_<时间戳>/`，可用 `--output <新目录>` 指定：

- `summary.json`：阶段、实际活跃人数、连接数、成功成交/s、实际循环/s、HTTP 完成/s、节奏延迟及请求 P50/P95/P99；成功和各类失败分开。
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
