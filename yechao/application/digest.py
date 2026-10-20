"""日报用例 —— 取窗口 → 摘掉没依据的截止日 → 调模型 → 回连 → 渲染 → 落库。

## 那条最承重的判据在这里落地

```
取窗口内的条目
   ↓
核验截止日（**没依据的不发给模型**）
   ↓
调模型合成行 → 回连条目
   ↓  对不上的行丢弃，由机械行兜底
渲染
   ↓
落库
```

**「没依据的截止日不发给模型」**：

> 只在显示端拦是不够的 —— 字段还在输入里，模型就会自己把那个日期写进正文，
> 读者看到的还是同一个瞎编的数。**它看不到，才写不出来。**

※ 落点在 `domain/digest.py::build_model_input`。本模块只负责**调用顺序**。

## 模型挂了日报也不能空

模型返回空 / 解析不出 / 调用失败 ⇒ **机械行兜底**。
「今天没有日报」与「今天模型没跑成」是两句完全不同的话 —— 后者必须看出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yechao.application.jobs import new_run_id
from yechao.domain import digest as dom
from yechao.domain.errors import (
    RUN_DIGEST,
    RUN_FAILED,
    RUN_OK,
    FailureLedger,
    ModelError,
    RunRecord,
)
from yechao.domain.models import Digest, Item

# ── 账的种类 ────────────────────────────────────────────────────────
#
# ※ 三条都有名字，因为下一步不同：模型挂了去查端点，回连不上要去看提示词，
# 机械行则说明「这一格没有模型写的版本」。混成一类会让「模型没跑成」与
# 「模型跑了但有行没对上」看起来一样。

MODEL_FAILED = "model_failed"        # 模型这一路整批失败
LINE_UNMATCHED = "line_unmatched"    # 模型写的行回连不到条目
LINE_MECHANICAL = "line_mechanical"  # 这一行的正文是机械行（没有模型版本）


@dataclass
class DigestDeps:
    items: Any       # ItemRepository
    digests: Any     # DigestRepository
    model: Any       # ModelClient
    clock: Any       # Clock
    config: Any      # ConfigStore
    audit: Any       # EgressAudit


@dataclass
class DigestOutcome:
    digest: Digest | None = None
    #: 这一次跑在库里的 id（`runs.run_id`）。见 `extract.ExtractOutcome`。
    run_id: str = ""
    #: 模型写了几行。
    by_model: int = 0
    #: 用机械行兜底了几行。
    mechanical: int = 0
    #: 整批失败的原因（None = 没失败）。
    failure: str | None = None
    #: 这一轮的账。※ 它**与 refine 那一侧同一个类型**（`FailureLedger`）——
    #: 原来这里是 `dict[str, int]`，而「同一条账两种形状」意味着落库时
    #: 要各写一份转换，那两份迟早漂移。
    ledger: FailureLedger = field(default_factory=FailureLedger)


def run(
    deps: DigestDeps,
    *,
    day: str,
    stats: str = "",
    use_model: bool = True,
    cloud_lines: dict[int, str] | None = None,
) -> DigestOutcome:
    """生成某一天的日报。

    `day` 是 `YYYY-MM-DD`（**本地日**）。※ 由调用方给 —— 领域层与用例层
    都不取「今天」：时间必须可注入，否则测试只能测「我这一刻跑的那次」。
    """
    started_at = deps.clock.now()
    since, until = deps.clock.range_of_day(day)

    items = deps.items.window_items(since=since, until=until)
    outcome = DigestOutcome()

    # ※ **群名要在这里补上。**
    #
    # 仓储读出来的 `Item.group_name` 是**占位值**（`群{id}`）—— 真正的群名
    # 由输出层按「当前配置」算，因为群名可以在配置里改。
    #
    # 而日报正文是**落盘成 Markdown** 的，它不经过输出层。
    # 不在这里补的话，页面上会出现 `群910000002` 这种既不是群号也不是名字的
    # 东西，而接口里同一个字段是对的 —— 两处代码单看都正常。
    import dataclasses

    group_names = {g.id: g.name for g in deps.config.groups() if g.name}
    if group_names:
        items = [
            dataclasses.replace(it, group_name=group_names.get(it.group_id, it.group_name))
            for it in items
        ]

    kinds_map = {c.slug: c.label for c in deps.config.categories().categories}

    # ※ **云端摘要优先。** 它是「二次处理」的产物：云端看到的是**脱敏后的
    # 条目**，所以它的措辞一定不包含本机原文里的敏感信息。
    #
    # 本地模型那一支仍然保留 —— 云端没配时（多数演示场景）它兜底。
    summaries: dict[int, str] = dict(cloud_lines or {})

    if items and use_model and not summaries:
        # ※ **这一步就是那条判据**：`build_model_input` 只输出
        # `deadline_supported` 为真的截止日。
        material = dom.build_model_input(items, deps.clock)
        try:
            from yechao.application import prompts as _p

            raw = deps.model.complete(
                system=_p.DIGEST_SYSTEM,
                user=_p.build_digest_user([], material),
                # ※ **不写死数字** —— 预算只有一处答案（prompts 的常量）。
                max_tokens=_p.DIGEST_MAX_TOKENS,
            )
            from yechao.application.prompts import parse_digest_lines

            parsed = parse_digest_lines(raw)
            valid_ids = {it.item_id for it in items}
            # ※ 回连不上的行**丢掉**（不是「保留但没有来源」）。
            summaries = {k: v for k, v in parsed.items() if k in valid_ids}
            # ※ 一行一笔。这一笔**没有主体**：回连不上，正是「不知道它属于
            # 哪条条目」。所以查「这条条目为什么在日报里是机械行」时看不到
            # 它 —— 那要查 `line_mechanical`。
            for _ in range(len(parsed) - len(summaries)):
                outcome.ledger.record(LINE_UNMATCHED, reason="no_such_item")

            # 审计：日报这次出网也留一条记录
            deps.audit.record(
                {
                    "surface": "digest",
                    "day": day,
                    "messages": len(items),
                    "chars": len(material),
                    "num_placeholders": 0,
                    "name_placeholders": 0,
                    # ※ 日报这一路的输入是**库里的条目**，不是原文 ——
                    # 库里存的已经是脱敏后的产物，所以这里没有新的姓名名单
                    # 可比对。报 `names_checked: 0` 而不是「0 命中」。
                    "names_checked": 0,
                    "sha256": _sha(material),
                }
            )
        except ModelError as e:
            # ※ 模型失败**不抛** —— 日报必须能产出来（机械行兜底）。
            # 但失败**要响**：`failure` 字段会一路传到 CLI 输出与日志。
            outcome.failure = f"{type(e).__name__}: {e}"
            # ※ 账里存的是**原因码**（异常类型名），不是 `str(e)` ——
            # 给人看的那一份在 `failure` 里，CLI 会打印。
            outcome.ledger.record(MODEL_FAILED, reason=type(e).__name__)

    draft = dom.draft(
        items, day=day, clock=deps.clock, kinds=kinds_map, summaries=summaries
    )
    outcome.by_model = draft.by_model
    outcome.mechanical = draft.line_count - draft.by_model

    # ※ 机械行**按条记账**（主体是条目 id）：它答的是「**这一条**条目
    # 为什么在日报里没有模型写的版本」。与 refine 那一侧按条记同一个取向
    # —— 「每一条都能回答为什么」这句话对条目也成立。
    # 只记**日报里真的有的行**：超出单篇上限、没进本篇的条目不该记成
    # 「这一行是机械行」—— 它们没有行。那一条由 `draft.omitted` 负责说。
    for sec in draft.sections:
        for ln in sec.lines:
            if not ln.by_model:
                outcome.ledger.record(LINE_MECHANICAL, item_id=ln.item_id)

    # ※ 模型一行都没给（或者根本没跑模型）时，`stats` 要能说明这件事。
    if outcome.failure and not stats:
        stats = "（模型这次没跑成，以下为机械整理）"

    # ※ 只传**这一路真的知道的**：几个群、提炼出几条。
    # 「多少条消息」「多少条送进模型」只有抽取那一路知道 —— 原来这里拿
    # `len(items)` 把 `messages` / `extracted` / `eligible` 填成同一个数，
    # 于是概览句永远印「送进模型 N/N」，读起来是「全都送了」，**而那是编的**。
    # 拿不到就不印（`digest_stats` 的后三个参数因此是可选）。
    full = dom.digest_stats(
        groups=_distinct_groups(items),
        items=draft.line_count,
    )
    digest = dom.make_digest(
        draft, digest_id=0, created_at=deps.clock.now(), stats=stats or full
    )

    # ※ 抬头 + 账 + 日报**同一次调用、同一个事务**（判据见
    # `ports.ItemRepository.insert_items`）。
    #
    # ※ `status` 在 digest 这一路**比退出码严**：模型没跑成时 CLI 仍回 0
    # （机械行兜底让日报照样有内容），但账里必须能看见「这一轮本该由模型
    # 写的那些行没写成」。判据是「这一轮的产出是不是**本该是模型写的**」——
    # 报 ok 会让「日报有内容」与「模型跑成了」混成一件事。
    model_path = bool(items) and use_model and not (cloud_lines or {})
    run = RunRecord(
        run_id=new_run_id(),
        kind=RUN_DIGEST,
        started_at=started_at,
        finished_at=deps.clock.now(),
        status=RUN_FAILED if outcome.failure else RUN_OK,
        seen=len(items),
        produced=draft.line_count,
        # ※ 这两列与 refine 同一个含义：**该送进模型的** / **真的送进去的**。
        # 没走模型那条路时是 0（「这一轮没有模型那一侧的事」）。
        model_eligible=len(items) if model_path else 0,
        model_sent=0 if (outcome.failure or not model_path) else len(items),
        entries=outcome.ledger.entries,
    )
    new_id = deps.digests.insert_digest(
        digest, [it.item_id for it in items], run=run
    )
    outcome.run_id = run.run_id
    import dataclasses

    outcome.digest = dataclasses.replace(digest, digest_id=new_id)
    return outcome


def _distinct_groups(items: list[Item]) -> int:
    return len({it.group_id for it in items})


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
