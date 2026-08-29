"""PvE 机器人引擎。

设计 spec：docs/superpowers/specs/2026-08-29-pve-bots-design.md

模块分工：
- templates.py    人格模板（decide 纯函数化，便于单测）+ MarketView/BotState 数据结构
- attention.py    注意力模型：看盘间隔 / 作息窗口 / 行情推送唤醒（纯函数）
- market_view.py  每轮统一构建的市场快照
- client.py       回环 HTTP 下单客户端（进程内签 JWT + HMAC client token）
- engine.py       调度核心：唤醒批次、护栏、执行、死亡判定、决策环形日志
- scheduler.py    lifespan 挂载的 APScheduler 包装（同 bot_detection 模式）
- naming.py       机器人用户名生成（辨识度款 / 低调款）
- service.py      账户池操作（批量生成 / 注资复活），供 admin API 调用
"""
