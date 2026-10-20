r"""时间解析 —— 把中文时间写法解析成绝对时刻。

## 要防什么

这个模块是**核验闸门**的地基。闸门的判据是「模型填的这个值，源文里查得到
吗」，而「查得到」需要一个**统一的口径**把源文里的时间写法与模型填的值
映到同一个坐标系里。

⚠️ **它不取当前时刻**（护栏钉这条）。「今年是哪一年」是调用方传进来的 ——
因为「9月30日」在源文里没写年份，要靠**消息发出时刻**去补，而那是一个
外部输入。

## 判据：中文时间写法有哪些形态

从语料里归纳（这个清单来自对本项目 `synth.py` 自造语料的观察，不是抄来的）：

| 形态 | 例子 | 解析难度 |
|---|---|---|
| 绝对月日 | `10月28日`、`10月28号` | 低 |
| 带斜杠 | `10/28`、`10-28` | ⚠️ 与分数/区间冲突 |
| 带年份 | `2026年10月28日` | 低 |
| 月日 + 时刻 | `10月18日 14:00` | 低 |
| 十二小时制 | `下午两点`、`晚上8点` | 中 |
| 半 | `九点半` | 中 |
| 中文数字 | `十月二十八日` | 中 |
| 相对日 | `明早`、`后天`、`下周三` | ⚠️ **推不出确定时刻** |

## 三个关键判断

**一、`9/3` 与 `9/30` 只差一个字符 —— 边界必须钉死。**

`10/28` 里如果判据写成 `\d+/\d+`，它会把 `10/2` 也匹配上，于是
「源文里写着 10/28」会给「模型填了 10/2」背书。**这是同一个数字串的两种读法，
而它们指向不同的日子。**

解法：日期两侧要有**数字边界** —— 匹配到的月日，它的右边不能紧跟另一个数字。
`9/30` 的 `/30` 之后不是数字 ⇒ 通过；`9/3` 的 `/3` 之后是 `0` ⇒ 拒绝。

⚠️ 同样的形状在**时刻**上也有，而且更隐蔽：`9点5分` 与 `9点50分`。
`9点5分` 的 `5` 后面紧跟「分」⇒ 通过；`9点50分` 的 `5` 后面是 `0` ⇒ 拒绝。

**二、区间 `9/3-9/5` 里的数字都短，但它们确实是日期。**

`9/3-9/5` 是一个**日期区间**。它里面的两个日期都只有一位月/日。所以
「短」不是拒绝的理由 —— 它只是要求判据不能用「长度」当唯一依据。

**三、「裸日不算依据」。**

源文里写着「本学期」或「月底」，模型填一个具体日期 —— 那**不是**源文里的
依据。判据必须是**字面匹配**（字面匹配虽然笨，
但它可测、可复现、能解释）。

⚠️ 推论：**「明早」推不出绝对时刻**。模型看到「明早集合」填了 `08:00`，
那是它自己补的。「明早」这个词在源文里，但 `08:00` 不在。

那么「明早」算不算一个**时刻依据**？—— 不算。
**它算一个「不适用」信号**：源文确实提到了时间，只是没精确到能填进
`start_ts` 那一格。见 `verify.py` 的「判据不适用」一节。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 「不是数字」的黑断言后缀 —— 与 `scrub.py` 同一取向：数字串要自成一段。
_NOT_DIGIT_AFTER = r"(?!\d)"
_NOT_DIGIT_BEFORE = r"(?<!\d)"

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 「月」的写法。`月` 与 `月份` 之外不收 —— 收太宽会把「一期」之类当成月份。
_MONTH_DAY = re.compile(
    _NOT_DIGIT_BEFORE
    + r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
    + _NOT_DIGIT_AFTER
)

#: 带年份的月日。
_FULL_DATE = re.compile(
    _NOT_DIGIT_BEFORE
    + r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?"
    + _NOT_DIGIT_AFTER
)

#: 斜杠 / 连字符日期：`10/28`、`10-28`。
#: ⚠️ **两侧都要数字边界** —— 这是 `9/3` 不给 `9/30` 背书的那条判据。
_SLASH_DATE = re.compile(
    _NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[/\-]\s*(\d{1,2})" + _NOT_DIGIT_AFTER
)

#: 中文数字月日：`十月二十八日`。
_CN_MONTH_DAY = re.compile(
    r"([零〇一二三四五六七八九十两]{1,3})\s*月\s*"
    r"([零〇一二三四五六七八九十两]{1,4})\s*[日号]"
)

#: 时刻：`14:00`、`14：00`、`14点00分`、`14点`。
#: ⚠️ 分钟部分要**数字边界** —— `9点5分` 不许给 `9点50分` 背书。
_HHMM_COLON = re.compile(_NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[:：]\s*(\d{2})" + _NOT_DIGIT_AFTER)
_HHMM_CN = re.compile(
    _NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[点時时]\s*(\d{1,2})\s*分?" + _NOT_DIGIT_AFTER
)
#: 只有小时：`14点`（`点钟` 也收）。
_HOUR_ONLY = re.compile(_NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[点時时](?:钟)?" + _NOT_DIGIT_AFTER)

#: 时段词 → 十二小时制的上午/下午。
_MERIDIEM = {
    "凌晨": 0, "早上": 0, "早晨": 0, "上午": 0, "中午": 12,
    "下午": 12, "傍晚": 12, "晚上": 12, "夜里": 12, "晚间": 12,
}

#: 中文数字时刻：`两点`、`八点半`。
_CN_HOUR = re.compile(
    r"([零〇一二三四五六七八九十两]{1,3})\s*[点時时](半|\d{1,2}|[零〇一二三四五六七八九十]{1,3})?分?"
)

#: 相对日词。⚠️ 这些**推不出**绝对日期 —— 它们只说明「源文提到了时间」。
#: 闸门据此判「不适用」而不是「无依据」（见 `verify.py`）。
RELATIVE_DAYS = (
    "今天", "今日", "明天", "明日", "明早", "明晚", "后天", "大后天",
    "昨天", "昨日", "前天", "本周", "这周", "下周", "上周", "下个月",
    "本月", "这个月", "月底", "月末", "年底", "年末", "学期末", "期末",
)


@dataclass(frozen=True)
class DateHit:
    """源文里出现的一个日期。`raw` 是**原文片段** —— 闸门要能指着它说话。"""

    raw: str
    month: int
    day: int
    year: int | None
    start: int
    end: int


@dataclass(frozen=True)
class TimeHit:
    """源文里出现的一个时刻。"""

    raw: str
    hour: int
    minute: int
    start: int
    end: int


def _cn_to_int(s: str) -> int | None:
    """中文数字 → 整数。支持 `十`、`二十八`、`两`、`九`。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        total = tens * 10 + ones
    else:
        for ch in s:
            d = _CN_DIGITS.get(ch)
            if d is None:
                return None
            total = total * 10 + d
    return total


