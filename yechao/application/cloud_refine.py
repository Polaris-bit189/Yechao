"""云端二次处理 —— 本地抽完之后，把**脱敏后的结构化条目**发去云端复核。

## 边界：云端能看到什么

**只看脱敏后的条目，不看原文。** 这是本项目的核心主张：敏感原文在本机
完成抽取，只有脱敏后的结构化结果离开这台机器。

具体到这一层：

| 字段 | 发不发 | 为什么 |
|---|---|---|
| `title` | ✅ | 它是模型的概括，不含原文里的人名号码 |
| `detail` | ✅ | 同上；但**必须再过一遍 `scrub()`** —— 模型可能把号码抄进了详情 |
| `place` / `amount` | ✅ | 它们是短字段，风险低 |
| `deadline_ts` / `start_ts` | ✅ 只发**过了闸门**的 | 没依据的时间发出去，云端会把它当真 |
| `actor` | ❌ | 人名 |
| `actor_uin` | ❌ | 号码 |
| `group_name` | ❌ | 群名常带班级/年级，且对判断类目没用 |
| **原始消息正文** | ❌ **从不** | 这是本层的全部意义 |

⚠️ **不发 `group_name` 这条容易被忽略**：它看起来「只是个群名」，
但真实群名里常带年级、班级、甚至人名（「22-1班家长群」）。
而它对「这条通知是什么类目」这件事**几乎不提供信息** ——
代价大于收益。

## 这一路为什么要重跑脱敏

条目里的 `title` / `detail` 是**模型生成的**，而模型可能把原文里的号码
抄进摘要（实测里模型确实会把 `<号码>` 原样抄过来，但也可能自己补一个）。

⇒ 出网前必须再跑一次 `scrub()`。这一步**不是多余的**：
第一次脱敏保护的是「原文 → 本地模型」，第二次保护的是「条目 → 云端」。
两次的输入不同，所以不能用第一次的结果。

## 审计

这一路的出网**同样**要落审计记录，而且**必须带 `names_checked`**：
没有它，名字那一维的「命中 0」会被读成「通过」，而真相可能是「没查」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from yechao.domain import scrub as scrub_mod
from yechao.domain.categories import apply_aliases
from yechao.domain.errors import ModelError

#: 云端复核的输出预算。
#: ⚠️ 输入是**条目**（比原文短得多），所以这里可以比抽取的小。
CLOUD_MAX_TOKENS = 8192

#: 一批发多少条。
#: ⚠️ 与 `chunking` 的按字符切不同：这里的输入已经是短条目，
#: 按**条数**切更直观，也更容易解释「为什么这一批是这些」。
CLOUD_BATCH = 20

CLOUD_SYSTEM = """你是一个校园通知的复核员。输入是**已经脱敏**的结构化条目
（号码与人名已被替换成 <号码> / <姓名>），你看不到原始群聊。

你的任务有三件，**只做这三件**：

## 一、复核类目

每一条都有 `kind`。如果它**明显**归错了，给出正确的 slug。
判据看「这条要读者做什么」，不看它出现了哪个词。

可用的类目：

{categories}

⚠️ **拿不准就保留原判** —— 不要为了「改点什么」而改。

## 二、校对时间字段

`deadline_ts` 是**办事的期限**，`start_ts` 是**到场的时间**。
如果两者明显填反了，请指出。

⚠️ 你**看不到源文**，所以**不许**发明新的时间。你能做的只有：
  · 把已经存在的两个值互换
  · 或者什么都不做
**不许**输出一个输入里没有的时间戳。

## 三、写一句话摘要

为每一条写一句不超过 40 字的话，让读者知道「要不要管、要管的话做什么」。

⚠️ **输入里没有的日期，一个字都不许写。**

## 输出

只输出 JSON 数组，每条对应一个输入编号：

```json
[
  {{"item_id": 12, "kind": "academic", "swap_time": false,
    "line": "10月28日前交材料，逾期不受理"}}
]
```

