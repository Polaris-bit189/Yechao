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

※ 反例：如果这里写 `date.today()`，那么同一个条目在不同机器上、同一天的不同
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

    ※ 它没有 `now()` —— 那不是疏忽。时刻必须由调用方传进来。
    """

    tz_offset: int = DEFAULT_TZ_OFFSET

    def day_start(self, ts: int) -> int:
        """这个时刻所在**本地日**的 00:00:00（Unix 秒）。

        ※ 不能写成 `ts - ts % 86400` —— 那是按 **UTC** 切的。必须先把
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

        ※ **按日对齐**，不按秒差取整：「明天 09:00 截止」在今晚 23:00
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
#
# ## 这一节要回答什么
#
# 读者翻开日报时，要能**一眼看出哪几条今天就得动**。所以分级不是「好看」，
# 它是一条**行动判据**：这条消息要求我现在做什么？
#
# ## 分级轴是「本地日差」，不是「秒差」
#
# 「明天 09:00 截止」在今晚 23:00 读到时，离截止只剩 10 小时 —— 但它仍然是
# 「明天截止」，不是「今天截止」。**人按日子安排事情**，所以分级必须按
# `days_apart`（本地日对齐）算，见 `Clock.days_apart`。
#
# ## 阈值为什么是这几个数
#
# 四个档位把「一个星期」切成四段，切点各自对应一个**行为**：
#
#   · `< 0` 已过期     —— 日子已经过去，做什么都晚了。文案是陈述，不是催促。
#   · `0–1` 紧急       —— 今天 / 明天。**只有这两天是「今天不动就没了」**：
#                        从第 2 天起，读者手里还攥着一整个完整的日子可以安排。
#                        切点在 1，因为「今天能不能拖」正是这个档位在回答的问题。
#   · `2–3` 临近       —— 半个星期以内。一周 7 天，一半是 3.5 ⇒ 取整到 3。
#                        这一档的读者今天不必动，但要**把它放进这两天的安排里**。
#   · `4–7` 普通       —— 本周剩下的部分。还在这一周里，所以仍然报「还有 N 天」。
#   · `> 7`            —— **回落到绝对日期**（`deadline_text` 返回 `None`）。
#                        见 `deadline_text` 的 docstring。
#
# ※ 「一个星期」是这套切点的**锚**：它是读者心理上「用天数数」与
# 「看日历上的日子」之间的分界，也是本语料（校园通知）里事件跨度的自然单位。
# 换一个锚（比如「一个月」），切点应当整体重推，而不是只改 7。
#
# ## 为什么阈值是**契约**，两份实现
#
# `days.py`（Python）与 `web/src/lib/time.ts`（TS）各有一份，这是**知情取舍**：
#   · 日报要落盘成 `.md`（后端渲染），前端拿不到 ⇒ 后端必须有一份
#   · 日历要跟随「当前时刻」变化（前端渲染），后端算完会过期 ⇒ 前端必须有一份
#
# ⇒ **改这里的任何一个数，前端那份必须同步改**，两边的测试各钉同一条边界。

EXPIRED = "expired"
URGENT = "urgent"
SOON = "soon"
NORMAL = "normal"

#: `urgent` 的上界（含）。0–1 天：今天 / 明天。
URGENT_MAX_DAYS = 1
#: `soon` 的上界（含）。2–3 天：半个星期以内。
SOON_MAX_DAYS = 3
#: `normal` 的上界（含）。4–7 天：一个星期以内。超过 7 天回落到绝对日期。
NORMAL_MAX_DAYS = 7


def deadline_level(days: int) -> str:
    """距死线还有 `days` 个本地日时的分级。

    ※ 边界是**契约**：0 / 1 ⇒ `urgent`；2–3 ⇒ `soon`；4–7 ⇒ `normal`；
    <0 ⇒ `expired`。切点的理由见上面那一节；改这里的任何一个数，
    前端 `web/src/lib/time.ts` 的 `dueInfo` 也要同步改。
    """
    if days < 0:
        return EXPIRED
    if days <= URGENT_MAX_DAYS:
        return URGENT
    if days <= SOON_MAX_DAYS:
        return SOON
    return NORMAL


def deadline_text(days: int) -> str | None:
    """分级文案。`None` = 超过一周，调用方该显示**绝对日期**。

    ※ 超过一周**回落到绝对日期**，而不是「还有 12 天」：
    一个星期是读者「用天数数」的边界。超过它，读者是在**安排**一件事
    而不是**执行**一件事，而「还有 12 天」既丢了日期锚点、又要求读者
    自己在脑子里做一次加法。`10月28日` 直接就是答案。

    （前端那份同此，见 `web/src/lib/time.ts` 的 `dueInfo`。）
    """
    if days < 0:
        return f"已过期 {-days} 天"
    if days == 0:
        return "今天截止"
    if days == URGENT_MAX_DAYS:
        return "明天截止"
    if days <= NORMAL_MAX_DAYS:
        return f"还有 {days} 天"
    return None
