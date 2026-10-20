"""切批 —— 不跨群、批大小、时间连续。

## 为什么这个文件必须存在

`chunking.py` 原先有两处「代码与它自己的声明对不上」，都是**静默失效**：

  · docstring 写着「硬约束二：时间要连续」，而 `BREAK_GAP_SECONDS` 定义了
    却没人用，`_split_group` 里的 `_rebalance()` 是个 `return chunk` 的空操作；
  · 整个模块**零测试** —— 所以上面那条从来没有被发现过。

一条声明配一个空操作，比没有这条声明更危险：读代码的人会以为它生效了。
这个文件把三件事钉住：**不跨群**（首要约束）、**批大小上限**、**时间连续的
实际行为**（次级约束，现在真的实现了）。

※ 断言的是**实际分批结果**（哪个 msg_id 落在哪一批），不是「函数被调用过」。
测试绿不等于对 —— 断言调用方的用法，才有意义。
"""

from __future__ import annotations

from yechao.domain import chunking
from yechao.domain.models import PendingMessage

#: 测试用的预算。取小值，让「装不下」这件事在几条消息内就发生。
BUDGET = 100
#: 实际生效的输入预算 —— `chunk()` 会再乘上留给输出的余量。
#: = int(100 * 0.75) = 75。下面的分批结果都是按这个数算出来的。
EFFECTIVE = int(BUDGET * chunking.HEADROOM)


def _msg(msg_id: int, group_id: int, ts: int, content: str) -> PendingMessage:
    return PendingMessage(
        msg_id=msg_id, group_id=group_id, ts=ts, sender="某同学", content=content
    )


def _ids(batch: chunking.Batch) -> list[int]:
    return [m.msg_id for m in batch.messages]


def _shape(batches: list[chunking.Batch]) -> list[tuple[int, list[int]]]:
    """`[(群号, [消息号…]), …]` —— 分批结果的可读形状，直接拿来做断言。"""
    return [(b.group_id, _ids(b)) for b in batches]


# ── 基本形状 ────────────────────────────────────────────────────────


def test_空输入切出空列表() -> None:
    assert chunking.chunk([]) == []


def test_批的字符数与条数按正文累加() -> None:
    b = chunking.Batch(
        group_id=1,
        messages=(
            _msg(1, 1, 0, "甲" * 3),
            _msg(2, 1, 1, "乙" * 5),
        ),
    )
    assert b.chars == 8
    assert len(b) == 2


# ── 首要约束：不跨群 ────────────────────────────────────────────────


def test_不跨群_两个群绝不合成一批() -> None:
    """※ 首要约束。混批的症状是「B 群的通知里出现 A 群的地点」。

    这里两个群各 2 条、总量远小于预算 —— 如果切批只看长度，它们会被并成
    一批。按群分开是**硬**的，不因为「合起来更省一次调用」而让步。
    """
    batches = chunking.chunk(
        [
            _msg(1, 11, 0, "甲" * 10),
            _msg(2, 22, 1, "乙" * 10),
            _msg(3, 11, 2, "丙" * 10),
            _msg(4, 22, 3, "丁" * 10),
        ]
    )
    assert _shape(batches) == [(11, [1, 3]), (22, [2, 4])]


def test_自然断点也不跨群() -> None:
    """时间连续是**群内**的次级约束，它不能把两个群拉到一起。

    两个群各自内部都跨了 2 小时（各自该切一刀），但群与群之间**不**合并：
    4 批，每批只含一个群。
    """
    gap = 2 * 3600
    batches = chunking.chunk(
        [
            _msg(1, 11, 0, "甲" * 10),
            _msg(2, 11, gap, "乙" * 10),
            _msg(3, 22, 0, "丙" * 10),
            _msg(4, 22, gap, "丁" * 10),
        ]
    )
    assert _shape(batches) == [(11, [1]), (11, [2]), (22, [3]), (22, [4])]
    for b in batches:
        assert len({m.group_id for m in b.messages}) == 1


def test_群内按时间升序_输入乱序也排序() -> None:
    batches = chunking.chunk(
        [
            _msg(3, 11, 300, "丙"),
            _msg(1, 11, 100, "甲"),
            _msg(2, 11, 200, "乙"),
        ]
    )
    assert _shape(batches) == [(11, [1, 2, 3])]


# ── 硬约束：批大小上限 ──────────────────────────────────────────────


def test_批大小不超预算_撞上就切_即使时间连续() -> None:
    """预算撞上时**照切不误**，不论时间连不连续。

    理由：撞预算的后果是端点的**静默截断**（回 200、`finish_reason=stop`，
    客户端拿不到任何信号）；而「切得不够漂亮」最多是多花一次调用。

    算术：`EFFECTIVE = 75`，每条 30 字。30 + 30 = 60 ≤ 75 放得下；
    第三条 90 > 75 ⇒ 切。所以 5 条切出 [2, 2, 1]。
    """
    msgs = [_msg(i, 11, i, "甲" * 30) for i in range(1, 6)]
    batches = chunking.chunk(msgs, budget=BUDGET)
    assert _shape(batches) == [(11, [1, 2]), (11, [3, 4]), (11, [5])]
    for b in batches:
        assert b.chars <= EFFECTIVE


