"""两条不变量 —— 违反它们不算 bug，算**设计被推翻**。

1. **网页侧不得写入主库。** 由数据库本身拒绝，不靠代码自觉。
2. **删除是软的。** 只打标记不删行；抽取重跑时它不会被重新抽出来。

※ 这里每一条都断言**行为**，不是断言**设置**。
「设了只读」与「只读真的生效」是两件事 ——
而一个「看起来在保护」的机制可能什么都没做，且不报错
"""

from __future__ import annotations

import sqlite3

import pytest

from yechao.domain.errors import ReadOnlyViolation
from yechao.domain.models import Item, PendingMessage, SourceRef
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    return SqliteStore(sqlite_store.connect(db)), db


def _item(**kw) -> Item:
    base = dict(
        item_id=0, kind="know", kind_label="只要知道",
        title="测试条目", detail=None, event_ts=1000, deadline_ts=None,
        group_id=1, group_name="群1", actor=None, actor_account=None,
        place=None, amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def _insert(store, n: int = 1, msgs: list[int] | None = None) -> list[int]:
    ids = list(range(1, n + 1)) if msgs is None else msgs
    store.upsert_messages([
        PendingMessage(msg_id=i, group_id=1, ts=1000 + i, sender="s", content=f"内容{i}")
        for i in ids
    ])
    items = [_item(title=f"条目{i}") for i in ids]
    srcs = [SourceRef(item_id=0, msg_id=i, quote="") for i in ids]
    return store.insert_items(items, srcs)


# ── 不变量 ①：网页侧不得写入主库 ──────────────────────────────────────


def test_只读连接真的会被数据库拒绝写入(store) -> None:
    """※ **断言行为，不断言设置。**

    这是不变量 ① 的核心用例：用只读连接真发一条 INSERT，断言它抛。
    光读 `PRAGMA query_only` 不够 —— 那只是一个设置，而设置与行为
    可以不一致。
    """
    _, db = store
    ro = sqlite_store.open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO kvs (key, value) VALUES ('x', 'y')")
    finally:
        ro.close()


def test_只读连接也挡得住临时表(store) -> None:
    """※ `mode=ro` 只管**主库文件**，管不到临时表。

    所以要 `PRAGMA query_only` 第二道闸。没有它的话，
    「只读连接」仍然能 `CREATE TEMP TABLE` —— 而那会让
    「网页侧不写库」这句话有一个静默的漏洞。
    """
    _, db = store
    ro = sqlite_store.open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TEMP TABLE t (x)")
    finally:
        ro.close()


def test_仓储层面也认得只读连接并给出指向性错误(store) -> None:
    """※ 不靠这一条来守（守的是 SQLite），但它让错误**说清原因** ——
    而不是 SQLite 那句 `attempt to write a readonly database`
    （那可能被误读成磁盘权限问题，排查方向完全错）。"""
    _, db = store
    ro_store = SqliteStore(sqlite_store.open_readonly(db))
    with pytest.raises(ReadOnlyViolation):
        ro_store.upsert_messages(
            [PendingMessage(msg_id=99, group_id=1, ts=0, sender="s", content="c")]
        )


def test_只读状态下读是正常的(store) -> None:
    """※ 阴性对照：只读**不该**把读也挡掉。

    没有这条的话，一个「所有操作都抛」的实现也能让上面三条通过 ——
    而那个实现什么都没做成。
    """
    s, db = store
    _insert(s)
    ro = SqliteStore(sqlite_store.open_readonly(db))
    rows, total = ro.search(limit=10)
    assert total == 1
    assert rows[0].title == "条目1"


# ── 不变量 ②：删除是软的 ─────────────────────────────────────────────


def test_删除之后界面上看不到_但行还在(store) -> None:
    s, _ = store
    [iid] = _insert(s)
    s.soft_delete(iid, actor="web", ts=2000)

    rows, total = s.search(limit=10)
    assert total == 0, "界面上应当看不到"
    assert s.item_detail(iid) is None

    # ※ 行还在 —— 这是「软」的意思
    raw = s.conn.execute("SELECT deleted FROM items WHERE item_id = ?", (iid,)).fetchone()
    assert raw is not None
    assert raw["deleted"] == 1


def test_软删的条目不会被下一轮抽取复活(store) -> None:
    """※ **不变量 ② 的第二半，也是它最容易被漏掉的一半。**

    「界面上消失」很容易做到；「不会被重新抽出来」不会自动成立。

    抽取的去重靠 `covered_msg_ids()` —— 它查的是 `item_sources`。
    如果那里加了 `WHERE i.deleted = 0`，软删的条目所覆盖的消息会
    重新出现在「待处理」里，于是用户删掉的东西**在下一轮自己回来** ——
    而那是产品里最惊吓的一种 bug，且无声（它看起来像「抽取又跑了一遍」）。
    """
    s, _ = store
    [iid] = _insert(s)
    s.soft_delete(iid, actor="web", ts=2000)

    covered = s.covered_msg_ids()
    assert 1 in covered, "被软删的条目所覆盖的消息**仍然算已处理**"


def test_撤销删除能把条目带回来(store) -> None:
    """※ 撤销需要「原来是什么」—— 这就是事件里存 `was_deleted` 的原因。"""
    s, _ = store
    [iid] = _insert(s)
    edit_id = s.soft_delete(iid, actor="web", ts=2000)
    assert s.undo(edit_id, ts=2100) is True
    assert s.item_detail(iid) is not None


def test_连删两次_只撤第二条不会把条目复活(store) -> None:
    """※ 撤销要用事件里存的 `was_deleted`，**不是**无条件把 `deleted` 置 0。

    观察到什么现象：连点两次删除（手抖、或前端没禁用按钮）之后撤销
    **第二条**，条目重新可见 —— 而用户只是想撤掉那次多余的删除，条目本来
    就该还是删着的。界面还可能显示「撤销成功」。

    为什么会错：撤销路径原来写死 `deleted = 0`。而 `soft_delete` 明明把
    「删之前它是什么状态」写进了 payload（`was_deleted`）—— 写了却没人读，
    正是「事件存了『原来是什么』却不用」的形状。

    判据：撤销的语义是「回到**这条事件发生之前**」。
      · 撤第二条（was_deleted=True）⇒ 回到「本来就已删」⇒ **仍不可见**
      · 撤第一条（was_deleted=False）⇒ 回到「还没被删」⇒ 可见

    怎么验证：下面两条用例分别钉住这两半。
    """
    s, _ = store
    [iid] = _insert(s)
    s.soft_delete(iid, actor="web", ts=2000)
    edit2 = s.soft_delete(iid, actor="web", ts=2100)  # 第二次

    assert s.undo(edit2, ts=2200) is True
    assert s.item_detail(iid) is None, (
        "撤掉多余的第二次删除，条目不该复活 —— 它本来就该还是删着的"
    )


def test_连删两次_撤完两条才恢复可见(store) -> None:
    """※ 与上一条配对：**撤完两条才可见**。

    没有这一条的话，一个「撤销删除永远什么都不做」的实现也能让上一条通过
    —— 而那个实现等于把撤销功能整个废掉。
    """
    s, _ = store
    [iid] = _insert(s)
    edit1 = s.soft_delete(iid, actor="web", ts=2000)
    edit2 = s.soft_delete(iid, actor="web", ts=2100)

    assert s.undo(edit2, ts=2200) is True
    assert s.item_detail(iid) is None
    assert s.undo(edit1, ts=2300) is True
    assert s.item_detail(iid) is not None, "撤到第一条（删之前它没被删）应当恢复可见"


def test_日报详情不含软删条目(store) -> None:
    """※ **同一个条目，两个读路径不能给出相反的答案。**

    观察到什么现象：日报发出之后用户删掉其中一条，`search` 已经看不到它
    （0 条），而 `digest_detail` 仍然把它列出来 —— 而且**都不报错**。

    为什么会漏：`item_digests` 是「这篇日报引用了哪些条目」的**历史记录**，
    写 SQL 时容易觉得「历史就该原样保留」。但日报详情页是**读界面**，它的
    口径必须与 `search` / `item_detail` 一致 —— 软删的语义是「界面上消失」
    （不变量 ②），不是「只在某些页面上消失」。
    """
    from yechao.domain.models import Digest

    s, _ = store
    [iid] = _insert(s)
    did = s.insert_digest(
        Digest(digest_id=0, day="2026-10-20", window_from=0, window_to=99999,
               created_at=1, body_md="正文", item_count=1, preview="摘要"),
        [iid],
    )
    assert len(s.digest_detail(did)[1]) == 1, "前提：没删之前详情里有它"

    s.soft_delete(iid, actor="web", ts=2000)
    assert s.search(limit=10)[1] == 0, "前提：search 已经看不到它"
    assert s.digest_detail(did)[1] == [], (
        "日报详情仍然列出软删条目 —— 与 search 的口径相反，而且不报错"
    )
    # 阴性对照：日报本身还在，只是条目那一栏空了
    assert s.digest_detail(did)[0].digest_id == did


def test_改类目是可撤销的_且记住原来的值(store) -> None:
    """※ 撤销**需要**「原来是什么」。直接改掉之后，撤销就无从下手。"""
    s, _ = store
    [iid] = _insert(s)
    s.set_kind(iid, "attend", actor="web", ts=2000)
    assert s.item_detail(iid).kind == "attend"

    ok = s.undo(None, ts=2100)  # None = 撤最近一步
    assert ok is True
    assert s.item_detail(iid).kind == "know", "应当回到原来的类目"


def test_撤销过一次之后_再撤不会重复撤同一条(store) -> None:
    """※ 撤销**本身也是一条事件**（append-only，留痕）。

    如果 `last_editable` 不排除 `undo` 这一类，第二次「撤最近一步」
    会拿到那条 `undo` 事件、去撤它 —— 用户看到的是按钮点了没反应。
    """
    s, _ = store
    [iid] = _insert(s)
    s.set_kind(iid, "attend", actor="web", ts=2000)
    assert s.undo(None, ts=2100) is True
    assert s.undo(None, ts=2200) is False, "没有可撤的了"


# ── 同事务 ──────────────────────────────────────────────────────────


def _n(s, table: str) -> int:
    return int(s.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_条目_来源_账在同一个事务里_账写不进去则条目也不落(store) -> None:
    """※ **这条判据是重写过的 —— 原来那条是空转的。**

    原来的写法是：插 3 条，然后数两张表的行数（`items == 3`、
    `item_sources == 3`）。它断言的是「写完之后两边都有」，
    而**把实现拆成两个独立事务，它照样绿** —— 因为两边都写成功了。
    它压根没碰「要么一起成、要么一起不成」这半句。

    ## 判据：**让第二笔写失败，断言第一笔也回滚**

    这里让**账**写不进去：`reason` 超过数据库的上限 64 字
    （见 `domain/errors.py` 的「账里不存正文」）。于是

      · 同一个事务 ⇒ 条目、来源、抬头、账，**四样一样都不留**
      · 拆成两个事务（先条目、后账）⇒ 条目留在库里，而它没有账 —— **红**

    ## 它分辨不了什么（说清楚，免得它被当成万能的）

    「账写在条目**之前**、且各自成事务」那种拆法，这个方向分辨不出来
    （账先失败，条目压根没写，四张表都空）。那个方向由下面那条
    `test_来源挂不上时_抬头与账也一起回滚` 覆盖 ——
    **两条合起来才是完整的判据**，单看任何一条都有一种拆法能骗过它。
    """
    from yechao.domain.errors import LedgerEntry, RunRecord

    s, _ = store
    s.upsert_messages([
        PendingMessage(msg_id=7, group_id=1, ts=1007, sender="s", content="内容7")
    ])
    run = RunRecord(
        run_id="run-x", kind="refine", started_at=1000, finished_at=1001,
        # ※ 65 字：比 `REASON_MAX_CHARS`（64）多一个字，数据库会拒。
        entries=(LedgerEntry(kind="filtered", reason="x" * 65),),
    )
    with pytest.raises(sqlite3.Error):
        s.insert_items([_item(title="会回滚的条目")], [SourceRef(0, 7, "")], run=run)

    assert _n(s, "items") == 0, "条目没有跟着回滚"
    assert _n(s, "item_sources") == 0, "来源没有跟着回滚"
    assert _n(s, "runs") == 0, "抬头留下了 —— 它和条目不在一个事务里"
    assert _n(s, "run_entries") == 0


def test_来源挂不上时_抬头与账也一起回滚(store) -> None:
    """反向：**失败发生在条目与来源写完之后**。

    ※ 判据（能分辨什么）：账在 `insert_items` 里是**第一件事**，条目与
    来源跟在后面。若账走的是「自己一个事务、先提交」，下面这次失败
    （来源指向一个不存在的条目下标 ⇒ `StorageError`）会留下抬头与账，
    而条目与来源回滚掉 —— 库里于是有「跑过一轮、什么都没产出、但有 N 笔账」，
    而**没有一条账能对上条目**。

    与上一条配对：上一条造「账写不进去」，这条造「条目写不进去」。
    两条覆盖了两个失败方向，拆事务的两种拆法各被其中一条抓住。
    """
    from yechao.domain.errors import LedgerEntry, RunRecord

    s, _ = store
    s.upsert_messages([
        PendingMessage(msg_id=9, group_id=1, ts=1009, sender="s", content="内容9")
    ])
    run = RunRecord(
        run_id="run-y", kind="refine", started_at=1000, finished_at=1001,
        entries=(LedgerEntry(kind="filtered", reason="pure_ack"),),
    )
    with pytest.raises(Exception) as ei:
        # 下标 7 不存在 ⇒ 写来源那一步抛 `StorageError`
        s.insert_items([_item(title="会回滚的条目")], [SourceRef(7, 9, "")], run=run)
    assert "不存在的条目下标" in str(ei.value)

    assert _n(s, "items") == 0
    assert _n(s, "item_sources") == 0
    assert _n(s, "runs") == 0, "抬头留在库里了 —— 它和条目不在一个事务里"
    assert _n(s, "run_entries") == 0


def test_没有_item_id_的来源比条目多时拒绝写入(store) -> None:
    """※ 这是「解包顺序错」那道防线的**实现侧**。

    ※ **契约改过一次，这条用例也跟着改了，值得记。**

    原来它断言「只要来源比条目多就拒绝」。而**跨群转发合并**之后，
    一条条目挂多条来源是**正常状态** —— 那个判据把正常情况也拒了，
    第一次跑合并之后的 refine 就崩在 `来源比条目多：20 > 18`，
    而且错误信息说「调用方把两个列表配错了」——**指向错误的方向**。

    ⇒ 现在只拒绝真正可能配错的形状：**没有指定 `item_id` 的来源**
    比条目多。显式指了 `item_id` 的，按它挂，不靠数量。
    """
    s, _ = store
    with pytest.raises(Exception) as ei:
        s.insert_items([_item()], [SourceRef(0, 1, ""), SourceRef(0, 2, "")])
    assert "配错" in str(ei.value)


def test_一个条目可以挂多条来源(store) -> None:
    """※ 跨群转发的形状：一条条目、三条来源。

    这是那个契约错误的**正面用例** —— 它证明「来源比条目多」是合法的，
    而不只是「不再报错」。
    """
    s, _ = store
    s.upsert_messages([
        PendingMessage(msg_id=i, group_id=1, ts=1000, sender="s", content=f"c{i}")
        for i in (10, 20, 30)
    ])
    [iid] = s.insert_items(
        [_item(title="期中活动报名截止")],
        [
            SourceRef(1, 10, ""),   # item_id=1 ⇒ 合并后第 1 条
            SourceRef(1, 20, ""),
            SourceRef(1, 30, ""),
        ],
    )
    detail = s.item_detail(iid)
    assert detail.source_count == 3
    assert len(s.sources_of(iid)) == 3


def test_写入消息是幂等的(store) -> None:
    """※ 重跑摄取不该产出重复消息 —— 重复消息会导致重复条目，而那是无声的。"""
    s, _ = store
    msg = PendingMessage(msg_id=5, group_id=1, ts=0, sender="s", content="c")
    assert s.upsert_messages([msg]) == 1
    assert s.upsert_messages([msg]) == 0
    assert s.message_count() == 1


# ── 对外表名（契约）──────────────────────────────────────────────────


def test_锁住的表名都在(tmp_path) -> None:
    """※ 这些表名是**对外契约**。改了就失效。"""
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    conn = sqlite_store.connect(db)
    try:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    for required in ("items", "messages", "item_sources", "digests", "item_digests"):
        assert required in names, f"契约表名 {required} 不在"
    # ※ 失败记账的两张表**不在**对外契约那五个名字里（那是锁住的），
    # 但「建库之后它们得在」是一条独立的事实 —— 缺了的话，refine 会在
    # 落库那一刻报 `no such table: runs`，而那要到那时才响。
    for required in ("runs", "run_entries"):
        assert required in names, f"失败记账的表 {required} 不在"
