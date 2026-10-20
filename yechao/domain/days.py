"""本地日的口径 —— **全项目只有这一处**。

## 要防什么

同一个时刻，在**不同时区**会被算成不同的**日期**。东八区在每天
00:00–08:00 之间，UTC 还是前一天 —— 而那正是用户最容易看日报的时间段
（早上）。

⇒ 「本地日」必须只有一个口径，否则会出现「日报标题写 10月18日，里面列的
条目却是 10月19日 凌晨的」这种两边代码单看都正常的错。

## 为什么这一层不能取当前时刻

「今天是几号」是**外部输入**（护栏钉这条）。领域层拿到的是一个 Unix 秒，
它算出那个秒落在哪一天 —— 这是纯函数。

⚠️ 反例：如果这里写 `date.today()`，那么同一个条目在不同机器上、同一天的不同
时刻会落进不同的日报，而**测试只能测「在我这台机器上跑的那一刻」**。

## 时区从哪来

由调用方（适配层从配置读）传进来。默认 `UTC+8` —— 这是本项目的目标用户
所在时区，也是 `docs` 里反复出现的「本地日」指的时区。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

#: 默认时区偏移（秒）。东八区。
DEFAULT_TZ_OFFSET = 8 * 3600

#: 一整天。
DAY_SECONDS = 86400


@dataclass(frozen=True)
class Clock:
    """一个**纯函数式**的时钟：给定 Unix 秒，回答它落在哪个本地日。

    ⚠️ 它没有 `now()` —— 那不是疏忽。时刻必须由调用方传进来。
    """

    tz_offset: int = DEFAULT_TZ_OFFSET

    def day_start(self, ts: int) -> int:
        """这个时刻所在**本地日**的 00:00:00（Unix 秒）。

        ⚠️ 不能写成 `ts - ts % 86400` —— 那是按 **UTC** 切的。必须先把
        偏移加回去，取整到天，再减回来。这是本项目在这件事上唯一的判据。
        """
        shifted = ts + self.tz_offset
        day = shifted - (shifted % DAY_SECONDS)
        return day - self.tz_offset

    def day_of(self, ts: int) -> str:
        """`YYYY-MM-DD`（本地日）。"""
        # 换成时区感知写法；偏移已手工加在秒数上，故仍用 UTC 钟读，
        # 不能改用机器本地时区，否则「本地日」口径会被二次偏移。
        d = _dt.datetime.fromtimestamp(
            self.day_start(ts) + self.tz_offset, tz=_dt.timezone.utc
        )
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"

    def day_start_from_str(self, day: str) -> int:
        """`YYYY-MM-DD` → 那一天的 00:00:00（Unix 秒，本地）。"""
        y, m, d = (int(x) for x in day.split("-"))
        dt = _dt.datetime(y, m, d, tzinfo=_dt.timezone.utc)
        return int(dt.timestamp()) - self.tz_offset

    def days_apart(self, a: int, b: int) -> int:
        """两个时刻相差几个**本地日**（正数 = a 在 b 之后）。

        ⚠️ **按日对齐**，不按秒差取整：「明天 09:00 截止」在今晚 23:00
        应当是「明天截止」，不是「0 天」。
        """
        return (self.day_start(a) - self.day_start(b)) // DAY_SECONDS

    def range_of_day(self, day: str) -> tuple[int, int]:
        """某一天的 `[起, 止)` 半开区间。"""
        start = self.day_start_from_str(day)
        return start, start + DAY_SECONDS


#: 默认时钟。**只读** —— 要别的时区就显式构造一个。
DEFAULT_CLOCK = Clock()


# ── 截止日的分级 ────────────────────────────────────────────────────

#: 分级阈值（**本地日**差）。与前端 `format.ts::relDeadline` **必须同阈值**。
#:
#: ⚠️ 两份实现在 TS 与 Python 各有一份，这是**知情取舍**（前端注释逐字）：
#:   · 日报要落盘成 `.md`（后端渲染），前端拿不到 ⇒ 后端必须有一份
#:   · 日历要跟随「当前时刻」变化（前端渲染），后端算完会过期 ⇒ 前端必须有一份
#: 两份注释互相指路，且测试各钉同一条边界（0 / 1 / 2 / 7 / 8 天）。

EXPIRED = "expired"
URGENT = "urgent"
SOON = "soon"
NORMAL = "normal"

#: `soon` 的上界（含）。2..3 天。
SOON_MAX_DAYS = 3
#: `normal` 的上界（含）。4..7 天。超过 7 天回落到绝对日期。
NORMAL_MAX_DAYS = 7


def deadline_level(days: int) -> str:
    """距死线还有 `days` 个本地日时的分级。

    ⚠️ 边界是**契约**：0 / 1 ⇒ `urgent`；2–3 ⇒ `soon`；4–7 ⇒ `normal`；
    <0 ⇒ `expired`。改这里的任何一个数，前端那份也要同步改。
    """
    if days < 0:
        return EXPIRED
    if days <= 1:
        return URGENT
    if days <= SOON_MAX_DAYS:
        return SOON
    return NORMAL


def deadline_text(days: int) -> str | None:
    """分级文案。`None` = 超过一周，调用方该显示**绝对日期**。

    ⚠️ 超过一周回落到绝对日期，而不是「还有 12 天」：那时人已经在看日历了，
    绝对日期更好读（前端注释逐字）。而且**相对词的价值在于「我刚刚经历过的
    两天」**，再往前它就丢掉了时间锚点。
    """
    if days < 0:
        return f"已过期 {-days} 天"
    if days == 0:
        return "今天截止"
    if days == 1:
        return "明天截止"
    if days <= NORMAL_MAX_DAYS:
        return f"还有 {days} 天"
    return None
