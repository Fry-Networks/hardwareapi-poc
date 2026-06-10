"""
Algorand per-device wallet generation and AES-256-GCM mnemonic encryption.
Key loaded from DIIISCO_ENCRYPTION_KEY env var (32-byte hex string).
"""
import os
import base64
from algosdk import account, mnemonic as algo_mnemonic_module
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    raw = os.environ.get("DIIISCO_ENCRYPTION_KEY", "")
    if not raw or len(raw) != 64:
        raise RuntimeError("DIIISCO_ENCRYPTION_KEY must be a 64-char hex string (32 bytes)")
    return bytes.fromhex(raw)


def generate_device_wallet() -> dict:
    """Generate fresh Algorand account. Returns address and plaintext mnemonic."""
    private_key, address = account.generate_account()
    mnem = algo_mnemonic_module.from_private_key(private_key)
    return {"address": address, "mnemonic": mnem}


def encrypt_mnemonic(mnem: str) -> dict:
    """AES-256-GCM encrypt. Returns base64-encoded nonce + ciphertext (with auth tag)."""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, mnem.encode("utf-8"), None)
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "enc": base64.b64encode(ct).decode("ascii"),
    }


def decrypt_mnemonic(enc: str, nonce: str) -> str:
    """Decrypt AES-256-GCM ciphertext. Returns plaintext mnemonic."""
    key = _get_key()
    aesgcm = AESGCM(key)
    ct = base64.b64decode(enc)
    iv = base64.b64decode(nonce)
    return aesgcm.decrypt(iv, ct, None).decode("utf-8")
