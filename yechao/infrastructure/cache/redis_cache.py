"""Redis 缓存 —— 云上那一个。

## 三条判据，每条都从「要防什么」来

### ① 失败**开放**（fail open），不是失败关闭

观察到什么现象：缓存连不上时，如果让异常穿出去，`/api/urgent` 会回 500 ——
而用户看到的是「页面坏了」。真相是「缓存挂了，但条目一条不少」。

判据：**缓存的职责是快，不是正确。** 它挂掉时正确的行为是退回「去算」，
而不是让整个服务跟着挂。所以每个操作都兜住异常、记一笔、返回「未命中」。

※ 但**兜住不等于吞掉**：`stats()["errors"]` 与 `last_error` 把它报出来，
`/api/health` 再把它报出去。一个被兜住、但没人能看见的异常，
正是「失败看起来像成功」。

### ② 超时必须短

一条**卡住**的连接比一条断掉的连接危险得多：断掉的会立刻抛异常（兜住就完事），
卡住的会把「快」变成「页面转圈」。所以连接与读都有 2 秒上限。

### ③ 存进去的必须是**字符串**（`decode_responses=True`）

不设它时 `get()` 回 `bytes`，调用方 `json.loads(bytes)` 在部分版本上能过、
部分版本上抛 —— 症状是「有时候缓存一命中就报错」，而那看起来像 JSON 坏了。
显式解码，把这一整类问题消掉。
"""

from __future__ import annotations

from typing import Any

from yechao.domain.errors import ConfigError

#: ※ 见 docstring ②。两个都短，理由不是省资源，是**别把快变成卡**。
SOCKET_TIMEOUT = 2.0


class RedisCache:
    """真缓存。连不上时**降级**，并把降级报出来。"""

    def __init__(
        self, client: Any, *, default_ttl: int = 10, label: str = "redis"
    ) -> None:
        self._client = client
        self._default_ttl = default_ttl
        self._label = label
        self._hits = 0
        self._misses = 0
        self._errors = 0
        self._last_error: str | None = None

    @classmethod
    def from_url(cls, url: str, *, default_ttl: int = 10) -> "RedisCache":
        """按连接串建一个。

        ※ `redis` 驱动**在这里才 import**，且**没装就报错**、不回落成空实现。
        回落的症状是「用户以为缓存开着，其实什么都没缓存」—— 而那**看起来
        是成功的**（页面照常，只是慢）。

        ※ 这里**不试连**：RDS / Redis 是网络对端，启动时不通不等于以后不通。
        探活是 `/api/health` 的事（它每次都探），不是启动闸门。
        """
        try:
            import redis
        except ImportError as e:  # pragma: no cover - 依赖缺失时才走
            raise ConfigError(
                "要用 Redis 得先装驱动：`pip install redis`（或 "
                "`pip install \"yechao[redis]\"`）。"
                "※ 装了它之前，程序**不会**退回空缓存 —— 那会让你以为缓存开着。"
            ) from e

        client = redis.Redis.from_url(
            url,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_TIMEOUT,
            # ※ 判据 ③：不设它 `get()` 回 bytes。
            decode_responses=True,
        )
        # 连接串里可能带密码（`redis://:pass@host:6379/0`）—— 标签只留主机，
        # 不把凭据带进任何会被打印的地方（`/api/health` 会把它报出去）。
        label = url.split("@")[-1] if "@" in url else url
        return cls(client, default_ttl=default_ttl, label=label)

    # ── 端口 ────────────────────────────────────────────────────

    def backend_name(self) -> str:
        return "redis"

    def get(self, key: str) -> str | None:
        """读。**连不上时返回 `None`（未命中），不抛。** 见 docstring ①。"""
        try:
            value = self._client.get(key)
        except Exception as e:  # noqa: BLE001 - 见 docstring ①：兜住并记账
            self._record_error(e)
            self._misses += 1
            return None
        if value is None:
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        seconds = self._default_ttl if ttl is None else ttl
        try:
            if seconds and seconds > 0:
                self._client.setex(key, seconds, value)
            else:
                # ※ ttl <= 0 是「不过期」。它是一条**显式**的路，不是兜底 ——
                # 装配处默认给的是正数，走到这里说明有人明确配了 0。
                self._client.set(key, value)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)

    def stats(self) -> dict[str, Any]:
        """命中率。※ `hit_rate` 只在**查过**的时候才有值 —— 见包 docstring。"""
        lookups = self._hits + self._misses
        return {
            "backend": "redis",
            "enabled": True,
            "hits": self._hits,
            "misses": self._misses,
            "lookups": lookups,
            "hit_rate": (self._hits / lookups) if lookups else None,
            "errors": self._errors,
            "last_error": self._last_error,
            "note": (
                "缓存连不上，已降级为「每次去算」"
                if self._errors and self._last_error
                else ""
            ),
        }

    def ping(self) -> tuple[bool, str]:
        """探活。**不抛** —— 探活失败要能被报出来，而不是把探活的人打倒。"""
        try:
            self._client.ping()
        except Exception as e:  # noqa: BLE001
            return False, f"Redis 连不上（{self._label}）：{type(e).__name__}: {e}"
        return True, f"Redis 连得上（{self._label}）"

    # ── 内部 ────────────────────────────────────────────────────

    def _record_error(self, e: Exception) -> None:
        self._errors += 1
        self._last_error = f"{type(e).__name__}: {e}"
