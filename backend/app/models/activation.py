# app/models/activation.py  (示例)
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field

class ActivationCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True, nullable=False)

    # 是否已使用
    is_used: bool = Field(default=False, nullable=False)

    # 谁用掉的（可选）
    used_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    used_at: Optional[datetime] = None

    # 发行信息（可选）
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")

    # 可加：expires_at / note / batch_id 等
