"""SQLite 仓储 —— 端口的具体实现。

## 不变量 ① 的落点就在这个文件里

**网页侧不得写入主库。** 做法是 `open_readonly()` —— 它用
`file:...?mode=ro` 打开连接，**由 SQLite 自己拒绝**写操作。

为什么不靠代码自觉：

> 靠自觉的约束迟早会被绕开，而且绕开时没有声音。让数据库拒绝，写错了会
> **立刻报错**。

⚠️ `mode=ro` 只管**主库文件**；它管不到临时表。所以还要加 `PRAGMA
query_only` —— 见 `connect` 的注释。

## 为什么用 `sqlite3.Row` 而不是字典

`Row` 支持按下标与按列名两种取法。⚠️ 但那正是危险所在：按下标取时，
列序变了会**静默地**把 `event_ts` 填进 `deadline_ts`。
所以下面的映射**一律按列名取**，不按下标。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from yechao.domain.errors import NotFound, ReadOnlyViolation, StorageError
from yechao.infrastructure.persistence.schema import SCHEMA


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """打开一个连接。"""
    p = str(path)
    if readonly:
        uri = f"file:{Path(p).as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.OperationalError as e:
            # ⚠️ 库还不存在时**不要**默默建一个空库 —— 那会让「还没建库」
            # 看起来像「库是空的」，两者是完全不同的话。
            raise StorageError(
                f"主库打不开（只读）：{p}。先跑一次 `python -m yechao demo` 建库。"
            ) from e
        # ⚠️ `query_only` 是第二道闸：把**这个连接**标成只读，于是连
        # `CREATE TEMP TABLE` 之类也会被拒。`mode=ro` 管不到临时表。
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(p, check_same_thread=False)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """⚠️ **接口层的唯一入口。** 见模块 docstring 与不变量 ①。"""
    return connect(path, readonly=True)


def is_readonly(conn: sqlite3.Connection) -> bool:
    """这个连接是不是只读的。给测试与自检用。"""
    try:
        return bool(conn.execute("PRAGMA query_only").fetchone()[0])
    except sqlite3.Error:
        return False


def guard_writable(conn: sqlite3.Connection) -> None:
    """写之前显式检查一句，让失败**指向真正的原因**。

    ⚠️ 不靠它来守 —— 守的是 SQLite 自己。这一句只是为了让错误信息说清
    「这个连接不该写」，而不是 SQLite 那句 `attempt to write a readonly
    database`（那有可能被误读成磁盘权限问题，排查方向完全错）。
    """
    if is_readonly(conn):
        raise ReadOnlyViolation(
            "这个连接是只读打开的；写主库的活由 CLI / 用例那条路走。"
        )


def init_db(path: str | Path) -> None:
    """建库。**幂等**。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一个事务。

    ⚠️ 抽取的落库**必须**整体在一个事务里：条目 + 来源 + 记账分开提交会在
    崩溃时留下「写了条目、没记账」，下一轮重抽产出重复条目，而且**无声**。
    """
    guard_writable(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── SQL 片段 ────────────────────────────────────────────────────────

#: ⚠️ 表别名固定为 `i`。改这里的列清单时要**同时**改 `_row_to_item`。
_ITEM_COLS = """
    i.item_id, i.kind, i.title, i.detail, i.event_ts, i.deadline_ts, i.group_id,
    i.actor, i.actor_uin, i.place, i.amount, i.links, i.start_ts,
    i.deadline_supported, i.start_supported, i.deleted, i.source_count,
    i.kind_locked, i.created_at
