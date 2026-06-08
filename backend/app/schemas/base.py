from decimal import Decimal
from typing import Annotated

from pydantic.functional_serializers import PlainSerializer

# Decimal 字段在 JSON 序列化时输出为 number 而非 string
Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float)]
Price = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float)]
