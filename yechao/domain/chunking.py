"""切批 —— 把待处理消息分成若干批送进模型。

## 要防什么

**端点会静默截断。** 输入超过上下文时，它回 200、`finish_reason=stop`、
`usage` 只报截断后的数 —— 客户端拿不到任何信号。

⇒ **长度预算必须由切批负责。** 这是切批存在的唯一理由，也是它唯一的硬约束。

## 硬约束一：不跨群混批

**不同群的对话没关系。** 把 A 群与 B 群的消息混在一批里，模型会把 A 群的
上下文套到 B 群 —— 症状是「B 群的通知里出现了 A 群的地点」。

⇒ 批次边界**必须**落在群与群的交界处，无论长度预算还剩多少。

## 硬约束二：群内时间连续（**次级**约束）

同一群里，批次应当覆盖**连续的时间段**。理由：一句「好的，那就明天下午三点」
的含义在它的**前一条**里。批次从中间切开，那条消息就没了上下文。

⇒ 群内出现**自然断点**（与上一条消息的间隔 > `BREAK_GAP_SECONDS`）时就切一刀，
让批次边界落在对话结束的地方，而不是纯粹按长度。

※ 这条是**次级**的，体现在两处：

1. 它**只在群内**起作用。首要约束是不跨群，那条不因为时间是否连续而让步。
2. 它**不能突破长度预算**。预算撞上时照切不误 —— 因为撞预算的后果是
   **静默截断**（没有信号），而「切得不够漂亮」最多是多花一次调用。
   两个触发条件的先后与优先级见 `_split_group`。

> 落地位置是 `_split_group`，由 `tests/domain/test_chunking.py` 钉住实际的
> 分批结果。**一条声明配一个空操作，比没有这条声明更危险**：读代码的人会
> 以为它生效了。

## 「预算」用什么单位

字符数，不是 token。理由：token 数要调 tokenizer 才能算，而那是一个
**依赖模型的**操作 —— 领域层不碰外部世界（护栏钉这条）。

※ 代价是字符→token 的换算有一个**不确定的系数**（中文大约 1 字 ≈ 1 token，
英文与代码更低）。所以 `DEFAULT_BUDGET_CHARS` 取得**保守**：宁可多切几批
（多花几次调用），也不要撞上静默截断 —— 后者的失败**没有信号**。

## 为什么留 `headroom`

端点报的上下文上限是**输入 + 输出**的总和。切批只管输入，而输出也要占额度
（模型要吐 JSON）。不留余量就是在赌输出很短 —— 而赌输了的表现是
`finish_reason=length`，那个信号**有**（`ModelTruncated`），但那时已经白花了
一次调用。

⇒ 预留 1/4 给输出。
"""

from __future__ import annotations

from dataclasses import dataclass

from yechao.domain.models import PendingMessage

#: 单批的**输入**字符预算。
#: 取 6000：绝大多数本地模型的上下文是 8k–32k token，而一批消息除正文外
#: 还要带提示词（类目判据、输出格式说明）—— 那些固定开销实测在 1k 字符量级。
DEFAULT_BUDGET_CHARS = 6000

#: 留给输出的余量比例。见模块 docstring。
HEADROOM = 0.75


@dataclass(frozen=True)
class Batch:
    """一批。`messages` 保证**同群**且**按时间升序**。"""

    group_id: int
    messages: tuple[PendingMessage, ...]

    @property
    def chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def __len__(self) -> int:
        return len(self.messages)


#: 时间间隔超过这么久，视为**对话的自然断点** —— 群内优先在这里切。
#:
#: 取 30 分钟：校园群的消息密度是「一波一波」的（课间、放学后），
#: 半小时的空档基本说明上一个话题结束了。
#:
#: ※ 这个值是**启发式**，不是量出来的最优解。它要同时躲开两头：
#:   · 太小（比如 5 分钟）⇒ 相邻消息几乎条条成断点，批次碎成「一条一批」，
#:     切批省调用次数的意义就没了。
#:   · 太大（比如 4 小时）⇒ 一天里大多数间隔都不成断点，等于没实现。
#: 30 分钟与「一节课」同量级，是这两头中间的一个取值。
#:
#: 怎么验证它合适：跑一遍真实语料，看**相邻消息间隔的分布** ——
#: 若切出来的批次大多是 1 条消息，说明阈值偏小；若一天只切出一批，说明偏大。
#: 这是个可测的数，不是拍脑袋 —— 所以它是常量而不是散在判据里的魔数。
BREAK_GAP_SECONDS = 30 * 60


