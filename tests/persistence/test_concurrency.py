"""并发读 —— 这个文件抓的是一个**只在浏览器里出现**的 bug。

## 事故形状

类目页要为七个类目各拉一次预览，**七次请求同时发出**。后端那个
`app.reader` 原本是**一个共享的 SQLite 连接**，`check_same_thread=False`
只是关掉了 Python 那层线程检查，**并没有让连接变成并发安全**。

结果：七个线程同时用一个连接 ⇒ SQLite 报错 ⇒ HTTP 500 ⇒
页面上写「读不到这一类的条目」。

而它长得像「这一个类目没数据」—— 用户看到的是数据问题，不是故障。

## 为什么原有 185 个测试一个都没抓到

它们**全是单线程**的。单线程下共享连接工作得很好。

⇒ 这个文件用真正的线程池去打，并且**断言 0 个失败**。
※ 单次可能碰巧全过（竞态本来就时灵时不灵），所以循环多轮。
"""

from __future__ import annotations

import concurrent.futures

import pytest

from yechao.domain.models import Item, PendingMessage, SourceRef
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    sqlite_store.init_db(path)

    # 种几类数据，让并发查询有东西可查。
    store = SqliteStore(sqlite_store.connect(path))
    store.upsert_messages([
        PendingMessage(msg_id=i, group_id=1, ts=1000 + i, sender="s", content=f"c{i}")
        for i in range(1, 8)
    ])
    kinds = ["todo", "attend", "know", "claim", "trade", "recruit"]
    items = [
        Item(
            item_id=0, kind=kinds[i % len(kinds)], kind_label="",
            title=f"t{i}", detail=None, event_ts=1000 + i, deadline_ts=None,
            group_id=1, group_name="", actor=None, actor_account=None,
            place=None, amount=None, links=(), source_count=1, start_ts=None,
        )
        for i in range(1, 8)
    ]
    store.insert_items(items, [SourceRef(0, i, "") for i in range(1, 8)])
    store.conn.close()
    return path


def test_共享连接在多线程下会炸_所以必须按线程隔离(db):
    """※ **这是那个 bug 的回归用例。**

    断言的是**行为**：用 `factory` 建出来的 store，在多线程下并发读
    **不能有任何一次失败**。

    如果哪天有人把它改回「一个连接大家用」，这条会红 —— 而红的方式
    可能是「时红时绿」，所以要跑多轮。
    """
    reader = SqliteStore(factory=lambda: sqlite_store.open_readonly(db))

    slugs = ["todo", "attend", "know", "claim", "trade", "recruit"]

    def one(slug: str) -> tuple[str, str | None]:
        try:
            rows, _total = reader.search(kinds=[slug], limit=12)
            # 摸一下结果，确保连接真的被用到了（不是空转）。
            _ = [r.title for r in rows]
            return slug, None
        except Exception as e:  # noqa: BLE001 — 这里就是要抓住"任何失败"
            return slug, f"{type(e).__name__}: {e}"

    failures: list[tuple[str, str]] = []
    # 三轮 —— 竞态本来就时灵时不灵，只跑一次可能碰巧全过。
    for _ in range(3):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for slug, err in pool.map(one, slugs * 3):
                if err:
                    failures.append((slug, err))

    assert not failures, (
        f"并发读失败了 {len(failures)} 次 —— 连接没有按线程隔离：\n"
        + "\n".join(f"  {s}: {e}" for s, e in failures[:5])
    )


def test_factory_模式下每个线程拿到不同连接(db):
    """※ 行为断言之外再钉一条结构：不同线程的连接对象必须不同。

    这条是前一条的**解释**。只有前一条的话，有人可能用「加一把大锁」绕过
    （那样也能过，但把所有读串行化了）；这一条说明我们选的是**按线程隔离**
    而不是全局加锁。
    """
    reader = SqliteStore(factory=lambda: sqlite_store.open_readonly(db))
    seen: list[int] = []
    lock = __import__("threading").Lock()

    def grab() -> None:
        cid = id(reader.conn)
        with lock:
            seen.append(cid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: grab(), range(8)))

    # 线程池可能复用线程，所以「不同 id 的数量」不一定等于 8；
    # 但至少要 >1，否则说明所有线程共用了一个连接。
    assert len(set(seen)) > 1, f"8 个线程只拿到 {len(set(seen))} 个连接"


def test_写连接也必须按线程隔离(db):
    """※ 同一个 bug 的**另一半**：并发**写**。

    原来的并发用例只打了 `reader`（读）。而 `serve` 的写端点（删条目、改类目、
    撤销、取消作业）跑在**同一个** FastAPI 线程池里 —— 写连接共享时，实测约
    10% 的写请求回 500，还夹着几个**假 404**（`SELECT ... WHERE item_id=?` 被
    搅成 None，于是对一条存在的条目说「没有这条条目」，而那句话是假的）。

    判据：按线程隔离的 store，多线程同时改类目 —— **0 个失败**，且改动真的落库。
    """
    writer = SqliteStore(factory=lambda: sqlite_store.connect(db))

    def one(item_id: int) -> str | None:
        try:
            writer.set_kind(item_id, "todo", actor="t", ts=2000)
            return None
        except Exception as e:  # noqa: BLE001 — 这里就是要抓住「任何失败」
            return f"{type(e).__name__}: {e}"

    failures: list[str] = []
    # 多轮 —— 竞态本来就时灵时不灵。
    for _ in range(5):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for err in pool.map(one, list(range(1, 8)) * 2):
                if err:
                    failures.append(err)

    assert not failures, (
        f"并发写失败了 {len(failures)} 次 —— 写连接没有按线程隔离：\n"
        + "\n".join(failures[:5])
    )

    # 空转自检：写必须真的落库，不能「没报错但什么都没发生」。
    got = writer.item_detail(1)
    assert got is not None and got.kind == "todo", (
        "并发写没报错，但改动没落库 —— 这条用例在空转"
    )


def test_装配出来的写连接也按线程隔离(tmp_path):
    """※ 上一条测的是「工厂模式的 store 扛得住并发」；这一条测的是
    **组合根有没有真的用工厂**。两件事缺一不可。

    只用上一条的话，有人把 `bootstrap._build_stores` 改回「一条共享连接」，
    它照样绿 —— 因为它自己建的是一个工厂 store，跟组合根怎么装配无关。
    """
    import threading

    from yechao.bootstrap import Settings, make_app

    st = Settings.from_paths(data_dir=tmp_path / "d", config_dir=tmp_path / "c")
    st.ensure_dirs()
    sqlite_store.init_db(st.db_path)
    app = make_app(settings=st)
    try:
        here = app.store.conn
        there: list[object] = []
        t = threading.Thread(target=lambda: there.append(app.store.conn))
        t.start()
        t.join()
        assert there and there[0] is not here, (
            "组合根装配出来的写连接是**共享**的 —— "
            "`serve` 的并发写请求会撞出 InterfaceError（约 10% 回 500，"
            "还夹着假 404）"
        )
    finally:
        app.close()
