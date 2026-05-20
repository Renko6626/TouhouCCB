import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
from unittest.mock import patch

import pytest

from app.services.anti_bot import (
    verify_client_token, parse_whitelist, generate_client_token_for_test,
)


def _make_token(secret: str, uid: int, ts: int) -> str:
    return generate_client_token_for_test(secret, uid, ts)


def test_verify_client_token_valid():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = "test_secret_32_bytes_hex_abcdef"
        ts = int(time.time())
        token = _make_token("test_secret_32_bytes_hex_abcdef", 1, ts)
        assert verify_client_token(token, str(ts), 1) is True


def test_verify_client_token_expired():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = "test_secret_32_bytes_hex_abcdef"
        ts = int(time.time()) - 60  # 60s ago, beyond 30s window
        token = _make_token("test_secret_32_bytes_hex_abcdef", 1, ts)
        assert verify_client_token(token, str(ts), 1) is False


def test_verify_client_token_tampered():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = "test_secret_32_bytes_hex_abcdef"
        ts = int(time.time())
        token = _make_token("test_secret_32_bytes_hex_abcdef", 1, ts)
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert verify_client_token(tampered, str(ts), 1) is False


def test_verify_client_token_wrong_uid():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = "test_secret_32_bytes_hex_abcdef"
        ts = int(time.time())
        token = _make_token("test_secret_32_bytes_hex_abcdef", 1, ts)
        # token 算给 uid=1，但当 uid=2 验
        assert verify_client_token(token, str(ts), 2) is False


def test_verify_client_token_invalid_ts():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = "test_secret_32_bytes_hex_abcdef"
        assert verify_client_token("any", "not_a_number", 1) is False


def test_verify_client_token_empty_secret():
    with patch("app.services.anti_bot.settings") as m:
        m.CLIENT_TOKEN_SECRET = ""
        ts = int(time.time())
        assert verify_client_token("any", str(ts), 1) is False


def test_parse_whitelist_empty():
    assert parse_whitelist("") == set()


def test_parse_whitelist_basic():
    assert parse_whitelist("1,5,12") == {1, 5, 12}


def test_parse_whitelist_with_spaces_and_garbage():
    assert parse_whitelist(" 1 , 5 , abc , 12 ") == {1, 5, 12}


def test_parse_whitelist_only_garbage():
    assert parse_whitelist("abc,xyz,") == set()
