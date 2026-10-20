"""时钟 —— 取当前时刻的**唯一**地方。

※ 领域层不许取当前时间（护栏钉这条）。它拿到的是一个 Unix 秒，算出那个秒
落在哪一天 —— 纯函数。

这个类是在适配层：它 `import time`，而领域层不碰。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from yechao.domain.days import DEFAULT_TZ_OFFSET, Clock


@dataclass
class SystemClock:
    """真实时钟。

    ※ 它是 `domain.days.Clock` 的**超集**：除了 `now()`，它还提供
    `day_start` / `day_of` / `days_apart` 那些纯计算 —— 直接委托给
    那个 frozen dataclass。

    为什么委托而不是重复实现：「本地日」的口径全项目只有一处。
    两份实现必漂移，而漂移的症状是「日报标题写 10月18日，里面列的条目却是
    10月19日 凌晨的」。
    """

    tz_offset: int = DEFAULT_TZ_OFFSET

    def __post_init__(self) -> None:
        self._pure = Clock(tz_offset=self.tz_offset)

    def now(self) -> int:
        """当前 Unix 秒。

        ※ 只有这里可以调 `time.time()`。测试里换成 `FixedClock`。
        """
        return int(time.time())

    # ── 委托给纯函数的那一份 ──

    def day_start(self, ts: int) -> int:
        return self._pure.day_start(ts)

    def day_of(self, ts: int) -> str:
        return self._pure.day_of(ts)

    def day_start_from_str(self, day: str) -> int:
        return self._pure.day_start_from_str(day)

    def days_apart(self, a: int, b: int) -> int:
        return self._pure.days_apart(a, b)

    def range_of_day(self, day: str) -> tuple[int, int]:
        return self._pure.range_of_day(day)

    def today(self) -> str:
        """今天（本地日）的 `YYYY-MM-DD`。"""
        return self._pure.day_of(self.now())


@dataclass
class FixedClock:
    """固定时刻的时钟 —— 测试用。

    ※ 它存在的理由就是那条判据：**测试不许依赖「跑用例的那一刻是几号」**。
    没有它的话，一条关于「今天截止」的断言在 23:59 与 00:01 会得到不同结果，
    而那种失败看起来像「代码坏了」。
    """

    ts: int
    tz_offset: int = DEFAULT_TZ_OFFSET

    def __post_init__(self) -> None:
        self._pure = Clock(tz_offset=self.tz_offset)

    def now(self) -> int:
        return self.ts

    def set(self, ts: int) -> None:
        self.ts = ts

    def day_start(self, ts: int) -> int:
        return self._pure.day_start(ts)

    def day_of(self, ts: int) -> str:
        return self._pure.day_of(ts)

    def day_start_from_str(self, day: str) -> int:
        return self._pure.day_start_from_str(day)

    def days_apart(self, a: int, b: int) -> int:
        return self._pure.days_apart(a, b)

    def range_of_day(self, day: str) -> tuple[int, int]:
        return self._pure.range_of_day(day)

    def today(self) -> str:
        return self._pure.day_of(self.ts)
