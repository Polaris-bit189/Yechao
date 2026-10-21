"""失败记账落库 —— 「每一条消息都能回答为什么」的兑现。

## 这个文件要证明的四件事

1. **账留下来了**：进程退出之后还能按 `run_id` / `msg_id` 查出来。
   （在这之前，`FailureLedger` 是一个内存列表，`run()` 一返回就没了。）
2. **账里不存原文**：只有计数、标识与原因码 —— 由一条**行为**判据钉住
   （把两张表整张 dump 出来搜正文里的标记），而不是靠读代码相信。
3. **账与它解释的那次写入同生共死**：见 `test_invariants.py` 里那一对
   （一个造「账写不进去」，一个造「条目写不进去」）。
4. **一轮都不能少**：没有待处理消息的跑、一个源都读不到的摄取，
   都要留下一笔 —— 「我跑了，怎么什么都没发生」正是最需要证据的一问。

## 为什么自己造一套桩

`test_wiring.py` 那几个桩是为「接线」写的（永远成功、返回固定 JSON）。
这里要的是**失败**（模型端点挂了、解析出 0 条），而一个永远成功的桩
造不出那两种账 —— 拿它来测账会得到一个恒为空的结果，而断言恒为真。

## 假号码、假群号

※ 红线：**自己编**。下面这些号按格式编（群号 820000001 起、
消息号从 1 起），不是从任何地方抄的。
"""

from __future__ import annotations

import sqlite3

import pytest

from yechao.application import extract as extract_uc
from yechao.domain.errors import (
    FailureLedger,
    LedgerEntry,
    LedgerEntryTooLong,
    ModelError,
    RunRecord,
)
from yechao.domain.models import PendingMessage
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.audit_log import NullAuditLog
from yechao.infrastructure.runtime.clock import FixedClock

GROUP = 820000001
#: 固定时刻：判据是「测试不许依赖跑用例的那一刻是几号」。
NOW = 1792333200


# ── 桩 ──────────────────────────────────────────────────────────────


class _Cfg:
    """最小配置桩。

    ※ 群 tier 给 `high` —— 预筛对高优群**一律保留**。不这么做的话，
    这段正文会被预筛丢掉，于是「模型那一侧」根本不会发生，而这几条用例
    会变成在测预筛（它已经有自己的判据了）。
    """

    def groups(self):
        from yechao.domain.config_models import GroupConfig

        # ※ 给 5 个高优群：`test_没跑到的消息也留下账` 要靠「一条消息一批」
        # 造出多批（切批的首要约束是**不跨群**，所以群就是批次的自然分界）。
        return [
            GroupConfig(id=GROUP + i, name=f"群{i + 1}", tier="high")
            for i in range(5)
        ]

    def persons(self):
        return []

    def categories(self):
        from yechao.domain.categories import default_categories
        from yechao.domain.config_models import CategorySet

        return CategorySet(categories=default_categories(), aliases={})

    def nicknames(self):
        return []


class _DeadModel:
    """端点挂了的模型 —— 每一批都抛 `ModelError`。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        raise ModelError("端点连不上")


class _MuteModel:
    """回了、但回的不是可解析的东西 —— 「解析出 0 条」那一种失败。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        return "好的，我看看。"  # 没有 JSON


class _Prompt:
    EXTRACT_SYSTEM = "system"
    EXTRACT_MAX_TOKENS = 1024

    def build_extract_user(self, messages, scrubbed):
        return "\n".join(scrubbed)


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    s = SqliteStore(sqlite_store.connect(db))
    yield s
    s.conn.close()


def _deps(store, model) -> extract_uc.ExtractDeps:
    return extract_uc.ExtractDeps(
        messages=store, items=store, model=model,
        audit=NullAuditLog(), clock=FixedClock(ts=NOW),
        config=_Cfg(), prompt=_Prompt(),
    )


def _seed(
    store, msg_ids, *, content: str = "请在明天下午三点前把材料交到教务处",
    group: int = GROUP,
) -> None:
    """写几条消息。

    ※ 每条正文都带上自己的 msg_id：预筛的「刷屏」判据的桶是
    (发送者, 群, 正文) 三元组，而三条一模一样的消息会被判成刷屏 ——
    那会让「模型那一侧」根本不发生，用例也就测错了对象。
    """
    store.upsert_messages([
        PendingMessage(
            msg_id=i, group_id=group, ts=1000 + i, sender="某人",
            content=f"{content}（第 {i} 条）",
        )
        for i in msg_ids
    ])


