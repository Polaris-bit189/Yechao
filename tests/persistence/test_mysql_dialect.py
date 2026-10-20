"""MySQL 适配器的**静态**验证 —— 这台机器上没有 MySQL，能证明什么、不能证明什么。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：`yechao/infrastructure/persistence/mysql_store.py` 是云上那条路的
实现，而本地跑的一切（含全部测试）都是 SQLite。也就是说，那份代码**从来没被
执行过** —— 它是「写了但没跑」的最典型形状。

**为什么不能靠「本地跑通」**：换一个后端才暴露的问题（占位符写错、忽略重复键
写成了 SQLite 的写法、自增主键拿不到、保留字没转义）在 SQLite 上一个都不会
出现。等它们在云上出现时，代价是**已经在烧的云资源**。

**所以这里的做法**：用一个**假连接**把 `MySQLStore` 的每个方法**真的跑一遍**，
把每条语句记下来，再逐条核方言形状。

## ※ 这个测试证明什么、**不**证明什么（不许假装跑过）

· **证明**：语句是 MySQL 的**形状** —— 占位符是 `%s` 不是 `?`、忽略重复键
  用的是 `INSERT IGNORE`、UPSERT 用的是 `ON DUPLICATE KEY UPDATE`、
  保留字加了反引号、自增主键回填走了 `cursor.lastrowid`、
  建表脚本里没有 `AUTOINCREMENT` / `CREATE INDEX IF NOT EXISTS`。
· **不证明**：MySQL 的**解析器**会接受这些语句。假连接不做语法分析。
  真语法验证需要一台真 MySQL —— 那正是这里没有的东西。

⇒ 报告里如实写了这一条：**没跑过真库**。
"""

from __future__ import annotations

import sys

import pytest

from yechao.domain.errors import ConfigError, ReadOnlyViolation, StorageError
from yechao.infrastructure.persistence import mysql_store
from yechao.infrastructure.persistence.mysql_store import MySQLStore
from yechao.infrastructure.persistence.schema_mysql import SCHEMA_MYSQL


# ── 假连接 ──────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.rowcount = 0
        self.lastrowid = 0
        self._rows: list[dict] = []

    def execute(self, sql, args=None):
        self._conn.log.append((sql, args))
        self.rowcount, self.lastrowid, self._rows = self._conn.respond(sql, args)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self._conn.closed_cursors += 1


class _FakeConn:
    """一个只记账、不解析 SQL 的假连接。

    ※ 它**不是** MySQL。它的作用只有一个：让 `MySQLStore` 的代码路径真的
    跑起来，并把「它到底发了什么语句」留下来。
    """

    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []
        self.began = 0
        self.committed = 0
        self.rolled_back = 0
        self.closed_cursors = 0
        self._seq = 100
        self.edit_row = {
            "edit_id": 1, "kind": "delete", "item_id": 1,
            "payload": '{"was_deleted": false}', "actor": "web",
            "ts": 0, "undone": 0, "undo_of": None,
        }
        self.job_row = {
            "job_id": "abc123", "kind": "reorder", "state": "running",
            "total": 1, "done": 0, "message": "", "error": "",
            "actor": "", "started_at": 0, "finished_at": None,
            "cancel_requested": 0,
        }
        self.digest_row = {
            "digest_id": 1, "day": "2026-10-18", "window_from": 0,
            "window_to": 1, "created_at": 0, "body_md": "# x",
            "preview": "", "item_count": 0,
        }

    def cursor(self):
        return _FakeCursor(self)

    def begin(self):
        self.began += 1

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass

    def ping(self, reconnect: bool = False):
        return True

    def respond(self, sql, args):
        low = " ".join(str(sql).lower().split())
        if low.startswith("insert ignore"):
            # ※ 真实 MySQL 的行为：重复键被忽略 ⇒ 影响 0 行。
            return 1, 0, []
        if low.startswith("insert"):
            self._seq += 1
            return 1, self._seq, []
        if low.startswith("update") or low.startswith("delete"):
            return 1, 0, []
        if "version()" in low:
            return 1, 0, [{"v": "8.0.0-fake"}]
        # 顺序要紧：下面两条也含 `count(*)`，必须排在通用的那条
        # **前面** —— 否则 `counts_by_kind` 会收到 n 而没有 kind，
        # 报 KeyError。第一版就是这么挂的，留个提醒。
        if low.startswith("select `kind`"):
            return 1, 0, [{"kind": "todo", "n": 3}]
        if low.startswith("select `actor_account`"):
            return 1, 0, [{"actor_account": 101, "n": 1}]
        if "count(*) as `n`" in low:
            return 1, 0, [{"n": 3}]
        if "select 1 as `x`" in low:
            return 1, 0, [{"x": 1}]
        if "select distinct `msg_id`" in low:
            return 1, 0, [{"msg_id": 7}]
        if low.startswith("select `deleted` from `items`"):
            return 1, 0, [{"deleted": 0}]
        if low.startswith("select `state` from `jobs`"):
            return 1, 0, [{"state": "running"}]
        if low.startswith("select `cancel_requested` from `jobs`"):
            return 1, 0, [{"cancel_requested": 1}]
        if low.startswith("select `value` from `kvs`"):
            return 1, 0, [{"value": "v"}]
        if low.startswith("select * from `edits`"):
            return 1, 0, [dict(self.edit_row)]
        if low.startswith("select * from `jobs`"):
            return 1, 0, [dict(self.job_row)]
        if low.startswith("select * from `digests`"):
            return 1, 0, [dict(self.digest_row)]
        return 0, 0, []


