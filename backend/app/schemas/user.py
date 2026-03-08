from datetime import datetime
from typing import Optional

from fastapi_users import schemas
from pydantic import BaseModel, ConfigDict


class UserRead(schemas.BaseUser[int]):
    username: str
    cash: float
    debt: float

    model_config = ConfigDict(from_attributes=True)


class UserCreate(schemas.BaseUserCreate):
    username: str
    # 虽然这里允许输入，但 UserManager 逻辑会进行拦截
    cash: float = 100.0
    is_superuser: bool = False


class UserUpdate(schemas.BaseUserUpdate):
    username: Optional[str] = None
    cash: Optional[float] = None
    is_superuser: Optional[bool] = None


class HoldingRead(BaseModel):
    market_id: int
    market_title: str
    outcome_id: int
    outcome_label: str
    amount: float


class UserSummary(BaseModel):
    cash: float
    debt: float
    holdings_value: float
    net_worth: float
    rank: str


class TransactionRead(BaseModel):
    id: int
    type: str  # buy, sell, settle
    shares: float
    price: float
    cost: float
    timestamp: datetime