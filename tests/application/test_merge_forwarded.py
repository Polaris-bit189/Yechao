"""跨群转发的合并。

## 要防什么

**同一条通知被三个群各自转发，这是要合并的信号，不是要丢掉的重复。**

这里有一个反例值得记住：判「重复」时如果把
「群」这一维从桶里拿掉，跨群转发会被误判成刷屏。

而它还有**另一半**：如果只在预筛里保住三条、**不在产出处合并**，
用户会在日报里看到同一件事出现三次 —— 而 `source_count` 永远是 1。

※ 那是一个**承诺过、界面上还显示着**的能力（`Item.source_count` 就在
契约里），实际没实现。第一版就是这样：契约里有这个字段，抽取里没有任何
地方会产出大于 1 的值。

## 合并与「不合并」的判据要共用

「同一件事」这个判断在 `domain/conflicts.py` 里已经有一份
（给日历折重复用）。本模块**复用它**，不另写一份 ——
两份必漂移，而漂移的症状是「日历上折了但日报里还是三条」。
"""

from __future__ import annotations

from yechao.application.extract import _merge_forwarded
from yechao.domain.models import Item, SourceRef


def _item(**kw) -> Item:
    base = dict(
        item_id=0, kind="know", kind_label="",
        title="期中活动报名截止", detail=None, event_ts=1000, deadline_ts=None,
        group_id=1, group_name="", actor=None, actor_account=None,
        place=None, amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def test_三个群转发同一条_合并成一条且来源数为三() -> None:
    items = [
        _item(group_id=1, event_ts=1000),
        _item(group_id=2, event_ts=1300),
        _item(group_id=3, event_ts=1600),
    ]
    srcs = [SourceRef(0, 10, ""), SourceRef(0, 20, ""), SourceRef(0, 30, "")]

    merged, merged_srcs = _merge_forwarded(items, srcs)

    assert len(merged) == 1, "三条转发应当合并成一条"
    assert merged[0].source_count == 3
    # ※ 来源**一条都不许丢** —— 用户点进去要能看到三个群都说过
    assert len(merged_srcs) == 3
    assert {s.msg_id for s in merged_srcs} == {10, 20, 30}


def test_标题不同的事件不合并() -> None:
    items = [
        _item(title="期中活动报名截止", event_ts=1000),
        _item(title="宿舍水电检修通知", event_ts=1010),
    ]
    srcs = [SourceRef(0, 1, ""), SourceRef(0, 2, "")]
    merged, _ = _merge_forwarded(items, srcs)
    assert len(merged) == 2


def test_时间隔得太远不合并() -> None:
    """※ 同一个标题、隔了十小时的两条 —— 多半是两件不同的事
    （或者同一件事被重新提起两次），不该折成一条。"""
    items = [
        _item(title="期中活动报名截止", event_ts=0),
        _item(title="期中活动报名截止", event_ts=10 * 3600),
    ]
    srcs = [SourceRef(0, 1, ""), SourceRef(0, 2, "")]
    merged, _ = _merge_forwarded(items, srcs)
    assert len(merged) == 2


def test_类目不同不合并() -> None:
    """※ 同一个标题被分到不同类目，说明模型对它的判断不一致 ——
    那是**要让人看见**的事，不是要藏起来的重复。"""
    items = [
        _item(kind="know", event_ts=1000),
        _item(kind="attend", event_ts=1010),
    ]
    srcs = [SourceRef(0, 1, ""), SourceRef(0, 2, "")]
    merged, _ = _merge_forwarded(items, srcs)
    assert len(merged) == 2


def test_合并后来源仍按条目顺序对齐() -> None:
    """※ **这条守的是一个很隐蔽的错。**

    `insert_items(items, sources)` 按**顺序**把第 i 个来源挂到第 i 个
    条目上。合并时如果来源的顺序被打乱，它会被挂到**错的条目**上 ——
    而库里看起来完全正常（每条都有来源、数量也对），
    只有点进去看才发现「这条的工作通知挂着另一条的通知」。

    ⇒ 判据：合并后，每个源里的 `msg_id` 必须落在**它原来那条条目**的
    时间附近。这里用一个「A 有两条来源、B 有一条」的形状来钉。
    """
    a1 = _item(title="期中活动报名截止", event_ts=1000)
    b1 = _item(title="宿舍水电检修通知", event_ts=2000)
    a2 = _item(title="期中活动报名截止", event_ts=1500)  # A 的转发

    items = [a1, b1, a2]
    srcs = [SourceRef(0, 10, ""), SourceRef(0, 20, ""), SourceRef(0, 30, "")]
    merged, merged_srcs = _merge_forwarded(items, srcs)

    assert len(merged) == 2
    # 第 0 条是 A（两条来源），第 1 条是 B（一条来源）
    assert merged[0].title == "期中活动报名截止"
    assert merged[0].source_count == 2
    assert merged[1].title == "宿舍水电检修通知"
    assert merged[1].source_count == 1
    # ※ A 的两条来源必须排在前面（与 merged[0] 对齐）
    assert [s.msg_id for s in merged_srcs] == [10, 30, 20]


def test_单条不合并时原样返回() -> None:
    items = [_item()]
    srcs = [SourceRef(0, 1, "")]
    merged, merged_srcs = _merge_forwarded(items, srcs)
    assert len(merged) == 1
    assert merged[0].source_count == 1
    assert len(merged_srcs) == 1
