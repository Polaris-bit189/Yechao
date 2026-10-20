"""追踪 —— **流水线上的一层标记**，不是第七个数据源。

## 要防什么

「我关心的那个人今天说了什么」和「有没有人提到奖学金」这两个问题，
散在群聊里是**看不见**的：日报按天聚合、信息流按类目筛，而这两件事都
不是类目。用户能做的只有自己翻群。

## 为什么它不是数据源

`infrastructure/sources/` 下的每一个源都回答同一个问题：**消息从哪儿来**。
追踪不回答那个问题 —— 它**不产出任何一条消息**，只是给**已有的**消息
加一层判断：

    · 这个账号说的话，**值不值得越过预筛**（人物）
    · 这条条目的标题/详情里，**有没有那个名词**（名词）

⇒ 把它做成源，会造出「同一件事被两条路各读一遍」的形状：追踪源产出的
消息与真源产出的消息**没有共同的主键**（`msg_id` 对不上），于是
`covered_msg_ids` 的幂等保证失效，重跑一轮就多一份条目。而那**不报错**。

## 两类规则，两种可判定性（这决定了实现分两段）

| kind | 判据 | 能在哪一步判 |
|---|---|---|
| `person` | 消息的 `actor_account` 等于那个账号 | **预筛之前** —— 消息头上就有 |
| `term` | 条目的标题/详情里出现那个串 | **条目成型之后** —— 只有条目有标题 |

这个差别是承重的，不是实现细节：

  · **人物规则必须在预筛之前生效**，否则「这个账号说的话不被丢掉」这句
    话就落不了地（预筛正是把消息丢掉的那一步）。
  · **名词规则只能在条目之后生效**：拿它去预筛等于拿「条目里有没有这个词」
    去判「消息要不要发给模型」—— 而那时候还没有条目。硬要做，只能退化成
    「消息正文里有没有这个词」，那是一条**看起来像**名词规则的词表判据，
    而它判的是消息、不是条目。

## 标记**不落库**，读的时候按当前规则算

理由与 `interfaces/view.py` 里 `kind_label` 不存库是**同一条**：规则是
用户可以随时改的配置，而存下来的标记会在改规则之后**过期** —— 症状是
「我在 `tracks.toml` 里加了个名词，`/api/tracked` 什么都没有」，
而两处代码单看都正常。

⇒ 一处算，处处一致。代价是每次读要拿当前规则比一遍（规则是个位数条，
比的是标题与详情两个短字段）。

※ 那**人物**那边留下了什么？——两样，都不是「标记」：

  · **一条条目**。它本来会被预筛丢掉，现在它在了，而且 `actor_account`
    就是那个账号（`/api/items?person=<账号>` 本来就能按它筛）。
  · **一条 `runs` 抬头**。补抽那一趟是**单独一次跑**，它有自己的
    `run_id` 与账 —— 所以「这一条为什么在库里，而它本该被丢掉」查得到。

## 两趟抽取，为什么

「人物规则在预筛之前生效」这句话有一个**机械的后果**：那一批消息不能
与别的消息一起过预筛 —— 预筛是按**群**分层级的（`tiers: {group_id: tier}`），
它没有「这条消息的发送者被追踪」这一维。

所以 `run()` 把待处理消息**分成两堆**：

  ① 常规那一趟：不含被追踪人物的消息 —— 与不追踪时**逐字节相同**
  ② 补抽那一趟：**只有**被追踪人物的消息，并且在这一趟里把群层级标成高优

※ 为什么 ① 要**排除**掉被追踪的消息，而不是「跑完再把它们捞回来」：
预筛会把它们记一笔 `filtered` 账（「这条被丢了」），而它们随后**真的
进了条目** —— 那本账就变成了一句与事实相反的记录，而且它**落了库**
（`run_entries`）。「一个失败如果看起来像成功」是危险的，反过来
「一条成功的记录看起来像失败」同样危险：它会让人去查一个不存在的问题。

※ 代价（写下来，因为它是真的）：两堆分开跑 ⇒ 被追踪的消息**看不到
群里别人的话**作为上下文。`with_context` 补的邻居只在**同一堆**里找。
这一条是刻意接受的：预筛的层级判据是按群给的，要在**一堆**里同时做到
「这个人的消息无条件留、别人的消息照常判」，只有给预筛加一条按账号的
判据 —— 而那是 domain 层的判据（`domain/prefilter.py`），追踪是它
**上面**的一层标记，不该去改它。

## 补抽那一趟为什么能越过预筛

`domain/prefilter.py` 的判据次序里，「高优群 ⇒ 无条件保留」是它
**表达「这条一定要看」**的那一档。补抽这一趟装的消息**已经**是被用户
明确点过名的 —— 对它们来说「这条要不要看」这个问题**已经被回答过了**。

所以这一趟把群层级标成 high，是**借用预筛已有的那档语义**说
「无条件保留」，而不是在声称「这个群是高优群」：群配置没有被改写，
只是这一趟的**视图**（`_PromotedConfig` 是只读的，不碰文件）。

※ **两条硬丢弃仍然生效**：`pure_ack`（纯应答）与 `pure_symbol`（纯符号）。
它们判的是「这条消息**没有信息量**」，与「谁发的」无关 —— 把一条「收到」
抽成条目只会让日报多一行噪声，而模型对它也只能回空。追踪要防的是
「这个人的话**因为没命中锚或太短**而被默认丢掉」，那一条由层级判据
挡不住、也正是这一趟要解决的。

## 怎么验证

  · `tests/application/test_track.py`：两趟的划分、越过的判据、名词命中
  · `tests/interfaces/test_tracked_api.py`：`/api/tracked` 按规则查
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from yechao.application import extract as extract_uc
from yechao.domain.config_models import KIND_PERSON, KIND_TERM, TrackRule
from yechao.domain.errors import FailureLedger

#: 补抽那一趟里，群层级被标成这个值。
#:
#: ※ 它是 `domain/prefilter.py` 的 `TIER_KEEP` 里那一个取值。**不在这里
#: 重新定义一套**：写死另一个词（比如 `"tracked"`）会得到一条恒不触发的
#: 判据 —— 预筛不认识它，于是消息照旧被丢，而代码看起来一切正常。
PROMOTED_TIER = "high"


# ── 命中判据 ────────────────────────────────────────────────────────
#
# ※ 三个函数都是**纯的**（输入进、结果出，不碰 I/O）—— 所以「哪条规则
# 命中了」可以逐条断言，而不是只能端到端地看结果。


def hit_person(msg: Any, rules: list[TrackRule]) -> list[TrackRule]:
    """这条**消息**命中了哪些人物规则。

    ※ 匿名消息（`actor_account is None`）**永不命中** —— 与
    `search(persons=...)` 的 SQL 行为同一条判据：「匿名」与「账号是 0」
    在本项目里是两件事，不允许用 0 冒充（见 `PendingMessage.actor_account`）。
    """
    account = getattr(msg, "actor_account", None)
    if account is None:
        return []
    try:
        value = int(account)
    except (TypeError, ValueError):
        return []
    return [
        r for r in rules
        if r.kind == KIND_PERSON and r.account == value
    ]


def hit_terms(text: str | None, rules: list[TrackRule]) -> list[TrackRule]:
    """这段**文本**命中了哪些名词规则。

    ※ 子串匹配，不做分词：`"奖学金" in "关于奖学金评定的通知"`。
    分词需要词典与切分器，而这两样都会把「为什么命中」变成一句要再解释
    一次的话。子串判据可以被穷举测试（「奖学金」命中、「奖学」也命中、
    「助学金」不命中），这就够了 —— 名词规则是**召回**取向的：
    多标一条的代价是用户多看一眼，漏标一条的代价是它沉进群里。
    """
    if not text:
        return []
    return [r for r in rules if r.kind == KIND_TERM and r.value in text]


def hits_for_item(item: Any, rules: list[TrackRule]) -> list[TrackRule]:
    """一条**条目**命中了哪些规则。**这是 `/api/tracked` 与条目标记的唯一判据。**

    · 人物：按 `actor_account`（与 `hit_person` 同一条判据 —— 条目的账号
      就是它来源消息的账号，`extract._build_item` 从 `src.actor_account` 抄）
    · 名词：在**标题 + 详情**里找

    ※ 为什么不把「来源消息的正文」也算进名词那一维：条目一旦成型，它
    **就是**这份产品给用户看的东西。拿源文去判会造出「这条条目上标着
    『奖学金』，而条目卡片上根本没有这两个字」—— 用户点进去找不到依据，
    而那是本项目的闸门（`domain/verify.py`）明确要防的形状。
    """
    out = hit_person(item, rules)
    text = f"{getattr(item, 'title', '') or ''}\n{getattr(item, 'detail', '') or ''}"
    out += hit_terms(text, rules)
    return out


# ── 结果 ────────────────────────────────────────────────────────────


@dataclass
class TrackOutcome:
    """一次带追踪的抽取的结果。

    ※ 字段名与 `extract.ExtractOutcome` **刻意一致**（`items` / `ledger` /
    `batches_ok` / `batches_total` / `coverage` / `run_id`）—— 报告那一层
    （`interfaces/cli._report_extract`）对两者一视同仁。另起一套字段名会
    逼着报告写两份，而两份迟早漂移。
    """

    items: list[Any] = field(default_factory=list)
    ledger: FailureLedger = field(default_factory=FailureLedger)
    #: 常规那一趟的 `run_id` —— 用户拿它查账。
    run_id: str = ""
    #: 补抽那一趟的 `run_id`（没有补抽时是空串）。
    #: ※ 单独给出来：两趟各留一条 `runs` 抬头，混成一个的话
    #: 「这一次跑到底跑了几趟」在库里就看不出来了。
    tracked_run_id: str = ""
    batches_total: int = 0
    batches_ok: int = 0
    sent_to_model: int = 0
    eligible: int = 0
    #: 因为人物规则被**单独抽出来**的消息条数。0 = 这一轮没有可追踪的人物
    #: （或者没配人物规则）。
    #:
    #: ※ 它数的是**真的会进补抽那一趟的**（已经产出过条目的不算 ——
    #: 见 `run()` 里那段关于 `covered_msg_ids` 的注释）。所以它每一轮
    #: 归零是**正常**的，而它恒不为零才是要查的。
    forced: int = 0

    @property
    def coverage(self) -> float:
        """真的送进模型的比例。分母是**两趟合起来**该送的。"""
        if self.eligible == 0:
            return 1.0
        return self.sent_to_model / self.eligible


# ── 两趟 ────────────────────────────────────────────────────────────


def run(
    deps: Any,
    rules: list[TrackRule] | None = None,
    *,
    since: int | None = None,
    until: int | None = None,
    budget: int = 200,
) -> TrackOutcome:
    """跑一轮**带追踪的抽取**。

    `deps` 是 `extract.ExtractDeps`（组合根装配好的那一份）。
    返回合并后的 `TrackOutcome`。

    ## 没有人物规则时**只有一趟**，而且那一趟与不追踪时完全相同

    这一条是刻意守的：`term` 规则只影响读（标记），不该让抽取多花一次
    调用；而「没配追踪」时更不该有任何行为差别。所以：

        rules 里没有 person ⇒ 直接把 deps 交给 `extract.run`，不做包装。
    """
    rules = list(rules or [])
    person_rules = [r for r in rules if r.kind == KIND_PERSON]

    if not person_rules:
        # ── 一趟：与不追踪时逐字节相同 ──
        base = extract_uc.run(
            deps, since=since, until=until, budget=budget
        )
        return _merge(base, None, forced=0)

    # ── 划分：哪些消息是被追踪人物的 ──
    #
    # ※ 这里问一次 `pending_messages()` 只为了**分堆**，不是取数据：
    # 两趟各自还会去问一次（`extract.run` 的第一步）。多这一次读是刻意的
    # —— 让分堆与抽取读的是**同一个** `since/until`，比把窗口塞进包装器
    # 再由包装器转发要少一层猜。
    #
    # ※ **已经产出过条目的消息要在这里就排掉**（`covered_msg_ids`），
    # 而不是留给两趟各自去滤。理由是一个实测出来的症状：不排的话，
    # 被追踪的人**只要还在窗口里**，每一轮都会多跑一趟空趟 ——
    # 报告里那句「N 条消息单独抽了一趟」于是永远不为零，而它**一条都没抽**。
    # 那是「一个数字看起来正常、其实什么都没说」的形状。
    # ※ 与 `extract.run` 用**同一个**「已处理」定义（含账里已判完的）——
    # 两处口径不同的话，被追踪的人**只要还在窗口里**，每一轮都会多跑一趟
    # 空趟，而报告里那句「N 条消息单独抽了一趟」永远不为零。
    covered = deps.items.covered_msg_ids() | deps.items.judged_msg_ids(
        kinds=extract_uc.JUDGED_KINDS
    )
    window = [
        m for m in deps.messages.pending_messages(since=since, until=until)
        if m.msg_id not in covered
    ]
    forced_ids = {
        m.msg_id for m in window if hit_person(m, person_rules)
    }

    if not forced_ids:
        # 没有一条命中 ⇒ 还是只有一趟。**不因为「配了人物规则」就多跑一趟。**
        base = extract_uc.run(deps, since=since, until=until, budget=budget)
        return _merge(base, None, forced=0)

    # ── ① 常规那一趟：看不见被追踪的消息 ──
    base = extract_uc.run(
        replace(deps, messages=_ExcludingMessages(deps.messages, forced_ids)),
        since=since, until=until, budget=budget,
    )

    # ── ② 补抽那一趟：只有被追踪的消息，群层级在这一趟里标成高优 ──
    groups = {m.group_id for m in window if m.msg_id in forced_ids}
    extra = extract_uc.run(
        replace(
            deps,
            messages=_OnlyMessages(deps.messages, forced_ids),
            config=_PromotedConfig(deps.config, groups),
        ),
        since=since, until=until, budget=budget,
    )

    return _merge(base, extra, forced=len(forced_ids))


def _merge(base: Any, extra: Any, *, forced: int) -> TrackOutcome:
    """把两趟的结果合成一份。

    ※ 账**逐笔重记**（不是把两个 `FailureLedger` 拼起来）：`FailureLedger`
    没有「合并」这个操作，而给它加一个就要改 `domain/errors.py`。
    重记走的是它的公开接口（`record`），而且 `record` 会再校验一次
    `reason` 的长度 —— 多一道由值对象自己守的关，代价是几百次内存操作。

    ※ `run_id` 取**常规那一趟**的：它是用户查账时最想要的那一个
    （绝大多数消息走的是那一趟）。补抽那一趟的 id 单独给出来。
    """
    out = TrackOutcome(run_id=base.run_id, forced=forced)
    out.items = list(base.items)
    out.batches_total = base.batches_total
    out.batches_ok = base.batches_ok
    out.sent_to_model = base.sent_to_model
    out.eligible = base.eligible

    if extra is not None:
        out.items += list(extra.items)
        out.batches_total += extra.batches_total
        out.batches_ok += extra.batches_ok
        out.sent_to_model += extra.sent_to_model
        out.eligible += extra.eligible
        out.tracked_run_id = extra.run_id

    for e in list(base.ledger.entries) + (
        list(extra.ledger.entries) if extra is not None else []
    ):
        out.ledger.record(
            e.kind, msg_id=e.msg_id, group_id=e.group_id,
            batch_no=e.batch_no, item_id=e.item_id, reason=e.reason,
        )
    return out


# ── 两个消息视图（都是**只读**的包装，不碰库、不碰文件）────────────
#
# ※ 为什么是两个类而不是一个带 `mode` 参数的类：两个方向的语义完全不同
# （「当成不存在」vs「只看见这些」），而一个 `mode` 参数会让调用点读成
# `_Messages(store, ids, exclude=True)` —— 布尔开关在调用点是最难读的东西。
# 两个类各自只有三行，换来的是调用点一眼能看懂。


class _ExcludingMessages:
    """把某些消息**当成不存在**。

    ※ 它只影响**这一趟**读到的消息集合 —— 库里的行一条都不动。
    `message_count()` 仍然报**真实的**总数：它是「库里现在有多少」，
    不是「这一趟看得到多少」，两者混起来会让报告里那个数变成一句假话。
    """

    def __init__(self, real: Any, drop_ids: set[int]) -> None:
        self._real = real
        self._drop = set(drop_ids)

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        return [
            m for m in self._real.pending_messages(since=since, until=until)
            if m.msg_id not in self._drop
        ]

    def message_by_id(self, msg_id: int) -> Any | None:
        return self._real.message_by_id(msg_id)

    def message_count(self) -> int:
        return self._real.message_count()

    def upsert_messages(self, messages: list[Any], *, run: Any = None) -> int:
        return self._real.upsert_messages(messages, run=run)


class _OnlyMessages:
    """**只**看得见这些消息。

    ※ 与 `_ExcludingMessages` 的差别不只是取反：补抽那一趟的窗口是
    「用户点过名的那些」，而它必须**与常规那一趟互不相交** —— 交集会让
    同一条消息被抽两遍，产出两条条目，而两条条目都不报错。
    互不相交由 `run()` 的划分保证（按 `msg_id` 二分）。
    """

    def __init__(self, real: Any, keep_ids: set[int]) -> None:
        self._real = real
        self._keep = set(keep_ids)

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        return [
            m for m in self._real.pending_messages(since=since, until=until)
            if m.msg_id in self._keep
        ]

    def message_by_id(self, msg_id: int) -> Any | None:
        return self._real.message_by_id(msg_id)

    def message_count(self) -> int:
        return self._real.message_count()

    def upsert_messages(self, messages: list[Any], *, run: Any = None) -> int:
        return self._real.upsert_messages(messages, run=run)


class _PromotedConfig:
    """把指定的群**在这一趟里**当成高优群。

    ※ 它**只改视图，不改文件** —— `set_*` 那几个方法没有转发，
    调它们会 `AttributeError`（而不是静默写盘）。这是刻意的：一个能被
    写入的「临时视图」迟早会被某个调用方真的写进去，而那会把用户的
    群配置改掉，且看起来像一次正常的配置更新。

    ※ 没有转发 `set_groups` / `set_persons` / `set_categories` / `set_llm`
    —— 抽取用例一个都不用（它只读）。真要用时再加，并在这里写清理由。
    """

    def __init__(self, real: Any, groups: set[int]) -> None:
        self._real = real
        self._promote = set(groups)

    def groups(self) -> list[Any]:
        """真实的群配置，加上「被追踪的那些群在这一趟里是高优」这一条视图。

        ## ※ 被追踪的群**不在配置里**时也要提升

        这条是实测出来的：第一版只改**已有**的 `GroupConfig`，而
        `prefilter.prescreen` 取层级用的是 `tiers.get(group_id, "normal")`
        —— 一个不在 `groups.toml` 里的群拿到的是 `normal`，于是追踪**不生效**，
        而代码看起来一切正常（`_PromotedConfig` 确实返回了提升过的视图）。

        而「消息来自一个没配过的群」是真会发生的：源读到什么就是什么，
        配置里那份清单管的是**监视哪些群**，不是**只可能收到哪些群**。

        ※ 合成的那一项 `name` 必须与 `extract._group_name` 的回落值
        （`f"群{group_id}"`）**逐字一致** —— 它会被写进条目的 `group_name`。
        对不上不会报错，只会让这一条条目的群名与别处不一样。
        """
        from yechao.domain.config_models import GroupConfig

        real = list(self._real.groups())
        seen = {g.id for g in real}
        out = [
            replace(g, tier=PROMOTED_TIER) if g.id in self._promote else g
            for g in real
        ]
        for gid in sorted(self._promote - seen):
            out.append(
                GroupConfig(id=gid, name=f"群{gid}", tier=PROMOTED_TIER)
            )
        return out

    def persons(self) -> list[Any]:
        return self._real.persons()

    def nicknames(self) -> list[str]:
        return self._real.nicknames()

    def categories(self) -> Any:
        return self._real.categories()
