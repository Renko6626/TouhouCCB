import json
from pathlib import Path

from thccb_quant.logging_setup import setup_logging, get_logger


def test_log_emits_json_with_fields(tmp_path: Path):
    log_dir = tmp_path / "logs"
    setup_logging(log_dir, "system")
    log = get_logger("test", strategy="grid_x", outcome_id=42)
    log.info("hello", extra_field="v")

    files = list(log_dir.glob("*.jsonl"))
    assert len(files) >= 1
    line = files[0].read_text().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["strategy"] == "grid_x"
    assert payload["outcome_id"] == 42
    assert payload["extra_field"] == "v"
