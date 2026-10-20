"""值对象 —— 领域层里流转的形状。

## 三条纪律

**一、时间一律是 Unix 秒（int），一律由调用方传进来。**
领域层不取当前时刻（护栏 `test_领域层不取当前时间` 钉这条）。「现在」是一
个外部输入，和「这段文字」没什么两样。

**二、字段名与前端契约逐字对应。**
`Item` 的字段名就是 `web/src/types.ts` 里 `Item` 的字段名，一个字母都不许
差。前端那一侧是冻结契约，改任一边都要同时改另一边。

**三、生成字段（`kind_label` / `kind_icon` / `group_name`）**在**后端拼好**。
让前端自己查两张表的话，同一个字段在两处拼，漂移之后的症状是「卡片上有类目
名、详情页没有」—— 两边代码单看都正常。

## 为什么 `frozen=True`

值对象是不可变的：一个 `WindowItem` 被造出来之后，它代表的就是那一刻的那
一份事实。可变会让「谁在哪一步改了它」变成要顺着调用链找的问题，而这类
问题在筛选/排序/去重的组合里极难查。

⚠️ **但不可变不等于「全都 freeze」**：`deadline_ts` 会被闸门**清成 None**
（源文里找不到依据）。那不是"修改这个对象"，那是**产出一个新的对象** ——
所以用 `dataclasses.replace`，而不是 `setattr`。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PendingMessage:
    """一条**待处理的原始消息** —— 从数据源读到、还没过预筛。

    `content` 是**原文**，未经规范化、未经脱敏。它只在本机内存里流转，
    离开本机之前必须先过 `scrub`。

    ⚠️ `sender` 与 `actor_uin` 的关系：群昵称（`sender`）在**本机内部**是
    重要信号 —— 群昵称常带「22-1班班主任助理张三」这类角色与班级，是判断
    「这条通知是不是官方发的」最可靠的依据。所以它在入口**不**被抹掉，
    只在**出网那一刻**才抹（边界画在出网，不是入口）。
    """

    msg_id: int
    group_id: int
    ts: int  # Unix 秒：消息发出的时间
    sender: str  # 群昵称（完整，常含角色与班级前缀）
    content: str  # 原文
    #: QQ 号。**匿名消息为 None** —— 且**不该被人物筛选命中**。
    #: 「匿名」与「没有 uin」在本项目里是同一件事，不允许用 0 冒充。
    actor_uin: int | None = None
    #: 这条消息来自哪个源（`qq` / `meeting` / `mail`）。给审计用。
    source: str = "qq"


@dataclass(frozen=True)
class ExtractionCandidate:
    """**模型输出的原始形状** —— 还没回连来源、还没过核验闸门。

    它是「模型说了什么」，不是「我们采信了什么」。这两个之间隔着两道闸门
    （逐字回连 + 机械核验），所以刻意分成两个类型。

    ⚠️ `quote` 必须能在源文里**逐字找到** —— 程序靠它回连到原条目。
    对不上的整条丢弃。
    """

    kind: str
    title: str
    quote: str  # 逐字摘录，用来回连源消息
    detail: str | None = None
    #: ⚠️ 「截止日」与「活动开始时刻」是两件事，模型容易混。
    #: 这两个字段**分别**过核验，见 `verify.py`。
    deadline: str | None = None
    start: str | None = None
    place: str | None = None
    amount: str | None = None


@dataclass(frozen=True)
class Item:
    """一条**已采信的**结构化条目 —— 它与 `web/src/types.ts` 的 `Item` 同形。

    生成字段（`kind_label` / `kind_icon`）也在后端拼好。
    """

    item_id: int
    kind: str  # slug，如 "academic"
    kind_label: str  # 中文，如 "学业"
    kind_icon: str  # emoji
    title: str
    detail: str | None
    event_ts: int  # 消息发出的时间（**永远有**）
    #: ⚠️ **已过核验闸门**：None 表示源文里找不到依据。
    #: 前端据此**不显示**它 —— 所以「清掉」这个动作是在数据层做的，
    #: 不是在渲染层（只在渲染层设卡，绕过去太容易）。
    deadline_ts: int | None
    group_id: int
    group_name: str
    actor: str | None  # 展示用的人名（可能已剥过前缀）
    actor_uin: int | None
    place: str | None
    amount: str | None
    links: tuple[str, ...] = ()
    source_count: int = 0
    #: 活动**本身开始**的时刻。
    #: ⚠️ 与 `event_ts` 是两件事：`event_ts` = 消息发出的时间（永远有）；
    #: `start_ts` = 活动开始的时间。**为 None 是常态**（多数条目没有活动
    #: 时刻），不是异常。
    start_ts: int | None = None

    # ── 下面是**内部**痕迹，不进 API 输出（前端不认这些字段） ──

    #: 截止日在源文里有依据。**不变量：`deadline_ts is None` ⇒ 本项恒 False。**
    deadline_supported: bool = False
    #: 开始时刻在源文里有依据。
    start_supported: bool = False
    #: 这条是软删过的（界面上消失，库里的行仍在）。
    deleted: bool = False


@dataclass(frozen=True)
class SourceRef:
    """条目 ↔ 源消息的**一对多**引用。

    ⚠️ 为什么不是一条：同一件事会在多个群里被转发（「三个不同群转发同一条
    通知」），而那恰恰是**最该保留、最该合并**的信号 —— 所以 `source_count`
    是一个有意义的量，不是冗余。
    """

    item_id: int
    msg_id: int
    #: 模型给的逐字摘录。可空 —— 人工录入的条目没有摘录。
    quote: str = ""


@dataclass(frozen=True)
class WindowItem:
    """落进某个**日报窗口**的条目（带它的源消息）。

    单独一个类型是因为日报渲染要的是「条目 + 它的出处」，而 `Item` 本身
    不带出处（出处是多对多的，见 `SourceRef`）。
    """

    item: Item
    sources: tuple[PendingMessage, ...] = field(default_factory=tuple)


def replace_item(item: Item, **changes: object) -> Item:
    """产出一个**修改过的新条目**。

    ⚠️ 刻意包一层而不是让调用方直接用 `dataclasses.replace`：闸门清字段时
    必须**同时**把对应的 `*_supported` 置 False，这两件事分开做迟早会漏
    （「函数返回值对、调用方解包顺序错」的同一形状：测试断言了清字段，
    没断言旗标）。
    见 `yechao/domain/verify.py`。
    """
    return dataclasses.replace(item, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Digest:
    """一篇日报。"""

    digest_id: int
    day: str  # YYYY-MM-DD
    window_from: int
    window_to: int
    created_at: int
    body_md: str
    item_count: int
    #: 纯文本摘要（≤120 字），从正文**第一段**提取，已剥 markdown 记号。
    #: ⚠️ 可选：旧快照里没有这个字段，前端按 `preview ?? ''` 处理。
    preview: str | None = None
