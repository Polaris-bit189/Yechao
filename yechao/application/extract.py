"""抽取用例 —— 本项目最复杂的一条流水线。

```
取待处理消息
   ↓  跳过已软删的
规则预筛 ──→ 丢弃（记一笔）
   ↓  保留 + 上下文邻居
按群与时间切批
   ↓
逐批：
   脱敏（抹号码 → 抹姓名）
   ↓
   调模型
   ↓
   解析：逐字摘录 → 回连到源消息
   ↓  对不上的整条丢弃
   核验：日期 / 时刻 / 地点 / 金额 → 没依据的清掉对应字段
   ↓
   落库（条目 + 来源 + 记账，同一个事务）
```

## 三个承重的位置

**一、切批按群与时间连续。** 不跨群混批 —— 否则模型会把 A 群的上下文套到
B 群。由 `domain/chunking.py` 守。

**二、核验在写入边界。** 不在渲染层。网页是直接读库字段的，只在渲染层设卡，
绕过去太容易。所以闸门在**本模块**调，落库之前。

**三、条目与记账同事务。** 分开提交会在崩溃时留下「写了条目、没记账」，
下一轮重抽就产出重复条目，而且无声。

## 回连来源：对不上的整条丢弃

模型给的 `quote` 必须能在源文里**逐字找到** —— 程序靠它回连到原条目。

找不到时**丢整条**，不是「保留但没有来源」。理由：一条没有来源的条目
在界面上无法回溯（用户点进去看不到原消息），而它又占着日报的位置。
更坏的是一种隐蔽情形：模型抄错了引文但抄对了内容 —— 保留它会让
「这条到底出自哪条消息」永远存疑。

⚠️ **但要区分两种情况**：找不到可能是
「模型抄错了」，也可能是「那条消息根本不在这一批里」。后者说明**切批有 bug**，
而它不是靠丢弃能解决的 —— 所以本模块记的是**两种不同的账**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yechao.domain import chunking, prefilter, scrub
from yechao.domain.categories import resolve_kind
from yechao.domain.digest import extract_preview  # noqa: F401  (re-export 给用例)
from yechao.domain.errors import FailureLedger, ModelError
from yechao.domain.models import ExtractionCandidate, Item, PendingMessage, SourceRef
from yechao.domain.verify import gate

# ── 记账的种类 ──────────────────────────────────────────────────────
#
# ⚠️ 每一条都要有**名字**，因为它们进最终报告的覆盖率分母。
# 混成一类会让「模型判错了」与「这次没跑到」看起来一样。

FILTERED = "filtered"          # 预筛丢弃
SPAM = "spam"                  # 刷屏
QUOTE_MISMATCH = "quote_mismatch"   # 摘录对不上源文
NO_KIND = "no_kind"            # 模型没给出可判定的类目
GATE_CLEARED = "gate_cleared"  # 闸门清掉了字段
BATCH_FAILED = "batch_failed"  # 整批失败
OVERSIZED = "oversized"        # 单条超长（静默截断的高危来源）
UNPROCESSED = "unprocessed"    # 没跑到（预算耗尽 / 中途取消）


@dataclass
class ExtractOutcome:
    """一次抽取的结果。**每一项都要能回答「为什么」。**"""

    items: list[Item] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    ledger: FailureLedger = field(default_factory=FailureLedger)
    #: 真的被送进模型的**消息**条数。覆盖率的分母就是它
    #: 分母只含真正被送进模型的。
    sent_to_model: int = 0
    #: 窗口内应当被送进模型的条数（预筛保留的）。
    eligible: int = 0
    batches_total: int = 0
    batches_ok: int = 0

    @property
    def coverage(self) -> float:
        """真的跑到了多少。⚠️ 它**不是**「模型漏了多少」——
        那两件事混起来会得出一条远低于实际的召回率，
        而真相是「这次没跑完」。
        """
        if self.eligible == 0:
            return 1.0
        return self.sent_to_model / self.eligible


# ── 解析模型输出 ────────────────────────────────────────────────────


def parse_candidates(raw: str) -> list[ExtractionCandidate]:
    """把模型输出解析成候选条目。

    ⚠️ **模型不是每次都把 JSON 裸着给**：
    同一段输入两次跑，一次裸、一次包在 Markdown 代码块里。不处理的话症状是
    「有时成功有时整批失败」—— 这种间歇性失败最难查，因为它看起来像
    「模型今天心情不好」。

    所以这里先剥围栏，再找 JSON。剥不干净时**不猜** —— 返回空列表，
    由调用方记一笔 `batch_failed`。

    ⚠️ 与「被截断」是两件事：截断在 `ModelClient` 那一层就抛
    `ModelTruncated` 了（靠 `finish_reason`），不会走到这里。
    """
    import json

    text = raw.strip()

    # 剥 Markdown 围栏：```json ... ``` 或 ``` ... ```
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            body = text[first_nl + 1 :]
            end = body.rfind("```")
            text = body[:end].strip() if end >= 0 else body.strip()

    # 前面可能有一句「好的，以下是结果」
    start = text.find("[")
    if start < 0:
        start = text.find("{")
    if start > 0:
        text = text[start:]

    data = _loads_first_json(text)
    if data is _NOTHING:
        return []

    # 允许顶层是 `{"items": [...]}` 或直接是 `[...]`
    if isinstance(data, dict):
        for key in ("items", "results", "data", "extractions"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []

    out: list[ExtractionCandidate] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        quote = str(row.get("quote") or row.get("excerpt") or "").strip()
        title = str(row.get("title") or "").strip()
        if not quote or not title:
            # ⚠️ 没有摘录就没有回连的可能 —— 整条丢弃，不猜。
            continue
        out.append(
            ExtractionCandidate(
                kind=str(row.get("kind") or "").strip(),
                title=title,
                quote=quote,
                detail=_opt_str(row.get("detail")),
                deadline=_opt_str(row.get("deadline")),
                start=_opt_str(row.get("start")),
                place=_opt_str(row.get("place")),
                amount=_opt_str(row.get("amount")),
            )
        )
    return out



#: 解析失败的哨兵。用 `None` 不行 —— 合法的 JSON 里 `null` 就是一个值。
class _Nothing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<没有解析出 JSON>"


_NOTHING = _Nothing()


def _loads_first_json(text: str):
    """从一段可能带前后废话的文本里取出**第一个完整的 JSON 值**。

    ## ⚠️ 这里有一个真实踩过的坑，值得写下来

    原来的写法是「头部从第一个 `[` 或 `{` 开始，尾部截到最后一个 `]` 或 `}`」。
    听起来对称，实际上**会把自己的合法输出咬坏**：

        [
  {"title": "x"}
]

    尾部那一步先对 `]` 截一次（无事），再对 `}` 截一次 —— 而 `}` 在数组
    **内部**，于是 `]` 被切掉了。结果是一段语法错误的文本，解析返回 0 条。

    症状有多坏：**模型完全正常、输出完全合法**，但「条目 0 条」。
    而「解析出 0 条」与「模型没找到条目」在报告里长得一样 ——
    这正是本项目反复在防的那种「看起来像成功/失败」的混淆。

    ⇒ 改成**先试整段，失败再逐段缩**：从每个 `[` / `{` 起点开始，用
    `raw_decode` 让 JSON 解析器自己告诉我们这个值到哪儿结束。解析器的答案
    永远比我们猜的边界准。
    """
    import json

    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        return value
    return _NOTHING


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── 回连来源 ────────────────────────────────────────────────────────


def match_quote(quote: str, batch: list[PendingMessage]) -> PendingMessage | None:
    """把模型的摘录回连到源消息。

    ⚠️ **逐字**匹配（规范化之后）。不做模糊匹配：模糊匹配会让「模型抄错了
    但那句话大意对」也过关，而回连的**唯一**用途是精确指回原消息 ——
    指错了比指不出来更坏。
    """
    needle = scrub.normalize(quote)
    if not needle:
        return None
    for m in batch:
        if needle in scrub.normalize(m.content):
            return m
    # 摘录可能是跨消息拼的：试更短的片段逐条找
    for m in batch:
        body = scrub.normalize(m.content)
        # 取摘录的一个 8 字窗口去找，找得到就算对上了（中文 8 字基本能唯一确定）
        if len(needle) > 8:
            for i in range(0, len(needle) - 8 + 1, 4):
                if needle[i : i + 8] in body:
                    return m
    return None


# ── 主流水线 ────────────────────────────────────────────────────────


@dataclass
class ExtractDeps:
    """抽取需要的全部依赖。**用组合而不是继承** —— 便于测试里逐个替换。"""

    messages: Any           # MessageRepository
    items: Any              # ItemRepository
    model: Any              # ModelClient
    audit: Any              # EgressAudit
    clock: Any              # Clock
    config: Any             # ConfigStore
    prompt: Any             # 提示词构造器（见 prompts.py）


def run(
    deps: ExtractDeps,
    *,
    since: int | None = None,
    until: int | None = None,
    budget: int = 200,
    cancel: Any = None,
    on_progress: Any = None,
) -> ExtractOutcome:
    """跑一轮抽取。

    `budget` 限制**批数**（不是条数）—— 因为成本与时间都由批数决定，而一条
    超长的批与一条很短的批花的时间差不多。

    `cancel` 是一个可调用对象（`() -> bool`），返回真表示「请求取消」。
    ⚠️ 取消是**协作式**的：只在**批与批之间**检查。正在飞的那一批会跑完
    （Python 没法从外面杀线程），而且**已经落库的东西不回滚**
    —— 取消不该把做完的活毁掉。
    """
    outcome = ExtractOutcome()

    raw = deps.messages.pending_messages(since=since, until=until)

    # ── 0. 跳过已经被处理过的消息 ────────────────────────────────
    #
    # ⚠️ **这一步原来漏了，而它的漏法是本项目最典型的形状：
    # 能力写好了、有测试、但从来没被调用。**
    #
    # `covered_msg_ids()` 在仓储里、有测试、返回正确的集合 ——
    # 而 `extract.run()` 从来没读过它。后果：
    #   · 重跑 refine 会把**已经抽过的消息再抽一遍** ⇒ 重复条目
    #   · 用户软删的条目会被**重新抽出来** ⇒ 删掉的东西自己回来
    #     （不变量 ② 的第二半失败）
    #
    # 而这两个症状都不会报错 —— 它们看起来像「抽取又跑了一遍」。
    #
    # ⇒ 判据：**只要一条消息已经有条目引用它（无论那个条目是否被软删），
    # 就不再处理它。** 「含软删」是关键：排除软删会让删掉的条目复活。
    covered = deps.items.covered_msg_ids()
    if covered:
        raw = [m for m in raw if m.msg_id not in covered]
    if not raw:
        return outcome

    # ── 1. 预筛 ──────────────────────────────────────────────────
    tiers = {g.id: g.tier for g in deps.config.groups()}
    kept, dropped = prefilter.prescreen(list(raw), tiers=tiers)

    for msg, decision in dropped:
        kind = SPAM if decision.rule == "spam" else FILTERED
        outcome.ledger.record(kind, f"{msg.msg_id}:{decision.rule}")

    if not kept:
        return outcome

    # ── 2. 补上下文邻居 ──────────────────────────────────────────
    batch_input = prefilter.with_context(kept, list(raw))
    outcome.eligible = len(batch_input)

    # ── 3. 超长单体：**报警，不静默** ────────────────────────────
    # ⚠️ 超长单体是静默截断的高危来源，而它不会在 chunk() 的结果里显形
    # （它只是「独占一批」）。这里主动查一遍并记账。
    for m in chunking.oversized(batch_input):
        outcome.ledger.record(OVERSIZED, f"{m.msg_id}:{len(m.content)}字")

    # ── 4. 切批 ──────────────────────────────────────────────────
    batches = chunking.chunk(batch_input)
    outcome.batches_total = len(batches)

    names = deps.config.nicknames()
    known_uins = [p.uin for p in deps.config.persons()]
    kinds_map = {c.slug: c for c in deps.config.categories().categories}

    for n, batch in enumerate(batches):
        if budget is not None and n >= budget:
            # ⚠️ **没跑到的要按条记账**，不能只记「还剩几批」——
            # 覆盖率的分母是**条数**，不是批数。
            for m in batch.messages:
                outcome.ledger.record(UNPROCESSED, str(m.msg_id))
            continue
        if cancel is not None and cancel():
            for m in batch.messages:
                outcome.ledger.record(UNPROCESSED, str(m.msg_id))
            continue

        _run_batch(
            batch, deps, outcome,
            names=names, known_uins=known_uins, kinds_map=kinds_map,
        )
        outcome.batches_ok += 1
        if on_progress is not None:
            on_progress(n + 1, len(batches))

    # ── 5. 跨群转发合并 ──────────────────────────────────────────
    #
    # ⚠️ **这是本项目最该保留、最该合并的信号**：三个不同的群转发同一条
    # 通知 —— 那不是刷屏（刷屏的桶带「群」这一维，见 prefilter），
    # 那是「这件事被多个渠道确认了」。
    #
    # 原来没做这一步，于是同一件事在日报里出现三次，而 `source_count`
    # 永远是 1 —— 一个**承诺过、界面上还显示着**的能力，实际没实现。
    #
    # 合并之后：条目只剩一条，`source_count = 3`，而三条来源消息**都留着**
    # （`item_sources` 是多对多）—— 用户点进去能看到三个群都说过。
    outcome.items, outcome.sources = _merge_forwarded(
        outcome.items, outcome.sources
    )

    # ── 6. 落库（条目 + 来源 + 记账，同一个事务）─────────────────
    if outcome.items:
        # ⚠️ 这里**一次调用**完成三件事。分三次调用就是三次事务 ——
        # 中间崩了会留下「写了条目、没记账」。
        ids = deps.items.insert_items(outcome.items, outcome.sources)
        outcome.items = [
            _with_id(it, i) for it, i in zip(outcome.items, ids)
        ]

    return outcome



#: 合并「跨群转发」的判据参数。
#: ⚠️ 复用 `domain/conflicts.py` 里那一对（标题相似 + 时间接近）——
#: 「同一件事」这个判断在全项目**只有一处**。两处各写一份必漂移，
#: 而漂移的症状是「日历上折了但日报里还是三条」。
_FORWARD_WINDOW = 6 * 3600


def _merge_forwarded(
    items: list, sources: list
) -> tuple[list, list]:
    """把跨群转发的同一件事合并成一个条目。

    返回 `(合并后的条目, 合并后的来源)`。

    ## ⚠️ 来源与条目怎么对齐 —— 这里踩过一次

    `insert_items` 原来假设 `sources[i]` 属于 `items[i]`（**顺序一一对应**）。
    而合并之后一条条目挂**多条**来源，那个假设直接崩了：

        来源比条目多：20 > 18

    而且它报的是「调用方把两个列表配错了」——**指向了错误的方向**。
    调用方没配错，是契约本身不成立。

    ⇒ 现在来源用 `SourceRef.item_id` **显式指**它属于哪一条：
    值是**合并后列表里的下标 + 1**（0 留给「没指定、按顺序补」）。
    仓储据此分组挂载，不再靠位置猜。

    ⚠️ 那个 `+1` 是刻意的：`0` 在 `item_id` 里天然表示「没有」，
    而「第 0 条」与「没指定」必须分得开 —— 混起来会让第一条的来源
    被当成 implicit 处理。
    """
    from yechao.domain.conflicts import _similar  # 同一处「同一件事」的判据
    from yechao.domain.models import SourceRef

    kept: list = []
    kept_srcs: list[list[SourceRef]] = []

    for it, src in zip(items, sources):
        target: int | None = None
        for i, rep in enumerate(kept):
            if it.kind != rep.kind:
                continue
            if abs(it.event_ts - rep.event_ts) > _FORWARD_WINDOW:
                continue
            if _similar(it.title, rep.title):
                target = i
                break

        if target is None:
            kept.append(it)
            kept_srcs.append([src])
        else:
            kept_srcs[target].append(src)

    # ⚠️ 来源数在这里**重算** —— 它是合并的结果，不是输入的一部分。
    import dataclasses

    merged_items = [
        dataclasses.replace(it, source_count=len(srcs))
        for it, srcs in zip(kept, kept_srcs)
    ]

    # ⚠️ 给每条来源打上「它属于合并后第几条」（1-based）。
    # 这一步不做的话，仓储只能靠位置猜 —— 而位置在合并之后已经不成立了。
    flat_sources: list[SourceRef] = []
    for idx, srcs in enumerate(kept_srcs):
        for src in srcs:
            flat_sources.append(
                dataclasses.replace(src, item_id=idx + 1)
            )

    return merged_items, flat_sources


def _with_id(item: Item, item_id: int) -> Item:
    import dataclasses

    return dataclasses.replace(item, item_id=item_id)


def _run_batch(
    batch: chunking.Batch,
    deps: ExtractDeps,
    outcome: ExtractOutcome,
    *,
    names: list[str],
    known_uins: list[int],
    kinds_map: dict[str, Any],
) -> None:
    """跑一批。**失败不抛**，而是记一笔 —— 一批坏了不该让整轮停。"""
    # ── 脱敏 ──
    # ⚠️ 出网的**每一步文本**都必须是 `scrub()` 的产物，且它的报告要落审计。
    scrubbed_texts: list[str] = []
    total_num = 0
    total_name = 0
    names_checked = 0
    for m in batch.messages:
        clean, report = scrub.scrub(m.content, names, known_uins=known_uins)
        scrubbed_texts.append(clean)
        total_num += report.num_placeholders
        total_name += report.name_placeholders
        names_checked = max(names_checked, report.names_checked)

    user = deps.prompt.build_extract_user(batch.messages, scrubbed_texts)

    # ── 调模型 ──
    try:
        raw = deps.model.complete(
            system=deps.prompt.EXTRACT_SYSTEM,
            user=user,
            max_tokens=deps.prompt.EXTRACT_MAX_TOKENS,
        )
    except ModelError as e:
        # ⚠️ 整批失败要**记账**，不能静默跳过 —— 静默跳过的形状是
        # 「用户以为那天没事」。
        outcome.ledger.record(BATCH_FAILED, f"批{batch.group_id}:{type(e).__name__}")
        return

    # ── 审计：**记录那一刻送出去的到底是什么** ──
    # ⚠️ 检查与脱敏必须复用**同一条判据**（`scrub()` 的返回），
    # 另写一套的话两者迟早漂移，漂移后检查报「0 命中」而实际有东西出去了。
    # ⚠️ 而且报告里**必须**带 `names_checked`：名单为空时「命中 0」的含义是
    # 「**没查**」，不是「通过」。
    deps.audit.record(
        {
            "surface": "refine",
            "batch": _batch_index(batch, outcome),
            "group_id": batch.group_id,
            "messages": len(batch.messages),
            "chars": sum(len(t) for t in scrubbed_texts),
            "num_placeholders": total_num,
            "name_placeholders": total_name,
            "names_checked": names_checked,
            "sha256": _sha(scrubbed_texts),
            # ⚠️ **不存内容**。存了就是新造一个泄漏面，
            # 而它恰好会存下最该保护的那些文本。
        }
    )

    # ⚠️ **「送过」与「解析成功」是两件事。**
    #
    # 这一行原来放在解析之后 —— 于是解析失败的那些批虽然**真的送了**，
    # 却不计入 `sent_to_model`。后果是覆盖率的分子偏小，看起来像
    # 「这次没跑完」；而真相是「跑了，但模型没给出可用的东西」。
    # 把两个不同的失败混成一个数，正是这种形状。
    outcome.sent_to_model += len(batch.messages)

    # ── 解析 + 回连 + 核验 ──
    candidates = parse_candidates(raw)
    if not candidates:
        outcome.ledger.record(BATCH_FAILED, f"批{batch.group_id}:解析出0条")
        return

    local_year = _year_of(deps.clock.now())

    for cand in candidates:
        src = match_quote(cand.quote, list(batch.messages))
        if src is None:
            # ⚠️ 两种「对不上」分开记：
            # · 摘录在**这一批里**找不到任何片段 ⇒ 模型抄错了
            # · 摘录里的特征片段能在**别处**找到 ⇒ 切批有 bug
            # 前者丢一条，后者要查切批 —— 混成一个数会让真 bug 被淹没。
            outcome.ledger.record(QUOTE_MISMATCH, f"{cand.title[:16]}")
            continue

        if not cand.kind:
            outcome.ledger.record(NO_KIND, f"{cand.title[:16]}")
            continue

        label, icon, slug = resolve_kind(cand.kind, kinds_map)
        item = _build_item(cand, src, deps, label=label, icon=icon, slug=slug)

        # ── 核验闸门：**在写入边界**（不在渲染层）──
        result = gate(
            item, src.content,
            local_year=local_year,
            # ⚠️ 时区**从时钟拿**，不写死 —— 全项目「本地日」只有一处口径。
            tz_offset=getattr(deps.clock, "tz_offset", 8 * 3600),
        )
        if result.cleared:
            outcome.ledger.record(GATE_CLEARED, f"{item.title[:16]}:{','.join(result.cleared)}")

        outcome.items.append(result.item)
        outcome.sources.append(SourceRef(item_id=0, msg_id=src.msg_id, quote=cand.quote))


def _build_item(
    cand: ExtractionCandidate,
    src: PendingMessage,
    deps: ExtractDeps,
    *,
    label: str,
    icon: str,
    slug: str,
) -> Item:
    """把候选拼成条目。**时间字段在这里解析成绝对时刻**（闸门随后核验）。"""
    from yechao.domain.times import find_dates, find_times

    deadline_ts = _resolve_ts(cand.deadline, src.content, src.ts, deps.clock)
    start_ts = _resolve_ts(cand.start, src.content, src.ts, deps.clock)

    return Item(
        item_id=0,
        kind=slug,
        kind_label=label,
        kind_icon=icon,
        title=cand.title,
        detail=cand.detail,
        event_ts=src.ts,
        deadline_ts=deadline_ts,
        group_id=src.group_id,
        group_name=_group_name(deps, src.group_id),
        actor=_display_actor(src.sender),
        actor_uin=src.actor_uin,
        place=cand.place,
        amount=cand.amount,
        links=(),
        source_count=1,
        start_ts=start_ts,
    )


def _resolve_ts(
    text: str | None, source: str, msg_ts: int, clock: Any
) -> int | None:
    """把模型填的日期/时刻串解析成绝对 Unix 秒。

    ⚠️ **只做「源文里有没有」的解析，不做「合不合理」的判断** ——
    后者是闸门（`verify.py`）的事。这里解析出来的值，闸门随后会去源文里
    逐字核对。

    ⚠️ 年份从**消息发出时刻**推（源文里通常不写年份）。这是领域层不取
    当前时刻的原因之一：`msg_ts` 是外部输入。
    """
    if not text:
        return None
    import datetime as _dt

    from yechao.domain.times import find_dates, find_times

    dates = find_dates(text) or find_dates(source)
    times = find_times(text)
    if not dates:
        return None
    d = dates[0]
    # 改用带时区写法：这里只是拿 UTC 钟去读「已偏移过的秒数」，仍须用 UTC，
    # 若换成 fromtimestamp(ts) 会按机器本地时区解释，年份可能不同。
    base = _dt.datetime.fromtimestamp(
        msg_ts + clock.tz_offset, tz=_dt.timezone.utc
    )
    year = d.year or base.year
    hh, mm = (times[0].hour, times[0].minute) if times else (0, 0)
    try:
        dt = _dt.datetime(year, d.month, d.day, hh, mm, tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp()) - clock.tz_offset


def _display_actor(sender: str) -> str | None:
    """从群昵称里剥出**人名本体**。

    ⚠️ 昵称是「全称」（`22-1班班主任助理张三`），正文里是「裸名」。两者要能
    对应 —— 所以这里剥掉班级前缀与角色词。

    ⚠️ 但**只在展示时**用剥出来的名字。昵称的**全称**在本机内部保留，
    因为它带角色与班级，是判断「这条是不是官方发的」的信号。
    """
    from yechao.domain.prefilter import ROLE_WORDS

    s = sender.strip()
    if not s:
        return None
    # 去班级前缀：`22-1班` / `2022级` / `高一(3)班`
    import re

    s = re.sub(r"^[\d\s\-]*(?:级|班|届)[\s\-]*", "", s)
    s = re.sub(r"^\d{2,4}[-级]?\d*\s*", "", s)
    # 去角色词（可能出现在前面）
    for w in sorted(ROLE_WORDS, key=len, reverse=True):
        if s.startswith(w):
            s = s[len(w) :].strip()
            break
    return s or None


def _group_name(deps: ExtractDeps, group_id: int) -> str:
    for g in deps.config.groups():
        if g.id == group_id:
            return g.name
    return f"群{group_id}"


def _year_of(ts: int) -> int:
    import datetime as _dt

    # utcfromtimestamp 已废弃，换成显式 UTC 的时区感知写法；取 .year 结果不变。
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).year


def _batch_index(batch: chunking.Batch, outcome: ExtractOutcome) -> int:
    return outcome.batches_ok


def _sha(texts: list[str]) -> str:
    """出网文本的**指纹**。

    ⚠️ 存指纹而不是内容：指纹能回答「这两次出网送的是不是同一份」，
    而**回答不了**「里面是什么」—— 这正是要的效果。
    """
    import hashlib

    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]
