r"""时间解析 —— 把中文时间写法解析成绝对时刻。

## 要防什么

这个模块是**核验闸门**的地基。闸门的判据是「模型填的这个值，源文里查得到
吗」，而「查得到」需要一个**统一的口径**把源文里的时间写法与模型填的值
映到同一个坐标系里。

※ **它不取当前时刻**（护栏钉这条）。「今年是哪一年」是调用方传进来的 ——
因为「9月30日」在源文里没写年份，要靠**消息发出时刻**去补，而那是一个
外部输入。

## 判据：中文时间写法有哪些形态

从语料里归纳（这个清单来自对本项目 `synth.py` 自造语料的观察，不是抄来的）：

| 形态 | 例子 | 解析难度 |
|---|---|---|
| 绝对月日 | `10月28日`、`10月28号` | 低 |
| 带斜杠 | `10/28`、`10-28` | ※ 与分数/区间冲突 |
| 带年份 | `2026年10月28日` | 低 |
| 月日 + 时刻 | `10月18日 14:00` | 低 |
| 十二小时制 | `下午两点`、`晚上8点` | 中 |
| 半 | `九点半` | 中 |
| 中文数字 | `十月二十八日` | 中 |
| 相对日 | `明早`、`后天`、`下周三` | ※ **推不出确定时刻** |

## 判据的形状：日期/时刻是**一个 token**，不是一个子串

要回答的是「模型填的这个月日，源文里有没有」。最直白的写法是**子串匹配**：
`"10月28" in source`。它错在哪？

**同一个字符序列，边界不同就是不同的值。** 源文写 `11/25`，
而它的子串 `11/2` 也是一个合法的月日 —— 子串匹配会让源文给**另一个日子**
背书，而用户会按那个日子去办事。

⇒ 所以判据不是「这段字符出现过」，而是「这段字符**自成一段**」：
匹配到的月日，它的**左右两边都不能紧邻数字**。

### 为什么是「两侧」，不是「长度」

长度这条判据**两个方向都不成立**：

  · 短的确实是日期：`11/2-11/5` 是一个日期**区间**，两个端点都只有一位月/日。
    「短」不是拒绝的理由。
  · 长的确实包含短的：`11/25` 包含 `11/2`。「长」不是接受的理由。

⇒ 长度既非必要也非充分。**边界才是那个判据**，长度只是它的一个副作用。

※ 同样的形状在**时刻**上更隐蔽，因为「分」这个字把边界藏了起来：
源文写 `7点40分`，模型填 `7点4分`。`7点4` 之后是 `0`（数字）⇒ 拒绝；
而源文真的写 `7点4分` 时，`4` 之后是「分」（非数字）⇒ 通过。

### 「裸日不算依据」

源文只写「28日」不写月份时，模型填一个**完整日期** —— 算不算有依据？

**不算。** 理由不是「判据要严格」，而是**这一格恰恰是错的来源**：
月份不在源文里，它是从别处推出来的，而**闸门看不到那个别处**。
闸门拿到的只有消息正文（`gate(item, src.content, ...)`），
拿不到消息的发出时刻，所以它无法核对月份。

⇒ 一个「日」对得上、而「月」无从核对的日期，**不是可核对的依据**。
用户按错的月份去办，会错过期限 —— 这正是闸门要拦的那件事。

※ 由此推出一条**结构下限**：本模块只认「月 + 日」齐备的日期。
`28日` 这种**裸日不是本模块意义上的日期**（`_MONTH_DAY` 要求 `月` 在场），
所以它既不会产出 `DateHit`，也就不会给任何日期背书。

### 年份：源文写了的才核对

模型填的年份通常来自**消息发出时刻**（源文里一般不写年份，
见 `extract._resolve_ts`）。闸门同样拿不到消息时刻，所以：

  · 源文里**没写年份**（`10月28日`）⇒ 年份**不参与判据**，只比月日；
  · 源文里**写了年份**（`2027年10月28日`）⇒ 年份参与判据，
    模型填的年份必须与它一致。否则「源文写着 2027 年」会给「2026 年」背书。

见 `find_date_literal` 的 `year` 参数。

### 相对日词推不出绝对时刻

模型看到「明早集合」填了 `08:00`，那是它自己补的：「明早」这个词在源文里，
`08:00` 不在。

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

# ── 跨行：`\s*` 允许换行，是**对的**，但理由必须写下来 ──────────────
#
# 现象（实测）：`find_dates("10月\n28日")` → `DateHit(raw='10月\n28日', 10, 28)`。
# 即 `\s*` 把**换行**也吃进去，把断成两行的月与日拼成了一个日期。
# 第一眼像 bug（「两段文本被粘起来了」）。要回答的是：它到底是不是。
#
# 判定依据是**调用方喂进来的是什么**。查了全部调用点，只有两类：
#   · `application/extract.py::_resolve_ts` —— `find_dates(text)` 的 `text`
#     是模型填的日期串；`find_dates(source)` / `find_times(text)` 的 `source`
#     是 `src.content`（一条 `PendingMessage` 的正文）。
#   · `domain/verify.py::gate(item, src.content, ...)` —— 闸门收到的 `source`
#     同样是**单条消息的正文**（见 `extract.py` 里 `gate(...)` 的实参）。
# 两处的 `source` 都是单条消息正文，**不是**多条消息拼起来的批文本。
# `chunking` 决定的是「哪些消息进同一批」，它**从不**把多条消息的正文连成
# 一段文本再交出去；核验也是**逐条**做的。所以「把 A 消息的日期与 B 消息的
# 日期粘起来」这条路径根本不存在。
#
# ⇒ **一条消息内部换行是合法的排版，不是消息边界。** 群公告常写成
#   「报名截止：10月\n28日」，会议转写的正文更是整段多行（`chunking` 的
#   docstring 就把「会议转写」列为超长单体的来源之一）。所以 `\s*` 允许换行
#   **恰恰是对的** —— 要修的不是判据，而是「没有人回答过这个问题」这件事。
#
# ※ 由此留下一条**前提**，改这里的人必须看见：
#   `find_dates` / `find_times` 只接受**单条消息正文**。
#   若哪天有人把多条消息拼成一段文本喂进来，跨行拼接就**变成真 bug** ——
#   那时 A 消息行尾的「10月」会与 B 消息行首的「28日」粘成一个日期，为原文
#   里并不存在的日期背书。真有这个需求时，应当在**拼接处**插一个不会被
#   `\s*` 吃掉的分隔符（或改成逐条调用），而不是在这里收紧 `\s`。
#
# 残余风险（已知、可接受）：单条消息里若**恰好**有一行以「月」结尾、下一行
# 以「数字 + 日/号」开头，也会被拼上。实测这要求换行**紧跟在「月」之后**
# ——「10月表彰\n28日交表」不会命中（「月」后紧跟的是「表」，不是空白），
# 而「10月\n28日的安排」这种写法读起来本身就是 10 月 28 日。
#
# 怎么验证：`tests/domain/test_times.py` 里钉了两条 ——
# `find_dates("10月\n28日")` 命中 10 月 28 日（跨行仍是一个日期）；
# 同一条消息里的两个**独立**日期不互相吞并（`10月1日 …\n10月5日` 出两个命中）。

#: 「月」的写法。`月` 与 `月份` 之外不收 —— 收太宽会把「一期」之类当成月份。
#: ※ 里面的 `\s*` 允许换行，这是**有意的** —— 理由见上面「跨行」一节。
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
#: ※ **两侧都要数字边界** —— 这是 `11/2` 不给 `11/25` 背书的那条判据。
_SLASH_DATE = re.compile(
    _NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[/\-]\s*(\d{1,2})" + _NOT_DIGIT_AFTER
)

#: 中文数字月日：`十月二十八日`。
_CN_MONTH_DAY = re.compile(
    r"([零〇一二三四五六七八九十两]{1,3})\s*月\s*"
    r"([零〇一二三四五六七八九十两]{1,4})\s*[日号]"
)

#: 时刻：`14:00`、`14：00`、`14点00分`、`14点`。
#: ※ 分钟部分要**数字边界**，而不是「必须是两位」：`14:5` 是 `14:05` 的一种
#: 合法写法（人确实会这么打），而 `14:50` 里那个 `14:5` **不是** `14:5` ——
#: 右侧的 `(?!\d)` 已经把这件事判掉了。用 `\d{2}` 会把前者也一起拒掉，
#: 那是拿「长度」当判据，与「两侧才是判据」自相矛盾。
_HHMM_COLON = re.compile(_NOT_DIGIT_BEFORE + r"(\d{1,2})\s*[:：]\s*(\d{1,2})" + _NOT_DIGIT_AFTER)
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

#: 相对日词。※ 这些**推不出**绝对日期 —— 它们只说明「源文提到了时间」。
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
    """找出源文里所有的日期。按出现位置排序，重叠的取先长后短。

    ※ `text` 必须是**单条消息的正文**（或模型填的单个日期串），**不能**是
    多条消息拼起来的文本 —— 见上面「跨行」一节：本函数允许日期跨换行，
    那条判据只在「换行 = 一条消息内部的排版」这个前提下成立。
    """
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
    """找出源文里所有的时刻。零宽的前导时段词会被吸收进 `raw`。

    ※ 与 `find_dates` 同一条前提：`text` 是**单条消息的正文**，
    不是多消息拼接的文本（理由见上面「跨行」一节）。
    """
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

    ※ 只在**需要**时加 12：`下午两点` → 14，但 `晚上8点` 已经是 20 就不动
    （源文里写 20 点还带「晚上」的情况存在）。
    """
    if add == 12 and hour < 12:
        return hour + 12
    if add == 0 and hour == 12:
        # `凌晨12点` 其实是 0 点
        return 0
    return hour


