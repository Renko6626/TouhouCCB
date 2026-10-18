# 主工作区集成后检查

执行目录：`/data/sunyunbo/www/TouhouCCB/backend`。

```bash
DATABASE_URL=sqlite+aiosqlite:////tmp/sunyunbo/fx-single-writer-root-verification-20261018.db venv/bin/python -m pytest -q tests/test_fx_writer.py tests/test_market_read_state.py tests/test_trading_context.py tests/test_trading_stream.py tests/test_admin_fx.py tests/test_fx_trading_service.py tests/test_fx_net_trading.py
```

实际输出结尾：`115 passed in 22.18s`，退出0。root `git diff --check` 退出0。迁移前521个既有文件哈希无变化；迁移后454个无关文件哈希无变化。迁移的70个路径中，仅原排行榜实现/API说明有既有差异，其余与候选一致；root每IP10额度保持。

实测代码6c76757；后续58101e1只更新计划交付状态，`git diff --quiet 6c76757 -- backend` 退出0，未为文档再跑负载。独立PG已由控制器fast stop成功；不涉及生产数据库或服务。
