from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ActivateRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=64)


class CreateActivationCodesRequest(BaseModel):
    count: int = Field(ge=1, le=500)
    length: int = Field(default=16, ge=8, le=64)


class ActivationCodeRead(BaseModel):
    id: int
    code: str
    is_used: bool
    used_by_user_id: Optional[int] = None
    used_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
