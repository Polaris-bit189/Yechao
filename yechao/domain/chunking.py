"""切批 —— 把待处理消息分成若干批送进模型。

## 要防什么

**端点会静默截断。** 输入超过上下文时，它回 200、`finish_reason=stop`、
`usage` 只报截断后的数 —— 客户端拿不到任何信号。

⇒ **长度预算必须由切批负责。** 这是切批存在的唯一理由，也是它唯一的硬约束。

## 硬约束一：不跨群混批

**不同群的对话没关系。** 把 A 群与 B 群的消息混在一批里，模型会把 A 群的
上下文套到 B 群 —— 症状是「B 群的通知里出现了 A 群的地点」。

⇒ 批次边界**必须**落在群与群的交界处，无论长度预算还剩多少。

## 硬约束二：时间要连续

同一群里，批次应当覆盖**连续的时间段**。理由：一句「好的，那就明天下午三点」
的含义在它的**前一条**里。批次从中间切开，那条消息就没了上下文。

⇒ 切点尽量落在**时间间隔较大**的地方（对话的自然断点），而不是纯粹按长度。

## 「预算」用什么单位

字符数，不是 token。理由：token 数要调 tokenizer 才能算，而那是一个
**依赖模型的**操作 —— 领域层不碰外部世界（护栏钉这条）。

⚠️ 代价是字符→token 的换算有一个**不确定的系数**（中文大约 1 字 ≈ 1 token，
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


#: 时间间隔超过这么久，视为**对话的自然断点** —— 优先在这里切。
#: 取 30 分钟：校园群的消息密度是「一波一波」的（课间、放学后），
#: 半小时的空档基本说明上一个话题结束了。
BREAK_GAP_SECONDS = 30 * 60


def _split_group(
    messages: list[PendingMessage], budget: int
) -> list[list[PendingMessage]]:
    """把一个群的消息切成若干段。

    策略：先按预算切，再在**切点附近**（往后看一小段）找一个时间间隔最大的
    位置把切点挪过去。这样既不超预算，又尽量落在自然断点上。
    """
    if not messages:
        return []

    ordered = sorted(messages, key=lambda m: (m.ts, m.msg_id))
    out: list[list[PendingMessage]] = []
    cur: list[PendingMessage] = []
    used = 0

    for m in ordered:
        size = len(m.content)
        if cur and used + size > budget:
            # 回头在窗口里找自然断点
            cur = _rebalance(cur, ordered_rest=None)
            out.append(cur)
            cur, used = [], 0
        cur.append(m)
        used += size

    if cur:
        out.append(cur)
    return out


def _rebalance(
    chunk: list[PendingMessage], *, ordered_rest: list[PendingMessage] | None
) -> list[PendingMessage]:
    """把切点挪到 chunk 内时间间隔最大的地方。

    ⚠️ 目前只做「不挪」的版本 —— 因为「往后看一小段再挪」需要把边界消息
    推到**下一批**，而下一批的预算就得重算，那会让整个切分变成一个全局优化
    问题。**先跑通，再优化**。

    留这个函数是为了标出**将来该改哪里**，不是为了现在做。
    """
    return chunk


def chunk(
    messages: list[PendingMessage], *, budget: int = DEFAULT_BUDGET_CHARS
) -> list[Batch]:
    """切批。**不跨群**。

    ⚠️ 超长的**单条**消息（比预算还长）怎么办？
    —— 它自己成一批，**不切它**。理由：单条消息是一个语义单元，从中间切开会
    把它劈成两条半截话，两条都读不懂。而这种消息来自「会议转写」这类源时
    **应当在数据源内部就被切好**——切分归一化的责任在数据源。

    ⚠️ 所以本函数遇到超长单体时的行为是「让它独占一批」，并且调用方应当
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

    ⚠️ 这个函数存在的意义：超长单体是**静默截断的高危来源**，而它不会在
    `chunk()` 的结果里显形（它只是「独占一批」）。调用方必须能查到它、
    并据此**报警**而不是继续跑 ——

    「一个失败如果看起来像成功，它比失败本身更危险」。
    """
    effective = int(budget * HEADROOM)
    return [m for m in messages if len(m.content) > effective]


def estimate_batches(
    messages: list[PendingMessage], *, budget: int = DEFAULT_BUDGET_CHARS
) -> int:
    """**不发请求**地估算会切出几批。给 `--plan-only` 与进度显示用。"""
    return len(chunk(messages, budget=budget))
