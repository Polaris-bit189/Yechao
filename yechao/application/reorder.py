"""重排用例 —— 用新提示词重判存量条目。

## 要防什么

用户改了类目判据（`categories.toml` 的 `desc`），希望**存量条目**按新判据
重新分类 —— 而不是只有新条目受益。

## 两条纪律

**一、重排是 append-only 的。** 它产出一批**新的事件**（`reorder` 类），
而不是直接 `UPDATE items.kind`。理由与「删除是软的」同源：**撤销需要
「原来是什么」**。

※ 所以 `undo` 能工作：把那一批事件翻回去，条目的 `kind` 回到重排前。

**二、一次只允许一个重排在跑。** 并发两个会让事件的顺序失去意义
（后一个撤销回来可能覆盖前一个的结果）。

## 为什么它要单独一个 job 而不是塞进 refine

`refine` 处理的是**消息**，`reorder` 处理的是**条目** —— 两者的输入、成本、
失败模式都不同。混在一起会让「重排」这个词在进度条上失去意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yechao.domain.errors import ModelError, ValidationError
from yechao.domain.models import Item


@dataclass
class ReorderDeps:
    items: Any      # ItemRepository
    edits: Any      # EditRepository
    jobs: Any       # JobRepository
    model: Any      # ModelClient
    config: Any     # ConfigStore
    audit: Any      # EgressAudit


#: 每条目重判时取多少条上下文。
_BATCH = 20


def run(deps: ReorderDeps, *, budget: int | None = None) -> dict[str, Any]:
    """跑一次重排。**返回摘要**（`{actor, changed, total, failed}`）。

    ※ 撤销要靠 `actor` 把这一批认出来。一次重排会改很多条，只有这个值
    能把它们算作同一批 —— 前端不把它带回来，服务端就不知道要撤哪些行。
    """
    from yechao.application.jobs import run_batches

    all_items, _ = deps.items.search(limit=100000, offset=0)

    # ※ **库里一条条目都没有时，也要建作业。**
    #
    # 原来这里提前返回，于是 `POST /api/jobs` 拿不到 `job_id` —— 而那个端点
    # 必须回一个作业给前端轮询。没有作业可回，它只好自己造一个，于是出现
    # **两个 job_id**：端点返回一个、`run` 真正用的是另一个，
    # `/api/jobs/{id}/undo` 于是永远找不到这一批。
    #
    # 走 `run_batches` 的空列表分支更诚实：它建作业、`total=0`、状态 `done`、
    # 消息是「没有可处理的内容」—— 界面上看得出「跑了，但确实没事做」，
    # 而不是一个分不清「跑过了」和「没跑」的空白。

    # ※ 方法名与端口（`JobRepository`）逐字一致：`any_job_running` 不是
    # `busy`，`create_job` 不是 `create`。原来这里写成 `busy()`，而实现里
    # 只有 `any_job_running()` —— 一跑就 AttributeError，而 `reorder` 此前
    # 零测试，所以这个错位一直没被发现。
    if deps.jobs.any_job_running():
        # ※ 409 的文案要**说清下一步**（等它结束），不是「出错了」。
        raise ValidationError("已有任务在跑，等它结束再来")

    job_id = deps.jobs.create_job("reorder", total=len(all_items))
    actor = f"reorder:{job_id}"

    chunks = [all_items[i : i + _BATCH] for i in range(0, len(all_items), _BATCH)]

    changed = 0

    def _one(_i: int, chunk: list[Item]) -> None:
        nonlocal changed
        changed += _reorder_chunk(deps, chunk, actor=actor)

    result = run_batches(
        chunks,
        jobs=deps.jobs,
        job_id=job_id,
        run_one=_one,
        cancelled=lambda: _is_cancelled(deps, job_id),
    )

    return {
        "actor": actor if changed else "",
        "changed": changed,
        "total": len(all_items),
        "failed": result.failed,
        "job_id": job_id,
    }


def _reorder_chunk(deps: ReorderDeps, chunk: list[Item], *, actor: str) -> int:
    """重判一批条目。返回改了几条。"""
    from yechao.application.prompts import EXTRACT_SYSTEM, build_extract_system
    from yechao.application.extract import parse_candidates

    cats = deps.config.categories().categories
    system = build_extract_system(cats)

    # ※ 送出去的是**条目**（已在库里、已脱敏），不是原文。
    # 所以这一路没有新的姓名可比对 —— 审计里 `names_checked` 是 0，
    # 而那个 0 的含义是「这一路本来就无可比对」。
    lines = []
    for it in chunk:
        lines.append(f"[{it.item_id}] {it.title}：{it.detail or ''}")
    material = "\n".join(lines)

    try:
        raw = deps.model.complete(system=system, user=material, max_tokens=4096)
    except ModelError:
        raise

    deps.audit.record(
        {
            "surface": "reorder",
            "messages": len(chunk),
            "chars": len(material),
            "num_placeholders": 0,
            "name_placeholders": 0,
            "names_checked": 0,
            "sha256": _sha(material),
        }
    )

    cands = parse_candidates(raw)
    if not cands:
        return 0

    valid = {it.item_id: it for it in chunk}
    changed = 0
    for c in cands:
        # 重排的输出用 quote 回连，但这里的「源文」是条目清单 —— 编号在
        # 开头，所以直接找编号更可靠。
        target = _find_by_quote(c.quote, valid)
        if target is None or not c.kind:
            continue

        # ※ **手改过的类目不动。** `kind_locked` 一直是「写了没人读」：
        # `set_kind` 置 1、撤销置 0，而重排不看它 —— 于是用户改完类目、
        # 跑一次重排就被静默冲掉（静默的数据丢失是最难发现的一种）。
        if target.kind_locked:
            continue
        if target.kind == c.kind:
            continue
        # ※ 走 `set_kind`（它追加事件），**不直接改字段** —— 见模块 docstring 一。
        deps.items.set_kind(target.item_id, c.kind, actor=actor)
        changed += 1
    return changed


def _find_by_quote(quote: str, valid: dict[int, Item]) -> Item | None:
    """把模型给的摘录回连到**一条**条目；对不上、或对上不止一条，都返回 None。

    ※ 原来两个方向都试（`needle in title` 与 `title in needle`）。反方向会让
    **短标题抢走长标题的摘录**：`选课通知` 是 `选课通知（补充）` 的子串，于是
    模型对后者说的话被写到前者身上。而症状是 `changed=1` —— 看起来完全正常，
    只是改错的不是那一条；撤销也只能撤这一条。

    ⇒ 只做**一个方向**：摘录必须是某一条的 `标题：详情` 里的连续片段（这正是
    模型看到的输入形状）。命中不止一条时**不猜** —— 宁可不改，也不改错。

    ※ 与 `extract.match_quote` 同一个取向：那边写着「指错了比指不出来更坏」，
    所以也只做逐字匹配。
    """
    from yechao.domain.scrub import normalize

    needle = normalize(quote)
    if not needle:
        return None

    hits = [
        it
        for it in valid.values()
        if needle in normalize(f"{it.title}：{it.detail or ''}")
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return None

    # 多条命中 ⇒ 先看有没有标题与摘录**完全相同**的那一条；仍然不唯一就不猜。
    exact = [it for it in hits if normalize(it.title) == needle]
    return exact[0] if len(exact) == 1 else None


def _is_cancelled(deps: ReorderDeps, job_id: str) -> bool:
    """这一批跑完之后，作业该不该停。

    ※ **读的是 `cancel_requested` 旗标，不是 `state == 'cancelled'`。**

    取消是**协作式**的（见 `jobs.py`）：`/api/jobs/{id}/cancel` 只把旗标置 1，
    `state` 要等 `run_batches` 真的停下来才被写成 `'cancelled'`。所以在这个
    检查发生的时刻，`state` **永远还是 `'running'`** —— 原来按 `state` 判，
    等于取消请求被静默忽略，作业照跑完。那是「看起来像成功」的失败。

    旁证：`SqliteStore.cancel_requested()` 写好了、有方法体，而**全仓没有
    调用者** —— 它本来就是给这里用的。同一类 bug（能力写好了但没接线），
    与 `covered_msg_ids` 那次同形。

    ※ 两个名字都对齐端口：`cancel_requested` / `get_job`（不是 `get`）。
    `get_job` 返回的是 **dict**（仓储直接 `dict(row)`），不是对象 ——
    按属性取会静默拿到 `""`，所以两种都认。
    """
    if deps.jobs.cancel_requested(job_id):
        return True
    job = deps.jobs.get_job(job_id)
    if not job:
        return False
    state = job.get("state", "") if isinstance(job, dict) else getattr(job, "state", "")
    return state == "cancelled"


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def undo(deps: ReorderDeps, job_id: str) -> tuple[bool, int]:
    """撤销一次重排。

    ※ 返回 `(是否撤到了, 撤了几条)`。`False` 时前端显示
    「这一批没什么可撤的 —— 可能之前已经撤过了」，这不是报错。
    """
    return deps.edits.bulk_undo(f"reorder:{job_id}")