def _count(store, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _dump(store, *tables: str) -> str:
    """把若干张表的**每一列**读出来拼成一段文本。

    ※ 整表 dump 而不是挑几列：判据是「账里不存原文」，而「原文可能从哪一列
    漏进去」这个问题不该由写测试的人来猜 —— 猜漏一列，判据就静默失效。
    """
    out: list[str] = []
    for table in tables:
        for row in store.conn.execute(f"SELECT * FROM {table}"):
            out.extend("" if v is None else str(v) for v in tuple(row))
    return "\n".join(out)


# ── 1. 账留下来了 ───────────────────────────────────────────────────


def test_跑一轮之后账在库里_且按消息查得到(store) -> None:
    """※ **这条用例抓的就是审计说的那件事。**

    观察到什么现象：`FailureLedger` 是内存列表，`extract.run()` 一返回它就
    没了 —— 「预筛丢弃、批失败、没跑到」只出现在 CLI 那一次输出里，
    **进程退出即消失**。

    判据是行为：跑一轮（模型端点挂了），然后**从库里**查
    「这一条消息为什么没进条目」，答案必须在那儿。
    """
    _seed(store, (1, 2))
    outcome = extract_uc.run(_deps(store, _DeadModel()))

    assert outcome.run_id, "这一轮没留下 run_id —— 账没落库"
    assert _count(store, "runs") == 1
    # 两条消息在同一批里，各记一笔（按条记，不是按批记一笔）
    assert _count(store, "run_entries") == 2

    rows = store.entries_of_message(1)
    assert [e.kind for e in rows] == ["batch_failed"]
    e = rows[0]
    assert e.msg_id == 1
    assert e.group_id == GROUP
    assert e.batch_no == 1, "批号是 1-based"
    assert e.reason == "ModelError", "原因码应当是异常类型名，不是散文"


class _HalfModel:
    """一批里**只抽出一条** —— 另一条是「模型看过、里面没有」。

    ※ 为什么需要这个桩：`[]`（一条都没抽出来）在本项目里是**真失败**
    （`parse_empty`，下一轮要重试），所以「看过但没抽出东西」只发生在
    「一批里抽出了一些、但不是每一条」这种情形。
    """

    _ROWS = (
        "```json\n"
        '[{"kind":"todo","title":"交材料",'
        '"quote":"请在明天下午三点前把材料交到教务处（第 1 条）",'
        '"detail":null,"deadline":null,"start":null,"place":null,"amount":null}]\n'
        "```"
    )

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        return self._ROWS


def test_判完的结局只有那几种(store) -> None:
    """※ 这一条守的是**不能算进去**的那几个 —— 每一个对应一种「算进去就会丢东西」。

      · `batch_failed` —— 一次网络抖动不该变成永久丢失
      · `unprocessed`  —— 它存在的意义就是「没跑到，下一轮继续」
    """
    from yechao.application import extract as ex

    store.conn.execute(
        "INSERT INTO runs (run_id, kind, started_at, finished_at, status, seen, produced)"
        " VALUES ('r1', 'refine', 1, 2, 'ok', 0, 0)"
    )
    kinds = (ex.FILTERED, ex.SPAM, ex.NO_ITEM, ex.NO_KIND,
             ex.BATCH_FAILED, ex.UNPROCESSED)
    for i, kind in enumerate(kinds, start=1):
        store.conn.execute(
            "INSERT INTO run_entries (run_id, kind, msg_id, group_id, reason)"
            " VALUES ('r1', ?, ?, ?, 'x')",
            (kind, i, GROUP),
        )
    # 没有 msg_id 的那一笔（`quote_mismatch`）不该被算进来
    store.conn.execute(
        "INSERT INTO run_entries (run_id, kind, group_id, reason)"
        " VALUES ('r1', 'quote_mismatch', ?, 'not_in_batch')",
        (GROUP,),
    )
    store.conn.commit()

    judged = store.judged_msg_ids(kinds=ex.JUDGED_KINDS)
    assert judged == {1, 2, 3, 4}, (
        f"判完的集合是 {sorted(judged)} —— 判据与 `JUDGED_KINDS` 那一段说明不符"
    )


def test_判完的消息下一轮不再重判(store) -> None:
    """※ 这一条守的是**代价**，不是正确性。

    观察到什么现象：`covered_msg_ids()` 只认「产出过条目」的消息，于是被预筛
    刷掉的那些每一轮都要重新过一遍预筛。实测（本机 QQ 库 49.2 万条）：一轮
    150 秒，其中 114 秒花在重新预筛那 49 万条上，而第二轮只新写了 21 条 ——
    它**不报错**，看起来只是「有点慢」。

    判据：第二轮**看不到**第一轮已经判完的消息。
    """
    store.upsert_messages([
        PendingMessage(msg_id=1, group_id=GROUP, ts=1000, sender="某人",
                       content="好的"),  # 预筛会丢（纯应答）
    ])
    model = _DeadModel()
    first = extract_uc.run(_deps(store, model))
    assert [e.kind for e in store.entries_of_message(1)] == ["filtered"]
    assert model.calls == 0, "前提：这一条根本没走到模型"

    second = extract_uc.run(_deps(store, _DeadModel()))
    assert second.batches_total == 0, (
        "判完的消息第二轮又被送去切批了 —— 「已处理」的定义没生效"
    )


def test_模型看过却没抽出东西的下一轮不再送(store) -> None:
    """同一批里抽出了别的、但没抽出这一条 ⇒ 这一条也算判完。

    ※ 这一笔账原来没有，于是那些消息**每一轮都会被重新送去模型**。
    """
    _seed(store, (1, 2))          # 同一个群 ⇒ 同一批
    model = _HalfModel()
    extract_uc.run(_deps(store, model))
    assert model.calls == 1
    assert [e.kind for e in store.entries_of_message(2)] == ["no_item"], (
        "「看过了、里面没有」没留下账 —— 账要能回答「这条为什么没进条目」"
    )

    second = extract_uc.run(_deps(store, _HalfModel()))
    assert second.batches_total == 0, "看过的那一条第二轮又被送进去了"


def test_rejudge_把判完的消息重新纳回来(store) -> None:
    """※ 这是「已处理 = 账里已判完」那条判据的**出口**。

    改了预筛规则（群 tier、动作词）之后，旧消息本来不会被重判 —— 而那正是
    「每轮不再重筛全库」换来的代价。`rejudge=True` 把那些记录**这一次**不当数。

    ※ 但 `covered`（产出过条目的）**照旧跳过**：再判一遍会产出**重复条目**
    —— 那不叫重判，那叫重抽。
    """
    store.upsert_messages([
        PendingMessage(msg_id=1, group_id=GROUP, ts=1000, sender="某人",
                       content="好的"),  # 预筛会丢（纯应答）
    ])
    first = extract_uc.run(_deps(store, _DeadModel()))
    assert store.get_run(first.run_id).seen == 1

    second = extract_uc.run(_deps(store, _DeadModel()))
    assert store.get_run(second.run_id).seen == 0, "判完的又进来了 —— 判据没生效"

    third = extract_uc.run(_deps(store, _DeadModel()), rejudge=True)
    assert store.get_run(third.run_id).seen == 1, (
        "--rejudge 没把它重新纳进来 —— 改了预筛规则之后旧消息就再没有回头路"
    )


def test_预算不够时先跑最新的(store) -> None:
    """※ 预算不够跑完全部批次时，先跑**最新的**那一批。

    观察到什么现象：切批的输出是「按群号、群内按时间升序」—— 那对切批是对的，
    但它同时决定了「预算先花在哪」。一个刚接上的真实来源可以带进几十万条历史
    （本机 QQ：49.2 万条），于是头 200 批全是几个月前的消息，今天的排在最后。
    实测：接上 QQ 之后第一批抽出来的是 **7 月 17 日**的消息。

    而本产品的产出是按天的日报 —— 今天的通知晚几小时进库，日报就是空的，
    而界面上它与「今天群里很安静」是同一句话。
    """
    for i in range(1, 4):
        _seed(store, (i,), group=GROUP + i - 1)   # ts 递增
    extract_uc.run(_deps(store, _DeadModel()), budget=1)

    assert [e.kind for e in store.entries_of_message(3)] == ["batch_failed"], (
        "预算花在了最老的那一批上 —— 今天的通知会排在几个月前的消息后面"
    )
    assert [e.kind for e in store.entries_of_message(1)] == ["unprocessed"]


def test_没跑到的下一轮还会再跑(store) -> None:
    """※ 与上一条**相反**的那一半：预算没跑到的**必须**留下。

    算进去的症状是「一次预算不够 = 那几条永远不被抽取」，而它不报错 ——
    日报里只是少了几条。
    """
    for i in range(4):
        _seed(store, (i + 1,), group=GROUP + i)   # 不跨群 ⇒ 一批一个群

    extract_uc.run(_deps(store, _DeadModel()), budget=1)
    # 最新的一批跑了（失败），较早的没跑到
    assert [e.kind for e in store.entries_of_message(1)] == ["unprocessed"]

    second = extract_uc.run(_deps(store, _DeadModel()), budget=1)
    assert second.batches_total > 0, (
        "没跑到的那些第二轮不见了 —— 一次预算不够变成了永久丢失"
    )


def test_预筛丢弃的账带判据名(store) -> None:
    """预筛丢掉的那一条，事后要能说清是**哪条判据**丢的。"""
    store.upsert_messages([
        PendingMessage(msg_id=1, group_id=GROUP, ts=1000, sender="某人",
                       content="好的"),
    ])
    extract_uc.run(_deps(store, _DeadModel()))

    rows = store.entries_of_message(1)
    assert [e.kind for e in rows] == ["filtered"]
    assert rows[0].reason, "预筛的账必须带判据名（不然「为什么丢的」答不出来）"
    assert rows[0].group_id == GROUP


def test_解析出零条与模型挂了是两种原因码(tmp_path) -> None:
    """※ 两件事的下一步不同：一个去查端点，一个去查提示词/解析。

    混成一个原因码，排查的人会在错的地方找。这条把两个原因码并排钉住。
    """
    reasons: list[str] = []
    for model in (_DeadModel(), _MuteModel()):
        db = tmp_path / f"{type(model).__name__}.db"
        sqlite_store.init_db(db)
        s = SqliteStore(sqlite_store.connect(db))
        try:
            _seed(s, (1,))
            extract_uc.run(_deps(s, model))
            reasons.append(s.entries_of_message(1)[0].reason)
        finally:
            s.conn.close()

    assert reasons == ["ModelError", "parse_empty"]


def test_没跑到的消息也留下账(store) -> None:
    """※ 预算耗尽 ⇒ 剩下的消息记 `unprocessed`。

    这一条原来只活在 CLI 的计数里；而「这一条为什么没跑到」正是用户
    最常问的一问（「我明明有 300 条消息，怎么只抽了 20 条」）。
    """
    # ※ **一条消息一个群**：切批的首要约束是不跨群，所以群就是批次的
    # 自然分界 —— 这样 5 条消息切出 5 批，`budget=1` 之后有 4 批没跑到。
    for i in range(1, 6):
        _seed(store, (i,), group=GROUP + i - 1)
    outcome = extract_uc.run(_deps(store, _DeadModel()), budget=1)

    assert outcome.batches_total == 5, outcome.batches_total
    # ※ **先跑的是最新的一批**（见 `extract.run` 里「新消息优先」那一段），
    # 所以没跑到的是较早的那些 —— 这里查第 1 条（最老）。
    later = [e for e in store.entries_of_message(1) if e.kind == "unprocessed"]
    assert later, "没跑到的消息没有账 —— 那一问就答不出来"
    assert later[0].reason == "budget"
    assert later[0].batch_no and later[0].batch_no >= 2


class _MixedModel:
    """一批产出条目、另一批回空数组。

    ※ 造这个桩的理由：**条目与账必须在同一轮里都落下来**。
    「跑成功的那一轮没有账」与「账只在失败的那一轮才有」看起来一样，
    而后者会让「每一条消息都能回答为什么」在成功的那一半上失效。
    """

    #: 模型回的「这一批没有条目」—— 包着 Markdown 围栏（真实模型不是每次
    #: 都给裸 JSON，所以这里**故意**带上围栏）。
    _EMPTY = """```json
[]
```"""

    #: 正常产出的一批（同样包着围栏）。
    _ROWS = """```json
[{"kind":"todo","title":"交材料","quote":"请在明天下午三点前把材料交到教务处","detail":null,"deadline":null,"start":null,"place":null,"amount":null}]
```"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        # 第 2 条消息自成一个群（切批不跨群）⇒ 它独占一批 ⇒ 那一批回空数组。
        if "第 2 条" in user:
            return self._EMPTY
        return self._ROWS


def test_有产出的一轮里条目与账都在(store) -> None:
    """※ **成功的那一轮也要留下账**（不只是失败的那一轮）。

    `test_跑一轮之后账在库里_且按消息查得到` 用的是「模型全挂了」那一轮 ——
    它证明不了「有产出时账也在」。这一条补上：一条消息产出条目、
    另一条所在的那一批**模型回了合法的 `[]`** ⇒ 条目 1 条、账 1 笔，
    **两者同一次调用落库**。

    ※ 那笔账是 `no_item`（「看过了，这一批里没有要办的事」），**不是**
    `batch_failed`。合法的 `[]` 与「解析不出 JSON」是两件事：前者这一批跑成了，
    后者要重试。混成一个的症状实测过 —— 接上真实 QQ 群之后，普通闲聊被模型
    老实回 `[]`，却被记成失败，于是那一轮永远「有失败」、那些消息永远重发。
    """
    _seed(store, (1,), group=GROUP)
    _seed(store, (2,), group=GROUP + 1)
    outcome = extract_uc.run(_deps(store, _MixedModel()))

    assert len(outcome.items) == 1, "前提：有一批产出了条目"
    assert _count(store, "items") == 1
    run = store.get_run(outcome.run_id)
    assert run is not None
    assert run.produced == 1
    entries = store.entries_of_run(run.run_id)
    assert [e.kind for e in entries] == ["no_item"], (
        "空数组那一批的账没落 —— 有产出的那一轮里，账被吞了"
    )
    assert entries[0].msg_id == 2


def test_没有待处理消息也留下一笔账(store) -> None:
    """※ **空库跑一轮 refine 也要留下账。**

    「我跑了 refine，怎么什么都没发生」是最需要证据的一问 ——
    而它原来什么痕迹都没有：库里既没有条目、也没有账，
    两者看起来完全一样。
    """
    outcome = extract_uc.run(_deps(store, _DeadModel()))

    assert outcome.run_id
    runs = store.recent_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].seen == 0
    assert runs[0].produced == 0
    assert runs[0].status == "ok", "一条消息都没看到，这一轮没有「没成的事」"
    assert store.entries_of_run(runs[0].run_id) == []


def test_两轮跑各有各的账(store) -> None:
    """※ 账挂在 `run_id` 上 —— 两轮的账不能串起来。

    没有这一条，一个「永远往同一行上写」的实现也能让上面的用例通过。
    """
    # ※ 用**时间窗**把两轮的消息分开，而不是「跑完第一轮再塞第二条」——
    # 第一轮的模型挂了 ⇒ 一条条目都没产出 ⇒ `covered_msg_ids()` 是空的
    # ⇒ 第二条消息进来之后，**第一条会再被处理一遍**（那是**对的**：
    # 没产出条目的消息不该算已处理）。拿它当「两轮」的前提会得到
    # 一个看着像 bug 的结果，而真相是前提写错了。
    _seed(store, (1, 2))
    first = extract_uc.run(_deps(store, _DeadModel()), until=1002)
    second = extract_uc.run(_deps(store, _DeadModel()), since=1002)

    assert first.run_id != second.run_id
    assert _count(store, "runs") == 2
    assert [e.msg_id for e in store.entries_of_run(first.run_id)] == [1]
    assert [e.msg_id for e in store.entries_of_run(second.run_id)] == [2]


def test_抬头与账对得上(store) -> None:
    """抬头里的 `seen` / `produced` / `model_sent` 必须与**同一轮的账**一致。

    ※ 这一条是「账与抬头同事务」的正面用例：分开写的话，
    抬头可能是这一轮、账是上一轮的。
    """
    _seed(store, (1, 2, 3))
    outcome = extract_uc.run(_deps(store, _DeadModel()))

    run = store.get_run(outcome.run_id)
    assert run is not None
    assert run.seen == 3
    assert run.produced == 0
    assert run.model_eligible == 3
    # ※ `model_sent` 是 **0**，不是 3：端点连不上时消息**没有真的送进去**
    # （覆盖率 0%）。「该送 3 条」与「真的送到了 0 条」是两句话，
    # 而覆盖率就是这两个数的比 —— 把它们填成同一个数会让覆盖率永远是 100%。
    assert run.model_sent == 0
    assert run.status == "failed", "有整批失败 ⇒ status 与退出码同一条"
    assert len(store.entries_of_run(run.run_id)) == 3


def test_没有这一次跑时_get_run_返回_None(store) -> None:
    """※ 「查不到这一次跑」与「这一轮没有账」**必须分得开**。

    混成一个空列表的话，`ledger --run 打错的id` 会显示「这一轮没有账」
    —— 一句听起来像「一切正常」的假话。
    """
    assert store.get_run("不存在") is None
    assert store.entries_of_run("不存在") == []


# ── 2. 账里不存原文 ─────────────────────────────────────────────────


def test_账里不存原文(store) -> None:
    """※ **隐私红线，而且它是一条行为判据。**

    观察到什么现象：`FailureLedger` 原来的 `detail` 是自由字符串 ——
    `quote_mismatch` 记的是**候选标题的前 16 字**、`gate_cleared` 记的是
    标题。标题是模型从正文里抄的，也就是**内容**。那些账一旦落库，
    就是把内容写进了磁盘。

    ⇒ 判据：把 `runs` / `run_entries` 两张表**整张 dump** 出来，正文里那串
    标记一个字都不许出现。

    怎么验证：把 `reason` 改回 `cand.title`，这条立刻红。
    """
    marker = "青梧桥东侧三号门集合"
    _seed(store, (1, 2), content=f"{marker}，请带上学生证")
    extract_uc.run(_deps(store, _DeadModel()))

    assert _count(store, "run_entries") > 0, "前提：这一轮真的记了账"
    ledger_dump = _dump(store, "runs", "run_entries")
    assert marker not in ledger_dump, "账里出现了正文 —— 那是新造了一个泄漏面"
    # ※ 阳性对照：正文**确实**进了消息表（否则上面那句可能只是因为
    # 这一轮压根没跑起来 —— 空转的判据比没有判据更危险）。
    assert marker in _dump(store, "messages")


def test_原因码有长度上限_超了报错而不是截断() -> None:
    """※ 超长几乎只有一种原因：**有人把正文塞进了 `reason`**。

    所以这里**报错**，而不是截断 —— 截断会把「账里混进了正文」变成一条
    看不见的失败，而那正是本项目最怕的形状。

    ※ 这一条守的是**用例层**（`FailureLedger.record`）；数据库那一层还有
    一道（`CHECK (length(reason) <= 64)`），见下面那条。
    两道都要有：一道让错误信息说清「你塞的是正文」，一道让绕过去的人
    **也写不进去**。
    """
    led = FailureLedger()
    with pytest.raises(LedgerEntryTooLong) as ei:
        led.record("filtered", reason="正文" * 40)
    assert "64" in str(ei.value), "错误信息要说清上限是多少"
    # 阴性对照：正常的短码不该被拦
    led.record("filtered", reason="pure_ack")
    assert len(led) == 1


def test_数据库自己也拒绝超长的原因码(store) -> None:
    """※ 用例层那道是「说清原因」，**守的是数据库自己**。

    做法：**绕开** `FailureLedger` 直接构造一笔超长的账，让仓储去写 ——
    写不进去才对。
    """
    _seed(store, (1,))
    run = RunRecord(
        run_id="run-x", kind="refine", started_at=NOW, finished_at=NOW,
        entries=(LedgerEntry(kind="filtered", reason="x" * 65),),
    )
    with pytest.raises(sqlite3.Error):
        store.insert_items([], [], run=run)
    assert _count(store, "runs") == 0, "抬头也不该留下"
    assert _count(store, "run_entries") == 0


# ── 3. 另外两个用例也留账（ingest / digest）─────────────────────────


def test_摄取时读不到的源也留下账(store) -> None:
    """※ 「哪几个源没读到」原来只活在 `IngestOutcome.errors` 里。

    CLI 打印完它就没了 —— 而「今天怎么只有这些」恰恰是事后最要问的一句。
    判据：一轮摄取跑完，库里有一条 `ingest` 抬头 + 一笔
    `source_unreadable` 账，**原因码里带源名**（不然连哪个源坏了都不知道）。
    """
    from yechao.application import ingest as ingest_uc
    from yechao.domain.errors import SourceUnavailable

    class _坏源:
        name = "web"

        def read(self, *, since=None, until=None):
            raise SourceUnavailable("目标文件不在")

    deps = ingest_uc.IngestDeps(
        messages=store, sources=[_坏源()], clock=FixedClock(ts=NOW)
    )
    outcome = ingest_uc.run(deps)

    assert outcome.run_id
    run = store.get_run(outcome.run_id)
    assert run is not None
    assert run.kind == "ingest"
    assert run.status == "failed", "有源没读到 ⇒ status 与退出码同一条"
    entries = store.entries_of_run(run.run_id)
    assert [(e.kind, e.reason) for e in entries] == [
        ("source_unreadable", "web:SourceUnavailable")
    ]
    # ※ 原因码里**只有源名与类型名**，没有 `str(e)` 的上下文。
    assert "目标文件不在" not in entries[0].reason


def test_日报模型没跑成时留下账_而不是只留一片机械行(store) -> None:
    """※ 日报这一路的失败原来只出现在 CLI 的一行 warn 里。

    而「这一篇日报的每一行都是机械行」在页面上看起来**完全正常** ——
    正文有内容、排版对、没有报错。所以「模型没跑成」必须落进账里。

    判据：一轮 digest 跑完，抬头是 `failed`，账里有一笔 `model_failed`
    （原因码 = 异常类型名），以及**每条条目各一笔** `line_mechanical`
    （主体是条目 id）—— 后者答的是「**这一条**为什么没有模型写的版本」。
    """
    from yechao.application import digest as digest_uc
    from yechao.domain.models import SourceRef

    _seed(store, (7,))
    [item_id] = store.insert_items(
        [_item(title="期中活动报名")], [SourceRef(0, 7, "")]
    )
    clock = FixedClock(ts=NOW)
    deps = digest_uc.DigestDeps(
        items=store, digests=store, model=_DeadModel(), clock=clock,
        config=_Cfg(), audit=NullAuditLog(),
    )
    outcome = digest_uc.run(deps, day=clock.day_of(1000), use_model=True)

    assert outcome.run_id
    run = store.get_run(outcome.run_id)
    assert run is not None
    assert run.kind == "digest"
    assert run.status == "failed", "模型没跑成，日报照样有内容 —— 但账要说出来"

    entries = store.entries_of_run(run.run_id)
    kinds = sorted(e.kind for e in entries)
    assert kinds == ["line_mechanical", "model_failed"], kinds
    assert [e.reason for e in entries if e.kind == "model_failed"] == ["ModelError"]
    assert [e.item_id for e in entries if e.kind == "line_mechanical"] == [item_id]


def _item(**kw):
    """造一个条目值对象（只填用例真的会读的字段）。"""
    from yechao.domain.models import Item

    base = dict(
        item_id=0, kind="todo", kind_label="要办", title="测试条目",
        detail=None, event_ts=1000, deadline_ts=None, group_id=GROUP,
        group_name="群1", actor=None, actor_account=None, place=None,
        amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


# ── 4. 只读侧也能查账 ───────────────────────────────────────────────


def test_只读连接查得到账_但写不进账(tmp_path) -> None:
    """※ 查账是**读**，走只读连接（不变量 ①）。

    账是证据 —— 一个能改证据的查询工具不叫证据。所以两件事都要断言：
    只读连接**查得到**账、**写不进**账。
    """
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    w = SqliteStore(sqlite_store.connect(db))
    try:
        _seed(w, (1,))
        extract_uc.run(_deps(w, _DeadModel()))
    finally:
        w.conn.close()

    ro = SqliteStore(sqlite_store.open_readonly(db))
    try:
        runs = ro.recent_runs(limit=5)
        assert len(runs) == 1
        assert ro.entries_of_run(runs[0].run_id), "只读连接应当查得到账"
        assert ro.entries_of_message(1), "按消息查也要能查到"
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute(
                "INSERT INTO runs (run_id, kind, started_at, finished_at) "
                "VALUES ('x', 'refine', 0, 0)"
            )
    finally:
        ro.conn.close()
