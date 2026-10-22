from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from urllib.parse import urlsplit

RoundKey = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
Position = Annotated[int, Field(strict=True, ge=0, le=1000)]


class IntegrationConfigUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    enabled: bool = Field(strict=True)
    returned_url: str = Field(default='', max_length=2048)
    secret: str = Field(default='', max_length=512, repr=False)

    @field_validator('secret')
    @classmethod
    def valid_secret(cls, value):
        if value and len(value) < 32:
            raise ValueError('密钥至少需要 32 个字符，留空则保留原密钥')
        return value

    @field_validator('returned_url')
    @classmethod
    def valid_url(cls, value):
        if not value:
            return ''
        try:
            url = urlsplit(value)
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment or any(c.isspace() for c in value)):
                raise ValueError()
            url.port
        except ValueError:
            raise ValueError('请输入不含账号、查询参数或片段的 HTTP(S) 地址') from None
        return value.rstrip('/')


class IntegrationConfigRead(BaseModel):
    enabled: bool
    returned_url: str
    secret_configured: bool
    active_round_key: str | None


class RoundStart(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    round_key: RoundKey
    game_key: RoundKey
    round_number: int = Field(ge=1, strict=True)
    left_label: str = Field(min_length=1, max_length=200)
    right_label: str = Field(min_length=1, max_length=200)
    team_name: str = Field(default='', max_length=100)


class RoundResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_position: Position


class PredictionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    position: Position


class RewardBand(BaseModel):
    max_distance: int
    payout: Decimal


class RoundPublic(RoundStart):
    status: Literal['open', 'closed', 'settled', 'cancelled']
    stake: Decimal
    bands: list[RewardBand]
    target_position: int | None
    created_at: datetime
    closed_at: datetime | None
    finished_at: datetime | None


class RoundSummary(RoundPublic):
    participant_count: int
    total_payout: Decimal
    total_refund: Decimal


class RoundState(BaseModel):
    round_key: RoundKey
    status: Literal['missing', 'open', 'closed', 'settled', 'cancelled']
    exists: bool
    summary: RoundSummary | None = None


class MyPrediction(BaseModel):
    round_key: str
    status: str
    position: int | None
    stake: Decimal
    distance: int | None
    payout: Decimal | None
    refund: Decimal
    net_profit: Decimal | None
    submitted_at: datetime | None


class TopPrediction(BaseModel):
    username: str
    position: int
    distance: int
    payout: Decimal


class RoundResults(BaseModel):
    round_key: str
    target_position: int
    top_five: list[TopPrediction]
