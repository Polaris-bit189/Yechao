"""重排用例 —— 这个模块此前**一条测试都没有**。

## 为什么单独一个文件、而且先写它

审计时发现 `reorder` 全仓零测试（`grep -rn reorder tests/` 为空），而它
同时藏着两个真缺陷：

1. `reorder.run` 调 `deps.jobs.busy()`，实现里只有 `any_job_running()`
   ⇒ **一跑就 `AttributeError`**。主路径必崩，而没有任何测试踩到它。
2. `reorder.undo` 调 `bulk_undo(actor)`，而 `bulk_undo` 只认
   `kind='bulk_kind'`；重排记的是 `set_kind` ⇒ **撤销永远返回
   `(False, 0)`**，类目保持在改后的值 —— 用户点了撤销什么都不会发生，
   界面还可能显示成功。

## 这里断言的是**整条链路**

不是「`bulk_undo` 返回得对」，而是「跑一次重排 → 撤销 → 条目的类目真的
回来了」。理由见 `tests/persistence/test_wiring.py` 的开头：本项目最典型
的失败是「函数返回值对、调用方没用」，只有跑整条链路才抓得到。
"""

from __future__ import annotations

import pytest

from yechao.application import reorder as reorder_uc
from yechao.domain.errors import ValidationError
from yechao.domain.models import Item, PendingMessage, SourceRef
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.audit_log import NullAuditLog


# ── 桩 ──────────────────────────────────────────────────────────────


