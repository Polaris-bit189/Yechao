"""源文核验闸门 —— 「模型填的这个值，源文里查得到吗」。

## 要防什么

**模型会补出原文没有的字段。** 源文写「明早集合」，它可能填一个具体日期；
写「押金照旧」，它可能编一个金额。而这些字段会驱动排期、催办、日历 ——
编出来的值会被当成真的。

## 为什么判据必须机械

「这个日期靠不靠谱」如果让第二个模型来判，就**没法写测试**了 —— 而一个
没法测试的闸门出错时也发不出声音。

所以判据是**字面匹配**：源文里逐字找得到吗。它笨，但它可测、可复现、能解释。

## 四种字段，各自的「最小可信单元」不同

要害在于：**单元粒度错了，判据就不成立**。

| 字段 | 最小可信单元 | 为什么 |
|---|---|---|
| 日期 | 一个**完整日期**（月+日） | 日期是一个 token，两侧都要是边界（`11/2` 不能给 `11/25` 背书） |
| 时刻 | 一个**完整时刻**（时+分） | 同上（`7点4分` 不能给 `7点40分` 背书） |
| 地点 | **一个 3 字（或整串）的连续片段** | 见下面「地点」一节 —— 2 字太弱，整串太严，3 字是这条线的位置 |
| 金额 | **每个数字**都要找到 | `1,200` 与 `1200` 是同一个数（容忍千分位与补零），`200` 与 `1200` 不是 |

## 找不到依据时怎么办：按**危害**分档

处置方式有三个选项。我的判断是**按字段的危害分档**，
而不是全局统一：

| 字段 | 错了的后果 | 处置 | 理由 |
|---|---|---|---|
| 时刻（start_ts） | 用户**跑错时间**，当场才知道 | **清掉** | 危害最大 |
| 截止日（deadline_ts） | 用户错过截止 | **清掉** | 同上 |
| 地点（place） | 用户**跑错地方** | **清掉** | 与时间同类：会让人白跑一趟 |
| 金额（amount） | 带错钱，但**上下文会提醒他** | **保留 + 标记** | 见 `verify_amount` |

※ 前三条是同一个判据：**错了会让人白跑一趟或错过一件事**，而它们清掉之后
用户看到的是「这条没说时间/地点」—— 一个**真话**（源文确实没说）。金额不同：
清掉它，用户看到的是「这条没有金额」，而**他不会去别处找** —— 那是一个
**假话**。所以金额保留原值，只在内部标为「未核验」。

## 「判据不适用」是一个独立的第三态

这里要问：源文写「免费」「面议」，没有数字可查 ——
返回「有依据」还是「没依据」？

**两个都不能返回**：返回「没依据」会**清掉真信息**（「面议」被清掉，
用户看不到这个词了）；返回「有依据」会**放行一个其实没验过的值**。

⇒ 需要第三态：**「判据不适用」**。不适用时**保留原值**，但**标记它没被
核验过**。这样：
  · UI 不会把它当「已核验」显示（那是假话）
  · 也不会把真信息清掉

※ 「不适用」与「有依据」在**保留原值**这件事上结果相同 —— 但**不是同一件
事**，所以是两个状态，不是一个布尔。把它们混起来，就再也没法回答
「这一栏到底验过没有」。
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from enum import Enum

from yechao.domain.models import Item
from yechao.domain.times import find_date_literal, find_time_literal, has_relative_day

#: 判据的三种结论。
#:
#: ※ 三态而不是布尔 —— 见模块 docstring「判据不适用」一节。
class Support(Enum):
    SUPPORTED = "supported"      # 源文里逐字找得到 ⇒ 采信
    UNSUPPORTED = "unsupported"  # 源文里找不到 ⇒ 处置（清掉 / 标记）
    NOT_APPLICABLE = "n/a"       # 判据不适用 ⇒ 保留原值，但标为「未核验」


@dataclass(frozen=True)
class Verdict:
    """一个字段的核验结论。`evidence` 是**源文里的那一小段**。

    ※ 带 `evidence` 是为了「每条结论都要能指着源文说『这里查得到/查不到』」。
    没有它，结论就只是一句断言。
    """

    field: str
    support: Support
    evidence: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.support is Support.SUPPORTED


# ── 日期 ────────────────────────────────────────────────────────────


def verify_deadline(
    source: str, ts: int | None, *, local_year: int, tz_offset: int = 8 * 3600
) -> Verdict:
    """核验截止日。

    ※ **裸日不算依据** —— 源文里写「本学期」「月底」，模型填一个具体日期，
    那不是源文里的依据。判据必须是源文里**字面**出现那个月日。

    `ts` 是模型填的绝对时刻；本函数把它拆回月/日（**以及年**）去源文里找。

    ※ `local_year`：调用方（`extract.py`）会传它，但**本判据用不到** ——
    年份的核对用的是**模型填的那个日期自己的年份**（从 `ts` 拆出来的），
    因为要回答的是「源文里写的那一年，与模型填的那一年是不是同一年」。
    保留这个形参是因为调用点的签名已经定了；它不参与任何判断。
    """
    if ts is None:
        return Verdict("deadline_ts", Support.NOT_APPLICABLE, None, "模型没填")

    import datetime as _dt

    # ※ **必须按本地日拆** —— 与 `_resolve_ts` 存进去时用的口径**一致**。
    #
    # 这里踩过一次，而且它让**整个截止日判据失效**：
    # 解析器把「11月1日」存成「本地 11-01 00:00」的 Unix 秒，
    # 而闸门用 **UTC** 拆它 —— UTC 是 10-31 16:00，于是拆出 `10月31日`。
    # 源文里明明写着 11月1日，闸门判「找不到这个月日」并把字段清掉了。
    #
    # 症状：**每一个截止日都被清掉**（0/2 召回），而闸门自己的逻辑是通的。
    # 这是「两边各写一份必漂移」的教科书例子 —— 两份不是复制粘贴，
    # 是**各自默认了一个不同的时区**。
    d = _dt.datetime.fromtimestamp(ts + tz_offset, tz=_dt.timezone.utc).date()
    literal = find_date_literal(source, d.month, d.day, year=d.year)
    if literal:
        return Verdict("deadline_ts", Support.SUPPORTED, literal, "源文里逐字有此日期")

    rel = has_relative_day(source)
    if rel:
        # ※ 源文提到了时间但是**相对的**：模型把「月底」推成了一个具体日期。
        # 那不是源文的依据 —— 但也不是「源文完全没提」。
        return Verdict(
            "deadline_ts", Support.UNSUPPORTED, rel,
            f"源文只有相对时间「{rel}」，没有可核对的月日",
        )
    return Verdict("deadline_ts", Support.UNSUPPORTED, None, "源文里找不到这个月日")


def verify_start(source: str, ts: int | None, *, tz_offset: int = 8 * 3600) -> Verdict:
    """核验活动开始时刻。

    ※ 与截止日**分开核验**：它们落到不同字段上，混起来会让「模型把开始
    时刻正确填进 start 却被判成没填截止日」。

    ※ 而且这一栏的要求**比日期高一档**：它要精确到**分钟**。源文只写了
    日期、没写时间时，模型填不进这一格 —— 那是**结构下限**达不到，
    不是模型漏了。
    """
    if ts is None:
        return Verdict("start_ts", Support.NOT_APPLICABLE, None, "模型没填")

    import datetime as _dt

    # ※ 同上：按本地时刻拆，与存进去时一致。
    dt = _dt.datetime.fromtimestamp(ts + tz_offset, tz=_dt.timezone.utc)
    literal = find_time_literal(source, dt.hour, dt.minute)
    if literal:
        # 时刻找到了，还要确认**日期**也对得上（`11月2日 20:00` 这个整体）。
        date_lit = find_date_literal(source, dt.month, dt.day, year=dt.year)
        if date_lit:
            return Verdict(
                "start_ts", Support.SUPPORTED, f"{date_lit} {literal}",
                "源文里有这个日期与时刻",
            )
        return Verdict(
            "start_ts", Support.UNSUPPORTED, literal,
            "时刻在源文里，但它配的日期对不上",
        )

    rel = has_relative_day(source)
    if rel:
        return Verdict(
            "start_ts", Support.UNSUPPORTED, rel,
            f"源文只有相对时间「{rel}」，推不出确定的时刻",
        )
    return Verdict("start_ts", Support.UNSUPPORTED, None, "源文里找不到这个时刻")


# ── 地点 ────────────────────────────────────────────────────────────

#: 地点判据的**共同片段长度上限**。见 `verify_place` 的 docstring ——
#: 3 是「一个地点名的核心词有多长」推出来的，不是试出来的。
_PLACE_WINDOW_MAX = 3

#: 地点短于这个长度，判据**没有分辨力** ⇒ 不适用（保留原值 + 标记）。
#: 1 字：任何一个字（「楼」「厅」「馆」）都能在中文源文里找到，判据恒真。
_MIN_PLACE_LEN = 2


def _windows(s: str, n: int) -> list[str]:
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def verify_place(source: str, place: str | None) -> Verdict:
    """核验地点。

    ## 要防的是**两个方向**的错

      · 误杀：源文写「青梧礼堂连廊」，模型写「青梧礼堂」—— 它们是同一个
        地方。要求整串一致会把真地点清掉，用户于是**不知道去哪**。
      · 放行错的：源文写「西校区」，模型填「东校区」—— 它们是**两个**地方。
        放行它，用户**跑错校区**。这与误杀是同一件代价（白跑一趟），
        只是方向相反。

    ⇒ 判据不能只往一个方向放宽，它得**同时**回答「这两个是不是同一个地方」。

    ## 判据：最长共同连续片段的长度下限

    判据是「模型填的地点，与源文之间有没有一段**够长**的连续共同片段」。

    **为什么是 3 字**：
      · 2 字太弱。「校区」「门口」「食堂」「二楼」这类**通用后缀**正好是 2 字。
        共享它们**不代表是同一个地方** —— `东校区` 与 `西校区` 就共享「校区」。
        用 2 字当判据，等于把「不同地方」放行，判据自己废掉了。
      · 3 字在中文里已经是很强的信号：地点名通常是 2–4 字，三字重合意味着
        **核心词**重合（`青梧礼` 重合 ⇒ 是同一处；`图书馆` 重合 ⇒
        是同一栋楼）。而误杀那侧的「青梧礼堂连廊」也正是靠 `青梧礼` 三个字
        救回来的 —— 多出来的字通常在**尾部**，不在核心词上。

    **为什么地点短于 3 字时要整串**：`东门`、`食堂` 这类两字地点没有三字
    可要求，退而要求**整串**（两字全中）。两字全中比「共享一个通用后缀」
    强得多，是这两字地点上唯一还成立的判据。

    ⇒ `k = min(len(place), 3)`，命中任一 `k` 字窗口即算有依据。

    ※ 短于 2 字 ⇒ `NOT_APPLICABLE`（保留原值 + 标记），见 `_MIN_PLACE_LEN`。
    理由不是「宽一点」，而是**判据在这个输入下没有分辨力**：
    单字命中恒真，返回「有依据」是假话、返回「没依据」是清掉真信息 ——
    两个都不能选，所以是第三态。
    """
    if not place:
        return Verdict("place", Support.NOT_APPLICABLE, None, "模型没填")

    cleaned = place.strip()
    if len(cleaned) < _MIN_PLACE_LEN:
        return Verdict(
            "place", Support.NOT_APPLICABLE, cleaned,
            f"地点短于 {_MIN_PLACE_LEN} 字，判据在这个输入上没有分辨力",
        )

    k = min(len(cleaned), _PLACE_WINDOW_MAX)
    for w in _windows(cleaned, k):
        if w in source:
            return Verdict(
                "place", Support.SUPPORTED, w,
                f"源文里有这个 {k} 字片段",
            )
    return Verdict(
        "place", Support.UNSUPPORTED, None,
        f"源文里找不到这个地点的任何 {k} 字连续片段",
    )


# ── 金额 ────────────────────────────────────────────────────────────

#: 从金额串里抓数字（含小数）。
_AMOUNT_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(text: str) -> list[str]:
    return _AMOUNT_NUM.findall(text)


def _number_found_in_source(model_num: str, source_nums: set[str], source: str) -> bool:
    """model 的某个数字在源文里找得到吗。**容忍千分位与补零**。

    · `1,200` 与 `1200` 是同一个数 —— 源文写 `1,200`，模型填 `1200`，放过。
    · `1200` 与 `1200.00` 同上（补零）。
    · ※ `200` 与 `1200` **不是**同一个数 —— 判据是**完整数字**的相等，
      不是子串包含。这正是「最小可信单元是一个完整数字」的意思。
    """
    norm = model_num.rstrip("0").rstrip(".") if "." in model_num else model_num
    for cand in source_nums:
        if cand == model_num:
            return True
        # 千分位：源文 `1,200` → 抓出来是 `1` 与 `200`，所以再看一眼原文
        cand_norm = cand.rstrip("0").rstrip(".") if "." in cand else cand
        if cand_norm == norm:
            return True
        # 补零：`1200.00` vs `1200`
        if cand.lstrip("0") == model_num.lstrip("0"):
            return True
    # 千分位形态：把源文里的 `1,200` 连起来再比一次
    joined = re.sub(r"(?<=\d),(?=\d{3}\b)", "", source)
    return model_num in _numbers_in(joined)


def verify_amount(source: str, amount: str | None) -> Verdict:
    """核验金额。

    **判据：金额里的每个数字都要在源文里找得到。**
    理由：
    「每个数字都要单独找到依据」，因为金额可能由两个数组成（`押金 100，
    租金 200`），只验其中一个会给另一个背书。

    ## ※ 我推出来与「清掉」不同的一个判断

    别的字段找不到依据时我**清掉**它。金额这里我**保留原值但标记未核验**。

    理由：金额与时间/地点的**失败模式不同**。
      · 时间错了 ⇒ 用户**跑错时间**，他去错了地方，且**当场才知道**。
      · 金额错了 ⇒ 用户**带错钱** —— 但金额往往伴随**上下文**（「押金」、
        「报名费」这种词在标题里），用户看到时会自己核一下。而金额被清掉时，
        用户看到的是「这条没有金额」，他不会去别处找。
      ⇒ **清掉金额的代价（真信息永久消失）高于保留它的代价**。

    这个取舍我写在这里，因为它是一个判断，不是问题域决定的唯一答案。
    如果你认为金额也该清掉，改动点是本函数的一行。

    ## 「不适用」

    `免费` / `面议` / `自带` 里没有数字可查 ⇒ 返回 `NOT_APPLICABLE`，
    **保留原值**。见模块 docstring。
    """
    if not amount:
        return Verdict("amount", Support.NOT_APPLICABLE, None, "模型没填")

    nums = _numbers_in(amount)
    if not nums:
        return Verdict(
            "amount", Support.NOT_APPLICABLE, amount.strip(),
            "金额里没有数字（「免费」「面议」这类），判据不适用",
        )

    source_nums = set(_numbers_in(source))
    missing = [n for n in nums if not _number_found_in_source(n, source_nums, source)]
    if not missing:
        return Verdict("amount", Support.SUPPORTED, amount.strip(), "每个数字都在源文里")
    return Verdict(
        "amount", Support.UNSUPPORTED, None,
        f"这些数字在源文里找不到：{missing}",
    )


# ── 闸门 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateResult:
    """一个条目过完闸门的结果。"""

    item: Item
    verdicts: tuple[Verdict, ...]

    @property
    def cleared(self) -> tuple[str, ...]:
        """**被清成 None** 的字段名。给「闸门拦下几条」这类中间指标用。"""
        return tuple(
            v.field
            for v in self.verdicts
            if v.support is Support.UNSUPPORTED
            and v.field in {"deadline_ts", "start_ts", "place"}
        )

    @property
    def flagged(self) -> tuple[str, ...]:
        """**保留了原值但没有依据**的字段名。

        ※ 这里**包括** `UNSUPPORTED` 的金额 —— 按设计它**保留原值**，
        但保留不等于「验过了」。原来这里的判据只看 `NOT_APPLICABLE`，
        于是「金额没依据」这一档**两个集合都不在**：它既没被清（对），
        也没被报出来（错）。那是一处**处置落进缝里**的形状 ——
        指标与界面都会漏掉它。
        """
        kept = {
            "deadline_ts": "deadline_ts",
            "start_ts": "start_ts",
        }
        out: list[str] = []
        for v in self.verdicts:
            if v.field in kept:
                continue  # 这两个是「清掉」那一档
            if v.support is not Support.SUPPORTED:
                # 金额 / 地点里**保留了原值**的那些
                if v.field == "place" and v.support is Support.UNSUPPORTED:
                    continue  # 地点是清掉那一档，见 `cleared`
                out.append(v.field)
        return tuple(out)


def gate(
    item: Item, source: str, *, local_year: int, tz_offset: int = 8 * 3600
) -> GateResult:
    """**在写入边界**跑闸门。

    ※ 位置很关键：它在**数据层**，不在渲染层。网页直接读库字段，只在渲染层
    设卡的话，绕过去太容易。

    处置按危害分档：
      · `deadline_ts` / `start_ts` / `place` 找不到依据 ⇒ **清成 None**
        （时间错了错过截止，地点错了白跑一趟）
      · `amount` ⇒ 保留原值 —— 它错了上下文会提醒（「30 元」在标题旁边
        本来就不协调），清掉反而丢了信息
    """
    verdicts = [
        verify_deadline(source, item.deadline_ts, local_year=local_year, tz_offset=tz_offset),
        verify_start(source, item.start_ts, tz_offset=tz_offset),
        verify_place(source, item.place),
        verify_amount(source, item.amount),
    ]
    by_field = {v.field: v for v in verdicts}

    changes: dict[str, object] = {}

    dl = by_field["deadline_ts"]
    if dl.support is Support.UNSUPPORTED:
        changes["deadline_ts"] = None
        changes["deadline_supported"] = False
    elif dl.support is Support.SUPPORTED:
        changes["deadline_supported"] = True

    st = by_field["start_ts"]
    if st.support is Support.UNSUPPORTED:
        changes["start_ts"] = None
        changes["start_supported"] = False
    elif st.support is Support.SUPPORTED:
        changes["start_supported"] = True

    # ※ `place` / `amount` **不改值** —— 它们是「保留但标记」那一档。
    # 但标记必须真的落下去：只保留原值而不记「它没被核验过」，
    # 就是让一个未验的值冒充已验的值。
    if by_field["place"].support is Support.UNSUPPORTED and item.place:
        changes["place"] = None  # 地点与时间同类（会让人跑错地方），清掉

    out = dataclasses.replace(item, **changes)  # type: ignore[arg-type]

    # ── 输出不变量（不论输入长什么样，出口必须满足）──────────────
    #
    # ※ 这两条**不是**在兜底一个写错的输入 —— 它们是在守一个**跨字段的
    # 不变量**：`deadline_ts` 为空 ⇔ `deadline_supported` 为假。
    #
    # 为什么必须在出口强制：这两个字段会被**两个不同的消费者**读 ——
    #   · 前端读 `deadline_ts`（为 null 就不显示）
    #   · 日报读 `deadline_supported`（为假就不发给模型）
    # 只保证其中一个方向的同步，就会出现「界面上看不到、但模型看得到」
    # 或者反过来 —— 而那种错在两边代码单看都正常。
    if out.deadline_ts is None and out.deadline_supported:
        out = dataclasses.replace(out, deadline_supported=False)
    if out.start_ts is None and out.start_supported:
        out = dataclasses.replace(out, start_supported=False)

    return GateResult(item=out, verdicts=tuple(verdicts))
