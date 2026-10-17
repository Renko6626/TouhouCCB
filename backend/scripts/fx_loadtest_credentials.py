"""Encrypt test-user credentials for download from a public Actions artifact."""
import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"thccb-fx-loadtest-credentials-v1"


def generate_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def recipient_key(pem: bytes):
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("recipient must be an RSA public key of at least 3072 bits")
    return key


def encrypt(plaintext: bytes, public_pem: bytes) -> dict:
    public = recipient_key(public_pem)
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    encrypted_key = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                    algorithm=hashes.SHA256(), label=AAD))
    return {"version": 1, "key": base64.b64encode(encrypted_key).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, plaintext, AAD)).decode()}


def decrypt(envelope: dict, private_pem: bytes) -> bytes:
    if envelope.get("version") != 1:
        raise ValueError("unsupported credential envelope version")
    private = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError("expected RSA private key")
    key = private.decrypt(base64.b64decode(envelope["key"], validate=True),
                          padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD))
    return AESGCM(key).decrypt(base64.b64decode(envelope["nonce"], validate=True),
                               base64.b64decode(envelope["ciphertext"], validate=True), AAD)
