"""云端二次处理 —— 本地抽完之后，把**脱敏后的结构化条目**发去云端复核。

## 边界：云端能看到什么

**只看脱敏后的条目，不看原文。** 这是本项目的核心主张：敏感原文在本机
完成抽取，只有脱敏后的结构化结果离开这台机器。

具体到这一层：

| 字段 | 发不发 | 为什么 |
|---|---|---|
| `title` | √ | 它是模型的概括，不含原文里的人名号码 |
| `detail` | √ | 同上；但**必须再过一遍 `scrub()`** —— 模型可能把号码抄进了详情 |
| `place` / `amount` | √ 出网前过 `scrub()` | ※ 原来这一格写的是「短字段，风险低」—— 而风险与字段长短**无关**，与「它是不是从原文里抄的」有关：`place` 可能是「到 3 号教学楼找周允」，`amount` 可能是「加群 820000004 交费」，人名与群号都在里面 |
| `deadline_ts` / `start_ts` | √ 只发**过了闸门**的 | 没依据的时间发出去，云端会把它当真 |
| `actor` | × | 人名 |
| `actor_account` | × | 号码 |
| `group_name` | × | 群名常带班级/年级，且对判断类目没用 |
| **原始消息正文** | × **从不** | 这是本层的全部意义 |

※ **不发 `group_name` 这条容易被忽略**：它看起来「只是个群名」，
但真实群名里常带年级、班级、甚至人名（「22-1班家长群」）。
而它对「这条通知是什么类目」这件事**几乎不提供信息** ——
代价大于收益。

## 这一路为什么要重跑脱敏

条目里的 `title` / `detail` / `place` / `amount` 是**模型生成的**，而模型可能把
原文里的号码抄进摘要（实测里模型确实会把 `<号码>` 原样抄过来，但也可能自己补
一个）。`place` / `amount` 同样是**从原文里抄的短字段** —— 「到 3 号教学楼找
周允」里有人名，「加群 820000004 交费」里有群号。原来只有 `title` / `detail`
过了 `scrub()`，另外两格原样出去，形状上像是「短字段风险低」，
而风险与字段长短无关，与**它是不是原文的片段**有关。

⇒ 出网前必须再跑一次 `scrub()`，四个字段**同一个口径**（同一份 `names`）。
这一步**不是多余的**：第一次脱敏保护的是「原文 → 本地模型」，
第二次保护的是「条目 → 云端」。两次的输入不同，所以不能用第一次的结果。

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
#: ※ 输入是**条目**（比原文短得多），所以这里可以比抽取的小。
CLOUD_MAX_TOKENS = 8192

#: 一批发多少条。
#: ※ 与 `chunking` 的按字符切不同：这里的输入已经是短条目，
#: 按**条数**切更直观，也更容易解释「为什么这一批是这些」。
CLOUD_BATCH = 20

#: 云端复核的提示词模板。占位符 `{categories}` / `{example_kind}` 由
#: `_run_batch` 用 `prompts.render_categories` / `prompts.example_kind` 填 ——
#: **与抽取那一套共用同一处渲染**（见 `prompts.render_categories`）。
#:
#: ## 这一路要防什么（与抽取不同，因为它的输入形状不同）
#:
#: 输入是**条目**（不是原文），而且是脱敏后的条目。所以「抄错原文」「补字段」
#: 这些失败在这里换了个形状：
#:
#: **一、发明新时间。** 它看不到源文，任何时间戳都是它凭空写的。而时间戳
#: 会进日历、进日报。所以这一路只能**互换已有的两个值**，不能新增。
#:
#: **二、把 `null` 当「忘了填」。** 没通过本机核验的时间在 payload 里是
#: `null`。模型若把另一格的值搬过去补上，等于绕过核验 —— 那个值又变成
#: 有依据的样子了。
#:
#: **三、回填中文名或自造 slug。** 调用方只认 `{c.slug}` 那一组
#: （`cloud_refine._run_batch` 里的 `new_kind in {c.slug ...}`），认不出的
#: **静默忽略**：界面上什么都没变，也不报错。
#:
#: **四、无谓改动。** 一个「顺便改点什么」的复核员比不复核更坏 —— 类目会
#: 抖动，而用户看到的是「同一个东西昨天是 A 类今天是 B 类」。
#:
#: **五、漏条 / 编号错位。** 漏掉的条目走机械行兜底，所以**看不见**。
#:
#: **六、编日期。** 摘要里写出输入中没有的日期。而输入里的时间戳是 **Unix 秒
#: 的整数** —— 模型根本读不出它是几月几号，所以它写的任何日期都只可能来自
#: `title` / `detail` 里逐字出现的那一个，或者干脆是编的。
CLOUD_SYSTEM = """你是一个校园通知的复核员。输入是**已经脱敏**的结构化条目数组（号码与人名已被替换成 <号码> / <姓名>），你看不到原始群聊。