def find_dates(text: str) -> list[DateHit]:
    """找出源文里所有的日期。按出现位置排序，重叠的取先长后短。"""
    hits: list[DateHit] = []
    taken: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        return any(s < te and ts < e for ts, te in taken)

    for m in _FULL_DATE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            hits.append(DateHit(m.group(0), mo, d, y, s, e))
            taken.append((s, e))

    for pat, cn in ((_MONTH_DAY, False), (_CN_MONTH_DAY, True)):
        for m in pat.finditer(text):
            s, e = m.span()
            if _overlaps(s, e):
                continue
            mo = _cn_to_int(m.group(1)) if cn else int(m.group(1))
            d = _cn_to_int(m.group(2)) if cn else int(m.group(2))
            if mo is None or d is None:
                continue
            if 1 <= mo <= 12 and 1 <= d <= 31:
                hits.append(DateHit(m.group(0), mo, d, None, s, e))
                taken.append((s, e))

    for m in _SLASH_DATE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            hits.append(DateHit(m.group(0), mo, d, None, s, e))
            taken.append((s, e))

    return sorted(hits, key=lambda h: h.start)


def find_times(text: str) -> list[TimeHit]:
    """找出源文里所有的时刻。零宽的前导时段词会被吸收进 `raw`。"""
    hits: list[TimeHit] = []
    taken: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        return any(s < te and ts < e for ts, te in taken)

    def _extend_left(s: int) -> tuple[int, int]:
        """把紧挨着的时段词吸进来（`下午两点` 的 `下午`），并返回修正量。

        返回 `(新的起点, 该时段词带来的小时修正)`。
        """
        for word, add in _MERIDIEM.items():
            if text[max(0, s - len(word)): s] == word:
                return max(0, s - len(word)), add
        return s, 0

    for m in _HHMM_COLON.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            ns, add = _extend_left(s)
            h = _apply_meridiem(h, add)
            hits.append(TimeHit(text[ns:e], h, mi, ns, e))
            taken.append((ns, e))

    for m in _HHMM_CN.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            ns, add = _extend_left(s)
            h = _apply_meridiem(h, add)
            hits.append(TimeHit(text[ns:e], h, mi, ns, e))
            taken.append((ns, e))

    for m in _HOUR_ONLY.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        h = int(m.group(1))
        if h <= 23:
            ns, add = _extend_left(s)
            h = _apply_meridiem(h, add)
            hits.append(TimeHit(text[ns:e], h, 0, ns, e))
            taken.append((ns, e))

    for m in _CN_HOUR.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        h = _cn_to_int(m.group(1))
        if h is None or h > 23:
            continue
        tail = m.group(2)
        mi = 0
        if tail == "半":
            mi = 30
        elif tail and tail[0].isdigit():
            mi = int(tail)
        elif tail:
            v = _cn_to_int(tail)
            mi = v if v is not None else 0
        if mi > 59:
            continue
        ns, add = _extend_left(s)
        h = _apply_meridiem(h, add)
        hits.append(TimeHit(text[ns:e], h, mi, ns, e))
        taken.append((ns, e))

    return sorted(hits, key=lambda h: h.start)


def _apply_meridiem(hour: int, add: int) -> int:
    """把十二小时制补成二十四小时制。

    ⚠️ 只在**需要**时加 12：`下午两点` → 14，但 `晚上8点` 已经是 20 就不动
    （源文里写 20 点还带「晚上」的情况存在）。
    """
    if add == 12 and hour < 12:
        return hour + 12
    if add == 0 and hour == 12:
        # `凌晨12点` 其实是 0 点
        return 0
    return hour


def has_relative_day(text: str) -> str | None:
    """源文里有没有相对日词。返回命中的那个词（闸门要指着它说话）。"""
    for word in RELATIVE_DAYS:
        if word in text:
            return word
    return None


def find_date_literal(text: str, month: int, day: int) -> str | None:
    """源文里有没有**这个**月日。有就返回原文片段，没有返回 None。

    ⚠️ 这是闸门的核心判据：**字面匹配**。它笨，但它可测、可复现、能解释。
    """
    for hit in find_dates(text):
        if hit.month == month and hit.day == day:
            return hit.raw
    return None


def find_time_literal(text: str, hour: int, minute: int) -> str | None:
    """源文里有没有**这个**时刻（精确到分钟）。"""
    for hit in find_times(text):
        if hit.hour == hour and hit.minute == minute:
            return hit.raw
    return None
