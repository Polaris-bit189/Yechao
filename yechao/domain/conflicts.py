"""时间冲突与重复条目 —— 日历页的两个计算。

## 一、重复条目（同一件事被转发多次）

**判据**：两条条目的**标题高度相似**、且**时间重叠或很接近** ⇒ 判为同一件事。
被折叠的条目**仍在 `items` 里**，只在 `duplicates` 映射里指一个代表。

⚠️ **为什么不直接删掉**（前端注释逐字）：「被折叠的条目**仍在 `items` 里**
（行没删，可回溯那条不变量要求如此）—— 前端据此把它们**收起来**，而不是
不渲染。直接 `filter` 掉的话，屏幕上少的那条**不留痕迹**，而那正是
『静默丢数据』的形状」。

⇒ 本模块**只产出映射**，从不产出一个「删掉了 X 条」的结果。

## 二、活动撞车

同一时段有两个活动要参加 ⇒ 用户必须**选一个去**。

## 三、为什么分钟数要算但前端不显示

`Conflict.minutes` 字段存在，但前端**不读它**（前端注释逐字：「只报开始时刻，
不报『重叠 N 分钟』…那个数是从推算的活动时长（90 分钟）算出来的，不是抽出来
的 —— 印在界面上会让用户以为是已知事实」）。

⇒ 字段留着（它是给人看时要的一个具体数，也是 CLI 报告要用的），
但**它不是给人看的事实**。本模块在 `minutes` 的注释里标明这一点。

## 四、「你的课」的来源

`ConflictSide.item_id` 为 `None` = **它不是条目**（比如你的课）。
课表是一个**可选**的数据源：读不到时返回 `[{error: ...}]`，而**不是**空数组。

⚠️ 「课表读不出来」与「课表是空的、没有冲突」必须**能分辨**
（前端注释逐字）。所以课表读取的失败**不返回空列表**。
"""

from __future__ import annotations

from dataclasses import dataclass

from yechao.domain.models import Item

#: 活动时长的**推算值**（抽取没给时长时用）。
#: ⚠️ 这是一个**假设**，不是事实 —— 所以它算出来的重叠分钟数也不能当事实
#: 展示给用户。见模块 docstring 三。
ASSUMED_DURATION_MINUTES = 90

#: 判「活动 A 与活动 B 撞车」的重叠阈值。
#: 取 1 分钟：只要**真的重叠**就算，不设「重叠够久才算」——
#: 「只重叠 5 分钟」的两个活动，用户照样只能去一个。
MIN_OVERLAP_MINUTES = 1

#: 折同一条目时，「很接近」的阈值。
#: 取 6 小时：同一件事在早上被转发、下午又被转发，中间隔了大半天，
#: 但它们显然是一件事。24 小时会把「明天的事今天转、今天的事也今天转」
#: 混一起，6 小时是一个保守的起点。
SAME_EVENT_WINDOW_SECONDS = 6 * 3600


@dataclass(frozen=True)
class ConflictSide:
    """冲突里的一侧。字段名与前端 `ConflictSide` 逐个对应。"""

    label: str
    start: int
    end: int
    #: `None` = 它不是条目（比如**你的课**）。
    item_id: int | None
    kind: str  # 'activity' | 'course' | 'task'


@dataclass(frozen=True)
class Conflict:
    a: ConflictSide
    b: ConflictSide
    #: 重叠多少分钟。
    #: ⚠️ **它是推算值**（基于 `ASSUMED_DURATION_MINUTES`），不是抽出来的事实
    #: —— 前端刻意不显示它。留在这里是给 CLI 报告用的。
    minutes: int


def _norm_title(t: str) -> str:
    """标题归一化：只留实义字符，用于相似度比较。"""
    return "".join(ch for ch in t if ch.isalnum() or "一" <= ch <= "鿿")


def _similar(a: str, b: str) -> bool:
    """两条标题是不是「同一件事」。

    **判据：短的那个是长的那个的前缀，或者两者的公共前缀占短的那个的
    ≥ 60%。**

    为什么用前缀而不是编辑距离：中文通知的改写方式是**加前后缀**
    （「【重要】选课通知」vs「选课通知（补充）」），中段几乎不动。前缀匹配
    对这种改写形状最准，而且**可解释**（能指着说「前 4 个字对上了」）。
    """
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if short in long_:
        return True
    # 公共前缀
    n = 0
    for x, y in zip(short, long_):
        if x != y:
            break
        n += 1
    return n >= max(2, int(len(short) * 0.6))


