"""展示组装 —— 把领域对象变成 API 的输出形状。

## 为什么这件事在**后端**做

一个例子：展示字段（中文类目名、群名）应该在
**后端拼好**再返回。让前端自己查两张表的话，同一个字段在两处拼，
漂移之后的症状是「卡片上有类目名、详情页没有」—— 两边代码单看都正常。

※ **图标不在这一份里**：它由前端按 slug 自己映射
（`web/src/lib/theme.ts`）。理由见 `domain/categories.py` 的模块 docstring ——
后端拼一个只有自己看的图标字段，等于在用户数据里写前端库的命名。

## `kind_label` 为什么不存库

仓储读出来的 `Item` 里，`kind_label` 是 **slug 的占位**（见
`sqlite_store._row_to_item`）。真正的值由**这里**按「当前配置 + 别名表」算。

理由：类目是可以**改名**的。存库的话，改名之后存量条目的 `kind_label`
还是旧名字 —— 除非把全库重写一遍。而重写全库是一个会失败一半的操作
（写了一半崩了，一半旧一半新）。

⇒ **一处算，处处一致**：改名之后，卡片、详情页、日报立刻都跟着变
（因为日报正文是快照，那是**已知的**例外 —— 见 `digest` 的注释）。

## null 与空串的处理

前端的判据是 `field !== null && field !== ''` 才渲染。所以**空串与 null
在前端看起来一样**。

※ 但这不等于后端可以随便发空串：`null` 的含义是「这里没有这个信息」，
而空串的含义是「这里有一个空的字符串」。当字段走到**日报正文**里时，
那个区别就显形了（空串会在正文里留一个空的 `地点：`）。
⇒ 本模块**统一把空串归一成 None**。
"""

from __future__ import annotations

import json
from typing import Any

from yechao.domain.categories import apply_aliases, resolve_kind


def _s(v: Any) -> str | None:
    """空串与 None 归一成 None。见模块 docstring。"""
    if v is None:
        return None
    s = str(v)
    return s if s.strip() else None


def build_kind_lookup(
    categories: list[Any], aliases: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """`slug → (label, 真实 slug)`。

    ※ **建成一张表传下去**，而不是在循环里逐条查 —— 后者是 O(条目 × 类目)。
    在列表页（200 条 × 6 类目）上不明显，但那只是因为它小；
    而这个函数的存在让「怎么解析一个 slug」只有一处实现。
    """
    cats = {c.slug: c for c in categories}
    out: dict[str, tuple[str, str]] = {}
    for slug in cats:
        label, real = resolve_kind(slug, cats)
        out[slug] = (label, real)
    return out


def item_out(
    item: Any,
    *,
    kinds: dict[str, Any],
    aliases: dict[str, str],
    group_names: dict[int, str],
) -> dict[str, Any]:
    """**同一个 `item_out` 供所有端点用。**

    ※ `/api/items`、`/api/calendar`、`/api/urgent`、`/api/items/{id}`、
    `/api/digests/{id}.items` 都走它。分成几份实现的话，
    「日历上的条目少了一栏」这种漂移迟早出现。
    """
    # 先过别名（改名留痕），再解析展示名。
    eff_kind = apply_aliases(item.kind, aliases)
    label, real = resolve_kind(eff_kind, kinds)

    return {
        "item_id": item.item_id,
        "kind": real,
        "kind_label": label,
        "title": item.title,
        "detail": _s(item.detail),
        "event_ts": item.event_ts,
        # ※ 前端据 `null` **不显示**它 —— 所以「源文里没依据」这件事
        # 是在**数据层**落成 null 的，不是在渲染层设卡。
        "deadline_ts": item.deadline_ts if item.deadline_supported else None,
        "group_id": item.group_id,
        "group_name": group_names.get(item.group_id) or f"群{item.group_id}",
        # ※ `actor` 剥过前缀（展示用），`actor_account` 是**实名消息才有**的。
        # 匿名消息两者都为 None —— 且人物筛选**不该命中**它。
        "actor": _s(item.actor),
        "actor_account": item.actor_account,
        "place": _s(item.place),
        "amount": _s(item.amount),
        "links": list(item.links),
        "source_count": item.source_count,
        # ※ `start_ts` 与 `event_ts` 是**两件事**：前者是活动开始时间，
        # **为 null 是常态**（多数条目没有活动时刻），不是异常。
        "start_ts": item.start_ts if item.start_supported else None,
    }


def source_out(row: dict[str, Any], *, group_names: dict[int, str]) -> dict[str, Any]:
    return {
        "msg_id": row["msg_id"],
        "ts": row["ts"],
        "sender": row["sender"] or "",
        "group_name": group_names.get(row["group_id"]) or f"群{row['group_id']}",
        "content": row["content"],
    }


def digest_summary_out(d: Any) -> dict[str, Any]:
    return {
        "digest_id": d.digest_id,
        "day": d.day,
        "window_from": d.window_from,
        "window_to": d.window_to,
        "created_at": d.created_at,
        "item_count": d.item_count,
        # ※ 可选 —— 旧快照里没有这个字段。前端按 `preview ?? ''` 处理。
        "preview": d.preview,
    }


def job_out(j: dict[str, Any]) -> dict[str, Any]:
    """※ `done` 记的是**最后一批跑成功的批号**。

    原值传出去，**不在这里 +1** —— 加了就成了另一种谎报：第 1 批就抛异常时
    进度会显示成 2/N，看着像全做完了。
    """
    return {
        "job_id": j["job_id"],
        "kind": j["kind"],
        "state": j["state"],
        "total": j["total"],
        "done": j["done"],
        "message": j["message"] or "",
        "error": j["error"] or "",
        "actor": j["actor"] or "",
        "started_at": j["started_at"],
        "finished_at": j["finished_at"],
    }


def estimate_stale(ts: int, clock: Any) -> bool:
    """这条数据是不是「上一次运行留下的」。

    留一个显式的判据而不是让它散在别处 —— 因为「新鲜度」是一种很容易
    在重构里丢掉、而丢了也不会报错的语义。
    """
    return clock.days_apart(clock.now(), ts) > 30