#: 相对日词按**长到短**排 —— 闸门要指着源文里的那个词说话，
#: 而 `大后天` 里**含有** `后天`。按表里的顺序取，`大后天交表` 会被报成
#: 「源文只有相对时间『后天』」—— 一句**源文里没有的话**。
#: 证据必须能逐字对回源文，所以先试长的。
_RELATIVE_DAYS_LONGEST_FIRST = tuple(sorted(RELATIVE_DAYS, key=len, reverse=True))


def has_relative_day(text: str) -> str | None:
    """源文里有没有相对日词。返回命中的那个词（闸门要指着它说话）。

    ※ 返回的是**源文里真实存在的那个串**，不是「最接近的那个词」——
    长词优先，见 `_RELATIVE_DAYS_LONGEST_FIRST`。
    """
    for word in _RELATIVE_DAYS_LONGEST_FIRST:
        if word in text:
            return word
    return None


def find_date_literal(
    text: str, month: int, day: int, year: int | None = None
) -> str | None:
    """源文里有没有**这个**月日。有就返回原文片段，没有返回 None。

    ※ 这是闸门的核心判据：**字面匹配**。它笨，但它可测、可复现、能解释。

    `year`：模型填的那个日期的年份。
      · `year is None` ⇒ 年份**不参与**判据（调用方自己也不知道该是哪一年）。
      · 否则：源文**写明了年份**的那些命中，年份必须一致；
        源文**没写年份**的命中（`hit.year is None`）仍然算数 ——
        年份是从消息时刻推的，源文本来就没写，不该因此判它没依据。

    ※ 为什么必须区分这两者：源文写 `2027年10月28日`、模型填 `2026-10-28`，
    这是**两个不同的日子**；不核对年份就等于用前者的原文给后者背书。
    """
    for hit in find_dates(text):
        if hit.month != month or hit.day != day:
            continue
        if year is not None and hit.year is not None and hit.year != year:
            continue  # 源文写明了年份，且与模型填的不一致 ⇒ 不是这个日期
        return hit.raw
    return None


def find_time_literal(text: str, hour: int, minute: int) -> str | None:
    """源文里有没有**这个**时刻（精确到分钟）。"""
    for hit in find_times(text):
        if hit.hour == hour and hit.minute == minute:
            return hit.raw
    return None
