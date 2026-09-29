"""用户经济状态版本（计划 §3.2 冻结签名）。

任何改现金 / 债务 / 持仓 / 钱包 / 授信冻结的路径都必须调用 ``bump_economic_version``；
调用方负责 commit。强平与交易检查在用户锁内重验版本：不一致就重新发现依赖，
禁止用过期快照放行（spec §6.2 第 4 条）。
"""
from __future__ import annotations

from app.models.base import User


def bump_economic_version(user: User) -> int:
    """就地 +1 并返回新值；不 flush、不 commit。"""
    user.economic_version = int(user.economic_version or 0) + 1
    return user.economic_version


def economic_version_of(user: User) -> int:
    """只读访问器：None 视作 0（老行 / 未加载字段的防御）。"""
    return int(user.economic_version or 0)