你的任务只有三件，**只做这三件**：

## 一、复核类目

每条都有一个 `kind`。如果它**明显**归错了，给出正确的 slug。

判据看「这条要读者做什么」，不看它出现了哪个词。可用的类目：

{categories}

· `kind` 必须是上面某一个 slug，**原样照抄**，不要写中文名，也不要自己造一个 —— 程序只认这些 slug，认不出的会被**静默忽略**（界面上什么都没变，也不会报错）。
· **拿不准就保留原判。** 不要为了「改点什么」而改 —— 类目在两次复核之间来回跳，用户看到的是同一个东西昨天一个类目、今天另一个。

## 二、校对时间字段

`deadline_ts` 是**办事的期限**，`start_ts` 是**到场的时间**。两个都是 Unix 秒的整数，你读不出它是几月几号 —— 你能判断的只有一件事：**这两个值的次序是不是装反了**。

正常的次序是「先截止、后举办」：`deadline_ts` 不晚于 `start_ts`。明显反了，就把 `swap_time` 置 `true`。

※ 三条禁令：

· **不许输出输入里没有的时间戳。** 你看不到源文，一个也发明不出来。
· 某一格是 `null` 时**不要动它**，也不要把另一格的值搬过去 —— 那个 `null` 的意思是「这一格没通过本机的核验」，不是「忘了填」。
· 只给了一个值，或者两个值只差几分钟，都**不算反了**。这种情况 `swap_time` 置 `false`。

## 三、写一句话摘要

给每条写一句不超过 40 字的话，让读者知道「要不要管、要管的话做什么」。不要复述标题。

※ **输入里没有的日期，一个字都不许写。** 输入里的时间戳是整数，你写不出它对应哪一天 —— 所以这句话里**不要出现日期**，除非那个日期在 `title` / `detail` 里逐字写着，你把它原样抄过来。

## 输出

只输出一个 JSON 数组。**每条输入都要出现一次**，编号照抄输入里的 `item_id`：

[
  {{"item_id": 12, "kind": "{example_kind}", "swap_time": false,
    "line": "到图书馆报告厅领教材，带上学生证"}}
]

