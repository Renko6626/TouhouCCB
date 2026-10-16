# FX preset 交接入口

当前生产默认：[预热EMA10版](inventory_ema/SELECTED_CONFIG.json)，10分钟EMA实验及指标见[说明](inventory_ema/README.md)。活动当天继续采用[冻结版](intraday_6_8h/FROZEN_CONFIG.json)。总览见[README](README.md)。

运行参数在backend/app/services/pve_fx/presets.py，时间用秒，创建时保存完整个人参数。修改预设不会自动改写已有账户资金或参数；填入后保存运行设置，新账户默认暂停。

业务重置按[既有操作单](../../docs/business-reset.md)执行，保留身份称号、真人现金恢复源库initial_balance；开市和重新创建机器人由管理员后续操作。
