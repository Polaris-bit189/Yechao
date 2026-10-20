"""重排用例 —— 用新提示词重判存量条目。

## 要防什么

用户改了类目判据（`categories.toml` 的 `desc`），希望**存量条目**按新判据
重新分类 —— 而不是只有新条目受益。

## 两条纪律

**一、重排是 append-only 的。** 它产出一批**新的事件**（`reorder` 类），
而不是直接 `UPDATE items.kind`。理由与「删除是软的」同源：**撤销需要
「原来是什么」**。

⚠️ 所以 `undo` 能工作：把那一批事件翻回去，条目的 `kind` 回到重排前。

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

    ⚠️ `actor` 是**撤销的键** —— 丢了它就再也撤不回这一批
    （前端注释逐字：「返回的 `actor` 是**撤销的键**——丢了它就再也撤不回这一批」）。
    """
    from yechao.application.jobs import run_batches

    all_items, total = deps.items.search(limit=100000, offset=0)
    if not all_items:
        return {"actor": "", "changed": 0, "total": 0, "failed": 0}

    if deps.jobs.busy():
        # ⚠️ 409 的文案要**说清下一步**（等它结束），不是「出错了」。
        raise ValidationError("已经有一个后台任务在跑了，等它结束再试")

    job_id = deps.jobs.create("reorder", total=len(all_items))
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

    # ⚠️ 送出去的是**条目**（已在库里、已脱敏），不是原文。
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
        if target.kind == c.kind:
            continue
        # ⚠️ 走 `set_kind`（它追加事件），**不直接改字段** —— 见模块 docstring 一。
        deps.items.set_kind(target.item_id, c.kind, actor=actor)
        changed += 1
    return changed


def _find_by_quote(quote: str, valid: dict[int, Item]) -> Item | None:
    from yechao.domain.scrub import normalize

    needle = normalize(quote)
    for it in valid.values():
        if needle and (needle in normalize(it.title) or normalize(it.title) in needle):
            return it
    return None


def _is_cancelled(deps: ReorderDeps, job_id: str) -> bool:
    job = deps.jobs.get(job_id)
    return bool(job and getattr(job, "state", "") == "cancelled")


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def undo(deps: ReorderDeps, job_id: str) -> tuple[bool, int]:
    """撤销一次重排。

    ⚠️ 返回 `(是否撤到了, 撤了几条)`。`False` 时前端显示
    「这一批没有可撤销的改动（可能已经撤过了）」—— 不是报错。
    """
    return deps.edits.bulk_undo(f"reorder:{job_id}")
