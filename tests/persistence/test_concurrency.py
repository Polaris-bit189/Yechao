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
⚠️ 单次可能碰巧全过（竞态本来就时灵时不灵），所以循环多轮。
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
    kinds = ["notice", "academic", "activity", "life", "secondhand", "lostfound", "job"]
    items = [
        Item(
            item_id=0, kind=kinds[i % len(kinds)], kind_label="", kind_icon="",
            title=f"t{i}", detail=None, event_ts=1000 + i, deadline_ts=None,
            group_id=1, group_name="", actor=None, actor_uin=None,
            place=None, amount=None, links=(), source_count=1, start_ts=None,
        )
        for i in range(1, 8)
    ]
    store.insert_items(items, [SourceRef(0, i, "") for i in range(1, 8)])
    store.conn.close()
    return path


def test_共享连接在多线程下会炸_所以必须按线程隔离(db):
    """⚠️ **这是那个 bug 的回归用例。**

    断言的是**行为**：用 `factory` 建出来的 store，在多线程下并发读
    **不能有任何一次失败**。

    如果哪天有人把它改回「一个连接大家用」，这条会红 —— 而红的方式
    可能是「时红时绿」，所以要跑多轮。
    """
    reader = SqliteStore(factory=lambda: sqlite_store.open_readonly(db))

    slugs = ["notice", "academic", "activity", "life", "secondhand", "lostfound", "job"]

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
    """⚠️ 行为断言之外再钉一条结构：不同线程的连接对象必须不同。

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
