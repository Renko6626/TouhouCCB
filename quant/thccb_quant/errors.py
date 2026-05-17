"""量化交易脚本异常分类。spec §8。"""


class QuantError(Exception):
    """所有量化脚本异常基类。"""


class FatalAuthError(QuantError):
    """Refresh token 失效或刷新失败，全局停机。"""


class RiskRejected(QuantError):
    """风控规则拒绝下单，策略 catch 后跳过本轮。"""


class TransientError(QuantError):
    """5xx / 网络超时，rest 已重试过仍失败。"""


class BusinessError(QuantError):
    """4xx 业务错（余额不足、份额不足、市场已关等）。"""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class StrategyError(QuantError):
    """策略自身 bug，runner 隔离单策略停其他继续。"""
