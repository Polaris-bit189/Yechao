"""本地日口径与截止日分级。

## 为什么这个文件必须存在

`days.py` 里有两件事**各自都出过、而且都不出声**的错：

  1. **本地日的口径**。`day_start` 若写成 `ts - ts % 86400`，那是按 **UTC** 切的；
     东八区在 00:00–08:00 之间，UTC 还算前一天 —— 而那正是用户看日报的时段。
     症状是「日报标题写 10月19日，里面列的条目却是 10月18日 深夜的」，
     两边代码单看都正常。
  2. **分级的阈值**。它是与前端 `web/src/lib/time.ts` 的 `dueInfo` 的
     **跨语言契约**。两边各改各的，界面与日报会给出**不一致的紧急度**，
     而没有任何一处会报错。

这个文件把两件事都钉住，且**边界取具体的值**（-1 / 0 / 1 / 2 / 3 / 4 / 7 / 8），
不测「差不多」。

※ 样本是构造出来的时刻，不来自任何真实数据。
"""

from __future__ import annotations

import datetime as dt

import pytest

from yechao.domain.days import (
    DAY_SECONDS,
    EXPIRED,
    NORMAL,
    NORMAL_MAX_DAYS,
    SOON,
    SOON_MAX_DAYS,
    URGENT,
    URGENT_MAX_DAYS,
    Clock,
    deadline_level,
    deadline_text,
)

#: 生产默认时区（东八区）。测试必须与它同口径 —— 换一个时区，
#: 「本地日」这条判据测的就是另一件事了。
TZ = 8 * 3600


def _ts(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> int:
    """构造一个「本地时刻」的 Unix 秒（与 `_resolve_ts` 存进去的形状相同）。"""
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc).timestamp()) - TZ


# ── 本地日：这一节的判据是「按哪个时区切天」 ────────────────────────


def test_本地日按东八区切_不是按UTC() -> None:
    """※ **本文件最重要的一条。**

    构造本地 `2026-10-19 00:30`（此时 UTC 还是 `2026-10-18 16:30`）。
    按 UTC 切天会答 `2026-10-18`，按本地日应当答 `2026-10-19`。

    这是早上 00:00–08:00 读日报时的**常态**，不是边角。
    """
    clock = Clock()
    ts = _ts(2026, 10, 19, 0, 30)

    # 先确认夹具本身：这个时刻在 UTC 下确实还是 18 日 —— 否则这条用例是空转。
    utc_day = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date()
    assert utc_day == dt.date(2026, 10, 18), "夹具没构造出 UTC 与本地跨天的时刻"

    assert clock.day_of(ts) == "2026-10-19"


def test_本地日边界_23时59分与次日0时0分是两天() -> None:
    """切天只认本地零点，不认「差不多」。

    两边各取**边界的那一分钟** —— 23:59 与 00:00 只差 60 秒，
    但它们必须是两个不同的本地日。
    """
    clock = Clock()
    assert clock.day_of(_ts(2026, 10, 18, 23, 59)) == "2026-10-18"
    assert clock.day_of(_ts(2026, 10, 19, 0, 0)) == "2026-10-19"


def test_day_start_是本地零点() -> None:
    """`day_start` 必须回到**本地** 00:00，而不是 UTC 的 00:00。

    本地 `2026-10-19 00:30` 的本地零点 = 本地 `2026-10-19 00:00`
    = `_ts(2026, 10, 19, 0, 0)`。
    """
    clock = Clock()
    assert clock.day_start(_ts(2026, 10, 19, 0, 30)) == _ts(2026, 10, 19, 0, 0)
    # 反例：它不是 UTC 零点（那会差 8 小时）
    assert clock.day_start(_ts(2026, 10, 19, 0, 30)) != int(
        dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc).timestamp()
    )


def test_range_of_day_是半开区间() -> None:
    clock = Clock()
    start, end = clock.range_of_day("2026-10-19")
    assert end - start == DAY_SECONDS
    assert clock.day_of(start) == "2026-10-19"
    # 半开：上界那一秒属于**下一天**
    assert clock.day_of(end - 1) == "2026-10-19"
    assert clock.day_of(end) == "2026-10-20"


