"""
Secure credential vault — Fernet-encrypted, stored in Redis.
Set VAULT_MASTER_KEY env var for real encryption.
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)


class SecureVault:
    _HASH = "vault:secrets"

    def __init__(self, redis):
        master = os.getenv("VAULT_MASTER_KEY", "")
        if not master:
            log.warning("VAULT_MASTER_KEY not set — vault encryption is weak! Set it in Railway Variables.")
            master = "insecure-default-key-change-me-now"
        raw = hashlib.sha256(master.encode()).digest()
        self._f = Fernet(base64.urlsafe_b64encode(raw))
        self._r = redis

    def _encrypt(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def _decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()

    async def save(self, name: str, value: str):
        encrypted = self._encrypt(value)
        await self._r.hset(self._HASH, name, encrypted)

    async def get(self, name: str) -> str | None:
        raw = await self._r.hget(self._HASH, name)
        if raw is None:
            return None
        try:
            return self._decrypt(raw)
        except Exception:
            return raw  # fallback if somehow stored unencrypted

    async def list_keys(self) -> list[str]:
        keys = await self._r.hkeys(self._HASH)
        return list(keys or [])

    async def delete(self, name: str):
        await self._r.hdel(self._HASH, name)
