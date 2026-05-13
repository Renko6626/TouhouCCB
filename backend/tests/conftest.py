import pytest
from app.services.site_config import clear_cache


@pytest.fixture(autouse=True)
def reset_site_config_cache():
    """每个测试前清空 SiteConfig 缓存，防止直接写 DB 的测试拿到上一轮的缓存值。"""
    clear_cache()