class _Model:
    """一个把标题重新判成 `to_kind` 的模型桩。

    ※ 重排的回连靠 `quote`：它拿模型给的摘录去**条目清单**里找。
    所以桩返回的 `quote` 必须是某个条目的标题（逐字）。
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int = 2048) -> str:
        import json

        self.calls += 1
        out = []
        for title, kind in self.mapping.items():
            if title in user:  # 只对真的在这一次输入里的条目发言
                out.append({"kind": kind, "title": title, "quote": title,
                            "detail": None, "deadline": None, "start": None,
                            "place": None, "amount": None})
        return json.dumps(out, ensure_ascii=False)


class _Cfg:
    def categories(self):
        from yechao.domain.categories import default_categories
        from yechao.domain.config_models import CategorySet

        return CategorySet(categories=default_categories(), aliases={})


# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path) -> SqliteStore:
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    return SqliteStore(sqlite_store.connect(db))


def _item(**kw) -> Item:
    base = dict(
        item_id=0, kind="know", kind_label="只要知道",
        title="测试条目", detail=None, event_ts=1000, deadline_ts=None,
        group_id=820000001, group_name="群1", actor=None, actor_account=None,
        place=None, amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def _insert(store: SqliteStore, titles: list[str], *, kind: str = "know") -> list[int]:
    store.upsert_messages([
        PendingMessage(msg_id=i + 1, group_id=820000001, ts=1000 + i,
                       sender="某人", content=t)
        for i, t in enumerate(titles)
    ])
    return store.insert_items(
        [_item(title=t, kind=kind) for t in titles],
        [SourceRef(item_id=0, msg_id=i + 1, quote="") for i in range(len(titles))],
    )


def _deps(store: SqliteStore, mapping: dict[str, str]) -> reorder_uc.ReorderDeps:
    return reorder_uc.ReorderDeps(
        items=store, edits=store, jobs=store,
        model=_Model(mapping), config=_Cfg(), audit=NullAuditLog(),
    )


# ── 跑得起来 ────────────────────────────────────────────────────────


def test_重排能跑通并返回批次凭据(store) -> None:
    """※ **这一条就是缺陷 2 的回归。**

    修之前：`AttributeError: 'SqliteStore' object has no attribute 'busy'`
    —— 主路径第一步就崩，而 `reorder` 零测试，所以一直没人发现。

    判据里的 `actor` 是**撤销的凭据**：一次重排会改很多条，只有这个值能
    把它们认成同一批。它为空 = 前端没有东西可交给撤销。
    """
    [iid] = _insert(store, ["图书馆闭馆通知"])
    out = reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))

    assert out["changed"] == 1
    assert out["failed"] == 0
    assert out["actor"], "没有 actor 就没法撤销这一批"
    assert out["actor"].startswith("reorder:")
    assert store.item_detail(iid).kind == "todo"


def test_已有作业在跑时重排被拒(store) -> None:
    """※ 一次只允许一个重排（并发两个会让事件的顺序失去意义）。

    这条同时钉住**端口的方法名**：`deps.jobs` 上必须真的有
    `any_job_running()`。名字写错的话这里抛的是 `AttributeError` 而不是
    `ValidationError`。
    """
    _insert(store, ["图书馆闭馆通知"])
    store.create_job("refine", 10, ts=1)  # 另一个作业在跑

    with pytest.raises(ValidationError) as ei:
        reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))
    assert "等它结束" in str(ei.value), "409 的文案要说清下一步，不是「出错了」"


def test_取消之后重排会停在批与批之间(store) -> None:
    """※ 取消是**协作式**的：只在批与批之间生效，手上那批照常跑完。

    这条钉住的是「取消请求真的被读到了」。修之前 `_is_cancelled` 按
    `state == 'cancelled'` 判，而检查发生时 `state` **永远还是 `'running'`**
    ⇒ 请求被静默忽略、作业照跑完。而 `SqliteStore.cancel_requested()` 写好
    了却全仓没有调用者 —— 它本来就是给这里用的。

    ※ 需要 **> `_BATCH`（20）条**才会有第二批：取消只在两批之间被看到，
    只有一批的话这个检查只会在开跑前发生一次（那时还没人请求取消）。
    """
    titles = [f"通知条目{i:03d}" for i in range(21)]
    ids = _insert(store, titles)

    class _CancellingModel:
        """第一次被调时就请求取消当前那个重排作业。"""

        def complete(self, *, system: str, user: str, max_tokens: int = 2048) -> str:
            import json

            row = store.conn.execute(
                "SELECT job_id FROM jobs WHERE kind = 'reorder' "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            assert row is not None, "重排应当先建了作业"
            store.request_cancel(row["job_id"])
            out = [{"kind": "todo", "title": t, "quote": t, "detail": None,
                    "deadline": None, "start": None, "place": None, "amount": None}
                   for t in titles if t in user]
            return json.dumps(out, ensure_ascii=False)

    deps = reorder_uc.ReorderDeps(
        items=store, edits=store, jobs=store,
        model=_CancellingModel(), config=_Cfg(), audit=NullAuditLog(),
    )
    out = reorder_uc.run(deps)

    # 第一批（20 条）跑完了，第二批没开
    assert out["changed"] == 20, "取消该停在批与批之间，而不是把第一批也丢掉"
    assert store.get_job(out["job_id"])["state"] == "cancelled"
    # ※ 断言「只改了 20 条」而不是「某一条没改」：条目按 `event_ts DESC`
    # 取，哪一条落在最后一批取决于时间戳，写死某一索引会假红。
    left = [i for i in ids if store.item_detail(i).kind == "know"]
    assert len(left) == 1, "第二批那一条不该被处理"


def test_模型没改动的条目不被写事件(store) -> None:
    """※ 只改**该改的**。原类目与模型判出来的一样时不该写事件 ——
    否则撤销会撤出一批「什么也没改」的空事件，把批次撑大。"""
    [iid] = _insert(store, ["图书馆闭馆通知"], kind="todo")
    out = reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))
    assert out["changed"] == 0
    assert out["actor"] == "", "一条都没改时不该给撤销凭据"
    assert store.item_detail(iid).kind == "todo"


# ── 撤销 ────────────────────────────────────────────────────────────


def test_撤销重排把类目改回去(store) -> None:
    """※ **这一条就是缺陷 3 的回归。**

    修之前：`bulk_undo` 只查 `kind='bulk_kind'`，而重排记的是 `set_kind`
    ⇒ 撤销永远返回 `(False, 0)`，类目保持在改后的值 —— 用户点了撤销
    什么都不会发生，而界面还可能显示成功。
    """
    [iid] = _insert(store, ["图书馆闭馆通知"])
    out = reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))
    assert store.item_detail(iid).kind == "todo"

    ok, count = reorder_uc.undo(_deps(store, {}), out["job_id"])
    assert ok is True, "撤销重排返回了假 —— `bulk_undo` 认不出 `set_kind` 事件"
    assert count == 1
    assert store.item_detail(iid).kind == "know", "类目应当回到重排前"


def test_撤销一次重排会把这一批全撤回去(store) -> None:
    """※ 「一批」是**整批**，不是最后一条。

    撤销的凭据是 actor（一次操作只写一个 actor），不是事件类型、也不是
    最后一条事件。一次重排改了 3 条，撤销就要把 3 条都还原。
    """
    ids = _insert(store, ["图书馆闭馆通知", "社团招新报名", "选课时间调整"])
    mapping = {t: "todo" for t in
               ("图书馆闭馆通知", "社团招新报名", "选课时间调整")}
    out = reorder_uc.run(_deps(store, mapping))
    assert out["changed"] == 3

    ok, count = reorder_uc.undo(_deps(store, {}), out["job_id"])
    assert (ok, count) == (True, 3)
    for iid in ids:
        assert store.item_detail(iid).kind == "know"


def test_撤销不存在的批次不是错误(store) -> None:
    """※ `(False, 0)` 不是报错 —— 那是「这一批没东西可撤」（可能之前撤过了）。

    与 `/api/undo/batch` 的判据一致：请求本身合法，只是当前没有可撤的。
    """
    _insert(store, ["图书馆闭馆通知"])
    ok, count = reorder_uc.undo(_deps(store, {}), "根本没有这个作业")
    assert (ok, count) == (False, 0)


def test_撤销之后不能再撤一次(store) -> None:
    """※ 撤过的批次 `undone = 1`，第二次撤不该又改一遍。"""
    [iid] = _insert(store, ["图书馆闭馆通知"])
    out = reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))

    assert reorder_uc.undo(_deps(store, {}), out["job_id"]) == (True, 1)
    assert reorder_uc.undo(_deps(store, {}), out["job_id"]) == (False, 0)
    assert store.item_detail(iid).kind == "know"


def test_撤销重排不会动别的批次(store) -> None:
    """※ 撤销靠 **actor** 认批 —— 别批的改动不能被顺手撤掉。

    这条是**阴性对照**：没有它的话，一个「撤销 = 把所有类目都重置」的实现
    也能让上面几条通过，而那个实现会毁掉用户所有的手工编辑。
    """
    ids = _insert(store, ["图书馆闭馆通知", "社团招新报名"])
    manual, other = ids[0], ids[1]

    # 一次人工改类目（actor 是 web）
    store.set_kind(manual, "attend", actor="web", ts=1500)
    # 一次重排，改的是另一条
    out = reorder_uc.run(_deps(store, {"社团招新报名": "todo"}))
    assert out["changed"] == 1

    ok, count = reorder_uc.undo(_deps(store, {}), out["job_id"])
    assert (ok, count) == (True, 1)
    assert store.item_detail(other).kind == "know", "重排的那条回到重排前"
    assert store.item_detail(manual).kind == "attend", (
        "人工改的那条不该被重排的撤销碰掉"
    )


def test_批量改类目的撤销仍然有效(store) -> None:
    """※ **另一种形状的回归**：`bulk_set_kind` 记的是**一条** `bulk_kind`
    （payload 里是 `pairs`）。

    修 `bulk_undo` 时若把 `bulk_kind` 那条分支删掉、只留 `set_kind`，
    `/api/undo/batch` 的撤销就会静默失效 —— 而它是另一条独立的路径。
    """
    ids = _insert(store, ["图书馆闭馆通知", "社团招新报名"])
    actor, count = store.bulk_set_kind(ids, "todo", actor="bulk:1:2", ts=1600)
    assert (actor, count) == ("bulk:1:2", 2)

    ok, n = store.bulk_undo(actor, ts=1700)
    assert (ok, n) == (True, 2)
    for iid in ids:
        assert store.item_detail(iid).kind == "know"


# ── 记录的形状 ──────────────────────────────────────────────────────


def test_重排记的是_set_kind_事件而不是直接改字段(store) -> None:
    """※ 重排是 **append-only** 的：它产出一批新事件，而不是直接
    `UPDATE items.kind`。理由与「删除是软的」同源 —— **撤销需要
    「原来是什么」**。

    所以这里断言的是「事件真的写下来了，而且 payload 里有 `from`」——
    没有它，撤销就无从下手。
    """
    import json

    [iid] = _insert(store, ["图书馆闭馆通知"])
    out = reorder_uc.run(_deps(store, {"图书馆闭馆通知": "todo"}))

    rows = store.conn.execute(
        "SELECT kind, item_id, payload, actor FROM edits WHERE actor = ?",
        (out["actor"],),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "set_kind"
    assert rows[0]["item_id"] == iid
    assert json.loads(rows[0]["payload"]) == {"from": "know", "to": "todo"}
