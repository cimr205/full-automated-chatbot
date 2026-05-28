import redis.asyncio as aioredis


class Brain:
    """Permanent personal memory — facts, preferences, context about the user."""
    _HASH = "brain:facts"

    def __init__(self, redis: aioredis.Redis):
        self._r = redis

    async def remember(self, key: str, value: str) -> None:
        await self._r.hset(self._HASH, key.lower().strip(), value)

    async def recall(self, key: str) -> str | None:
        return await self._r.hget(self._HASH, key.lower().strip())

    async def all_facts(self) -> dict:
        return dict(await self._r.hgetall(self._HASH) or {})

    async def forget(self, key: str) -> None:
        await self._r.hdel(self._HASH, key.lower().strip())

    async def search(self, query: str) -> list[tuple[str, str]]:
        all_f = await self.all_facts()
        q = query.lower()
        return [(k, v) for k, v in all_f.items() if q in k or q in v.lower()]

    async def context_for_ai(self) -> str:
        facts = await self.all_facts()
        if not facts:
            return ""
        lines = [f"- {k}: {v}" for k, v in list(facts.items())[:40]]
        return "HVAD DU VED OM BRUGEREN:\n" + "\n".join(lines)
