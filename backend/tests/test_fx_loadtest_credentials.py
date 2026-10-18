"""Public artifacts must require the recipient private key and detect tampering."""
import base64
import json

import pytest
from cryptography.exceptions import InvalidTag

from scripts.fx_loadtest import export_credentials
from scripts.fx_loadtest_credentials import decrypt, generate_keys


def test_export_roundtrip_removes_plaintext_and_rejects_tampering(tmp_path):
    private, public = generate_keys()
    (tmp_path / "recipient.pem").write_bytes(public)
    original = {"run_id": "crypto_test", "users": [{"uid": 1, "token": "private-test-credential"}]}
    (tmp_path / "tokens.json").write_text(json.dumps(original))
    output = export_credentials(tmp_path, "crypto_test", tmp_path / "recipient.pem")
    assert not (tmp_path / "tokens.json").exists()
    assert output.stat().st_mode & 0o777 == 0o600
    assert "private-test-credential" not in output.read_text()
    envelope = json.loads(output.read_text())
    assert json.loads(decrypt(envelope, private)) == original
    wrong_private, _ = generate_keys()
    with pytest.raises(ValueError):
        decrypt(envelope, wrong_private)
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode()
    with pytest.raises(InvalidTag):
        decrypt(envelope, private)
