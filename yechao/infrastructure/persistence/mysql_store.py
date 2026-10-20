"""MySQL 仓储 —— 与 `sqlite_store.py` 实现**同一组端口**。

## 它为什么存在

`sqlite_store` 是单机默认；云上要挂 RDS。两者是**同一个端口的两个实现**，
所以用例层一行都不用改（`bootstrap` 决定装配哪一个）。

## ※ 这份文件里没有任何一行是「把 SQLite 的 SQL 抄一遍」

方言差异逐条列在下面，每一条都写了**观察到什么、为什么这么定**：

| # | 差异 | SQLite | MySQL | 不改会怎样 |
|---|---|---|---|---|
| 1 | 占位符 | `?` | `%s` | 语法错误 |
| 2 | 自增回填 | `conn.execute(...)` 直接给游标 | 必须 `cursor()` 再 `execute` | `AttributeError` |
| 3 | 忽略重复键 | `INSERT OR IGNORE` | `INSERT IGNORE` | 语法错误 |
| 4 | UPSERT | `ON CONFLICT(k) DO UPDATE` | `ON DUPLICATE KEY UPDATE` | 语法错误 |
| 5 | 事务 | 隐式开始，`commit()` | `begin()` / `commit()` / `rollback()` | 嵌套时静默早提交（见 `_transaction`） |
| 6 | 连接池 | 每线程一条连接 | 每线程一条连接 + `ping(reconnect=True)` | 连接被服务端 `wait_timeout` 掐掉后，**第一条语句才报错** |
| 7 | 只读连接 | `PRAGMA query_only` | `SET SESSION TRANSACTION READ ONLY` | 不变量 ① 在云上失效，而且**没有声音** |
| 8 | 全文检索 | FTS5 trigram | 无对应物 ⇒ 走 `LIKE` | 见 `search` 的注释（这是取舍，不是遗漏） |
| 9 | 行对象 | `sqlite3.Row` | `DictCursor` | 映射已共用（`rows.py`），两边都按列名取 |

## ※ 这份实现**没有连过真 MySQL**

这台机器上没有 MySQL，也没有装 `pymysql`。所以：

· `pymysql` 是**延迟导入**的（`connect_mysql` 里才 import）—— 没有它时
  **报错并说清怎么装**，而不是回落成 SQLite（回落是最坏的一种：用户以为
  连上了云库，其实写在本地文件里，而它**看起来是成功的**）。
· 对它的验证是**静态的**：`tests/persistence/test_mysql_dialect.py` 用一个
  假连接把每条语句**真的跑出来**，再逐条核方言形状（占位符、忽略重复键、
  UPSERT 写法、保留字转义、自增回填）。
· ※ 那个测试证明的是「方言形状对」，**不是**「MySQL 的解析器会接受它」。
  这两件事不一样，报告里如实写了。
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

from yechao.domain.errors import (
    ConfigError,
    NotFound,
    ReadOnlyViolation,
    StorageError,
)
from yechao.infrastructure.persistence.rows import (
    ENTRY_COLS,
    ITEM_COLS,
    RUN_COLS,
    as_ts,
    row_to_digest,
    row_to_entry,
    row_to_item,
    row_to_message,
    row_to_run,
)
from yechao.infrastructure.persistence.schema_mysql import SCHEMA_MYSQL

#: 支持的连接串 scheme。**只认这两种** —— 认不出的报错，不猜。
SUPPORTED_SCHEMES = ("sqlite", "mysql")

#: ※ 网络超时。RDS 走的是**网络**，默认「永远等」会让一个网络问题表现为
#: 「网页一直转圈」——那是「失败看起来像卡住」，比失败更难查。
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
WRITE_TIMEOUT = 30


# ── 连接串 ──────────────────────────────────────────────────────────


def parse_mysql_url(url: str) -> dict[str, Any]:
    """`mysql://user:pass@host:3306/dbname?charset=utf8mb4` → 连接参数。

    纯函数（不连库），所以「连接串写错了」这件事可以被单独测。

    ※ 缺哪一项就报哪一项，**不给默认值**。理由：`user` 默认成 `root`、
    `database` 默认成 `yechao` 这类「善意默认」的症状是连上了一个**别的库**
    —— 而那看起来是成功的，直到有人发现数据不对。
    """
    parts = urlsplit(url)
    if parts.scheme != "mysql":
        raise ConfigError(
            f"这不是一个 MySQL 连接串：{url.split('://')[0]}://…"
            f"（要 mysql://user:pass@host:3306/dbname）"
        )
    host = parts.hostname or ""
    user = unquote(parts.username or "")
    # ※ 密码要 unquote：连接串里的 `@` `:` `/` 必须写成 %40 %3A %2F，
    # 不还原的话，一个含 `@` 的密码会被原样发给服务端 ⇒ 认证失败，
    # 而错误信息是「Access denied」，指向的是「密码错了」而不是「没解码」。
    password = unquote(parts.password or "")
    database = (parts.path or "").lstrip("/")
    missing = [
        name
        for name, val in (
            ("主机", host), ("用户名", user), ("库名", database)
        )
        if not val
    ]
    if missing:
        raise ConfigError(
            "MySQL 连接串缺了：" + "、".join(missing)
            + "。形状是 mysql://user:pass@host:3306/dbname"
        )
    query = {
        kv.split("=", 1)[0]: kv.split("=", 1)[1]
        for kv in parts.query.split("&")
        if "=" in kv
    }
    return {
        "host": host,
        "port": int(parts.port or 3306),
        "user": user,
        "password": password,
        "database": database,
        "charset": query.get("charset", "utf8mb4"),
    }


def connect_mysql(url: str, *, readonly: bool = False) -> Any:
    """按连接串开一条 MySQL 连接。

    ※ `pymysql` **在这里才 import**：没装它时给一句能照着做的提示，
    而不是让 `import yechao` 本身就崩（那样连 SQLite 路径都用不了）。
    """
    params = parse_mysql_url(url)
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as e:  # pragma: no cover - 依赖缺失时才走
        raise ConfigError(
            "要用 MySQL 得先装驱动：`pip install pymysql`（或 "
            "`pip install \"yechao[mysql]\"`）。"
            "※ 装了它之前，程序**不会**退回 SQLite —— 那会让你以为连上了云库。"
        ) from e

    try:
        conn = pymysql.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            database=params["database"],
            charset=params["charset"],
            cursorclass=DictCursor,
            # ※ autocommit 关掉：事务由 `_transaction()` 显式管。
            # 开着它的话，`insert_items` 的「条目 + 来源 + 记账」会被拆成
            # 三条各自提交的语句 —— 崩溃时留下「写了条目、没记账」，
            # 下一轮重抽产出重复条目，而且无声。
            autocommit=False,
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
        )
    except Exception as e:
        raise StorageError(
            f"连不上 MySQL（{params['host']}:{params['port']}/"
            f"{params['database']}）：{type(e).__name__}: {e}。"
            f"检查 YECHAO_DB_URL、RDS 白名单与安全组。"
        ) from e

    if readonly:
        # ※ 不变量 ① 在 MySQL 上的落点：让**数据库自己**拒绝写。
        # 与 SQLite 的 `PRAGMA query_only` 是同一个角色 —— 靠代码自觉的
        # 约束迟早被绕开，而绕开时没有声音。
        cur = conn.cursor()
        try:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cur.close()
        conn.commit()
    return conn


def init_db(conn: Any) -> None:
    """建表。**幂等**（`CREATE TABLE IF NOT EXISTS`）。

    ※ 与 SQLite 那边的判据不同，这里**每次启动都跑**：

      · SQLite 那边「库文件在不在」本身就说明了建没建过 —— 所以只在文件
        不存在时建。
      · MySQL 那边没有「文件」这个概念，第一次连上去的是一个**空库**
        （RDS 实例刚开通）。所以建表语句要每次执行一遍，靠 `IF NOT EXISTS`
        保证重复执行安全。

    ※ 逐条执行而不是整段丢过去：`pymysql` 默认**不开**
    `CLIENT.MULTI_STATEMENTS`，整段带分号的脚本会被当成一条语句，
    报一个指向「SQL 语法」的错误 —— 而真正的原因是「它被当成一条了」。
    """
    cur = conn.cursor()
    try:
        for stmt in _split_statements(SCHEMA_MYSQL):
            cur.execute(stmt)
    finally:
        cur.close()
    conn.commit()


def _split_statements(script: str) -> list[str]:
    """把建表脚本切成单条语句。先丢掉整行注释，再按 `;` 切。

    ※ **为什么要丢注释**：脚本里那些 `-- ※ …` 是写给读代码的人的，
    而它们会黏在**下一条语句**前面。一起发过去 MySQL 是无害的（它认
    `--` 注释），但「这条语句以 `CREATE TABLE` 开头」就不成立了 ——
    而那条判据不是形式主义：它保证 `init_db` 真的在执行建表语句，
    而不是在执行一段注释。

    ※ 只对**这份脚本**成立（里面没有字符串字面量含 `;` 或 `--`、
    没有触发器、没有 `BEGIN...END`）。换成任意 SQL 就得用真正的解析器
    —— 这条注释是为了拦住「以后往里加一条带分号的语句」。
    """
    body = "\n".join(
        ln for ln in script.splitlines() if not ln.strip().startswith("--")
    )
    return [s.strip() for s in body.split(";") if s.strip()]


# ── 仓储 ────────────────────────────────────────────────────────────


class MySQLStore:
    """MySQL 上的仓储实现。

    ## 连接策略：**每线程一条连接**，不是连接池

    观察到的现象：`sqlite_store` 因为「一个连接被多个线程同时用」炸过一次
    （HTTP 端点在 FastAPI 的线程池里跑）。MySQL 这边同样的问题会更早出现 ——
    而且多一层：**服务端会掐空闲连接**（`wait_timeout`，RDS 默认常见 8 小时，
    也可以配成分钟级）。

    为什么不做成「有上限、有淘汰」的真连接池：`pymysql` 没有内置池，
    要真做就得再引一个依赖（DBUtils / SQLAlchemy）。本项目对依赖的态度是
    「clone 下来就能跑」优先，而这里的并发规模（FastAPI 默认线程池）远没有
    大到需要淘汰策略。

    ⇒ 采用与 SQLite 相同的形状：线程本地连接 + **每次取用时 `ping(reconnect=True)`**。
    后者是关键：没有它，一条被服务端掐掉的连接会在**下一条语句**上抛
    `(2006, 'MySQL server has gone away')`，而那个错指向「网络/权限」，
    排查方向完全错。
    """

    def __init__(
        self,
        conn: Any = None,
        *,
        factory: Any = None,
        readonly: bool = False,
        label: str = "",
    ) -> None:
        self._conn = conn
        self._factory = factory
        self._readonly = readonly
        #: ※ 只放**非机密**的东西（`host:port/db`）。它会出现在
        #: `/api/health` 的响应里 —— 那里面有密码的连接串不该进这里。
        self._label = label
        self._local = threading.local()

    # ── 连接 ────────────────────────────────────────────────────

    @property
    def conn(self) -> Any:
        if self._factory is None:
            assert self._conn is not None
            return self._conn
        cur = getattr(self._local, "conn", None)
        if cur is None:
            cur = self._factory()
            self._local.conn = cur
        else:
            # ※ 复用的连接先探活。`reconnect=True` 时它自己重连 ——
            # 静默重连是刻意的：这一层的契约是「给我一条能用的连接」，
            # 而「刚才那条被服务端掐了」不是调用方要处理的事。
            try:
                cur.ping(reconnect=True)
            except Exception as e:
                raise StorageError(f"MySQL 连接不可用：{e}") from e
        return cur

    def backend_name(self) -> str:
        """`/api/health` 用它回答「用的是什么」。"""
        return "mysql"

    def ping(self) -> tuple[bool, str]:
        """探活。**不抛** —— 探活本身失败要能被报出来，而不是把探活的人打倒。"""
        try:
            row = self._one("SELECT VERSION() AS `v`")
        except Exception as e:
            return False, f"MySQL 连不上：{type(e).__name__}: {e}"
        version = (row or {}).get("v") or "?"
        where = f"{self._label}，" if self._label else ""
        return True, f"MySQL 可读（{where}服务端版本 {version}）"

    def close(self) -> None:
        for conn in (self._conn, getattr(self._local, "conn", None)):
            if conn is None:
                continue
            try:
                conn.close()
            except Exception:
                pass

    # ── 低层执行 ────────────────────────────────────────────────

    def _guard_writable(self) -> None:
        """写之前显式检查一句，让失败**指向真正的原因**。

        ※ 不靠它来守 —— 守的是 MySQL 自己（`SET SESSION TRANSACTION
        READ ONLY`）。这一句只是为了让错误信息说清「这个连接不该写」，
        而不是 MySQL 那句 `Cannot execute statement in a READ ONLY
        transaction`（那可能被误读成「库的权限配错了」）。
        """
        if self._readonly:
            raise ReadOnlyViolation(
                "这个连接是只读打开的；写主库的活由 CLI / 用例那条路走。"
            )

    def _exec(self, sql: str, args: Any = None) -> tuple[int, int]:
        """执行一条语句，返回 `(影响行数, 自增主键)`。

        ※ 与 SQLite 的差别（差异 #2）：`sqlite3` 的 `conn.execute(...)`
        直接返回游标，所以那边一行就能拿到 `lastrowid`；`pymysql` 必须
        `conn.cursor()` 再 `execute`，**且游标要自己关**。
        """
        cur = self.conn.cursor()
        try:
            cur.execute(sql, args if args is not None else ())
            return int(cur.rowcount), int(cur.lastrowid or 0)
        finally:
            cur.close()

    def _rows(self, sql: str, args: Any = None) -> list[Any]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, args if args is not None else ())
            return list(cur.fetchall())
        finally:
            cur.close()

    def _one(self, sql: str, args: Any = None) -> Any:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, args if args is not None else ())
            return cur.fetchone()
        finally:
            cur.close()

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """一个事务。

        ## ※ 差异 #5：嵌套时**不**提前提交

        SQLite 那边（`sqlite_store.transaction`）嵌套时，内层的 `commit()`
        会把外层还没做完的活**一起提交**掉 —— 那是 sqlite3 模块的行为，
        不是谁选的。

        MySQL 这边用一个**深度计数**把它做成「整棵嵌套是一个原子单元」：
        只有最外层提交/回滚。这是刻意的，理由：

          · 调用方看到的是一个 `with` 块，期望的是「要么都成、要么都不成」；
          · `request_cancel` → `update` 就是一处真实的嵌套。SQLite 那边
            它恰好没出事（外层没有别的写），但那是运气，不是设计。
          · 差异的方向是**更安全**的那一侧：多回滚不会留下半截数据，
            早提交会。

        ※ 已知代价：两个后端在「嵌套事务中途失败」这一种输入上行为不同。
        这一条写在这里而不是藏起来 —— 两个实现行为不一致时，不一致本身
        就是文档的一部分。
        """
        self._guard_writable()
        conn = self.conn
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            conn.begin()
        self._local.depth = depth + 1
        try:
            yield conn
        except Exception:
            self._local.depth = depth
            if depth == 0:
                conn.rollback()
            raise
        else:
            self._local.depth = depth
            if depth == 0:
                conn.commit()

    # ── 消息 ────────────────────────────────────────────────────

    def upsert_messages(self, messages: list[Any], *, run: Any = None) -> int:
        """写入消息。带上 `run` 时，这一轮的账与消息**同一个事务**。

        ※ `messages` 允许为空：一轮「一个源都没读到」的摄取也必须留下账。
        """
        self._guard_writable()
        n = 0
        with self._transaction():
            self._write_run(run)
            for m in messages:
                # ※ 差异 #3：`INSERT OR IGNORE` → `INSERT IGNORE`。
                # 两者语义一致（重复主键跳过），但语法不通用。
                # 返回值也一致：忽略了就是 0 行。
                rowcount, _ = self._exec(
                    "INSERT IGNORE INTO `messages` "
                    "(`msg_id`, `group_id`, `ts`, `sender`, `content`, "
                    " `actor_account`, `source`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (m.msg_id, m.group_id, m.ts, m.sender, m.content,
                     m.actor_account, m.source),
                )
                n += rowcount
        return n

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        sql = "SELECT * FROM `messages` m WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            sql += " AND m.ts >= %s"
            args.append(since)
        if until is not None:
            sql += " AND m.ts < %s"
            args.append(until)
        sql += " ORDER BY m.group_id, m.ts, m.msg_id"
        return [row_to_message(r) for r in self._rows(sql, args)]

    def message_by_id(self, msg_id: int) -> Any | None:
        row = self._one(
            "SELECT * FROM `messages` WHERE `msg_id` = %s", (msg_id,)
        )
        return row_to_message(row) if row else None

    def message_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS `n` FROM `messages`")
        return int(row["n"])

    # ── 失败记账 ────────────────────────────────────────────────

    def _write_run(self, run: Any) -> None:
        """把一次跑的抬头与它全部的账写进**当前事务**。

        ※ 与 SQLite 那份**同名同义**（判据与推导写在
        `sqlite_store._write_run` 的 docstring 里，不在这里重抄一遍）。
        两边只有方言差异：占位符 `%s`、`self._exec` 而不是
        `self.conn.execute`。

        ※ MySQL 这边**嵌套事务是安全的**（`_transaction` 用深度计数，
        只有最外层提交）—— 但这里仍然不开事务，理由是两个后端**必须
        长得一样**：一个后端在事务里、另一个在事务外，症状是「换后端之后
        账与条目不再同生共死」，而那要到云上才出现。
        """
        if run is None:
            return
        self._exec(
            "INSERT INTO `runs` ("
            "`run_id`, `kind`, `started_at`, `finished_at`, `status`, `seen`, "
            "`produced`, `model_eligible`, `model_sent`) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run.run_id, run.kind, run.started_at, run.finished_at,
                run.status, run.seen, run.produced,
                run.model_eligible, run.model_sent,
            ),
        )
        for e in run.entries:
            self._exec(
                "INSERT INTO `run_entries` ("
                "`run_id`, `kind`, `msg_id`, `group_id`, `batch_no`, "
                "`item_id`, `reason`) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    run.run_id, e.kind, e.msg_id, e.group_id,
                    e.batch_no, e.item_id, e.reason,
                ),
            )

    def recent_runs(self, *, limit: int = 20) -> list[Any]:
        """最近跑过的若干次，最新的在前。**只读**（`serve` 那侧也调它）。"""
        rows = self._rows(
            f"SELECT {RUN_COLS} FROM `runs` r "
            "ORDER BY r.`started_at` DESC, r.`run_id` DESC LIMIT %s",
            (int(limit),),
        )
        return [row_to_run(r) for r in rows]

    def get_run(self, run_id: str) -> Any | None:
        """按 id 取抬头。**没有返回 `None`** —— 与「这一轮没有账」分得开。"""
        row = self._one(
            f"SELECT {RUN_COLS} FROM `runs` r WHERE r.`run_id` = %s", (run_id,)
        )
        return row_to_run(row) if row else None

    def entries_of_run(self, run_id: str) -> list[Any]:
        """一次跑的全部账，按记录顺序。"""
        rows = self._rows(
            f"SELECT {ENTRY_COLS} FROM `run_entries` e "
            "WHERE e.`run_id` = %s ORDER BY e.`entry_id`",
            (run_id,),
        )
        return [row_to_entry(r) for r in rows]

    def entries_of_message(self, msg_id: int, *, limit: int = 50) -> list[Any]:
        """一条消息的账，最新的在前 —— 「这条为什么没进条目」的答案。"""
        rows = self._rows(
            f"SELECT {ENTRY_COLS} FROM `run_entries` e "
            "WHERE e.`msg_id` = %s ORDER BY e.`entry_id` DESC LIMIT %s",
            (int(msg_id), int(limit)),
        )
        return [row_to_entry(r) for r in rows]

    # ── 条目 ────────────────────────────────────────────────────

    def insert_items(
        self, items: list[Any], sources: list[Any], *, run: Any = None
    ) -> list[int]:
        """**条目 + 来源 + 记账，同一个事务。**

        契约与 SQLite 那边逐字一致（见 `sqlite_store.insert_items` 的
        docstring）：来源用 `SourceRef.item_id` 挂，`0` 表示「随条目顺序
        自动分配」。这里**不重写**那套逻辑，只换方言 —— 因为那套逻辑是
        「跨群转发合并之后一条条目挂多条来源」这个领域事实决定的，
        与用哪个数据库无关。
        """
        self._guard_writable()

        explicit = [s for s in sources if getattr(s, "item_id", 0)]
        implicit = [s for s in sources if not getattr(s, "item_id", 0)]
        if len(implicit) > len(items):
            raise StorageError(
                f"没有指定 item_id 的来源比条目多：{len(implicit)} > {len(items)}。"
                f"调用方把两个列表配错了 —— 继续下去会把来源挂到错的条目上。"
            )

        ids: list[int] = []
        with self._transaction():
            # ※ 账在**这个**事务里，与条目/来源同生共死（判据见
            # `ports.ItemRepository.insert_items`）。`items` 为空时循环
            # 不跑，账照样落。
            self._write_run(run)

            for it in items:
                _, new_id = self._exec(
                    "INSERT INTO `items` ("
                    "`kind`, `title`, `detail`, `event_ts`, `deadline_ts`, "
                    "`group_id`, `actor`, `actor_account`, `place`, `amount`, "
                    "`links`, `start_ts`, `deadline_supported`, "
                    "`start_supported`, `source_count`, `created_at`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        it.kind, it.title, it.detail, it.event_ts,
                        it.deadline_ts, it.group_id, it.actor, it.actor_account,
                        it.place, it.amount,
                        json.dumps(list(it.links), ensure_ascii=False),
                        it.start_ts, int(it.deadline_supported),
                        int(it.start_supported), int(it.source_count or 0),
                        it.event_ts,
                    ),
                )
                if not new_id:
                    # ※ 拿不到自增主键就**不能往下走**：来源会挂到 0 上，
                    # 而那是「来源丢了」的静默版本（条目在，来源表里没有它）。
                    raise StorageError(
                        "插入条目之后没拿到自增主键 —— 来源会挂错。"
                        "（MySQL 的 `lastrowid` 只有 INSERT 之后才有值。）"
                    )
                ids.append(new_id)

            # ※ MySQL 没有 FTS5，所以这里**没有** items_fts 的增量写。
            # `search` 的 q 分支走 LIKE —— 见那里的注释。

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
                self._exec(
                    "INSERT IGNORE INTO `item_sources` "
                    "(`item_id`, `msg_id`, `quote`) VALUES (%s,%s,%s)",
                    (new_id, src.msg_id, src.quote),
                )

            for src, new_id in zip(implicit, ids):
                self._exec(
                    "INSERT IGNORE INTO `item_sources` "
                    "(`item_id`, `msg_id`, `quote`) VALUES (%s,%s,%s)",
                    (new_id, src.msg_id, src.quote),
                )

            for new_id in ids:
                # ※ 子查询查的是**另一张表**（item_sources），所以 MySQL
                # 允许这么写（`UPDATE t SET x = (SELECT ... FROM t)` 才需要
                # 绕一层派生表）。
                self._exec(
                    "UPDATE `items` SET `source_count` = ("
                    "  SELECT COUNT(*) FROM `item_sources` s "
                    "  WHERE s.item_id = %s) WHERE `item_id` = %s",
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

        total_row = self._one(
            f"SELECT COUNT(*) AS `n` FROM `items` i {where}", args
        )
        sql = (
            f"SELECT {ITEM_COLS} FROM `items` i {where} "
            "ORDER BY i.event_ts DESC, i.item_id DESC"
        )
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            args = args + [int(limit), int(offset)]
        return [row_to_item(r) for r in self._rows(sql, args)], int(
            total_row["n"]
        )

    def window_items(self, *, since: int, until: int) -> list[Any]:
        rows, _ = self._item_query(
            extra="AND i.event_ts >= %s AND i.event_ts < %s", args=[since, until]
        )
        return rows

    def items_with_deadline(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        extra = "AND i.deadline_ts IS NOT NULL"
        args: list[Any] = []
        # ※ 与 `search` 同一个口径：日期串与 Unix 秒都认（见那边的注释）。
        if since is not None:
            extra += " AND i.deadline_ts >= %s"
            args.append(as_ts(since))
        if until is not None:
            extra += " AND i.deadline_ts < %s"
            args.append(as_ts(until))
        rows, _ = self._item_query(extra=extra, args=args)
        return rows

    def search(self, **filters: Any) -> tuple[list[Any], int]:
        """查条目。返回 `(本页, 总数)`。

        筛选口径与 SQLite 那边**逐条对齐**（维度内并集、维度之间交集、
        `persons` 不命中匿名条目）—— 两个后端对同一个请求必须给同一个答案。

        ## ※ 唯一一处**行为不同**：`q`

        SQLite 那边 `len(q) >= 3` 走 FTS5 trigram（有索引、快），
        `< 3` 走 `LIKE`。MySQL 这边**一律走 `LIKE`**，因为：

          · MySQL 没有 trigram 分词器。它的全文索引（FULLTEXT）要用
            `WITH PARSER ngram`，而 `ngram_token_size` 是**服务端变量**
            （默认 2，RDS 上通常不让改）—— 查询长度与分词尺寸对不上时，
            它会**静默地什么都搜不到**（不是报错）。
          · 所以「有索引但可能静默失效」换成了「没索引但结果一定对」。
            代价是大表上慢（全表扫描），而演示规模下无感。

        判据（为什么这么定）：搜索框的正确性 > 大表上的速度，因为
        「搜不到」和「没有」长得一模一样 —— 那正是本项目最怕的一类失败。

        ※ 已知且**两边一致**的小缺陷：用户输入里的 `%` / `_` 会被
        LIKE 当成通配符（搜 `50%` 会匹配更多）。只在一侧修会让两个后端
        给出不同答案，比两边都错更难查 —— 所以这里保持与 SQLite 一致，
        单独留这条注释而不是偷偷改一边。
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
            extra += f" AND i.kind IN ({','.join(['%s'] * len(kinds))})"
            args.extend(kinds)
        if persons:
            extra += f" AND i.actor_account IN ({','.join(['%s'] * len(persons))})"
            args.extend(persons)
        if since:
            extra += " AND i.event_ts >= %s"
            args.append(as_ts(since))
        if until:
            extra += " AND i.event_ts < %s"
            args.append(as_ts(until) + 86400)
        if group is not None:
            extra += " AND i.group_id = %s"
            args.append(int(group))

        if q:
            # ※ 剥控制字符：NUL 会把 LIKE 的 pattern 截断成 `%` ——
            # 那是 SQL 层的怪行为，症状指向「查不到」而不是「输入有问题」。
            clean = "".join(ch for ch in str(q) if ch.isprintable())
            if clean:
                extra += " AND (i.title LIKE %s OR IFNULL(i.detail,'') LIKE %s)"
                # ※ `%` 在**参数里**，不在 SQL 文本里 —— 所以不用写成
                # `%%`（`%%` 只用于 SQL 文本里字面量的百分号）。
                args.extend([f"%{clean}%", f"%{clean}%"])

        rows, total = self._item_query(
            extra=extra, args=args, limit=limit, offset=offset
        )
        return rows, total

    def item_detail(self, item_id: int) -> Any | None:
        row = self._one(
            f"SELECT {ITEM_COLS} FROM `items` i "
            "WHERE i.item_id = %s AND i.deleted = 0",
            (item_id,),
        )
        return row_to_item(row) if row else None

    def sources_of(self, item_id: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT m.msg_id, m.ts, m.sender, m.group_id, m.content, s.quote "
            "FROM `item_sources` s JOIN `messages` m ON m.msg_id = s.msg_id "
            "WHERE s.item_id = %s ORDER BY m.ts",
            (item_id,),
        )
        # ※ DictCursor 出来的**已经是 dict**；`dict(r)` 是防御性的：
        # 换游标类（比如 `SSDictCursor`）时它照样成立。
        return [dict(r) for r in rows]

    def counts_by_kind(self) -> dict[str, int]:
        rows = self._rows(
            "SELECT `kind`, COUNT(*) AS `n` FROM `items` "
            "WHERE `deleted` = 0 GROUP BY `kind`"
        )
        return {r["kind"]: int(r["n"]) for r in rows}

    def counts_by_person(self) -> dict[int, int]:
        rows = self._rows(
            "SELECT `actor_account`, COUNT(*) AS `n` FROM `items` "
            "WHERE `deleted` = 0 AND `actor_account` IS NOT NULL "
            "GROUP BY `actor_account`"
        )
        return {int(r["actor_account"]): int(r["n"]) for r in rows}

    def covered_msg_ids(self) -> set[int]:
        """已经被引用过的消息 id —— **含被软删的条目**。

        两半都必须（理由与 SQLite 那边逐字相同）：含正常条目 ⇒ 重跑幂等；
        含软删条目 ⇒ 用户删掉的不会被下一轮复活。
        """
        rows = self._rows("SELECT DISTINCT `msg_id` FROM `item_sources`")
        return {int(r["msg_id"]) for r in rows}

    def deleted_item_ids(self) -> set[int]:
        rows = self._rows("SELECT `item_id` FROM `items` WHERE `deleted` = 1")
        return {int(r["item_id"]) for r in rows}

    def all_items(self, *, include_deleted: bool = False) -> list[Any]:
        rows, _ = self._item_query(include_deleted=include_deleted)
        return rows

    def item_count(self) -> int:
        row = self._one(
            "SELECT COUNT(*) AS `n` FROM `items` WHERE `deleted` = 0"
        )
        return int(row["n"])

    # ── 人工干预（append-only）──────────────────────────────────

    def append_edit(
        self, kind: str, payload: dict[str, Any], *, item_id: int | None = None,
        actor: str = "", ts: int = 0, undo_of: int | None = None,
    ) -> int:
        self._guard_writable()
        with self._transaction():
            _, edit_id = self._exec(
                "INSERT INTO `edits` "
                "(`kind`, `item_id`, `payload`, `actor`, `ts`, `undo_of`) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (kind, item_id, json.dumps(payload, ensure_ascii=False),
                 actor, ts, undo_of),
            )
            return edit_id

    def set_kind(
        self, item_id: int, kind: str, *, actor: str = "", ts: int = 0
    ) -> int:
        """人工改类目。返回 `edit_id`。

        ※ **先记事件、再改字段**，同一个事务（与 SQLite 那边同一条判据：
        分开做的话中间崩了会留下「改了但撤不回来」）。
        """
        row = self._one(
            "SELECT `kind` FROM `items` WHERE `item_id` = %s", (item_id,)
        )
        if row is None:
            raise NotFound(f"没有这条条目：{item_id}")
        old = row["kind"]
        with self._transaction():
            _, edit_id = self._exec(
                "INSERT INTO `edits` "
                "(`kind`, `item_id`, `payload`, `actor`, `ts`) "
                "VALUES ('set_kind', %s, %s, %s, %s)",
                (item_id, json.dumps({"from": old, "to": kind}, ensure_ascii=False),
                 actor, ts),
            )
            self._exec(
                "UPDATE `items` SET `kind` = %s, `kind_locked` = 1 "
                "WHERE `item_id` = %s",
                (kind, item_id),
            )
            return edit_id

    def soft_delete(self, item_id: int, *, actor: str = "", ts: int = 0) -> int:
        """**软**删。返回 `edit_id`。只置标记，不 `DELETE`。"""
        row = self._one(
            "SELECT `deleted` FROM `items` WHERE `item_id` = %s", (item_id,)
        )
        if row is None:
            raise NotFound(f"没有这条条目：{item_id}")
        with self._transaction():
            _, edit_id = self._exec(
                "INSERT INTO `edits` "
                "(`kind`, `item_id`, `payload`, `actor`, `ts`) "
                "VALUES ('delete', %s, %s, %s, %s)",
                (item_id, json.dumps({"was_deleted": bool(row["deleted"])}),
                 actor, ts),
            )
            self._exec(
                "UPDATE `items` SET `deleted` = 1, `deleted_at` = %s "
                "WHERE `item_id` = %s",
                (ts, item_id),
            )
            return edit_id

    def last_editable(self) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM `edits` WHERE `undone` = 0 AND `kind` != 'undo' "
            "ORDER BY `edit_id` DESC LIMIT 1"
        )
        return dict(row) if row else None

    def undo(self, edit_id: int | None, *, ts: int = 0) -> bool:
        """撤销一步。语义与 SQLite 那边逐字相同（含 `was_deleted` 那条判据）。"""
        if edit_id is None:
            row = self._one(
                "SELECT * FROM `edits` WHERE `undone` = 0 AND `kind` != 'undo' "
                "ORDER BY `edit_id` DESC LIMIT 1"
            )
        else:
            row = self._one(
                "SELECT * FROM `edits` WHERE `edit_id` = %s AND `undone` = 0",
                (edit_id,),
            )
        if row is None:
            return False

        payload = json.loads(row["payload"] or "{}")
        kind = row["kind"]
        item_id = row["item_id"]

        with self._transaction():
            if kind == "set_kind" and item_id is not None:
                self._exec(
                    "UPDATE `items` SET `kind` = %s, `kind_locked` = 0 "
                    "WHERE `item_id` = %s",
                    (payload.get("from"), item_id),
                )
            elif kind == "delete" and item_id is not None:
                # ※ 用事件里存的 `was_deleted`，**不是**无条件置 0 ——
                # 连点两次删除之后只撤第二条时，条目本来就该还是删着的。
                # （判据与推导过程见 `sqlite_store.undo` 的注释，这里是它的
                #  MySQL 版，行为必须一致。）
                if bool(payload.get("was_deleted")):
                    self._exec(
                        "UPDATE `items` SET `deleted` = 1 WHERE `item_id` = %s",
                        (item_id,),
                    )
                else:
                    self._exec(
                        "UPDATE `items` SET `deleted` = 0, `deleted_at` = NULL "
                        "WHERE `item_id` = %s",
                        (item_id,),
                    )
            elif kind == "bulk_kind":
                for iid, old in payload.get("pairs") or []:
                    self._exec(
                        "UPDATE `items` SET `kind` = %s, `kind_locked` = 0 "
                        "WHERE `item_id` = %s",
                        (old, iid),
                    )
            self._exec(
                "UPDATE `edits` SET `undone` = 1 WHERE `edit_id` = %s",
                (row["edit_id"],),
            )
            self._exec(
                "INSERT INTO `edits` "
                "(`kind`, `item_id`, `payload`, `actor`, `ts`, `undone`, `undo_of`) "
                "VALUES ('undo', %s, %s, %s, %s, 1, %s)",
                (item_id, json.dumps({"undid": row["edit_id"]}), row["actor"],
                 ts, row["edit_id"]),
            )
        return True

    def bulk_set_kind(
        self, item_ids: list[int], kind: str, *, actor: str = "", ts: int = 0
    ) -> tuple[str, int]:
        """批量改类目。返回 `(actor, 改了几条)`。一次提交只写一个 actor。"""
        pairs: list[list[Any]] = []
        with self._transaction():
            for iid in item_ids:
                row = self._one(
                    "SELECT `kind` FROM `items` WHERE `item_id` = %s", (iid,)
                )
                if row is None:
                    continue
                pairs.append([iid, row["kind"]])
                self._exec(
                    "UPDATE `items` SET `kind` = %s, `kind_locked` = 1 "
                    "WHERE `item_id` = %s",
                    (kind, iid),
                )
            if not pairs:
                return "", 0
            self._exec(
                "INSERT INTO `edits` (`kind`, `payload`, `actor`, `ts`) "
                "VALUES ('bulk_kind', %s, %s, %s)",
                (json.dumps({"pairs": pairs, "to": kind}, ensure_ascii=False),
                 actor, ts),
            )
        return actor, len(pairs)

    def bulk_undo(self, actor: str, *, ts: int = 0) -> tuple[bool, int]:
        """把 `actor` 认出来的那一批改类目**整批翻回去**。

        按 actor 扫 `bulk_kind` 与 `set_kind` **两种**事件 —— 撤销的凭据是
        actor，不是事件类型（判据与推导见 `sqlite_store.bulk_undo`）。
        """
        rows = self._rows(
            "SELECT * FROM `edits` "
            "WHERE `kind` IN ('bulk_kind', 'set_kind') "
            "  AND `actor` = %s AND `undone` = 0 "
            "ORDER BY `edit_id` DESC",
            (actor,),
        )
        if not rows:
            return False, 0

        n = 0
        with self._transaction():
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                if row["kind"] == "bulk_kind":
                    for iid, old in payload.get("pairs") or []:
                        self._exec(
                            "UPDATE `items` SET `kind` = %s, `kind_locked` = 0 "
                            "WHERE `item_id` = %s",
                            (old, iid),
                        )
                        n += 1
                else:
                    iid = row["item_id"]
                    if iid is None:
                        continue
                    self._exec(
                        "UPDATE `items` SET `kind` = %s, `kind_locked` = 0 "
                        "WHERE `item_id` = %s",
                        (payload.get("from"), iid),
                    )
                    n += 1
                self._exec(
                    "UPDATE `edits` SET `undone` = 1 WHERE `edit_id` = %s",
                    (row["edit_id"],),
                )
        return True, n

    # ── 日报 ────────────────────────────────────────────────────

    def insert_digest(
        self, digest: Any, item_ids: list[int], *, run: Any = None
    ) -> int:
        """写日报。`run` 的语义与 `insert_items` 逐字相同（同事务）。"""
        self._guard_writable()
        with self._transaction():
            self._write_run(run)
            # 同一天重跑要**覆盖**，不是插第二篇。
            self._exec("DELETE FROM `digests` WHERE `day` = %s", (digest.day,))
            _, did = self._exec(
                "INSERT INTO `digests` "
                "(`day`, `window_from`, `window_to`, `created_at`, `body_md`, "
                " `preview`, `item_count`) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (digest.day, digest.window_from, digest.window_to,
                 digest.created_at, digest.body_md, digest.preview,
                 digest.item_count),
            )
            for iid in item_ids:
                self._exec(
                    "INSERT IGNORE INTO `item_digests` "
                    "(`digest_id`, `item_id`) VALUES (%s,%s)",
                    (did, iid),
                )
            return did

    def list_digests(self, *, limit: int = 50) -> list[Any]:
        rows = self._rows(
            "SELECT * FROM `digests` ORDER BY `window_from` DESC LIMIT %s",
            (limit,),
        )
        return [row_to_digest(r) for r in rows]

    def digest_detail(self, digest_id: int) -> tuple[Any, list[Any]] | None:
        """日报正文 + 它引用的条目。**必须过滤软删**（口径与 SQLite 一致）。"""
        row = self._one(
            "SELECT * FROM `digests` WHERE `digest_id` = %s", (digest_id,)
        )
        if row is None:
            return None
        items = self._rows(
            f"SELECT {ITEM_COLS} FROM `item_digests` d "
            "JOIN `items` i ON i.item_id = d.item_id "
            "WHERE d.digest_id = %s AND i.deleted = 0 "
            "ORDER BY i.event_ts DESC",
            (digest_id,),
        )
        return row_to_digest(row), [row_to_item(r) for r in items]

    # ── 作业 ────────────────────────────────────────────────────

    def create_job(
        self, kind: str, total: int, *, actor: str = "", ts: int = 0
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._transaction():
            self._exec(
                "INSERT INTO `jobs` "
                "(`job_id`, `kind`, `state`, `total`, `actor`, `started_at`) "
                "VALUES (%s,%s,'running',%s,%s,%s)",
                (job_id, kind, total, actor, ts),
            )
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        """更新作业的进度字段。名字是**裸的 `update`**（`run_batches` 的契约）。"""
        allowed = {
            "state", "total", "done", "message", "error", "finished_at",
            "cancel_requested",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sql = "UPDATE `jobs` SET " + ", ".join(f"`{k}` = %s" for k in sets)
        with self._transaction():
            self._exec(sql + " WHERE `job_id` = %s", list(sets.values()) + [job_id])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM `jobs` WHERE `job_id` = %s", (job_id,)
        )
        return dict(row) if row else None

    def reclaim_stale(self, *, now: int, older_than: int) -> int:
        cutoff = now - older_than
        with self._transaction() as cur:
            cur.execute(
                """
                UPDATE `jobs`
                   SET `state` = 'failed', `finished_at` = %s,
                       `error` = '进程中途退出了 —— 这个作业没有正常收尾'
                 WHERE `state` = 'running' AND `started_at` > 0
                   AND `started_at` < %s
                """,
                (now, cutoff),
            )
            return cur.rowcount

    def any_job_running(self) -> bool:
        row = self._one(
            "SELECT 1 AS `x` FROM `jobs` WHERE `state` = 'running' LIMIT 1"
        )
        return row is not None

    def request_cancel(self, job_id: str) -> tuple[bool, str]:
        """请求取消。对**已经结束**的作业返回 `(False, 终态)` —— 手慢了一步。"""
        row = self._one(
            "SELECT `state` FROM `jobs` WHERE `job_id` = %s", (job_id,)
        )
        if row is None:
            raise NotFound(f"没有这个任务：{job_id}")
        state = row["state"]
        if state != "running":
            return False, state
        self.update(job_id, cancel_requested=1)
        return True, "running"

    def cancel_requested(self, job_id: str) -> bool:
        row = self._one(
            "SELECT `cancel_requested` FROM `jobs` WHERE `job_id` = %s",
            (job_id,),
        )
        return bool(row and row["cancel_requested"])

    # ── 杂项 ────────────────────────────────────────────────────

    def get_kv(self, key: str) -> str | None:
        row = self._one(
            "SELECT `value` FROM `kvs` WHERE `key` = %s", (key,)
        )
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        # ※ 差异 #4：`ON CONFLICT(key) DO UPDATE SET value = excluded.value`
        # → `ON DUPLICATE KEY UPDATE value = VALUES(value)`。
        #
        # `VALUES(value)` 在 MySQL 8.0.20 起被标记为废弃（建议用行别名
        # `AS new`），但它**仍然可用**，而 `AS new` 在 5.7 上不存在。
        # 选老写法：兼容面更宽，代价是一条废弃警告 —— 而警告是响的。
        with self._transaction():
            self._exec(
                "INSERT INTO `kvs` (`key`, `value`) VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
                (key, value),
            )