# ── 驱动 ────────────────────────────────────────────────────────────


def _item(**kw):
    from yechao.domain.models import Item

    base = dict(
        item_id=0, kind="todo", kind_label="", title="交材料", detail=None,
        event_ts=1000, deadline_ts=None, group_id=820000001, group_name="",
        actor=None, actor_account=101, place=None, amount=None, links=(),
        source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def _msg(msg_id: int):
    from yechao.domain.models import PendingMessage

    return PendingMessage(
        msg_id=msg_id, group_id=820000001, ts=1000 + msg_id,
        sender="某人", content="10月28日前提交材料",
    )


def _digest():
    from yechao.domain.models import Digest

    return Digest(
        digest_id=0, day="2026-10-18", window_from=0, window_to=86400,
        created_at=1, body_md="# 日报", item_count=1, preview="日报",
    )


def _run():
    from yechao.domain.errors import LedgerEntry, RunRecord

    return RunRecord(
        run_id="run-1", kind="refine", started_at=1000, finished_at=1001,
        seen=2, produced=1, model_eligible=2, model_sent=2,
        entries=(LedgerEntry(kind="filtered", msg_id=1, group_id=820000001,
                             reason="pure_ack"),),
    )


def _drive_all(store: MySQLStore) -> None:
    """把端口上**每一个**方法都调一遍。

    ※ 空转自检的另一半：只调三个方法就断言「方言对」，等于没验。
    这里覆盖了仓储端口上的全部方法（写、读、事务、作业、日报、KV、
    **失败记账**）。
    """
    from yechao.domain.models import SourceRef

    store.upsert_messages([_msg(1), _msg(2)], run=_run())
    store.pending_messages(since=0, until=9999)
    store.message_by_id(1)
    store.message_count()

    store.insert_items(
        [_item(title="甲"), _item(title="乙")],
        [SourceRef(item_id=0, msg_id=1, quote="q"), SourceRef(item_id=0, msg_id=2, quote="q")],
        run=_run(),
    )
    store.window_items(since=0, until=9999)
    store.items_with_deadline(since=0, until=9999)
    store.search(kinds=["todo"], persons=[101], since="2026-10-01",
                 until="2026-10-31", group=820000001, q="交材料",
                 limit=10, offset=0)
    store.search(q="短")  # 走 LIKE 的那条分支
    store.item_detail(1)
    store.sources_of(1)
    store.counts_by_kind()
    store.counts_by_person()
    store.covered_msg_ids()
    store.deleted_item_ids()
    store.all_items(include_deleted=True)
    store.item_count()
    store.bulk_set_kind([1, 2], "know", actor="bulk:1:2", ts=5)

    store.set_kind(1, "attend", actor="web", ts=6)
    store.soft_delete(1, actor="web", ts=7)
    store.last_editable()
    store.undo(None, ts=8)
    store.undo(1, ts=9)
    store.bulk_undo("bulk:1:2", ts=10)
    store.append_edit("delete", {"a": 1}, item_id=1, actor="web", ts=11)

    store.insert_digest(_digest(), [1, 2], run=_run())
    store.recent_runs(limit=5)
    store.get_run("run-1")
    store.entries_of_run("run-1")
    store.entries_of_message(1)
    store.list_digests(limit=5)
    store.digest_detail(1)

    job_id = store.create_job("reorder", total=2, actor="web", ts=12)
    store.update(job_id, done=1, message="1 批完成")
    store.get_job(job_id)
    store.any_job_running()
    store.request_cancel(job_id)
    store.cancel_requested(job_id)
    store.finish_job(job_id, state="done", ts=13)

    store.get_kv("categories_undo_stack")
    store.set_kv("categories_undo_stack", "{}")
    store.rebuild_fts()
    store.ping()


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def store(conn: _FakeConn) -> MySQLStore:
    return MySQLStore(conn, label="db.example.test:3306/yechao")


# ── 方言判据 ────────────────────────────────────────────────────────


def test_端口上每个方法都真的发过语句(store: MySQLStore, conn: _FakeConn) -> None:
    """※ **空转自检**：下面的方言判据都扫 `conn.log`。

    它空的时候，那些断言全都恒为真 —— 而报告里是绿的。
    """
    _drive_all(store)
    assert len(conn.log) >= 40, (
        f"只记到 {len(conn.log)} 条语句 —— 覆盖不全，方言判据在空转"
    )


def test_每条语句都是_MySQL_方言(store: MySQLStore, conn: _FakeConn) -> None:
    """逐条核：占位符、参数个数、以及 SQLite 专有的写法一个都不许出现。"""
    _drive_all(store)

    sqlite_only = (
        "INSERT OR IGNORE", "ON CONFLICT", "AUTOINCREMENT", "items_fts",
        "PRAGMA", "excluded.", "MATCH",
    )
    offenders: list[str] = []
    for sql, args in conn.log:
        flat = " ".join(sql.split())
        if "?" in flat:
            offenders.append(f"用了 SQLite 的 `?` 占位符：{flat[:90]}")
        if any(marker in flat for marker in sqlite_only):
            hit = next(m for m in sqlite_only if m in flat)
            offenders.append(f"出现了 SQLite 专有写法 {hit!r}：{flat[:90]}")
        if args:
            n = flat.count("%s")
            if n != len(args):
                offenders.append(
                    f"`%s` 有 {n} 个，参数有 {len(args)} 个：{flat[:90]}"
                )
    assert not offenders, "这些语句不是 MySQL 的形状：\n  " + "\n  ".join(offenders)


def test_忽略重复键用的是_INSERT_IGNORE(store: MySQLStore, conn: _FakeConn) -> None:
    """`upsert_messages` 的判据是**幂等**（已存在的按 `msg_id` 跳过）。

    SQLite 的 `INSERT OR IGNORE` 换成 MySQL 的 `INSERT IGNORE` ——
    两者语义一致，但语法不通用。抄一遍就漏了。
    """
    store.upsert_messages([_msg(1)])
    sqls = [" ".join(s.split()) for s, _ in conn.log]
    assert any(s.startswith("INSERT IGNORE INTO `messages`") for s in sqls), sqls


def test_UPSERT_用的是_ON_DUPLICATE_KEY_UPDATE(
    store: MySQLStore, conn: _FakeConn
) -> None:
    """`set_kv` 在 SQLite 上写的是 `ON CONFLICT(key) DO UPDATE SET value =
    excluded.value`；MySQL 那边是 `ON DUPLICATE KEY UPDATE value = VALUES(value)`。

    ※ 抄一遍会漏掉它 —— 而症状是**运行到那一行才报语法错误**。
    """
    store.set_kv("k", "v")
    sql = " ".join(conn.log[-1][0].split())
    assert "ON DUPLICATE KEY UPDATE" in sql, sql
    assert "excluded" not in sql, sql


def test_保留字被反引号包住(store: MySQLStore, conn: _FakeConn) -> None:
    """※ `key` 是 MySQL 保留字（SQLite 里不是）。

    不转义就是语法错误 —— 而它只在云上出现。
    """
    store.set_kv("k", "v")
    store.get_kv("k")
    for sql, _ in conn.log:
        flat = " ".join(sql.split())
        assert "`key`" in flat or "`kvs`" in flat, flat


def test_自增主键回填到真实的_id(store: MySQLStore, conn: _FakeConn) -> None:
    """※ 差异 #2：`pymysql` 必须 `cursor()` 再 `execute`，且 `lastrowid`
    是 DB-API 的属性（不是 sqlite3 那种「execute 直接返回游标」）。

    判据是**行为**：返回的 id 与来源挂的 id 必须**一致**。
    拿不到 `lastrowid` 时来源会挂到 0 上 —— 那是「条目在、来源丢了」的
    静默版本。
    """
    from yechao.domain.models import SourceRef

    ids = store.insert_items(
        [_item(title="甲"), _item(title="乙")],
        [
            SourceRef(item_id=1, msg_id=11, quote="q1"),
            SourceRef(item_id=2, msg_id=22, quote="q2"),
        ],
    )
    assert ids == [101, 102], ids

    inserted = [
        args for sql, args in conn.log
        if "INSERT IGNORE INTO `item_sources`" in " ".join(sql.split())
    ]
    assert [a[0] for a in inserted] == [101, 102], inserted


def test_拿不到自增主键就报错而不是把来源挂到_0() -> None:
    """※ 阴性对照：一个**永远不返回 `lastrowid`** 的连接必须让插入失败。

    没有这条，上面那条判据分不清「回填对了」与「恰好没检查」。
    """

    class _NoInsertId(_FakeConn):
        def respond(self, sql, args):
            rowcount, _lastrowid, rows = super().respond(sql, args)
            return rowcount, 0, rows

    store = MySQLStore(_NoInsertId())
    from yechao.domain.models import SourceRef

    with pytest.raises(StorageError, match="自增主键"):
        store.insert_items(
            [_item()], [SourceRef(item_id=0, msg_id=1, quote="q")]
        )


def test_建表脚本是_MySQL_方言() -> None:
    """建表脚本单独核一遍 —— 它不在 `_drive_all` 的语句流里。

    · 没有 `AUTOINCREMENT`（MySQL 是 `AUTO_INCREMENT`）
    · 没有 `CREATE INDEX IF NOT EXISTS`（MySQL 不支持，索引写在表内）
    · 每张表都显式给了 `utf8mb4`（不写就跟服务端默认走，可能是 latin1）
    """
    script = SCHEMA_MYSQL
    assert "AUTOINCREMENT" not in script
    assert "AUTO_INCREMENT" in script
    assert "CREATE INDEX" not in script
    assert script.count("ENGINE=InnoDB") >= 8
    assert script.count("CHARSET=utf8mb4") >= 8
    for table in ("messages", "items", "item_sources", "digests",
                  "item_digests", "edits", "jobs", "kvs",
                  # ※ 失败记账的两张表也在建表脚本里 —— 漏掉一张的症状是
                  # 「换后端之后 refine 落库报 no such table」。
                  "runs", "run_entries"):
        assert f"CREATE TABLE IF NOT EXISTS `{table}`" in script, table
    # ※ 两个后端对**长度上限**必须一样：SQLite 用 `CHECK`、MySQL 用列宽。
    assert "`reason`   VARCHAR(64)" in script, "reason 的长度上限丢了"
    assert "`run_id`   VARCHAR(64)" in script
    # ※ 没有 FTS 表：MySQL 没有对应物，检索走 LIKE（见 search 的注释）。
    assert "items_fts" not in script


def test_建表脚本每条语句都能被单独执行(store: MySQLStore, conn: _FakeConn) -> None:
    """※ `pymysql` 默认**不开** `CLIENT.MULTI_STATEMENTS`。

    整段带分号的脚本丢过去会被当成一条语句 —— 报出来的是一个指向
    「SQL 语法」的错误，而真正的原因是「它被当成一条了」。
    """
    statements = mysql_store._split_statements(SCHEMA_MYSQL)
    assert len(statements) >= 8
    assert all(s.upper().startswith("CREATE TABLE") for s in statements)
    assert all(";" not in s for s in statements)

    mysql_store.init_db(conn)
    executed = [" ".join(s.split()) for s, _ in conn.log]
    assert len(executed) == len(statements)


# ── 只读 / 事务 ─────────────────────────────────────────────────────


def test_只读实现拒绝写(conn: _FakeConn) -> None:
    """不变量 ① 在 MySQL 上的落点：写之前显式拦一句（真正的守门人是
    `SET SESSION TRANSACTION READ ONLY`，见 `connect_mysql`）。"""
    ro = MySQLStore(conn, readonly=True)
    with pytest.raises(ReadOnlyViolation):
        ro.upsert_messages([_msg(1)])
    with pytest.raises(ReadOnlyViolation):
        ro.set_kv("k", "v")


def test_只读实现仍然能探活(conn: _FakeConn) -> None:
    """※ 阴性对照：只读不该让探活也失败 —— 那会把「只读」变成一个
    「健康检查永远红」的部署。"""
    ro = MySQLStore(conn, readonly=True)
    ok, detail = ro.ping()
    assert ok is True
    assert "8.0.0-fake" in detail


def test_嵌套事务只提交一次(conn: _FakeConn) -> None:
    """※ 差异 #5：SQLite 那边嵌套时内层 `commit()` 会**提前提交外层**。

    MySQL 这边用深度计数做成「整棵嵌套是一个原子单元」——
    提交一次、回滚一次。这条钉住它（判据与代价写在 `_transaction` 的
    docstring 里）。
    """
    store = MySQLStore(conn)
    with store._transaction():
        with store._transaction():
            pass
    assert (conn.began, conn.committed, conn.rolled_back) == (1, 1, 0)


def test_嵌套里抛异常时整棵回滚(conn: _FakeConn) -> None:
    store = MySQLStore(conn)
    with pytest.raises(RuntimeError):
        with store._transaction():
            with store._transaction():
                raise RuntimeError("炸")
    assert (conn.began, conn.committed, conn.rolled_back) == (1, 0, 1)


# ── 连接串 ──────────────────────────────────────────────────────────


def test_连接串拆得出各项() -> None:
    p = mysql_store.parse_mysql_url("mysql://u:p@h.example.test:3307/db1?charset=utf8mb4")
    assert p == {
        "host": "h.example.test", "port": 3307, "user": "u",
        "password": "p", "database": "db1", "charset": "utf8mb4",
    }


def test_密码里的百分号编码会被还原() -> None:
    """※ 含 `@` 的密码必须写成 %40。不还原的话，服务端报的是
    「Access denied」—— 指向「密码错了」，而真正的原因是「没解码」。"""
    p = mysql_store.parse_mysql_url("mysql://u:p%40x%3Ay@h/db")
    assert p["password"] == "p@x:y"


def test_默认端口是_3306() -> None:
    assert mysql_store.parse_mysql_url("mysql://u:p@h/db")["port"] == 3306


@pytest.mark.parametrize(
    "url", ["mysql://h/db", "mysql:///db", "mysql://u:p@h/"]
)
def test_连接串缺项时报错而不是给默认值(url: str) -> None:
    """※ 「善意默认」（用户名默认 root、库名默认 yechao）的症状是
    连上了一个**别的库** —— 而那看起来是成功的。"""
    with pytest.raises(ConfigError, match="缺了"):
        mysql_store.parse_mysql_url(url)


def test_非_mysql_的连接串被拒() -> None:
    with pytest.raises(ConfigError, match="不是一个 MySQL 连接串"):
        mysql_store.parse_mysql_url("postgres://u:p@h/db")


def test_没装_pymysql_时报错而不是回落(monkeypatch) -> None:
    """※ 这一条是整份文件里最要紧的一条。

    `sys.modules["pymysql"] = None` 让 `import pymysql` 抛 `ImportError`
    （这是 CPython 的既定行为）。此时**必须报错**，而不是：
      · 悄悄换成 SQLite（用户以为连上了云库，其实写在本地文件里）
      · 悄悄换成内存库（更糟）
    两者的共同点是**看起来是成功的**。
    """
    monkeypatch.setitem(sys.modules, "pymysql", None)
    with pytest.raises(ConfigError, match="pip install pymysql"):
        mysql_store.connect_mysql("mysql://u:p@h.example.test/db")
