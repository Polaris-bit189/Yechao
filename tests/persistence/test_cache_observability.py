"""缓存的两个后端 —— 重点不在「缓存快不快」，在**它有没有在工作能被看出来**。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：本项目反复出现同一族失败 —— **一个失败如果看起来像成功，它比失败
本身更危险**。「缓存配错了、根本没生效」正是这一族里最典型的一个：它不报错、
不影响正确性，只让一切都慢一点。而「慢一点」在没有基线时是**看不见的**。

**所以判据是**：空实现与真实现都必须能回答两个问题 ——

1. 现在用的是哪个后端？（`redis` / `none`）
2. 命中率是多少？

而第 2 问的关键是：**`None`（没启用）与 `0.0`（启用了但没命中）不是一回事**。
前者是配置事实，后者是故障信号。把前者报成 `0%` 就是把「没开」说成
「开了但没用」—— 读的人会去查一个根本不存在的问题。
"""

from __future__ import annotations

import sys
import types

import pytest

from yechao.domain.errors import ConfigError
from yechao.infrastructure.cache.null_cache import NullCache
from yechao.infrastructure.cache.redis_cache import RedisCache


# ── 假 redis 客户端 ─────────────────────────────────────────────────


class _FakeRedis:
    """最小的假客户端。它**不是** redis，只实现被用到的那几个方法。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail
        self.pings = 0

    def _boom(self) -> None:
        if self.fail:
            raise ConnectionError("连接被拒绝")

    def get(self, key):
        self._boom()
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self._boom()
        self.store[key] = value
        self.ttls[key] = ttl

    def set(self, key, value):
        self._boom()
        self.store[key] = value
        self.ttls[key] = 0

    def delete(self, key):
        self._boom()
        self.store.pop(key, None)

    def ping(self):
        self.pings += 1
        self._boom()
        return True


class _FakeRedisFactory:
    """假装是 `redis.Redis` —— 只记下 `from_url` 被怎么调的。"""

    calls: list[tuple[str, dict]] = []

    @classmethod
    def from_url(cls, url: str, **kw):
        cls.calls.append((url, kw))
        return _FakeRedis()


def _install_fake_redis(monkeypatch) -> None:
    mod = types.ModuleType("redis")
    mod.Redis = _FakeRedisFactory
    _FakeRedisFactory.calls = []
    monkeypatch.setitem(sys.modules, "redis", mod)


# ── 空实现：可观测 ──────────────────────────────────────────────────


def test_空缓存说自己没启用_而不是命中率零() -> None:
    """※ **这是本文件最要紧的一条。**

    判据：「没启用」与「启用了但一次没命中」是两个不同的数。
    合并它们等于把「没开」说成「开了但没用」—— 而后者是一个需要有人
    去查的故障信号。
    """
    stats = NullCache().stats()
    assert stats["backend"] == "none"
    assert stats["enabled"] is False
    assert stats["hits"] is None
    assert stats["misses"] is None
    assert stats["hit_rate"] is None
    # ※ 显式地钉住「不是 0」—— 光断言 `is None` 不够，
    # 因为一个写成 `0.0` 的实现会**同时**让 `is None` 失败（好），
    # 但把这条写出来是为了让读的人看见它要防的是什么。
    assert stats["hit_rate"] != 0.0
    assert stats["note"], "空实现要说清自己没在工作"


def test_空缓存的读永远未命中且不抛() -> None:
    cache = NullCache()
    assert cache.get("任何键") is None
    assert cache.backend_name() == "none"


def test_空缓存什么都不存() -> None:
    """※ 阴性对照：如果 `set` 偷偷存了，上面那条「未命中」就没意义了。"""
    cache = NullCache()
    cache.set("k", "v")
    assert cache.get("k") is None
    cache.delete("k")  # 不该抛


def test_空缓存探活是通的_并说明那是配置不是故障() -> None:
    """※ `ok` 与 `enabled` 必须分开：

    · `ok: true`   —— 这一层**没有故障**
    · `enabled: false` —— 它**没在工作**

    合成一个字段就会让「没启用」读起来像「工作正常」。
    """
    ok, detail = NullCache().ping()
    assert ok is True
    assert "未启用" in detail


# ── 真实现：计数与降级 ──────────────────────────────────────────────


def test_redis_命中率是真算出来的() -> None:
    cache = RedisCache(_FakeRedis(), default_ttl=10)
    cache.set("a", "1")
    assert cache.get("a") == "1"     # 命中
    assert cache.get("b") is None    # 未命中
    assert cache.get("c") is None    # 未命中

    stats = cache.stats()
    assert stats["backend"] == "redis"
    assert stats["enabled"] is True
    assert (stats["hits"], stats["misses"], stats["lookups"]) == (1, 2, 3)
    assert stats["hit_rate"] == pytest.approx(1 / 3)


def test_redis_没查过时命中率是_None_而不是零() -> None:
    """※ 第三种状态：**启用了，但一次都没查过**。

    报 `0.0` 会让读的人以为「缓存没生效」，而真相是「还没有请求走到这里」。
    三种状态（没启用 / 没查过 / 查过没命中）各是一个数，不能合并。
    """
    stats = RedisCache(_FakeRedis()).stats()
    assert stats["enabled"] is True
    assert stats["lookups"] == 0
    assert stats["hit_rate"] is None


def test_redis_连不上时降级而不是把服务带崩() -> None:
    """※ 缓存的职责是快，不是正确。

    连不上时正确的行为是「退回每次都去算」，而不是让 `/api/urgent` 回 500
    —— 那会让用户看到「页面坏了」，而真相是「缓存挂了，条目一条不少」。
    """
    broken = _FakeRedis(fail=True)
    cache = RedisCache(broken, default_ttl=10)

    assert cache.get("a") is None       # 不抛
    cache.set("a", "1")                 # 不抛
    cache.delete("a")                   # 不抛

    stats = cache.stats()
    assert stats["enabled"] is True
    assert stats["errors"] >= 2, "兜住的异常必须**记账**，否则就是静默吞掉"
    assert stats["last_error"], "要说得出最后一次是什么错"
    assert stats["note"], "降级状态要能被读出来"


def test_redis_降级时探活报不通() -> None:
    ok, detail = RedisCache(_FakeRedis(fail=True)).ping()
    assert ok is False
    assert "ConnectionError" in detail or "连接被拒绝" in detail


def test_redis_写入带上_TTL() -> None:
    client = _FakeRedis()
    RedisCache(client, default_ttl=10).set("k", "v")
    assert client.ttls["k"] == 10, "默认 TTL 没传下去 —— 那条 key 会永不过期"

    RedisCache(client, default_ttl=10).set("k2", "v", ttl=3)
    assert client.ttls["k2"] == 3


def test_redis_TTL_为零是显式的不过期() -> None:
    """※ 走 `set` 而不是 `setex` —— 这是一条**显式**的路，不是兜底。"""
    client = _FakeRedis()
    RedisCache(client, default_ttl=0).set("k", "v")
    assert client.ttls["k"] == 0


# ── 装配 ────────────────────────────────────────────────────────────


def test_从连接串建时驱动没装就报错而不是回落(monkeypatch) -> None:
    """※ 与 `connect_mysql` 同一条判据：**没装驱动 ⇒ 报错**。

    回落的症状是「用户以为缓存开着，其实什么都没缓存」——
    而它看起来是成功的（页面照常，只是慢）。
    """
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(ConfigError, match="pip install redis"):
        RedisCache.from_url("redis://h.example.test:6379/0")


def test_连接串里的密码不进_label(monkeypatch) -> None:
    """※ `label` 会出现在 `/api/health` 的响应里 —— 那可能被贴进工单、
    日志、聊天窗口。连接串里的密码不该跟着走。"""
    _install_fake_redis(monkeypatch)
    cache = RedisCache.from_url("redis://:s3cret@h.example.test:6379/0")
    _, detail = cache.ping()
    assert "s3cret" not in detail, detail
    assert "h.example.test" in detail


def test_从连接串建时设了超时与解码(monkeypatch) -> None:
    """※ 两条都不能省：

    · **超时**：一条卡住的连接会把「快」变成「页面转圈」。
    · **`decode_responses`**：不设它 `get()` 回 `bytes`，调用方
      `json.loads(bytes)` 在部分版本上抛 —— 症状是「缓存一命中就报错」，
      而那看起来像 JSON 坏了。
    """
    _install_fake_redis(monkeypatch)
    RedisCache.from_url("redis://h.example.test:6379/0", default_ttl=7)
    _, kw = _FakeRedisFactory.calls[-1]
    assert kw["decode_responses"] is True
    assert kw["socket_timeout"] > 0
    assert kw["socket_connect_timeout"] > 0