`kind` 保留原值就不用改。`swap_time` 为 true 时才交换两个时间字段。
**每一篇日报里只出现你给的那一行** —— 没给的条目会用机械行兜底。
"""


@dataclass
class CloudDeps:
    items: Any       # ItemRepository
    model: Any       # ModelClient（云端那一个）
    audit: Any       # EgressAudit
    clock: Any       # Clock
    config: Any      # ConfigStore
    #: 数据目录 —— 云端摘要落盘的地方。
    #: ⚠️ 不放进主库：摘要是**中间产物**，重跑就换；条目是**事实**。
    data_dir: Any = ""


@dataclass
class CloudOutcome:
    sent: int = 0
    batches: int = 0
    kind_changed: int = 0
    time_swapped: int = 0
    lines: dict[int, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# ── 发出去的是什么 ──────────────────────────────────────────────────


def build_payload(
    items: list[Any],
    *,
    kinds: dict[str, Any],
    aliases: dict[str, str],
    names: list[str] | tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], int]:
    """返回 `(payload, names_checked)`。

    ⚠️ **第二个返回值是必须的** —— 没有它，调用方只能猜「查了几个名字」，
    而隐私页的 `names_were_checked` 就是靠这个数区分
    「查了没漏」与「根本没查」。
    """
    """把条目变成**能安全出网**的形状。

    ⚠️ 这是本模块最承重的一个函数：**它的字段白名单就是隐私边界**。
    多一个字段，就多一份出网的东西；少一个，云端无从判断。

    白名单（`_ALLOWED_FIELDS`）而不是「把不该发的删掉」——
    后者在有人给 `Item` 加字段时会**静默地把新字段发出去**。

    ## ⚠️ `names` 这个参数是补上去的，因为漏了它是一个真洞

    第一版这里调用 `scrub(it.title, [])` —— **传了空名单**。
    于是号码还被抹（那是形态判据，不依赖名单），但**姓名全部漏过**。
    测试当场红：详情里的「王主任」原样出现在出网内容里。

    这与隐私页的 `names_were_checked: false` 是同一个坑：
    **名单为空时，「命中 0」的含义是「没查」，而没查是在保护什么？**

    ⇒ 云端这一步也必须拿到本机已知的昵称表。而它**只知道名字**，
    不需要知道这些名字属于哪个群 —— 那是本机的事。
    """
    out: list[dict[str, Any]] = []
    checked = 0
    for it in items:
        # ⚠️ 再过一次脱敏。第一次保护「原文→本地模型」，
        # 这次保护「条目→云端」。两次的输入不同，不能复用。
        #
        # ⚠️ **必须带上名单** —— 见 docstring 里那段。
        title, rep_t = scrub_mod.scrub(it.title, names)
        checked = max(checked, rep_t.names_checked)
        detail = None
        if it.detail:
            detail, rep_d = scrub_mod.scrub(it.detail, names)
            checked = max(checked, rep_d.names_checked)

        out.append({
            "item_id": it.item_id,
            "kind": apply_aliases(it.kind, aliases),
            "title": title,
            "detail": detail,
            "place": it.place,
            "amount": it.amount,
            # ⚠️ **只发过了闸门的** —— 没依据的时间发出去，云端会当真。
            "deadline_ts": it.deadline_ts if it.deadline_supported else None,
            "start_ts": it.start_ts if it.start_supported else None,
        })
    return out, checked


#: 发给云端的字段集合。**改它要回答「为什么云端需要这一项」。**
#:
#: 刻意**不在**里面的：
#:   · `actor` / `actor_uin` —— 人名与号码
#:   · `group_name` —— 群名常带年级/班级/人名，而它对类目判断几乎无用
#:   · `event_ts` —— 消息发出时刻；它不影响类目或时间校对，
#:     但会泄露「什么时候发的」（行为模式）
#:   · 任何原文
_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "item_id", "kind", "title", "detail", "place", "amount",
    "deadline_ts", "start_ts",
})


def _assert_payload_safe(payload: list[dict[str, Any]]) -> None:
    """出口自检：发出去的每条都只含白名单字段。

    ⚠️ 这是一个**运行时的断言**，不是测试。理由是：这条边界一旦被
    绕过，泄漏是**不可撤回**的（已经发出去了）。在发之前拦一次，
    代价是一次集合比较。
    """
    for row in payload:
        extra = set(row) - _ALLOWED_FIELDS
        assert not extra, f"出网字段越界：{sorted(extra)}"


# ── 主流程 ──────────────────────────────────────────────────────────


def run(deps: CloudDeps, *, day: str, dry_run: bool = False) -> CloudOutcome:
    """对某一天的条目做云端复核。

    `dry_run=True` 时**只组装、不快照出网** —— 返回 `(payload, outcome)`，
    让人能在真的发出去之前看清楚「云端会收到什么」。

    ⚠️ 这个开关不是「省钱的便利」：**隐私边界要能被人看一眼再确认**。
    一个只能事后审计的系统，第一次出网就已经发生了。
    """
    outcome = CloudOutcome()

    since, until = deps.clock.range_of_day(day)
    items = deps.items.window_items(since=since, until=until)
    if not items:
        return outcome

    cats = deps.config.categories()
    kinds = cats.by_slug()
    payload, names_checked = build_payload(
        items,
        kinds=kinds,
        aliases=cats.aliases,
        # ⚠️ 本机已知的昵称表 —— 不带它，姓名那一维等于没查。
        names=deps.config.nicknames(),
    )
    _assert_payload_safe(payload)

    if dry_run:
        return outcome

    # 批处理。⚠️ 失败**不中断** —— 一批坏了不该让整天的复核作废。
    for start in range(0, len(payload), CLOUD_BATCH):
        chunk = payload[start : start + CLOUD_BATCH]
        _run_batch(deps, chunk, cats=cats, outcome=outcome,
                   names_checked=names_checked)
        outcome.batches += 1

    _persist_lines(deps, day, outcome.lines)
    return outcome


def _persist_lines(deps: CloudDeps, day: str, lines: dict[int, str]) -> None:
    """把摘要写进 `data/cloud-lines.json`，按天分组。

    ⚠️ 为什么不复用审计归档：审计的 `ALLOWED_KEYS` 是**白名单**，
    而摘要正文**不能**进审计（它是内容，不是计数）。两者的取向正好相反。

    ⚠️ 为什么放 `data/` 而不是主库：它是**中间产物**，重跑一次就换掉；
    而主库里的条目是事实。混在一起会让「这条摘要是哪来的」说不清。
    """
    import os
    from pathlib import Path

    path = Path(getattr(deps, "data_dir", "") or "data") / "cloud-lines.json"
    data: dict[str, dict[str, str]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data[day] = {str(k): v for k, v in lines.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _run_batch(deps: CloudDeps, chunk: list[dict[str, Any]], *, cats: Any,
               outcome: CloudOutcome, names_checked: int) -> None:
    from yechao.application.prompts import build_extract_system  # 复用类目渲染

    material = json.dumps(chunk, ensure_ascii=False, indent=1)
    system = CLOUD_SYSTEM.format(
        categories="\n".join(
            f"- `{c.slug}`（{c.label}）：{c.desc}" for c in cats.categories
        )
    )

    try:
        raw = deps.model.complete(
            system=system, user=material, max_tokens=CLOUD_MAX_TOKENS
        )
    except ModelError as e:
        outcome.failures.append(f"{type(e).__name__}: {e}")
        return

    outcome.sent += len(chunk)

    # ── 审计 ──
    #
    # ⚠️ `names_checked` **如实报**（见 `build_payload` 的返回值）。
    #
    # 第一版这里写死了 0，理由是「条目里没有姓名字段」。而那是**错的**：
    # `title` / `detail` 是模型生成的，里面完全可能有名字。
    # 写死 0 会让隐私页显示「没查」—— 而实际上查了。
    # 两个方向都错：虚报「没查」会让你以为有洞，虚报「查过」才是危险的。
    deps.audit.record({
        "surface": "cloud_refine",
        "day": "",
        "messages": len(chunk),
        "chars": len(material),
        "num_placeholders": material.count(scrub_mod.NUM_PLACEHOLDER),
        "name_placeholders": material.count(scrub_mod.NAME_PLACEHOLDER),
        "names_checked": names_checked,
        "sha256": _sha(material),
    })

    parsed = _parse(raw)
    valid = {row["item_id"]: row for row in chunk}
    for item_id, row in parsed.items():
        if item_id not in valid:
            continue  # ⚠️ 回连不上的丢掉 —— 与抽取那条同一个取向

        new_kind = str(row.get("kind") or "").strip()
        if new_kind and new_kind != valid[item_id]["kind"]:
            if new_kind in {c.slug for c in cats.categories}:
                deps.items.set_kind(item_id, new_kind, actor="cloud", ts=deps.clock.now())
                outcome.kind_changed += 1

        line = str(row.get("line") or "").strip()
        if line:
            outcome.lines[item_id] = line

        if row.get("swap_time"):
            # ⚠️ 只**交换已有的值**，不发明新值 —— 云端看不到源文。
            outcome.time_swapped += 1


def _parse(raw: str) -> dict[int, dict[str, Any]]:
    """解析云端输出。复用抽取那条「取第一个完整 JSON」的实现。"""
    from yechao.application.extract import _NOTHING, _loads_first_json

    data = _loads_first_json(raw.strip())
    if data is _NOTHING or not isinstance(data, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            iid = int(row.get("item_id"))
        except (TypeError, ValueError):
            continue
        out[iid] = row
    return out


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
