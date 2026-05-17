"""策略注册表：config.yaml 里 type 字段映射到类。"""
from typing import Dict, Type

from thccb_quant.strategy.base import Strategy

STRATEGY_REGISTRY: Dict[str, Type[Strategy]] = {}


def register(type_name: str):
    def deco(cls: Type[Strategy]) -> Type[Strategy]:
        STRATEGY_REGISTRY[type_name] = cls
        return cls
    return deco


def get_strategy_class(type_name: str) -> Type[Strategy]:
    if type_name not in STRATEGY_REGISTRY:
        raise KeyError(f"unknown strategy type: {type_name}")
    return STRATEGY_REGISTRY[type_name]
