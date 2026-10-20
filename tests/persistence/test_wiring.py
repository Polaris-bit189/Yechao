"""「能力写好了但没接线」—— 本项目最典型的一类失败。

## 这个文件为什么单独存在

失败：

> **接线存在但从未被调用。** 某个组件写完了、看起来完整、甚至有单元测试，
> 但**全项目没有任何地方实例化它**。它从不生效，而系统照常工作
> —— **没有任何报错**。

这个项目**真的踩到过一次**：`covered_msg_ids()` 在仓储里、有测试、
返回正确的集合 —— 而 `extract.run()` 从来没读过它。

后果两条，且**都不报错**：
  · 重跑 refine 会把已经抽过的消息再抽一遍 ⇒ 重复条目
  · 用户软删的条目会被重新抽出来 ⇒ 删掉的东西自己回来

## 为什么原来的测试没抓到

仓储层那条测试断言的是 **`covered_msg_ids()` 返回得对**。
它**没有**断言调用方用了它。

那正是那种测试抓不到的 bug：
**函数返回值对，调用方没用**。

⇒ 这个文件里的用例断言的是**行为链** —— 跑一遍、再跑一遍，看重跑会不会
产出新东西。它不关心某个函数返回什么，只关心**整条链路真的走了那一步**。
"""

from __future__ import annotations

import pytest

from yechao.application import extract as extract_uc
from yechao.domain.models import PendingMessage
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.audit_log import NullAuditLog
from yechao.infrastructure.runtime.clock import FixedClock


class _Cfg:
    """一份最小配置桩。

    ※ 用桩而不是真配置：这条用例测的是**管线有没有走那一步**，
    与配置怎么读无关。真有配置参与进来，失败原因就会有两种可能。
    """

    def groups(self):
        from yechao.domain.config_models import GroupConfig

        return [GroupConfig(id=1, name="群1")]

    def persons(self):
        return []

    def categories(self):
        from yechao.domain.categories import default_categories
        from yechao.domain.config_models import CategorySet

        return CategorySet(categories=default_categories(), aliases={})

    def nicknames(self):
        return []


