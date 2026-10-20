"""空缓存 —— 本机场景的默认后端。

## 它**不是**「没有缓存」，而是「一个说自己没在工作的缓存」

见包 docstring。这里只补一条实现上的判据：

※ **它一个计数都不记。**

一个自然的写法是「反正什么都不做，那把 `hits` 记成 0 就好了」——
那样 `/api/health` 会报出一个 `0.0` 的命中率，而那个数**在报告里是绿的**：
它看起来像「缓存开着、只是没命中」，读的人会去查 key 写错了没有，
而他该做的其实是「知道本机没开缓存」。

⇒ 四个计数（`hits` / `misses` / `lookups` / `errors`）在 `none` 后端上
一律是 `None`：`None` 的语义是「这个问题在这里不适用」，
而 `0` 的语义是「适用，值是零」。两者不能混。
"""

from __future__ import annotations

from typing import Any

#: `stats()` 里那些「不适用」的字段。
_NOT_APPLICABLE = None


class NullCache:
    """什么都不做的缓存。**可观测**：它说得清自己没在工作。"""

    def __init__(self, *, default_ttl: int = 0) -> None:
        # ※ `default_ttl` 收下来但**不用** —— 接口一致是为了让装配处
        # （`bootstrap`）不必分情况写。而它在这里没有意义，所以不参与
        # `stats()` 的任何输出。
        self._default_ttl = default_ttl

    def backend_name(self) -> str:
        """`/api/health` 用它回答「现在用的是哪个后端」。"""
        return "none"

    def get(self, key: str) -> str | None:
        """永远未命中。

        ※ 返回 `None`（未命中）而**不是抛异常**：调用方的契约是
        「拿不到就去算」，而空实现的正确行为就是「永远拿不到」。
        """
        return None

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        return None

    def delete(self, key: str) -> None:
        return None

    def stats(self) -> dict[str, Any]:
        """※ 见模块 docstring：计数一律是 `None`，**不是** 0。"""
        return {
            "backend": "none",
            "enabled": False,
            "hits": _NOT_APPLICABLE,
            "misses": _NOT_APPLICABLE,
            "lookups": _NOT_APPLICABLE,
            "hit_rate": _NOT_APPLICABLE,
            "errors": _NOT_APPLICABLE,
            # ※ 与 `RedisCache.stats()` 的键集**保持一致**：消费方
            # （`/api/health`）不该按后端分情况读字段 —— 那正是
            # 「两个形状长得一样、语义不一样」的温床。
            "last_error": _NOT_APPLICABLE,
            "note": "未启用缓存（本机默认后端）—— 这不是故障，是配置",
        }

    def ping(self) -> tuple[bool, str]:
        """※ 返回 `True`：**这一层没有故障**。

        这不是在说「缓存工作正常」—— 那是 `enabled` 字段要说的事。
        `ok` 与 `enabled` 分成两个字段，正是为了让「没启用」读起来
        不像「工作正常」。
        """
        return True, "未启用缓存（本机默认后端）—— 这不是故障"