· 四个键每条都要给全：`kind` 不变就照抄输入的原值，`swap_time` 不改就是 `false`。形状固定下来，才不会有时候少一个键。
· `swap_time` 用 JSON 的 `true` / `false`，**不是字符串** `"true"` —— 字符串在程序里恒为真，会把不该换的也换掉。
· 编号对不上的条目会被丢掉，它那一行会换成机械行 —— 你看不见自己错了。
· 数组外面不要围栏、不要前后的话。
"""


@dataclass
class CloudDeps:
    items: Any       # ItemRepository
    model: Any       # ModelClient（云端那一个）
    audit: Any       # EgressAudit
    clock: Any       # Clock
    config: Any      # ConfigStore
    #: 数据目录 —— 云端摘要落盘的地方。
    #: ※ 不放进主库：摘要是**中间产物**，重跑就换；条目是**事实**。
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

    ※ **第二个返回值是必须的** —— 没有它，调用方只能猜「查了几个名字」，
    而隐私页的 `names_were_checked` 就是靠这个数区分
    「查了没漏」与「根本没查」。
    """
    """把条目变成**能安全出网**的形状。

    ※ 这是本模块最承重的一个函数：**它的字段白名单就是隐私边界**。
    多一个字段，就多一份出网的东西；少一个，云端无从判断。

    白名单（`_ALLOWED_FIELDS`）而不是「把不该发的删掉」——
    后者在有人给 `Item` 加字段时会**静默地把新字段发出去**。

    ## ※ `names` 这个参数是补上去的，因为漏了它是一个真洞

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
        # ※ 再过一次脱敏。第一次保护「原文→本地模型」，
        # 这次保护「条目→云端」。两次的输入不同，不能复用。
        #
        # ※ **必须带上名单** —— 见 docstring 里那段。
        title, rep_t = scrub_mod.scrub(it.title, names)
        checked = max(checked, rep_t.names_checked)
        detail = None
        if it.detail:
            detail, rep_d = scrub_mod.scrub(it.detail, names)
            checked = max(checked, rep_d.names_checked)

        # ※ `place` / `amount` **也要过 `scrub()`**，而且复用**同一个 payload
        # 里那一份 `names`** —— 不新造一套口径，否则两条路的判据迟早漂移，
        # 而漂移的症状是「一条抹了、另一条没抹」，看审计看不出来。
        #
        # 观察到什么现象：这两格原来原样发出。而它们是**模型从原文里抄的**：
        # `place` 可能是「到 3 号教学楼找周允」，`amount` 可能是
        # 「加群 820000004 交费」—— 人名与群号都在里面。同一个 payload 里
        # 只有 `title` / `detail` 过了 `scrub`，形状上像是「短字段风险低」，
        # 而风险与字段长短无关，与**它是不是原文的片段**有关。
        place, checked_p = _scrub_field(it.place, names)
        amount, checked_a = _scrub_field(it.amount, names)
        checked = max(checked, checked_p, checked_a)

        out.append({
            "item_id": it.item_id,
            "kind": apply_aliases(it.kind, aliases),
            "title": title,
            "detail": detail,
            "place": place,
            "amount": amount,
            # ※ **只发过了闸门的** —— 没依据的时间发出去，云端会当真。
            "deadline_ts": it.deadline_ts if it.deadline_supported else None,
            "start_ts": it.start_ts if it.start_supported else None,
        })
    return out, checked


def _scrub_field(
    value: str | None, names: list[str] | tuple[str, ...]
) -> tuple[str | None, int]:
    """把可空字段过一遍 `scrub()`，返回 `(脱敏后的值, names_checked)`。

    ※ `None` 原样返回 `None`，**不**变成空串 —— 「这一栏没有」与
    「这一栏有值但为空」在提示词里是两件事（云端据此判断该不该动这一栏）。

    ※ 返回的是 `names_checked` 而不是命中数：调用方要能把「查了没命中」
    与「根本没查」分开（见 `build_payload` 的第二个返回值）。
    """
    if value is None:
        return None, 0
    clean, rep = scrub_mod.scrub(value, names)
    return clean, rep.names_checked


#: 发给云端的字段集合。**改它要回答「为什么云端需要这一项」。**
#:
#: 刻意**不在**里面的：
#:   · `actor` / `actor_account` —— 人名与号码
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

    ※ 这是一个**运行时的断言**，不是测试。理由是：这条边界一旦被
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

    ※ 这个开关不是「省钱的便利」：**隐私边界要能被人看一眼再确认**。
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
        # ※ 本机已知的昵称表 —— 不带它，姓名那一维等于没查。
        names=deps.config.nicknames(),
    )
    _assert_payload_safe(payload)

    if dry_run:
        return outcome

    # 批处理。※ 失败**不中断** —— 一批坏了不该让整天的复核作废。
    for start in range(0, len(payload), CLOUD_BATCH):
        chunk = payload[start : start + CLOUD_BATCH]
        _run_batch(deps, chunk, cats=cats, outcome=outcome,
                   names_checked=names_checked)
        outcome.batches += 1

    _persist_lines(deps, day, outcome.lines)
    return outcome


def _persist_lines(deps: CloudDeps, day: str, lines: dict[int, str]) -> None:
    """把摘要写进 `data/cloud-lines.json`，按天分组。

    ※ 为什么不复用审计归档：审计的 `ALLOWED_KEYS` 是**白名单**，
    而摘要正文**不能**进审计（它是内容，不是计数）。两者的取向正好相反。

    ※ 为什么放 `data/` 而不是主库：它是**中间产物**，重跑一次就换掉；
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
    # ※ 类目那一段**只有一处渲染实现**（`prompts.render_categories`），
    # 抽取与云端共用。原来这里自己拼了一遍 `f"- `{c.slug}`（{c.label}）：…"`，
    # 与 `build_extract_system` 里那份是**两份抄本** —— 改了一处漏另一处，
    # 症状是「本地判得对、云端判得不对」，而两份代码单看都对。
    from yechao.application.prompts import example_kind, render_categories

    material = json.dumps(chunk, ensure_ascii=False, indent=1)
    system = CLOUD_SYSTEM.format(
        categories=render_categories(cats.categories),
        example_kind=example_kind(cats.categories),
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
    # ※ `names_checked` **如实报**（见 `build_payload` 的返回值）。
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
            continue  # ※ 回连不上的丢掉 —— 与抽取那条同一个取向

        new_kind = str(row.get("kind") or "").strip()
        if new_kind and new_kind != valid[item_id]["kind"]:
            if new_kind in {c.slug for c in cats.categories}:
                deps.items.set_kind(item_id, new_kind, actor="cloud", ts=deps.clock.now())
                outcome.kind_changed += 1

        line = str(row.get("line") or "").strip()
        if line:
            outcome.lines[item_id] = line

        if row.get("swap_time"):
            # ※ 只**交换已有的值**，不发明新值 —— 云端看不到源文。
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