"""


def _row_to_message(row: sqlite3.Row) -> Any:
    from yechao.domain.models import PendingMessage

    return PendingMessage(
        msg_id=row["msg_id"],
        group_id=row["group_id"],
        ts=row["ts"],
        sender=row["sender"],
        content=row["content"],
        actor_uin=row["actor_uin"],
        source=row["source"],
    )


def _row_to_item(row: sqlite3.Row) -> Any:
    """⚠️ **一律按列名取**，不按下标 —— 见模块 docstring。

    ⚠️ `kind_label` / `kind_icon` / `group_name` 在这里填**占位**，真正的值
    由输出层按「当前配置 + 别名表」算。那样改类目名之后，存量条目会立刻
    跟着变 —— 而不是等着被重写一遍。
    """
    from yechao.domain.models import Item

    return Item(
        item_id=row["item_id"],
        kind=row["kind"],
        kind_label=row["kind"],  # 占位，输出层替换
        kind_icon="",            # 占位，输出层替换
        title=row["title"],
        detail=row["detail"],
        event_ts=row["event_ts"],
        deadline_ts=row["deadline_ts"],
        group_id=row["group_id"],
        group_name=f"群{row['group_id']}",  # 占位，输出层替换
        actor=row["actor"],
        actor_uin=row["actor_uin"],
        place=row["place"],
        amount=row["amount"],
        links=tuple(json.loads(row["links"] or "[]")),
        source_count=int(row["source_count"] or 0),
        start_ts=row["start_ts"],
        deadline_supported=bool(row["deadline_supported"]),
        start_supported=bool(row["start_supported"]),
        deleted=bool(row["deleted"]),
    )


def _row_to_digest(row: sqlite3.Row) -> Any:
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


def _fts_query(q: str) -> str:
    """把用户输入变成一个 FTS5 查询串。

    ⚠️ **必须整体加引号**：FTS5 查询语法里有 `"` `*` `(` `)` `:` `^` `-`
    这些元字符。用户输入一个 `-` 会让它成为语法错误，而错误信息指向
    「查询语法」—— 排查方向完全错（用户只是想搜一个减号）。
    加引号之后整串是一个 phrase，元字符变成字面量。
    """
    return '"' + q.replace('"', '""') + '"'


class SqliteStore:
    """一个类实现全部仓储端口。

    ## 为什么 `conn` 是 property

    ⚠️ **SQLite 的连接不是线程安全的。** `check_same_thread=False` 只是关掉
    了 Python 那一层「不许跨线程用」的检查，**并没有让连接变成并发安全**。

    而这个应用里，HTTP 端点是**同步函数**，FastAPI 把它们丢进线程池跑。
    一个页面并发发七个请求（类目页要为每类拉预览），七个线程同时用
    **同一个**连接 —— SQLite 当场报错，而报的是 500，页面看到的是
    「读不到这一类的条目」。

    ⚠️ 这个 bug 在后端 185 个测试里**一个都没抓到**：测试都是单线程的。
    它只在「浏览器真的并发请求」时才出现 —— 正是「测试绿 ≠ 对」的
    又一个实例。

    ⇒ 现在每个线程拿自己的连接（`threading.local`）。调用点的写法不用改
    （仍然是 `self.conn`），但底层按线程隔离。

    ⚠️ 写库那条路（CLI / 用例）本来就是单线程顺序跑的，多一个连接没有代价。
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        factory: Any = None,
    ) -> None:
        self._conn = conn
        self._factory = factory
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._factory is None:
            assert self._conn is not None
            return self._conn
        cur = getattr(self._local, "conn", None)
        if cur is None:
            cur = self._factory()
            self._local.conn = cur
        return cur

    # ── 消息 ────────────────────────────────────────────────────

    def upsert_messages(self, messages: list[Any]) -> int:
        guard_writable(self.conn)
        n = 0
        with transaction(self.conn):
            for m in messages:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (msg_id, group_id, ts, sender, content, actor_uin, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (m.msg_id, m.group_id, m.ts, m.sender, m.content,
                     m.actor_uin, m.source),
                )
                n += cur.rowcount
        return n

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        sql = "SELECT * FROM messages m WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            sql += " AND m.ts >= ?"
            args.append(since)
        if until is not None:
            sql += " AND m.ts < ?"
            args.append(until)
        sql += " ORDER BY m.group_id, m.ts, m.msg_id"
        return [_row_to_message(r) for r in self.conn.execute(sql, args)]

    def message_by_id(self, msg_id: int) -> Any | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        return _row_to_message(row) if row else None

    def message_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    # ── 条目 ────────────────────────────────────────────────────

    def insert_items(self, items: list[Any], sources: list[Any]) -> list[int]:
        """**条目 + 来源 + 记账，同一个事务。**

        ## ⚠️ 契约改过一次：从「一一对应」改成「按 `item_id` 挂」

        原来的实现假设 `sources[i]` 属于 `items[i]`（顺序一一对应），
        并在长度不对时拒绝写入。

        而**跨群转发合并**之后，一条条目可以挂**多条**来源 ——
        `len(sources) > len(items)` 是**正常状态**，不是配错。
        第一次跑合并之后的 refine 时，这个契约当场把整轮抽取打崩了：

            来源比条目多：20 > 18

        而它给出的错误信息（「调用方把两个列表配错了」）**指向了错误的
        方向** —— 调用方没配错，是契约本身错了。

        ⇒ 现在来源用 `SourceRef.item_id` 挂：`0` 表示「随条目顺序自动分配」，
        非零表示「挂到这条条目上」。合并之后的来源都带真 `item_id`。

        ⚠️ 仍然保留一条长度判据，但它换了个方向：**没有 `item_id` 的来源
        数量不能超过条目数** —— 那是唯一可能配错的形状。
        """
        guard_writable(self.conn)

        # 显式指了 item_id 的，按它挂；没指的，按顺序补。
        explicit = [s for s in sources if getattr(s, "item_id", 0)]
        implicit = [s for s in sources if not getattr(s, "item_id", 0)]
        if len(implicit) > len(items):
            raise StorageError(
                f"没有指定 item_id 的来源比条目多：{len(implicit)} > {len(items)}。"
                f"调用方把两个列表配错了 —— 继续下去会把来源挂到错的条目上。"
            )

        ids: list[int] = []
        with transaction(self.conn):
            for it in items:
                cur = self.conn.execute(
                    """
                    INSERT INTO items (
                        kind, title, detail, event_ts, deadline_ts, group_id,
                        actor, actor_uin, place, amount, links, start_ts,
                        deadline_supported, start_supported, source_count, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        it.kind, it.title, it.detail, it.event_ts,
                        it.deadline_ts, it.group_id, it.actor, it.actor_uin,
                        it.place, it.amount,
                        json.dumps(list(it.links), ensure_ascii=False),
                        it.start_ts, int(it.deadline_supported),
                        int(it.start_supported), int(it.source_count or 0),
                        it.event_ts,
                    ),
                )
                ids.append(int(cur.lastrowid))

            # ⚠️ 合并之后，一个条目对应**多条**来源 —— 所以这里按
            # `SourceRef.item_id` 分组，而不是 `zip`。
            #
            # 合并时用的 `item_id` 是**合并后列表里的下标 + 1**（见
            # `extract._merge_forwarded`）—— 这里把它换成真的库 id。
            by_new: dict[int, int] = {}
            for idx, new_id in enumerate(ids):
                by_new[idx + 1] = new_id

            def _target(src: Any) -> int:
                raw = int(getattr(src, "item_id", 0) or 0)
                return by_new.get(raw, 0)

            for src in explicit:
                new_id = _target(src)
                if not new_id:
                    raise StorageError(
                        f"来源指向一个不存在的条目下标：{src.item_id}"
                    )
                self.conn.execute(
                    "INSERT OR IGNORE INTO item_sources (item_id, msg_id, quote) "
                    "VALUES (?,?,?)",
                    (new_id, src.msg_id, src.quote),
                )

            for src, new_id in zip(implicit, ids):
                self.conn.execute(
                    "INSERT OR IGNORE INTO item_sources (item_id, msg_id, quote) "
                    "VALUES (?,?,?)",
                    (new_id, src.msg_id, src.quote),
                )

            # ⚠️ 来源数是**算一次写进列**，不是每次查询用子查询现算。
            for new_id in ids:
                self.conn.execute(
                    """
                    UPDATE items SET source_count =
                        (SELECT COUNT(*) FROM item_sources s WHERE s.item_id = ?)
                    WHERE item_id = ?
                    """,
                    (new_id, new_id),
                )
        return ids

    def _item_query(
        self,
        *,
        extra: str = "",
        args: list[Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> tuple[list[Any], int]:
        args = list(args or [])
        deleted_clause = "" if include_deleted else "AND i.deleted = 0"
        where = f"WHERE 1=1 {deleted_clause} {extra}"

        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM items i {where}", args
            ).fetchone()[0]
        )
        sql = (
            f"SELECT {_ITEM_COLS} FROM items i {where} "
            "ORDER BY i.event_ts DESC, i.item_id DESC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = args + [int(limit), int(offset)]
        return [_row_to_item(r) for r in self.conn.execute(sql, args)], total

    def window_items(self, *, since: int, until: int) -> list[Any]:
        rows, _ = self._item_query(
            extra="AND i.event_ts >= ? AND i.event_ts < ?", args=[since, until]
        )
        return rows

    def items_with_deadline(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """带截止日的条目。**不分页** —— 日历要全量（前端契约）。"""
        extra = "AND i.deadline_ts IS NOT NULL"
        args: list[Any] = []
        if since is not None:
            extra += " AND i.deadline_ts >= ?"
            args.append(int(since))
        if until is not None:
            extra += " AND i.deadline_ts < ?"
            args.append(int(until))
        rows, _ = self._item_query(extra=extra, args=args)
        return rows

    def search(self, **filters: Any) -> tuple[list[Any], int]:
        """查条目。返回 `(本页, 总数)`。

        ## 筛选口径（与前端 `filterFeed` 逐条对齐 —— 见它的注释）

        · `kinds`：**维度内并集**（`IN`）；空列表 = 不过滤
        · `persons`：同上，按 `actor_uin`。⚠️ **匿名条目 `actor_uin IS NULL`
          必不匹配**。这是 SQL `IN` 的天然行为，但要有测试钉住 ——
          否则以后有人改成 `COALESCE(actor_uin, 0)` 就悄悄破坏了它，
          而症状是「人物筛选里出现了匿名条目」，两处代码单看都正常。
        · `q`：`len < 3` 走 `LIKE '%q%'`（**大小写敏感、不分词**）；
          `len >= 3` 走 FTS5 trigram（不分大小写、3-gram）。
        · 维度之间是 **AND**。
        """
        kinds = filters.get("kinds") or []
        persons = filters.get("persons") or []
        since = filters.get("since")
        until = filters.get("until")
        group = filters.get("group")
        q = filters.get("q")
        limit = filters.get("limit")
        offset = filters.get("offset") or 0

        extra = ""
        args: list[Any] = []

        if kinds:
            extra += f" AND i.kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if persons:
            extra += f" AND i.actor_uin IN ({','.join('?' * len(persons))})"
            args.extend(persons)
        if since:
            extra += " AND i.event_ts >= ?"
            args.append(_as_ts(since))
        if until:
            extra += " AND i.event_ts < ?"
            args.append(_as_ts(until) + 86400)
        if group is not None:
            extra += " AND i.group_id = ?"
            args.append(int(group))

        if q:
            # ⚠️ **剥控制字符**：NUL 会把 LIKE 的 pattern 截断成 `%`，
            # 那是 SQL 层的怪行为，症状指向「查不到」而不是「输入有问题」。
            clean = "".join(ch for ch in str(q) if ch.isprintable())
            if len(clean) >= 3:
                extra += (
                    " AND i.item_id IN (SELECT rowid FROM items_fts "
                    "WHERE items_fts MATCH ?)"
                )
                args.append(_fts_query(clean))
            elif clean:
                extra += " AND (i.title LIKE ? OR IFNULL(i.detail,'') LIKE ?)"
                args.extend([f"%{clean}%", f"%{clean}%"])

        rows, total = self._item_query(
            extra=extra, args=args, limit=limit, offset=offset
        )
        return rows, total

    def item_detail(self, item_id: int) -> Any | None:
        row = self.conn.execute(
            f"SELECT {_ITEM_COLS} FROM items i WHERE i.item_id = ? AND i.deleted = 0",
            (item_id,),
        ).fetchone()
        return _row_to_item(row) if row else None

    def sources_of(self, item_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT m.msg_id, m.ts, m.sender, m.group_id, m.content, s.quote
            FROM item_sources s JOIN messages m ON m.msg_id = s.msg_id
            WHERE s.item_id = ?
            ORDER BY m.ts
            """,
            (item_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def counts_by_kind(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) n FROM items WHERE deleted = 0 GROUP BY kind"
        ).fetchall()
        return {r["kind"]: int(r["n"]) for r in rows}

    def counts_by_person(self) -> dict[int, int]:
        rows = self.conn.execute(
            """
            SELECT actor_uin, COUNT(*) n FROM items
            WHERE deleted = 0 AND actor_uin IS NOT NULL
            GROUP BY actor_uin
            """
        ).fetchall()
        return {int(r["actor_uin"]): int(r["n"]) for r in rows}

    def covered_msg_ids(self) -> set[int]:
        """已经被引用过的消息 id —— **含被软删的条目**。

        ⚠️ 这两半都是**必须**的：

        · 含**正常**条目 ⇒ 重跑抽取不会产出重复条目（幂等）
        · 含**软删**条目 ⇒ 不变量 ② 的第二半：
          「界面上消失，**且不会被下一轮抽取复活**」

        ⚠️ 第二半**不会自动成立**。软删只把 `items.deleted` 置 1，
        `item_sources` 那些行还在。所以这里必须查**所有**来源，
        **不能**加 `WHERE i.deleted = 0` —— 加了的话，用户删掉的条目会在
        下一轮自己回来，而那是产品里最惊吓的一种 bug（而且无声）。
        """
        rows = self.conn.execute("SELECT DISTINCT msg_id FROM item_sources")
        return {int(r["msg_id"]) for r in rows}

    def deleted_item_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT item_id FROM items WHERE deleted = 1")
        return {int(r["item_id"]) for r in rows}

    def all_items(self, *, include_deleted: bool = False) -> list[Any]:
        rows, _ = self._item_query(include_deleted=include_deleted)
        return rows

    def item_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE deleted = 0"
            ).fetchone()[0]
        )

    # ── 人工干预（append-only）──────────────────────────────────

    def append_edit(
        self, kind: str, payload: dict[str, Any], *, item_id: int | None = None,
        actor: str = "", ts: int = 0, undo_of: int | None = None,
    ) -> int:
        guard_writable(self.conn)
        with transaction(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO edits (kind, item_id, payload, actor, ts, undo_of)
                VALUES (?,?,?,?,?,?)
                """,
                (kind, item_id, json.dumps(payload, ensure_ascii=False),
                 actor, ts, undo_of),
            )
            return int(cur.lastrowid)

    def set_kind(self, item_id: int, kind: str, *, actor: str = "", ts: int = 0) -> int:
        """人工改类目。返回 `edit_id`（撤销的键）。

        ⚠️ **先记事件、再改字段**，且在**同一个事务**里。分开做的话中间崩了
        会留下「改了但撤不回来」。

        ⚠️ 事件里存的 `from` 就是「原来是什么」—— **撤销需要它**。
        """
        row = self.conn.execute(
            "SELECT kind FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"没有这条条目：{item_id}")
        old = row["kind"]
        with transaction(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO edits (kind, item_id, payload, actor, ts)
                VALUES ('set_kind', ?, ?, ?, ?)
                """,
                (item_id, json.dumps({"from": old, "to": kind}, ensure_ascii=False),
                 actor, ts),
            )
            # ⚠️ `kind_locked = 1`：手改过的类目**不被重排覆盖**。
            # 没有它的话，用户改完类目、跑一次重排，改动就被冲掉了 ——
            # 而界面上没有任何提示（静默覆盖是最难发现的一种数据丢失）。
            self.conn.execute(
                "UPDATE items SET kind = ?, kind_locked = 1 WHERE item_id = ?",
                (kind, item_id),
            )
            return int(cur.lastrowid)

    def soft_delete(self, item_id: int, *, actor: str = "", ts: int = 0) -> int:
        """**软**删。返回 `edit_id`。

        ⚠️ 只置标记，不 `DELETE`。行仍在，`item_sources` 也在 —— 所以下一轮
        抽取知道「这条消息已经被处理过」，不会复活它。
        """
        row = self.conn.execute(
            "SELECT deleted FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"没有这条条目：{item_id}")
        with transaction(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO edits (kind, item_id, payload, actor, ts)
                VALUES ('delete', ?, ?, ?, ?)
                """,
                (item_id, json.dumps({"was_deleted": bool(row["deleted"])}), actor, ts),
            )
            self.conn.execute(
                "UPDATE items SET deleted = 1, deleted_at = ? WHERE item_id = ?",
                (ts, item_id),
            )
            return int(cur.lastrowid)

    def last_editable(self) -> dict[str, Any] | None:
        # ⚠️ 排除 `undo` 这一类：撤销**本身**也是一条事件（留痕），
        # 但它不该被「撤最近一步」再拿出来一次 —— 那会让撤销变成
        # 「撤撤销」，而用户看到的是按钮点了没反应。
        row = self.conn.execute(
            "SELECT * FROM edits WHERE undone = 0 AND kind != 'undo' "
            "ORDER BY edit_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def undo(self, edit_id: int | None, *, ts: int = 0) -> bool:
        """撤销一步。

        ⚠️ `edit_id is None` 的语义是「撤**最近一步**」（前端显式发 `null`）。
        """
        if edit_id is None:
            row = self.conn.execute(
                "SELECT * FROM edits WHERE undone = 0 AND kind != 'undo' "
                "ORDER BY edit_id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM edits WHERE edit_id = ? AND undone = 0", (edit_id,)
            ).fetchone()
        if row is None:
            return False

        payload = json.loads(row["payload"] or "{}")
        kind = row["kind"]
        item_id = row["item_id"]

        with transaction(self.conn):
            if kind == "set_kind" and item_id is not None:
                self.conn.execute(
                    "UPDATE items SET kind = ?, kind_locked = 0 WHERE item_id = ?",
                    (payload.get("from"), item_id),
                )
            elif kind == "delete" and item_id is not None:
                self.conn.execute(
                    "UPDATE items SET deleted = 0, deleted_at = NULL WHERE item_id = ?",
                    (item_id,),
                )
            elif kind == "bulk_kind":
                for iid, old in payload.get("pairs") or []:
                    self.conn.execute(
                        "UPDATE items SET kind = ?, kind_locked = 0 WHERE item_id = ?",
                        (old, iid),
                    )
            self.conn.execute(
                "UPDATE edits SET undone = 1 WHERE edit_id = ?", (row["edit_id"],)
            )
            self.conn.execute(
                """
                INSERT INTO edits (kind, item_id, payload, actor, ts, undone, undo_of)
                VALUES ('undo', ?, ?, ?, ?, 1, ?)
                """,
                (item_id, json.dumps({"undid": row["edit_id"]}), row["actor"],
                 ts, row["edit_id"]),
            )
        return True

    def bulk_set_kind(
        self, item_ids: list[int], kind: str, *, actor: str = "", ts: int = 0
    ) -> tuple[str, int]:
        """批量改类目。返回 `(actor, 改了几条)`。

        ⚠️ `actor` 是**撤销的键** —— 前端丢了它就再也撤不回这一批。
        一次提交共用**一个** actor（不是逐条各一个）：逐条记的话，
        「改了 5 条发现错了」要撤 5 次，而中途任何别的操作都会把那一段切开。
        """
        pairs: list[list[Any]] = []
        with transaction(self.conn):
            for iid in item_ids:
                row = self.conn.execute(
                    "SELECT kind FROM items WHERE item_id = ?", (iid,)
                ).fetchone()
                if row is None:
                    continue
                pairs.append([iid, row["kind"]])
                self.conn.execute(
                    "UPDATE items SET kind = ?, kind_locked = 1 WHERE item_id = ?",
                    (kind, iid),
                )
            if not pairs:
                return "", 0
            self.conn.execute(
                """
                INSERT INTO edits (kind, payload, actor, ts)
                VALUES ('bulk_kind', ?, ?, ?)
                """,
                (json.dumps({"pairs": pairs, "to": kind}, ensure_ascii=False),
                 actor, ts),
            )
        return actor, len(pairs)

    def bulk_undo(self, actor: str, *, ts: int = 0) -> tuple[bool, int]:
        row = self.conn.execute(
            """
            SELECT * FROM edits
            WHERE kind = 'bulk_kind' AND actor = ? AND undone = 0
            ORDER BY edit_id DESC LIMIT 1
            """,
            (actor,),
        ).fetchone()
        if row is None:
            return False, 0
        payload = json.loads(row["payload"] or "{}")
        pairs = payload.get("pairs") or []
        with transaction(self.conn):
            for iid, old in pairs:
                self.conn.execute(
                    "UPDATE items SET kind = ?, kind_locked = 0 WHERE item_id = ?",
                    (old, iid),
                )
            self.conn.execute(
                "UPDATE edits SET undone = 1 WHERE edit_id = ?", (row["edit_id"],)
            )
        return True, len(pairs)

    # ── 日报 ────────────────────────────────────────────────────

    def insert_digest(self, digest: Any, item_ids: list[int]) -> int:
        guard_writable(self.conn)
        with transaction(self.conn):
            # ⚠️ 同一天重跑 digest 要**覆盖**，不是插第二篇 —— 否则日报列表里
            # 会出现两篇同一天，而用户不知道该看哪篇。
            self.conn.execute("DELETE FROM digests WHERE day = ?", (digest.day,))
            cur = self.conn.execute(
                """
                INSERT INTO digests
                    (day, window_from, window_to, created_at, body_md, preview, item_count)
                VALUES (?,?,?,?,?,?,?)
                """,
                (digest.day, digest.window_from, digest.window_to, digest.created_at,
                 digest.body_md, digest.preview, digest.item_count),
            )
            did = int(cur.lastrowid)
            for iid in item_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO item_digests (digest_id, item_id) VALUES (?,?)",
                    (did, iid),
                )
            return did

    def list_digests(self, *, limit: int = 50) -> list[Any]:
        # ⚠️ **按窗口倒序** —— 前端「往期」依赖这个顺序（它照列表顺序显示）。
        rows = self.conn.execute(
            "SELECT * FROM digests ORDER BY window_from DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_digest(r) for r in rows]

    def digest_detail(self, digest_id: int) -> tuple[Any, list[Any]] | None:
        row = self.conn.execute(
            "SELECT * FROM digests WHERE digest_id = ?", (digest_id,)
        ).fetchone()
        if row is None:
            return None
        items = self.conn.execute(
            f"""
            SELECT {_ITEM_COLS}
            FROM item_digests d JOIN items i ON i.item_id = d.item_id
            WHERE d.digest_id = ?
            ORDER BY i.event_ts DESC
            """,
            (digest_id,),
        ).fetchall()
        return _row_to_digest(row), [_row_to_item(r) for r in items]

    # ── 作业 ────────────────────────────────────────────────────

    def create_job(self, kind: str, total: int, *, actor: str = "", ts: int = 0) -> str:
        import uuid

        job_id = uuid.uuid4().hex[:12]
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO jobs (job_id, kind, state, total, actor, started_at)
                VALUES (?,?, 'running', ?, ?, ?)
                """,
                (job_id, kind, total, actor, ts),
            )
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "state", "total", "done", "message", "error", "finished_at",
            "cancel_requested",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sql = "UPDATE jobs SET " + ", ".join(f"{k} = ?" for k in sets)
        with transaction(self.conn):
            self.conn.execute(
                sql + " WHERE job_id = ?", list(sets.values()) + [job_id]
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def any_job_running(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE state = 'running' LIMIT 1"
        ).fetchone()
        return row is not None

    def request_cancel(self, job_id: str) -> tuple[bool, str]:
        """请求取消。

        ⚠️ 对**已经结束**的作业返回 `(False, 终态)` —— 那是「手慢了一步」，
        **不是错误**。前端据此显示「它已经结束了，这一步没赶上」。
        """
        row = self.conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"没有这个任务：{job_id}")
        state = row["state"]
        if state != "running":
            return False, state
        self.update_job(job_id, cancel_requested=1)
        return True, "running"

    def cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])

    def finish_job(self, job_id: str, *, state: str, ts: int = 0, **kw: Any) -> None:
        self.update_job(job_id, state=state, finished_at=ts, **kw)

    # ── 杂项 ────────────────────────────────────────────────────

    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM kvs WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO kvs (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def rebuild_fts(self) -> None:
        """重建全文索引。条目写完或删改之后调。"""
        guard_writable(self.conn)
        with transaction(self.conn):
            self.conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")


def _as_ts(v: Any) -> int:
    """`YYYY-MM-DD` 或 Unix 秒 → Unix 秒（按**本地日**起点）。

    ⚠️ 这里也走 `domain.days.Clock`，**不自己算** —— 「本地日」的口径
    全项目只有一处（两边各写一份必漂移）。
    """
    s = str(v).strip()
    if s.isdigit() and len(s) >= 9:
        return int(s)
    from yechao.domain.days import DEFAULT_CLOCK

    if len(s) == 10 and s[4] == "-":
        return DEFAULT_CLOCK.day_start_from_str(s)
    raise StorageError(f"不认识的日期写法：{v!r}（要 `YYYY-MM-DD` 或 Unix 秒）")
