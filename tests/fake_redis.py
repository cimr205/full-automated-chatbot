"""Minimal in-memory async Redis stand-in covering only the commands the
trading engine actually uses, so tests don't need a real Redis server."""


class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.hashes: dict = {}

    async def get(self, key):
        v = self.store.get(key)
        return v[0] if isinstance(v, list) else v

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        self.hashes.pop(key, None)

    async def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    async def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    async def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    async def hdel(self, name, key):
        self.hashes.get(name, {}).pop(key, None)

    async def hincrby(self, name, key, amount=1):
        h = self.hashes.setdefault(name, {})
        h[key] = str(int(h.get(key, 0)) + amount)
        return int(h[key])

    async def publish(self, channel, message):
        pass

    async def lpush(self, key, value):
        self.store.setdefault(key, [])
        self.store[key].insert(0, value)

    async def ltrim(self, key, start, end):
        if isinstance(self.store.get(key), list):
            self.store[key] = self.store[key][start:end + 1]