def find_duplicates(items: list[Item]) -> dict[int, int]:
    """找出「同一件事的重复」。

    返回 `{被折叠的 item_id: 代表它的 item_id}`。

    ⚠️ **代表的那一条是 `event_ts` 最早的**（而不是最新的）：最早的那条
    最可能是原文，后面的是转发。转发的那条可能有更多来源
    （`source_count` 更大），但「谁是原文」比「谁被转得多」更稳定。

    ⚠️ 跨群的转发**也要折** —— 三条不同群说同一件事，那是**最有价值**的
    信号（它说明这件事被多个渠道确认了）。折叠时 `source_count` 由调用方
    在合并后重算，本函数不管。
    """
    ordered = sorted(items, key=lambda i: (i.event_ts, i.item_id))
    dup: dict[int, int] = {}
    reps: list[Item] = []

    for it in ordered:
        matched: Item | None = None
        for rep in reps:
            if abs(it.event_ts - rep.event_ts) > SAME_EVENT_WINDOW_SECONDS:
                continue
            if not _similar(it.title, rep.title):
                continue
            # ⚠️ 类目不同时**不折** —— 同一个标题被分到不同类目，说明模型
            # 对它的判断不一致，那是**要让人看见**的事，不是要藏起来的重复。
            if it.kind != rep.kind:
                continue
            matched = rep
            break
        if matched is not None:
            dup[it.item_id] = matched.item_id
        else:
            reps.append(it)

    return dup


def find_conflicts(
    items: list[Item], *, duration_minutes: int = ASSUMED_DURATION_MINUTES
) -> list[Conflict]:
    """找出活动之间的撞车。

    只考虑**有开始时刻**的条目（`start_ts is not None`）—— 没有开始时刻的
    活动排不进时间轴，它对不出冲突。

    ⚠️ 被折叠的重复条目**已经**由 `find_duplicates` 处理，调用方应先折再算，
    否则同一件事的两个副本会互相「撞车」。
    """
    timed: list[tuple[Item, int, int]] = []
    for it in items:
        if it.start_ts is None:
            continue
        end = it.start_ts + duration_minutes * 60
        timed.append((it, it.start_ts, end))

    timed.sort(key=lambda t: (t[1], t[0].item_id))
    out: list[Conflict] = []
    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            it_a, s_a, e_a = timed[i]
            it_b, s_b, e_b = timed[j]
            # 已按开始时刻排序 ⇒ s_b >= s_a，只需比较 s_b 与 e_a
            overlap = min(e_a, e_b) - max(s_a, s_b)
            minutes = overlap // 60
            if minutes >= MIN_OVERLAP_MINUTES:
                out.append(
                    Conflict(
                        a=ConflictSide(
                            label=it_a.title, start=s_a, end=e_a,
                            item_id=it_a.item_id, kind="activity",
                        ),
                        b=ConflictSide(
                            label=it_b.title, start=s_b, end=e_b,
                            item_id=it_b.item_id, kind="activity",
                        ),
                        minutes=minutes,
                    )
                )
    return out


# ── 课表 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Course:
    """一节课。它**不是条目** —— 它来自课表，不在主库里。"""

    label: str
    start: int
    end: int


def course_conflicts(
    items: list[Item], courses: list[Course] | None
) -> list[Conflict] | list[dict[str, str]]:
    """算「活动撞课」。

    ⚠️ **课表读不出来时返回 `[{'error': '…'}]`，不是 `[]`**。

    理由（前端注释逐字）：「课表读不出来时是 `[{ error: "…" }]` —— 与
    『课表是空的、没有冲突』在界面上必须**能分辨**，所以不是空数组」。

    `None` 表示**读不出来**（文件不在 / 格式不认识）；`[]` 表示**课表是空的**。
    这两件事形状相同，正是问题的根源。
    """
    if courses is None:
        return [{"error": "课表读不出来，本页不报课冲突"}]

    out: list[Conflict] = []
    for it in items:
        if it.start_ts is None:
            continue
        a_start = it.start_ts
        a_end = a_start + ASSUMED_DURATION_MINUTES * 60
        for c in courses:
            overlap = min(a_end, c.end) - max(a_start, c.start)
            minutes = overlap // 60
            if minutes >= MIN_OVERLAP_MINUTES:
                out.append(
                    Conflict(
                        a=ConflictSide(
                            label=it.title, start=a_start, end=a_end,
                            item_id=it.item_id, kind="activity",
                        ),
                        # ⚠️ `item_id=None` —— 课**不是条目**。
                        b=ConflictSide(
                            label=c.label, start=c.start, end=c.end,
                            item_id=None, kind="course",
                        ),
                        minutes=minutes,
                    )
                )
    return out
