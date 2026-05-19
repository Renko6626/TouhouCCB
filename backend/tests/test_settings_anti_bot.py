import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.config import settings


def test_settings_has_client_token_secret():
    """settings 应该有 CLIENT_TOKEN_SECRET 字段（默认空 string）。"""
    assert hasattr(settings, "CLIENT_TOKEN_SECRET")
    # 测试环境没设 .env，应该是空 string（pydantic default）
    assert isinstance(settings.CLIENT_TOKEN_SECRET, str)
