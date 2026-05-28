import base64
import hashlib
import os

from cryptography.fernet import Fernet
import redis.asyncio as aioredis


class SecureVault:
    """Fernet-encrypted key-value store for credentials and secrets."""
    _HASH = "vault:secrets"

    def __init__(self, redis: aioredis.Redis):
        master = os.getenv("VAULT_MASTER_KEY", "")
        if not master:
            raise RuntimeError("VAULT_MASTER_KEY is not set")
        raw = hashlib.sha256(master.encode()).digest()
        self._f = Fernet(base64.urlsafe_b64encode(raw))
        self._r = redis

    async def save(self, name: str, value: str) -> None:
        enc = self._f.encrypt(value.encode()).decode()
        await self._r.hset(self._HASH, name.lower().strip(), enc)

    async def get(self, name: str) -> str | None:
        raw = await self._r.hget(self._HASH, name.lower().strip())
        if not raw:
            return None
        try:
            return self._f.decrypt(raw.encode()).decode()
        except Exception:
            return None

    async def list_keys(self) -> list[str]:
        return list(await self._r.hkeys(self._HASH) or [])

    async def delete(self, name: str) -> None:
        await self._r.hdel(self._HASH, name.lower().strip())

    async def search(self, query: str) -> list[str]:
        keys = await self.list_keys()
        q = query.lower()
        return [k for k in keys if q in k]
