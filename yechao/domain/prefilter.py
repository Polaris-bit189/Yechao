"""本地预筛 —— 哪些消息不值得发给模型。

## 要防什么

把消息全发给模型既慢又贵，而其中大部分是「收到」「好的」这类纯应答。

## 取向：先丢弃、后保留

**硬丢弃一旦命中就出局，保留规则没有翻案机会。**

顺序很重要：如果先判「值得留」，那些明显是
噪声（「收到」）的会先被某些规则捞回来 —— 比如「收到，明天 9 点在体育馆
集合」这条**必须**保留（它含时间地点），而「收到」必须丢。先判保留的话，
一条宽松的「含时间词」规则会把后面的「收到」也捞回来。

所以：
  1. 硬丢弃：无论出现在哪个群都不值得
  2. 保留：高优群 / 有角色的发送者 / 命中动作词
  3. 两者都不命中 ⇒ **丢弃**（默认不发给模型）

## 判据一：硬丢弃有哪些

**纯应答**。「收到」「好的」「谢谢」这类 —— 它们**无论出现在哪个群都不值得**，
所以排在最前。

⚠️ 但「纯应答」的判据不能是「包含这些词」：`收到，明天9点集合` 含「收到」，
而它必须保留。判据是**整条消息剥掉空白与标点后，就是那个应答词本身**
（或只由这些词堆成）。这是「整体匹配」而不是「包含匹配」。

**过短**。这里要问：按字符还是按字节？想拦的到底是什么？

我的判断：**想要拦住的是「信息量少」，不是「字数少」。** 但信息量不可计算，
所以用一个**代理判据**，并且明确它不是同一个东西：

  · 代理判据用**字符**数（`len()`），不用字节数。
    理由：中文一个字 3 字节而英文一个字母 1 字节 —— 按字节算，`abc` 是 3 而
    `好` 也是 3，可 `好` 的信息量远高于 `abc`。按字节算等于**悄悄对中文更宽松**。
  · 阈值取 4：`好的` / `收到` / `顶` 都被挡住；`明早集合`（4 字）留下。
  · ⚠️ 代理判据会**误伤**极短的但重要的消息（比如「停课」两个字）。所以
    长度判据**不是硬丢弃**，它排在**动作词保留规则之后** —— 见 `filter_one`。

**纯表情/纯符号**。剥掉空白、标点、emoji、数字之后**什么都不剩** ⇒ 丢弃。
`😂😂😂`、`+++`、`～～～` 都走这条。

**刷屏**。同一个人在同**一个群**、连续发**完全相同**的内容。

## 判据二：刷屏的桶要带哪些维度

要害在于：三个不同群转发同一条通知**不是**刷屏
—— 那恰恰是最该保留的。

⇒ 判「重复」的桶必须含 **(发送者, 群, 内容)** 三个维度。**漏掉「群」这个
维度**，跨群转发会被误判成刷屏 —— 而那正是最该留下的东西。
**漏掉「发送者」**，两个不同的人在同一个群发同一句「收到」会被合并 ——
不过那条本来就被纯应答规则挡掉了。

⚠️ 「连续」也有判据：本项目按**窗口内**判，不按「紧邻」。理由：语料里
「同一个人发三条一样的」中间常常夹着别人的插话，按紧邻判会漏。
判据是「同一个人在同一群内，同内容出现 ≥ 3 次」—— 3 这个数从「转发通常在
3 个群」推出来，取 ≥4 更安全。**我用 3**，并接受它可能把「发了三遍」当成
刷屏（那本来就是刷屏）。

## 判据三：保留规则用什么样的词

这里有个陷阱：`时间` `地点` `通知` 这些词几乎每条都有，
放进关键词表等于没筛。

**什么样的词能区分值得看和不值得看？**

我推出来的判据：**值得看的消息都在说「要你做什么」，而动作词是它最小的
共同载体。**

对比一下：
  · `通知` —— 是**类目名**，出现在标题里，但一条消息有没有它跟它值不值得看
    无关（「通知：今天下雨」不值得看）。
  · `报名` —— 是**动作**。看到它，这条消息在要读者做一件事（去报名）。

所以动作词表收的是**动词与指令**：`报名` `提交` `截止` `领取` `缴费`
`开会` `集合` `面试` `招新` `转让` `求购` ……

⚠️ 这张表是**枚举型判据**，它的风险是「真实语料里还有没枚举到的动作」。
缓解：表**尽量宽**，且它只用于**保留**（宽进没有漏检风险，只有多花预算的
风险）。真正的漏检风险在硬丢弃那一侧，而那一侧判据是**整体匹配**，很窄。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from yechao.domain.models import PendingMessage

# ── 硬丢弃：纯应答 ──────────────────────────────────────────────────

#: 纯应答词。⚠️ 判据是**整体匹配**，不是「包含」。
#: 「收到，明天9点集合」含「收到」但**不是**纯应答 —— 它必须保留。
_ACK_WORDS = (
    "收到", "好的", "好", "行", "可以", "了解", "明白", "明白啦", "嗯",
    "嗯嗯", "谢谢", "多谢", "感谢", "辛苦了", "赞", "顶", "支持", "同意",
    "ok", "OK", "okay", "yes", "no", "对", "是的", "确实", "没错",
    "哈哈哈", "哈哈", "呵呵", "嘿嘿", "嘻嘻", "笑死", "太强了", "牛逼",
    "6", "66", "666", "777", "888", "打卡", "+1", "1", "111",
)

#: 剥掉这些之后看剩下什么。空白、标点、emoji、符号。
_STRIP_CATEGORIES = {"Po", "Ps", "Pe", "Pi", "Pf", "Pd", "Pc", "Sm", "Sc", "Sk", "So", "Zs", "Cc", "Cf"}


def _strip_noise(text: str) -> str:
    """剥掉空白、标点、符号、emoji，只留「实义字符」。"""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch) in _STRIP_CATEGORIES:
            continue
        out.append(ch)
    return "".join(out)


def is_pure_ack(text: str) -> bool:
    """**整体**是纯应答吗。

    ⚠️ 关键在于「整体」：剥掉噪声后剩下的串，必须**只由应答词堆成**，
    而且**不能含数字**（含数字意味着有信息，如「收到 3 份」）。
    """
    stripped = _strip_noise(text)
    if not stripped:
        return False
    if any(ch.isdigit() for ch in stripped):
        return False
    if stripped in _ACK_WORDS:
        return True
    # 多词堆叠：「好的谢谢」「嗯嗯好的」。逐词剥离，剥完必须为空。
    rest = stripped
    for _ in range(8):
        if not rest:
            return True
        for w in sorted(_ACK_WORDS, key=len, reverse=True):
            if rest.startswith(w):
                rest = rest[len(w):]
                break
        else:
            return False
    return not rest


def is_pure_symbol(text: str) -> bool:
    """剥掉噪声之后什么都不剩（或者只剩重复的同一个字符）。

    `😂😂😂` / `+++` / `～～～` 走这条。
    ⚠️ 「只剩同一个字符重复」这条是为 `哈哈哈哈哈` 这类准备的 —— 它不是纯
    符号，但它同样没有信息量。
    """
    stripped = _strip_noise(text)
    if not stripped:
        return True
    # 纯重复的同一个字符（≥3 次）：`哈哈哈哈`、`啊啊啊啊`
    return len(set(stripped)) == 1 and len(stripped) >= 3


# ── 硬丢弃：过短 ────────────────────────────────────────────────────

#: 长度阈值（**字符**数，不是字节 —— 见模块 docstring）。
MIN_CONTENT_CHARS = 4


def is_too_short(text: str) -> bool:
    return len(_strip_noise(text)) < MIN_CONTENT_CHARS


# ── 保留：动作词 ────────────────────────────────────────────────────

#: 动作词 —— 看到它，说明这条消息在**要读者做一件事**。
#:
#: ⚠️ 这里**刻意不收** `时间` / `地点` / `通知` / `群` 这类词：
#: 它们几乎每条消息都有（或是类目名），放进表里等于没筛。
#: 收的是**动词与指令** —— 见模块 docstring 的对照。
ACTION_WORDS: tuple[str, ...] = (
    # 报名 / 提交 / 截止
    "报名", "报个名", "登记", "提交", "上交", "递交", "截止", "截止到",
    "截至", "材料", "申请", "填表", "填写", "签字", "确认", "核对",
    # 领取 / 缴费
    "领取", "认领", "领取", "缴费", "交费", "付款", "补交", "退款", "领书",
    # 集合 / 开会
    "集合", "开会", "会议", "班次会", "讲座", "宣讲", "培训", "入场",
    "签到", "上课", "补课", "调课", "停课", "考试", "测验", "体测", "军训",
    # 活动
    "招新", "摆摊", "活动", "比赛", "竞赛", "演出", "晚会", "展览", "开放日",
    # 二手 / 失物
    "转让", "求购", "出个", "闲置", "捡到", "丢失", "遗失", "拾到",
    # 招聘
    "招聘", "招募", "兼职", "实习", "面试", "岗位", "勤工助学",
    # 其它指令
    "注意", "务必", "请", "尽快", "尽快", "抓紧", "提醒", "重要", "紧急",
    "需", "须", "需要", "安排", "变更", "取消", "延期", "提前",
)

_ACTION_RE = re.compile("|".join(re.escape(w) for w in sorted(set(ACTION_WORDS), key=len, reverse=True)))


def has_action_word(text: str) -> str | None:
    """命中哪个动作词。返回它（给「为什么保留」留证据），没有返回 None。"""
    m = _ACTION_RE.search(text)
    return m.group(0) if m else None


# ── 保留：高优群 / 有角色的发送者 ───────────────────────────────────

#: 群层级 → 是否无条件保留。
#: ⚠️ 这三个键名与 `config/groups.toml` 的 `tier` **必须一致**（对外契约）。
TIER_KEEP: frozenset[str] = frozenset({"high"})

#: 发送者昵称里的**角色词**。有角色的人发的消息**默认可信**——
#: 判断「这条通知是不是官方发的」最可靠的依据。
#:
#: ⚠️ 这是**保留**规则，不是丢弃规则：命中它只是「保住」，不影响别的判断。
ROLE_WORDS: tuple[str, ...] = (
    "班主任", "辅导员", "班长", "团支书", "学委", "学习委员", "生活委员",
    "体育委员", "辅导员助理", "助理", "教务", "教务员", "老师", "教授",
    "院长", "处长", "主任", "社长", "会长", "部长", "主席", "管理员",
    "官方", "官方号", "通知号", "管理员", "班助", "导员",
)

_ROLE_RE = re.compile("|".join(re.escape(w) for w in sorted(set(ROLE_WORDS), key=len, reverse=True)))


def has_role(sender: str) -> str | None:
    m = _ROLE_RE.search(sender)
    return m.group(0) if m else None


# ── 结果 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    #: 判据的名字（`pure_ack` / `too_short` / `pure_symbol` / `action_word` …）。
    rule: str
    #: 命中的那个词/片段 —— **留证据**，让「为什么丢掉它」回答得了。
    evidence: str = ""
    #: 丢弃 vs 保留的**方向**：`"drop"` / `"keep"` / `"default"`。
    #: ⚠️ `default` 单列，因为「两者都不命中 ⇒ 丢弃」是一个**默认值**，
    #: 不是一条判据。混淆这两者会让覆盖率报告算错分母。
    by: str = "default"


def filter_one(msg: PendingMessage, *, tier: str = "normal") -> FilterDecision:
    """单条消息的判定。**顺序就是判据**（先丢后留）。

    ⚠️ 顺序：
      1. 硬丢弃（纯应答 / 纯符号）—— **整体匹配**，很窄，几乎不误伤
      2. 保留（高优群 / 有角色 / 动作词）
      3. 长度判据 —— ⚠️ **排在保留之后**！
         理由：长度是**代理判据**，它会误伤「停课」这类短而重要的消息。
         放在保留之后，凡是命中动作词/角色/高优群的短消息都已经**被保住了**，
         长度判据只对「什么都没有」的消息生效。
      4. 都不命中 ⇒ 丢弃（默认）

    `tier` 只接受 `high` / `normal` / `low`。`low`（如二手群）**不额外丢**，
    只**不额外保留** —— 「二手群里也可能有人发重要通知」这个判断由动作词兜住。
    """
    text = msg.content

    if is_pure_ack(text):
        return FilterDecision(False, "pure_ack", _strip_noise(text)[:12], by="drop")

    if is_pure_symbol(text):
        return FilterDecision(False, "pure_symbol", text.strip()[:12], by="drop")

    # ── 以下是保留规则 ──
    if tier in TIER_KEEP:
        return FilterDecision(True, "high_tier", tier, by="keep")

    role = has_role(msg.sender)
    if role:
        return FilterDecision(True, "role_sender", role, by="keep")

    action = has_action_word(text)
    if action:
        return FilterDecision(True, "action_word", action, by="keep")

    # ── 长度判据（故意放在保留之后，见 docstring）──
    if is_too_short(text):
        return FilterDecision(False, "too_short", text.strip()[:12], by="drop")

    return FilterDecision(False, "no_signal", "", by="default")


# ── 刷屏 ────────────────────────────────────────────────────────────

#: 同一 (发送者, 群, 内容) 在窗口内出现这么多次 ⇒ 判刷屏。
#: 3 从「转发通常在 3 个群」推出来，取 ≥3 —— 跨群转发由「群」这一维分开，
#: 所以这个数只对**同一群内**生效。
SPAM_THRESHOLD = 3


def find_spam(
    messages: list[PendingMessage],
) -> dict[int, str]:
    """找出刷屏消息。

    返回 `{msg_id: 保留哪一条的 msg_id}` —— 被标记的那些会被丢弃，映射指向
    留下代表它的那一条。

    ⚠️ **桶是 (发送者, 群, 内容) 三元组** —— 三个维度缺一不可：
      · 缺「群」⇒ **跨群转发被误判成刷屏**，而那恰恰是最该保留的
      · 缺「发送者」⇒ 同一群里两个人发同一句「收到」被合并
      · 缺「内容」⇒ 同一个人在同一群发两条不同消息被当成重复

    ⚠️ 判据是「≥ 3 次」，**不是**「紧邻的相同」：语料里同一个人发三条一样的
    中间常夹着别人的插话，按紧邻判会漏。
    """
    buckets: dict[tuple[str, int, str], list[PendingMessage]] = {}
    for m in messages:
        key = (m.sender.strip(), m.group_id, m.content.strip())
        buckets.setdefault(key, []).append(m)

    dup: dict[int, str] = {}
    for (_sender, _group, _content), group in buckets.items():
        if len(group) < SPAM_THRESHOLD:
            continue
        ordered = sorted(group, key=lambda m: (m.ts, m.msg_id))
        keeper = ordered[0]
        for m in ordered[1:]:
            dup[m.msg_id] = keeper.msg_id
    return dup


def prescreen(
    messages: list[PendingMessage],
    *,
    tiers: dict[int, str] | None = None,
) -> tuple[list[PendingMessage], list[tuple[PendingMessage, FilterDecision]]]:
    """跑一遍预筛。

    返回 `(保留的, [(被丢掉的, 为什么)])`。

    ⚠️ **被丢掉的那一份必须带原因返回**，不能只返回保留的：
    「静默跳过一条消息」= 用户以为那天没事。
    调用方拿它记进失败账本（`errors.FailureLedger`），覆盖率的分母就从那儿来。
    """
    tiers = tiers or {}
    spam = find_spam(messages)

    kept: list[PendingMessage] = []
    dropped: list[tuple[PendingMessage, FilterDecision]] = []

    for m in messages:
        if m.msg_id in spam:
            dropped.append(
                (m, FilterDecision(False, "spam", f"重复自 {spam[m.msg_id]}", by="drop"))
            )
            continue
        d = filter_one(m, tier=tiers.get(m.group_id, "normal"))
        if d.keep:
            kept.append(m)
        else:
            dropped.append((m, d))

    return kept, dropped


def with_context(
    kept: list[PendingMessage],
    all_messages: list[PendingMessage],
    *,
    before: int = 2,
    after: int = 1,
) -> list[PendingMessage]:
    """给保留的消息补上**上下文邻居**。

    ⚠️ 为什么需要：一条「好的，那就明天下午三点」单独看没有信息，它的含义
    在**前一条**里。预筛会把它丢掉（没有动作词），于是模型看到的上下文是
    断的。

    ⚠️ 补进来的邻居**只作上下文**，不产出条目 —— 调用方要能把它们区分开。
    本函数返回的列表是「送进模型的那一批」，与「这一批里哪些该产出条目」
    是两件事，分开由调用方处理。
    """
    if before <= 0 and after <= 0:
        return list(kept)

    by_group: dict[int, list[PendingMessage]] = {}
    for m in sorted(all_messages, key=lambda m: (m.group_id, m.ts, m.msg_id)):
        by_group.setdefault(m.group_id, []).append(m)

    index: dict[int, int] = {}
    for gid, msgs in by_group.items():
        for i, m in enumerate(msgs):
            index[m.msg_id] = i

    chosen: dict[int, PendingMessage] = {m.msg_id: m for m in kept}
    for m in kept:
        gid = m.group_id
        msgs = by_group.get(gid, [])
        i = index.get(m.msg_id)
        if i is None:
            continue
        for j in range(max(0, i - before), min(len(msgs), i + after + 1)):
            nb = msgs[j]
            chosen.setdefault(nb.msg_id, nb)

    return sorted(chosen.values(), key=lambda m: (m.group_id, m.ts, m.msg_id))
