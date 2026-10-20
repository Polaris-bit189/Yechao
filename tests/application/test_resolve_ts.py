"""`_resolve_ts` —— **模型填的串解析不出来时，字段就是空的**。

## 这条守的是一个「造数据」的缺陷

原来模型填的串解析不出来时，代码会**回落到源文里的第一个日期**：

```python
dates = find_dates(text) or find_dates(source)
```

后果不是「少一个字段」，而是**造出一个字段**：模型填「待定」「尽快」「本周内」，
会得到一个碰巧出现在源文里的绝对时刻；而闸门随后去源文里逐字核对它 ——
值本来就是从源文抄的，**必然通过**。于是界面上出现一个「已核验的截止日」，
而模型从来没说过它、源文里也不存在这个组合。

这与闸门存在的理由直接冲突（它要拦的就是「补出源文没有的字段」）。

## 为什么它一直没被测试抓到

端到端测试的模型桩从正文里**正则抄**日期，永远产出可解析的串；
`tests/domain/test_times.py` 只测 `find_dates` 本身。
**中间那一步（「解析不出来怎么办」）没有任何用例。**
"""

from __future__ import annotations

import datetime as dt

import pytest

from yechao.application.extract import _resolve_ts
from yechao.domain.days import DEFAULT_CLOCK

#: 源文里有两个日期 —— 回落的那个缺陷正是从这里「借」一个值出去。
SOURCE = "讲座报名截止到10月20日，讲座于10月22日 19:00 在报告厅举行。"

#: 消息发出的时刻。年份由它推。
MSG_TS = int(dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc).timestamp())


def _local(ts: int | None) -> str | None:
    if ts is None:
        return None
    d = dt.datetime.fromtimestamp(ts + DEFAULT_CLOCK.tz_offset, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    "text",
    ["待定", "尽快", "本周内", "月底", "明晚 19:00", "下周三", "", None],
)
def test_解析不出来的串一律返回_None(text) -> None:
    """※ 这一组是**回归用例**：它们原来都会返回源文里那个 10月20日。

    「不知道」和「10月20日」是两件事。把前者说成后者，读者会照着它去办事。
    """
    assert _resolve_ts(text, SOURCE, MSG_TS, DEFAULT_CLOCK) is None


def test_真的日期仍然解析得出来() -> None:
    """※ 阴性对照：不能为了修上面那条，把所有字段都变成空。

    没有这一条的话，一个「永远返回 None」的实现也能让上面那组全绿。
    """
    assert _local(_resolve_ts("10月22日", SOURCE, MSG_TS, DEFAULT_CLOCK)) == (
        "2026-10-22 00:00"
    )


def test_日期带时刻时两段都认() -> None:
    got = _resolve_ts("10月22日 19:00", SOURCE, MSG_TS, DEFAULT_CLOCK)
    assert _local(got) == "2026-10-22 19:00"


def test_年份从消息发出时刻推() -> None:
    """※ 源文通常不写年份。年份从 `msg_ts` 推，而不是从「现在」取 ——
    领域层与用例层都不取当前时刻，否则测试只能测「我这一刻跑的那次」。
    """
    next_year = int(dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.timezone.utc).timestamp())
    assert _local(_resolve_ts("1月8日", SOURCE, next_year, DEFAULT_CLOCK)) == (
        "2027-01-08 00:00"
    )
