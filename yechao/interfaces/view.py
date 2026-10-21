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
    """一条来源消息。

    ## `source` 传**原始 slug**，不在这里翻成中文名

    与 `kind_label` 的做法**不同**，而这是刻意的：

      · 类目名（`kind_label`）是**用户能改**的配置，且改完之后存量条目
        必须立刻跟着变 —— 那份「当前配置 + 别名表」只有后端有。
      · 源名是**闭集**（`infrastructure/sources/__init__.py` 的注册表），
        用户改不了，也不会漂移。它的中文名与图标是**展示映射**，
        与 `kindIcon` 同一种东西 —— 见本模块 docstring 里「图标不在这一份里」
        那一段（同一个理由：后端拼一个只有前端看得懂的字段，等于在
        用户数据里写前端库的命名）。

    ※ 认不出来的 slug **原样传**，由前端决定怎么显示 —— 后端不许在这里
    丢字段或替换成「未知」：`messages.source` 的默认值是空串（一条没标来源
    的消息），而**空串是一个合法值**，不是错误。前端据此区分「没标来源」
    与「标了一个我不认识的来源」。
    """
    return {
        "msg_id": row["msg_id"],
        "ts": row["ts"],
        "sender": row["sender"] or "",
        "group_name": group_names.get(row["group_id"]) or f"群{row['group_id']}",
        "source": row["source"] or "",
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


def run_out(run: Any) -> dict[str, Any]:
    """一次跑的抬头（`runs` 表的一行）。

    ※ `finished_at` 为 0 时**原样传 0**，不换成 `None`、更不换成「现在」——
    0 的含义是「还没收尾」（`runs.finished_at` 的默认值）。补成当前时刻会
    让一次崩在半路的跑看起来像刚刚跑完，而那正是这一页最不该说错的一句话。

    ※ `seen` 与 `produced` 两个数**都要给**：前者是「读到多少条」，
    后者是「交给库多少条」。只报一个的话，「今天怎么只有这些」与
    「今天读到的都重复了」就分不开了。
    """
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "seen": run.seen,
        "produced": run.produced,
        "model_eligible": run.model_eligible,
        "model_sent": run.model_sent,
    }


def unreadable_out(entry: Any, *, run: Any) -> dict[str, Any]:
    """一条「这个源没读到」的账。

    ## 为什么源名要从 `reason` 里**解出来**

    `application/ingest.py` 记的是 `reason=f"{name}:{type(e).__name__}"`
    —— 源名没有单独一列。这不是疏忽：`run_entries` 的四个主体列是
    「消息 / 群 / 批 / 条目」，一个**源名**哪个都不是，硬塞进去会造出一个
    语义不同的第五种主体（见 `schema.py` 里那四列的注释）。

    ※ 解不出来时 `source` 是空串，而**整条账照样带出去**（`reason` 原样）：
    丢掉一条看不懂的账，等于把「有个源没读到」这件事也一起丢了 ——
    而那正是这一页要回答的问题。
    """
    reason = entry.reason or ""
    name, _, why = reason.partition(":")
    return {
        "run_id": run.run_id,
        "run_started_at": run.started_at,
        "source": name,
        "reason": reason,
        "why": why,
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