class _Model:
    """一个**永远给出同样答案**的模型桩，并记下被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        return (
            '[{"kind":"know","title":"测试条目","quote":"这是一条测试消息",'
            '"detail":null,"deadline":null,"start":null,"place":null,"amount":null}]'
        )


class _Prompt:
    EXTRACT_SYSTEM = "system"
    EXTRACT_MAX_TOKENS = 1024

    def build_extract_user(self, messages, scrubbed):
        return "\n".join(scrubbed)


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    return SqliteStore(sqlite_store.connect(db))


def _deps(store, model: _Model) -> extract_uc.ExtractDeps:
    return extract_uc.ExtractDeps(
        messages=store, items=store, model=model,
        audit=NullAuditLog(), clock=FixedClock(ts=2000),
        config=_Cfg(), prompt=_Prompt(),
    )


def _seed(store, msg_ids=(1,)) -> None:
    store.upsert_messages([
        PendingMessage(msg_id=i, group_id=1, ts=1000 + i, sender="某人",
                       content="这是一条测试消息，请于某日前提交")
        for i in msg_ids
    ])


def test_重跑抽取不会产出重复条目(store) -> None:
    """※ **这条用例抓的就是那个真 bug。**

    判据是**行为链**：跑一次、再跑一次，第二次不该产出任何新条目，
    而且**根本不该调模型**（没有待处理的消息了）。
    """
    _seed(store)
    model = _Model()
    deps = _deps(store, model)

    first = extract_uc.run(deps)
    assert len(first.items) == 1, "第一次应当产出条目"
    calls_after_first = model.calls
    assert calls_after_first >= 1, "第一次应当调过模型"

    second = extract_uc.run(deps)
    assert second.items == [], (
        "第二次跑又产出了条目 —— 说明 `covered_msg_ids()` 没有被调用。"
        "那会让重跑产出重复条目，而软删的条目会自己回来。"
    )
    assert model.calls == calls_after_first, (
        "第二次跑**根本不该调模型** —— 没有待处理的消息了"
    )


def test_软删的条目不会被下一轮抽取复活(store) -> None:
    """※ **不变量 ② 的第二半**：界面上消失，**且不会被下一轮抽取复活**。

    这一条与上面那条是同一条管线上的两件事：

      · 上面：**正常**条目的来源让消息算「已处理」
      · 这条：**软删**条目的来源**同样**让消息算「已处理」

    第二半更容易被漏 —— 因为一个直觉上「删掉了就不该再算数」的想法，
    恰好会让删掉的东西回来。
    """
    _seed(store)
    model = _Model()
    deps = _deps(store, model)

    first = extract_uc.run(deps)
    assert len(first.items) == 1
    item_id = first.items[0].item_id

    store.soft_delete(item_id, actor="web", ts=2100)
    rows, _ = store.search(limit=10)
    assert rows == [], "界面上应当看不到"

    third = extract_uc.run(deps)
    assert third.items == [], (
        "软删之后又被重新抽出来了 —— 用户删掉的东西自己回来了"
    )


# ── 全文索引（`items_fts`）的接线 ─────────────────────────────────────


def _item(**kw):
    from yechao.domain.models import Item

    base = dict(
        item_id=0, kind="know", kind_label="只要知道",
        title="测试条目", detail=None, event_ts=1000, deadline_ts=None,
        group_id=820000001, group_name="群1", actor=None, actor_account=None,
        place=None, amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def _insert_raw(store, titles: list[str]) -> list[int]:
    """直接往仓储里插条目 —— 绕开抽取，只测「写进去之后搜不搜得到」。"""
    from yechao.domain.models import SourceRef

    store.upsert_messages([
        PendingMessage(msg_id=i + 1, group_id=820000001, ts=1000 + i,
                       sender="某人", content=t)
        for i, t in enumerate(titles)
    ])
    items = [_item(title=t) for t in titles]
    srcs = [SourceRef(item_id=0, msg_id=i + 1, quote="") for i in range(len(titles))]
    return store.insert_items(items, srcs)


def test_插入条目之后立刻就能搜到(store) -> None:
    """※ **这条用例抓的是「索引写好了但没人写它」。**

    观察到什么现象：`insert_items` 原来不碰 `items_fts`，索引只在新库建
    出来时重建过一次。于是 `search(q=...)` 对新条目回 **200 + 空列表** ——
    不报错。而搜索框是评委第一个会点的地方。

    为什么断言的是行为链（插进去 → 搜出来），不是断言某个函数被调用：
    「函数返回值对、调用方没用」正是本文件开头写的那个 bug 的形状。

    判据里的两条路都要覆盖：
      · `len(q) >= 3` 走 FTS5 trigram
      · `len(q) < 3` 走 `LIKE`（**原本就没坏**，但少了这条就分不清
        「修好了 FTS」与「把两条路一起改坏了」）
    """
    ids = _insert_raw(store, ["图书馆闭馆通知", "社团招新报名"])
    assert len(ids) == 2

    rows, total = store.search(q="图书馆闭", limit=10)
    assert total == 1, "插入之后 FTS 里没有它 —— `insert_items` 没写 items_fts"
    assert rows[0].title == "图书馆闭馆通知"

    # 短查询走 LIKE，本来就不经过 FTS
    rows, total = store.search(q="招新", limit=10)
    assert total == 1

    # 阴性对照：搜一个不存在的词不该命中 —— 否则「全命中」也能让上面过
    _, total = store.search(q="不存在的词", limit=10)
    assert total == 0


def test_FTS_与条目在同一个事务里(store) -> None:
    """※ **别把 FTS 写到事务外面。**

    判据：`insert_items` 是一个事务（「条目 + 来源 + 记账」）。若 FTS 的写
    放在事务之外，崩溃时就会留下「条目在、索引不在」—— 症状是刚抽出来的
    条目搜不到，而它**不报错**。

    怎么验证：构造一次**事务内失败**（显式来源指向不存在的条目下标 ⇒
    `StorageError`，而那一步在条目插入**之后**）。断言**搜不到**那条回滚
    掉的条目，而原有的条目仍然搜得到 —— 条目回滚了，索引也必须回滚。

    ※ 这里**不能用 `SELECT COUNT(*) FROM items_fts`** 来判断索引齐不齐：
    `items_fts` 是 **external content** 表（`content='items'`），无 `WHERE`
    的 `SELECT` 会被改写成去读**内容表** —— 于是它恒等于 `items` 的行数，
    有没有索引都是这个数。要用 `MATCH` 才是真的走索引。
    """
    _insert_raw(store, ["原有条目"])
    assert store.search(q="原有条目", limit=10)[1] == 1, "前提：已有条目的索引是齐的"

    from yechao.domain.models import SourceRef

    store.upsert_messages([
        PendingMessage(msg_id=99, group_id=820000001, ts=9000, sender="s", content="c")
    ])
    with pytest.raises(Exception):
        store.insert_items(
            [_item(title="会回滚的条目")],
            [SourceRef(item_id=7, msg_id=99, quote="")],  # 下标 7 不存在
        )

    assert store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1, (
        "条目应当随事务回滚"
    )
    assert store.search(q="会回滚的条目", limit=10)[1] == 0, (
        "FTS 写到事务外面了 —— 条目回滚了，索引却留下了一条幽灵记录"
    )
    assert store.search(q="原有条目", limit=10)[1] == 1, "原有条目不该受影响"


def test_存量库的索引由_init_db_回填(tmp_path) -> None:
    """※ 修复之前的库：条目一条不少，FTS 是空的 —— 搜索框静默失效。

    整表重建接线在 `init_db()`，充当**存量库的回填/修复**入口
    （主路径是 `insert_items` 的增量写）。
    """
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    s = SqliteStore(sqlite_store.connect(db))
    _insert_raw(s, ["图书馆闭馆通知"])

    # 模拟「旧库」：把索引清空，条目留着
    s.conn.execute("DELETE FROM items_fts")
    s.conn.commit()
    assert s.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1, (
        "清索引不该把条目也删掉"
    )
    _, total = s.search(q="图书馆闭", limit=10)
    assert total == 0, "前提：索引空时确实搜不到"

    s.conn.close()
    sqlite_store.init_db(db)  # 再跑一次 init_db（幂等 + 回填）
    s2 = SqliteStore(sqlite_store.connect(db))
    _, total = s2.search(q="图书馆闭", limit=10)
    assert total == 1, "init_db 没有回填索引"


def test_仓储实现满足端口(store) -> None:
    """※ **端口与实现的方法名必须逐字一致。**

    这条用例的形状值得说：`reorder.run` 里写的是 `deps.jobs.busy()`，而
    `SqliteStore` 里只有 `any_job_running()` —— 一跑就 `AttributeError`。
    而 `reorder` 整个模块**零测试**，所以端口与实现的对不上名一直没人发现。

    `runtime_checkable` 的 `Protocol` 允许 `isinstance` 检查**方法名是否
    存在**。它查不了签名（那是 `Protocol` 的已知局限），但「名字对不上」
    正是这次事故的形状 —— 这一条就能把它钉住。

    怎么验证：把 `ports.py` 里任何一个方法名改回 `busy` / `append`，
    这条立刻红。
    """
    from yechao.application import ports

    s = store
    for proto in (
        ports.MessageRepository,
        ports.ItemRepository,
        ports.EditRepository,
        ports.DigestRepository,
        ports.JobRepository,
    ):
        assert isinstance(s, proto), f"{s.__class__.__name__} 不满足 {proto.__name__}"


def test_新消息仍然会被处理(store) -> None:
    """※ **阴性对照**。

    上面两条断言「不重复处理」。如果没有这条，一个**永远不处理任何东西**
    的实现也能让它们通过 —— 而那个实现什么都没做成。
    """
    _seed(store, msg_ids=(1,))
    model = _Model()
    deps = _deps(store, model)

    assert len(extract_uc.run(deps).items) == 1

    # 来了一条新消息
    _seed(store, msg_ids=(2,))
    again = extract_uc.run(deps)
    assert len(again.items) == 1, "新消息没有被处理"