def _split_group(
    messages: list[PendingMessage], budget: int
) -> list[list[PendingMessage]]:
    """把一个群的消息切成若干段。**「时间连续」在这里落地。**

    ## 两个切点触发条件（先后即优先级）

    1. **预算装不下下一条**（`used + size > budget`）—— 硬约束。撞上就切，
       不论时间连不连续。理由见模块 docstring：撞预算的后果是**静默截断**，
       那没有信号；而「切得不够漂亮」最多是多花一次调用。
    2. **出现自然断点**（与上一条消息的间隔 > `BREAK_GAP_SECONDS`）——
       次级约束。这时即使预算还有富余也切一刀，让批次边界落在**对话结束**
       的地方。

    ## 为什么不再需要「回头挪切点」

    这里原先留过一个 `_rebalance()` 占位：想法是「按预算切完，再回头在切点
    附近找时间间隔最大的位置把切点挪过去」。**那个设计不需要了** ——
    条件 2 是在**走的过程中**就切，切点天然落在间隔最大的那些位置上，
    没有「切歪了再挪」这一步。而且挪切点要把边界消息推到下一批、重算下一批
    的预算，那会把线性扫描变成全局优化问题；条件 2 用同样的信息、线性的
    代价就达到了目的。占位函数已随之删除 —— 留着它会继续声明一件不做的
    事（见模块 docstring 的实现说明）。

    ## 边界

    · 空输入 → 空列表（不产出空批）。
    · 超长单体（单条 > 预算）→ 自己独占一批，**不切它**（见 `chunk()`）。
      实现上靠 `bool(cur)` 这一项：`cur` 为空时不触发条件 1，于是它总能被
      收进新批，不会出现「切出空批」或死循环。
    """
    if not messages:
        return []

    ordered = sorted(messages, key=lambda m: (m.ts, m.msg_id))
    out: list[list[PendingMessage]] = []
    cur: list[PendingMessage] = []
    used = 0
    prev_ts: int | None = None

    for m in ordered:
        size = len(m.content)
        over_budget = bool(cur) and used + size > budget
        # ※ 间隔用 **`>`（严格大于）**：恰好等于阈值的间隔**不**算断点。
        # 「够久了」这个判断的边界取闭还是开会改变分批结果，所以钉死在
        # 测试里（test_chunking.py 里钉了 1800 不切、1801 切）。
        natural_break = (
            bool(cur) and prev_ts is not None and m.ts - prev_ts > BREAK_GAP_SECONDS
        )
        if over_budget or natural_break:
            out.append(cur)
            cur, used = [], 0
        cur.append(m)
        used += size
        prev_ts = m.ts

    if cur:
        out.append(cur)
    return out


def chunk(
    messages: list[PendingMessage], *, budget: int = DEFAULT_BUDGET_CHARS
) -> list[Batch]:
    """切批。**不跨群**；群内再按「预算 / 自然断点」两个触发条件切（见
    `_split_group`）。

    先按 `group_id` 分组（首要约束），再对每个群单独切。群与群之间**绝不**
    合并 —— 不论各自的批次有多小。

    ※ 超长的**单条**消息（比预算还长）怎么办？
    —— 它自己成一批，**不切它**。理由：单条消息是一个语义单元，从中间切开会
    把它劈成两条半截话，两条都读不懂。而这种消息来自「会议转写」这类源时
    **应当在数据源内部就被切好**——切分归一化的责任在数据源。

    ※ 所以本函数遇到超长单体时的行为是「让它独占一批」，并且调用方应当
    **能看见**这件事 —— 见 `oversized()`。
    """
    effective = int(budget * HEADROOM)
    by_group: dict[int, list[PendingMessage]] = {}
    for m in messages:
        by_group.setdefault(m.group_id, []).append(m)

    batches: list[Batch] = []
    for gid in sorted(by_group):
        for part in _split_group(by_group[gid], effective):
            batches.append(Batch(group_id=gid, messages=tuple(part)))
    return batches


def oversized(
    messages: list[PendingMessage], *, budget: int = DEFAULT_BUDGET_CHARS
) -> list[PendingMessage]:
    """找出**单条就超过预算**的消息。

    ※ 这个函数存在的意义：超长单体是**静默截断的高危来源**，而它不会在
    `chunk()` 的结果里显形（它只是「独占一批」）。调用方必须能查到它、
    并据此**报警**而不是继续跑 ——

    「一个失败如果看起来像成功，它比失败本身更危险」。
    """
    effective = int(budget * HEADROOM)
    return [m for m in messages if len(m.content) > effective]
