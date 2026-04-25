import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.redemption import parse_csv_codes


def test_parse_csv_simple_lines():
    text = "ABC123\nDEF456\nGHI789\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC123", "DEF456", "GHI789"]
    assert invalid == []


def test_parse_csv_skips_header_named_code():
    text = "code\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_trims_whitespace_and_empty_lines():
    text = "  ABC  \n\n   \n DEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]


def test_parse_csv_marks_too_long_as_invalid():
    long = "X" * 129
    text = f"OK\n{long}\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["OK"]
    assert invalid == [long]


def test_parse_csv_dedupes_within_input():
    text = "ABC\nABC\nDEF\n"
    valid, invalid = parse_csv_codes(text)
    assert valid == ["ABC", "DEF"]