def test_批大小上限是乘过余量的那个数() -> None:
    """钉住「生效预算 = `budget * HEADROOM`」这件事本身。

    两条 40 字：80 > 75 ⇒ 各自成批。若哪天有人把余量去掉（或反过来乘错），
    这个断言会响 —— 而余量是留给模型输出 JSON 的，去掉它就是在赌输出很短。
    """
    assert EFFECTIVE == 75
    batches = chunking.chunk(
        [_msg(1, 11, 0, "甲" * 40), _msg(2, 11, 1, "乙" * 40)], budget=BUDGET
    )
    assert _shape(batches) == [(11, [1]), (11, [2])]


# ── 次级约束：群内时间连续 ──────────────────────────────────────────


def test_同群跨两小时切成两批() -> None:
    """※ **这是「时间连续」这条声明原先没有落地的证据。**

    修前：同群 4 条、总量远小于默认预算，`chunk()` 只按长度切 ⇒ **1 批**
    （2 小时的空档被无视）。
    修后：空档 > `BREAK_GAP_SECONDS`（30 分钟）⇒ 在空档处切一刀 ⇒ **2 批**。

    为什么要在空档处切：一句「好的，那就明天下午三点」的含义在它的**前一条**
    里；批次从对话中间切开，那条消息就没了上下文。而 2 小时的空档基本说明
    上一个话题结束了 —— 那是安全的切点。
    """
    gap = 2 * 3600
    batches = chunking.chunk(
        [
            _msg(1, 11, 0, "甲" * 10),
            _msg(2, 11, 60, "乙" * 10),
            _msg(3, 11, 60 + gap, "丙" * 10),
            _msg(4, 11, 120 + gap, "丁" * 10),
        ]
    )
    assert _shape(batches) == [(11, [1, 2]), (11, [3, 4])]


def test_间隔恰好半小时不切() -> None:
    """断点判据是**严格大于**阈值：恰好 1800 秒不算「够久了」。

    边界取闭还是开会改变分批结果，所以钉在这里 —— 改判据时这个测试必须
    一起改，而不是悄悄漂移。
    """
    half = chunking.BREAK_GAP_SECONDS
    assert half == 1800
    batches = chunking.chunk(
        [_msg(1, 11, 0, "甲"), _msg(2, 11, half, "乙"), _msg(3, 11, 2 * half, "丙")]
    )
    assert _shape(batches) == [(11, [1, 2, 3])]


def test_间隔超过半小时切一刀() -> None:
    batches = chunking.chunk(
        [
            _msg(1, 11, 0, "甲"),
            _msg(2, 11, chunking.BREAK_GAP_SECONDS + 1, "乙"),
        ]
    )
    assert _shape(batches) == [(11, [1]), (11, [2])]


# ── 超长单体 ────────────────────────────────────────────────────────


def test_超长单体独占一批_不切它() -> None:
    """单条超过预算 ⇒ 它自己成一批，**不切开它**。

    理由：单条消息是一个语义单元，从中间切开会把它劈成两条半截话。
    切分归一化的责任在数据源（见 `chunk()` 的 docstring）。
    """
    huge = "丙" * (EFFECTIVE + 1)
    batches = chunking.chunk(
        [_msg(1, 11, 0, "甲" * 30), _msg(2, 11, 60, huge), _msg(3, 11, 120, "乙" * 30)],
        budget=BUDGET,
    )
    assert _shape(batches) == [(11, [1]), (11, [2]), (11, [3])]
    # 正文一字未少 —— 它没有被切开
    assert batches[1].messages[0].content == huge


def test_oversized_的边界是严格大于生效预算() -> None:
    """`oversized()` 是「超长单体」的**报警口** —— 它不会在 `chunk()` 的结果
    里显形（超长单体只是「独占一批」），所以调用方只能靠它。

    边界：恰好等于生效预算 ⇒ **不算**超长（预算本来就装得下）；多一个字符
    ⇒ 算。这条边界错了，报警就会漏掉最危险的那一档。
    """
    at = _msg(1, 11, 0, "甲" * EFFECTIVE)
    over = _msg(2, 11, 1, "乙" * (EFFECTIVE + 1))
    found = chunking.oversized([at, over], budget=BUDGET)
    assert [m.msg_id for m in found] == [2]


# ── 估算 ────────────────────────────────────────────────────────────


def test_estimate_batches_与实际切出的批数一致() -> None:
    """`--plan-only` 与进度显示读的是这个数，它必须和真跑一样 ——
    否则「说好 3 批」会变成 5 次调用。"""
    gap = 2 * 3600
    msgs = [
        _msg(1, 11, 0, "甲" * 30),
        _msg(2, 11, 60, "乙" * 30),
        _msg(3, 11, 60 + gap, "丙" * 30),
        _msg(4, 22, 0, "丁" * 30),
    ]
    assert chunking.estimate_batches(msgs, budget=BUDGET) == len(
        chunking.chunk(msgs, budget=BUDGET)
    )