def test_days_apart_按日对齐而不是按秒差() -> None:
    """※ 「明天 09:00 截止」在今晚 23:00 读到时，是**明天**截止，不是 0 天。

    两个时刻只差 10 小时，但跨了一个本地日 ⇒ 差 1 天。
    """
    clock = Clock()
    tonight = _ts(2026, 10, 18, 23, 0)
    tomorrow_morning = _ts(2026, 10, 19, 9, 0)
    assert clock.days_apart(tomorrow_morning, tonight) == 1
    # 反方向是对称的
    assert clock.days_apart(tonight, tomorrow_morning) == -1
    # 同一天内不管差几小时都是 0
    assert clock.days_apart(_ts(2026, 10, 18, 23, 0), _ts(2026, 10, 18, 1, 0)) == 0


# ── 分级：阈值是与前端的跨语言契约 ──────────────────────────────────


@pytest.mark.parametrize(
    "days,expected",
    [
        (-1, EXPIRED),
        (0, URGENT),
        (1, URGENT),
        (2, SOON),
        (3, SOON),
        (4, NORMAL),
        (7, NORMAL),
        (8, NORMAL),
    ],
)
def test_分级边界(days: int, expected: str) -> None:
    """※ 这八个值就是**契约本身**（0 / 1 / 2 / 3 / 4 / 7 / 8 + 负数）。

    前端 `web/src/lib/time.ts` 的 `dueInfo` 必须给出同一档。
    改这里的任何一条，前端那份要同步改。
    """
    assert deadline_level(days) == expected


def test_阈值常量就是契约里的那几个数() -> None:
    """把切点**当数字**钉住。

    ※ 只断言「分级函数自洽」是不够的：把 `URGENT_MAX_DAYS` 改成 2 并同步改
    `deadline_level` 的判据，自洽性测试照样全绿，而前端**已经漂了**。
    所以这里直接断言数字 —— 改数字就必须看见这条用例红。
    """
    assert URGENT_MAX_DAYS == 1
    assert SOON_MAX_DAYS == 3
    assert NORMAL_MAX_DAYS == 7


def test_分级覆盖了四个档_不是空转() -> None:
    """分母自检：跑一段连续的日差，**四个档必须都出现过**。

    一个只会返回 `NORMAL` 的实现也能让上面每条参数化用例里的一半通过；
    这条用例保证「四个档位都真的可达」。
    """
    levels = {deadline_level(d) for d in range(-3, 12)}
    assert levels == {EXPIRED, URGENT, SOON, NORMAL}


@pytest.mark.parametrize(
    "days,expected",
    [
        (-1, "已过期 1 天"),
        (-3, "已过期 3 天"),
        (0, "今天截止"),
        (1, "明天截止"),
        (2, "还有 2 天"),
        (7, "还有 7 天"),
    ],
)
def test_文案(days: int, expected: str) -> None:
    assert deadline_text(days) == expected


def test_超过一周回落到绝对日期() -> None:
    """※ 8 天是第一处返回 `None` 的地方 —— 调用方据此改显示**绝对日期**。

    7 与 8 是这条判据的两个边界：7 还是「还有 7 天」，8 就不再给相对文案了。
    """
    assert deadline_text(7) == "还有 7 天"
    assert deadline_text(8) is None
    assert deadline_text(365) is None


def test_文案与分级不矛盾() -> None:
    """两级函数必须说同一件事：`None` 只可能出现在 `NORMAL` 档。

    ※ 这是一条**跨函数的不变量**。两条判据各自改一处、彼此不再对齐，
    症状是「界面标着紧急，文案却是绝对日期」—— 两边单看都正常。
    """
    for d in range(-3, 30):
        txt = deadline_text(d)
        lvl = deadline_level(d)
        if txt is None:
            assert lvl == NORMAL, f"第 {d} 天：没有文案却不是 NORMAL 档"
        elif lvl == EXPIRED:
            assert txt.startswith("已过期")
