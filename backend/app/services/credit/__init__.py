"""统一信贷风险执行包（spec 2026-09-29；计划 §3.1 冻结模块图）。

WP1 只交付基座与纯函数部分：

    keys.py        GroupKey / 命名空间 / 排序（纯函数）
    thresholds.py  R_initial / R_maintenance 派生与比较（纯函数）
    version.py     bump_economic_version(user)
    flags.py       进程级只读开关缓存 + 启动加载 + 测试覆写

后续工作包在同一包内补齐（接口已在计划 §3.2 冻结，不得改名）：

    lmsr_quote.py / fx_quote.py / valuation.py / gates.py / risk.py
    runs.py / execution.py / sweep.py / ownership.py

约定：本包**不 commit**、不发 SSE、不持全局锁；事务边界由调用方负责。
"""
