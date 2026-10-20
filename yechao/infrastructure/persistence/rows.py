"""行 → 领域对象：**两种存储共用**的映射。

## 为什么单独一个模块

SQLite 与 MySQL 两个适配器读的是**同一批列**，映射规则也必须一模一样 ——
否则同一个条目在两个后端上会长得不一样，而那种差异**不报错**：它表现为
「换了数据库之后，某几个字段的显示变了」。

这类漂移的典型来源是「各写一份」：两份代码一开始一样，改一边忘一边，
而忘记的那一刻没有任何东西会红。所以映射只留**一处**。

## 为什么能共用：两种驱动的行对象都支持按列名取

`sqlite3.Row` 与 pymysql 的 `DictCursor` 产出的行，都能 `row["列名"]`。
这正是下面一律**按列名取、不按下标取**的前提 ——

※ 按下标取时，列序变了会**静默地**把 `event_ts` 填进 `deadline_ts`：
不报错，只是时间错了。两个后端各自拼列清单，列序不一致的可能性是**真实存在**
的（MySQL 那张表在 `CREATE TABLE` 里多两列少两列都会挪位），所以这条纪律
在共用的映射里必须写死。
"""

from __future__ import annotations

from typing import Any

#: ※ 表别名固定为 `i`。改这里的列清单时要**同时**改 `row_to_item`。
ITEM_COLS = """
    i.item_id, i.kind, i.title, i.detail, i.event_ts, i.deadline_ts, i.group_id,
    i.actor, i.actor_account, i.place, i.amount, i.links, i.start_ts,
    i.deadline_supported, i.start_supported, i.deleted, i.source_count,
    i.kind_locked, i.created_at
"""


def row_to_message(row: Any) -> Any:
    from yechao.domain.models import PendingMessage

    return PendingMessage(
        msg_id=row["msg_id"],
        group_id=row["group_id"],
        ts=row["ts"],
        sender=row["sender"],
        content=row["content"],
        actor_account=row["actor_account"],
        source=row["source"],
    )


def row_to_item(row: Any) -> Any:
    """※ **一律按列名取**，不按下标 —— 见模块 docstring。

    ※ `kind_label` / `group_name` 在这里填**占位**，真正的值由输出层按
    「当前配置 + 别名表」算。那样改类目名之后，存量条目会立刻跟着变 ——
    而不是等着被重写一遍。
    """
    import json

    from yechao.domain.models import Item

    return Item(
        item_id=row["item_id"],
        kind=row["kind"],
        kind_label=row["kind"],  # 占位，输出层替换
        title=row["title"],
        detail=row["detail"],
        event_ts=row["event_ts"],
        deadline_ts=row["deadline_ts"],
        group_id=row["group_id"],
        group_name=f"群{row['group_id']}",  # 占位，输出层替换
        actor=row["actor"],
        actor_account=row["actor_account"],
        place=row["place"],
        amount=row["amount"],
        links=tuple(json.loads(row["links"] or "[]")),
        source_count=int(row["source_count"] or 0),
        start_ts=row["start_ts"],
        deadline_supported=bool(row["deadline_supported"]),
        start_supported=bool(row["start_supported"]),
        deleted=bool(row["deleted"]),
        kind_locked=bool(row["kind_locked"]),
    )


#: ※ 表别名固定为 `r`。与 `ITEM_COLS` 同一条纪律：列清单与映射必须一起改。
RUN_COLS = """
    r.run_id, r.kind, r.started_at, r.finished_at, r.status, r.seen,
    r.produced, r.model_eligible, r.model_sent
"""

#: ※ 表别名固定为 `e`。
ENTRY_COLS = """
    e.entry_id, e.run_id, e.kind, e.msg_id, e.group_id, e.batch_no,
    e.item_id, e.reason
"""


def row_to_run(row: Any) -> Any:
    """`runs` 的一行 → `RunRecord`。

    ※ `entries` 在这里是**空元组**：抬头行与账是两张表，把它们拼起来要
    多一次查询。列表页（`recent_runs`）不该为每一条抬头各查一次账 ——
    那是 N+1。需要账的地方显式调 `entries_of_run(run_id)`（一次查询），
    见 `RunLedgerRepository` 的注释。
    """
    from yechao.domain.errors import RunRecord

    return RunRecord(
        run_id=row["run_id"],
        kind=row["kind"],
        started_at=int(row["started_at"]),
        finished_at=int(row["finished_at"]),
        status=row["status"],
        seen=int(row["seen"] or 0),
        produced=int(row["produced"] or 0),
        model_eligible=int(row["model_eligible"] or 0),
        model_sent=int(row["model_sent"] or 0),
    )


def row_to_entry(row: Any) -> Any:
    """`run_entries` 的一行 → `LedgerEntry`。

    ※ 主体四列一律**原样**取（可能是 `None`）—— 不要 `or 0`：
    0 是合法的群号与条目号，用它表示「没有这一维」会让「第 0 条」与
    「不适用」分不开，而这两件事在「这条消息为什么没进条目」里指向
    完全不同的下一步。
    """
    from yechao.domain.errors import LedgerEntry

    return LedgerEntry(
        kind=row["kind"],
        msg_id=row["msg_id"],
        group_id=row["group_id"],
        batch_no=row["batch_no"],
        item_id=row["item_id"],
        reason=row["reason"] or "",
    )


def row_to_digest(row: Any) -> Any:
    from yechao.domain.models import Digest

    return Digest(
        digest_id=row["digest_id"],
        day=row["day"],
        window_from=row["window_from"],
        window_to=row["window_to"],
        created_at=row["created_at"],
        body_md=row["body_md"],
        item_count=row["item_count"],
        preview=row["preview"],
    )


def as_ts(v: Any) -> int:
    """`YYYY-MM-DD` 或 Unix 秒 → Unix 秒（按**本地日**起点）。

    ※ 这里也走 `domain.days.Clock`，**不自己算** —— 「本地日」的口径
    全项目只有一处（两边各写一份必漂移）。这一条与上面「映射只留一处」
    是同一个理由，所以这个函数也放在共用模块里。
    """
    s = str(v).strip()
    if s.isdigit() and len(s) >= 9:
        return int(s)
    from yechao.domain.days import DEFAULT_CLOCK
    from yechao.domain.errors import StorageError

    if len(s) == 10 and s[4] == "-":
        return DEFAULT_CLOCK.day_start_from_str(s)
    raise StorageError(f"不认识的日期写法：{v!r}（要 `YYYY-MM-DD` 或 Unix 秒）")
