"""
Persistent brain — remembers facts about the user across all sessions.
Stored as Redis hash brain:facts, injected into every AI system prompt.
"""


class Brain:
    _HASH = "brain:facts"

    def __init__(self, redis):
        self._r = redis

    async def remember(self, key: str, value: str):
        await self._r.hset(self._HASH, key, value)

    async def recall(self, key: str) -> str | None:
        return await self._r.hget(self._HASH, key)

    async def all_facts(self) -> dict:
        raw = await self._r.hgetall(self._HASH)
        return dict(raw or {})

    async def forget(self, key: str):
        await self._r.hdel(self._HASH, key)

    async def context_for_ai(self) -> str:
        facts = await self.all_facts()
        if not facts:
            return ""
        lines = "\n".join(f"  {k}: {v}" for k, v in facts.items())
        return f"\nHVAD DU VED OM BRUGEREN (din hukommelse):\n{lines}\n"
