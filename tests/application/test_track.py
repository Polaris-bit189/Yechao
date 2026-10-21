"""追踪 —— 人物与名词，**流水线上的一层标记**。

## 这个文件要证明的五件事

1. **没有规则时，与不追踪逐字节相同。** 判据是行为：同一条消息在两种跑法
   下产出同样的结果 —— 而不是「代码看起来有个 if」。
2. **被追踪人物的消息不被预筛丢掉。** 对照组是同一条消息、同一个群，
   只是发送者没被追踪 —— 它必须**仍然**被丢掉。这一条是整个特性的核心，
   而它只有在有对照组时才说明得了问题（否则「留下来了」可能只是因为它
   本来就该留）。
3. **两趟互不相交。** 同一条消息被抽两遍会产出两条条目，而**两条都不报错**
   —— 所以这条只能用「来源集合恰好等于全集、且每条来源只挂一个条目」来钉。
4. **名词规则只影响标记，不影响抽取。** 它不该让抽取多花一次调用。
5. **补抽那一趟不改写群配置。** 「把群层级标成高优」是**视图**，不是写盘。

## 样本全部自己编

群号用 820000001 段，账号用 17700000000 段，消息内容是编出来的短句。
没有一条来自任何真实语料。
"""

from __future__ import annotations

import json
import re

import pytest

from yechao.application import track
from yechao.bootstrap import Settings, make_app
from yechao.domain.config_models import TrackRule
from yechao.domain.models import Item, PendingMessage

GROUP = 820000001
#: 被追踪的那个人。
TRACKED = 17700001001
#: 对照组 —— 同一个群里、发同一句话，但**没被追踪**。
OTHER = 17700002002

#: 这一条**一定**会被预筛的默认判据丢掉：没有日期/金额/链接锚、没有祈使
#: 结构、长度也够（不是 `too_short`）⇒ 落在 `no_signal` 上。
#: ※ 它是「追踪到底做了什么」的对照组。内容自己编的，格式对就够。
DROPPABLE = "明天的事定下来了"


# ── 桩 ──────────────────────────────────────────────────────────────


class _Model:
    """模型桩。把提示词里每一行消息**逐字抄回来**当条目。

    ※ 必须逐字抄：`extract.match_quote` 是逐字匹配，抄错一个字整条就被丢掉
    —— 那样这个文件测的就不是追踪，而是「桩写得对不对」。
    """

    def __init__(self, *, dead: bool = False) -> None:
        self.dead = dead
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int = 2048, response_schema: dict | None = None) -> str:
        self.calls += 1
        if self.dead:
            from yechao.domain.errors import ModelError

            raise ModelError("连不上模型端点 http://127.0.0.1:9/v1/chat/completions")

        # 类目从**真的发出去的系统提示词**里取（类目表用反引号包着 slug）。
        found = re.search(r"`([a-z_]+)`", system)
        kind = found.group(1) if found else "todo"

        out = []
        for line in user.splitlines():
            if not re.match(r"^\[\d+\] ", line):
                continue
            body = line.split(": ", 1)[-1].strip()
            if not body:
                continue
            out.append({"kind": kind, "title": body[:12], "quote": body})
        return json.dumps(out, ensure_ascii=False)


# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """一个装配好的 App，落在**临时目录**上。

    ※ 群层级是 `normal` —— 这一点是承重的：高优群里的消息本来就无条件
    保留，那样「追踪有没有生效」就测不出来了（两种跑法都会留下它）。
    """
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    cfg, data = tmp_path / "config", tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    (cfg / "groups.toml").write_text(
        "[[group]]\n"
        f"id = {GROUP}\n"
        'name = "某班"\n'
        'tier = "normal"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    # ※ 这一段**不是可有可无的**：`[llm]` 没配时 `refine` 拒绝跑 ——
    # 而这里用的是 `make_app`（不经过 CLI 的那道门），端点是个占位：
    # 客户端在用例里被桩替换掉了，这个地址从来不会被连。
    (cfg / "settings.toml").write_text(
        '[llm]\nbase_url = "http://127.0.0.1:9/v1/chat/completions"\n'
        'model = "假模型"\n',
        encoding="utf-8",
    )
    a = make_app(settings=Settings.from_paths(data_dir=data, config_dir=cfg))
    yield a
    a.close()


def _msg(msg_id: int, content: str, account: int | None, *, ts: int = 1000) -> PendingMessage:
    return PendingMessage(
        msg_id=msg_id, group_id=GROUP, ts=ts, sender="某人",
        content=content, actor_account=account,
    )


def _item(title: str, *, account: int | None = None, detail: str | None = None) -> Item:
    return Item(
        item_id=0, kind="todo", kind_label="要办的事", title=title, detail=detail,
        event_ts=1000, deadline_ts=None, group_id=GROUP, group_name="某班",
        actor=None, actor_account=account, place=None, amount=None,
        links=(), source_count=1, start_ts=None,
    )


def _person(account: int = TRACKED) -> TrackRule:
    return TrackRule(kind="person", value=str(account))


# ── 判据 ①：没有规则 ⇒ 与不追踪逐字节相同 ──────────────────────────


def test_没有人物规则时只有一趟_而且那一趟就是常规那一趟(app) -> None:
    """※ 判据是行为，不是「代码里有个 if」。

    这一条同时钉住两件事：产出为空（没人追踪 ⇒ 那条消息照常被丢），
    以及**只有一条抬头**（没多跑一趟）。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, TRACKED)])

    out = track.run(app.extract_deps(), [], budget=200)

    assert out.items == [], "没有规则时不该有东西留下来"
    assert out.forced == 0
    assert out.tracked_run_id == "", "没配人物规则却多跑了一趟 —— 那是白花的模型调用"
    assert out.ledger.count("filtered") == 1, "它应当走常规那一趟、被预筛记一笔"
    assert len(app.reader.recent_runs(limit=10)) == 1


def test_名词规则不让抽取多跑一趟(app) -> None:
    """※ 名词规则只影响**标记**（读的时候算），不参与抽取。

    它若让抽取多跑一趟，症状是「加了个名词，模型调用次数翻倍」——
    而它**不报错**，只是账单变贵。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, TRACKED)])

    out = track.run(
        app.extract_deps(), [TrackRule(kind="term", value="奖学金")], budget=200
    )

    assert out.forced == 0
    assert out.tracked_run_id == ""
    assert out.items == []


# ── 判据 ②：被追踪人物的消息不被预筛丢掉 ────────────────────────────


def test_被追踪人物的消息不被预筛丢掉(app) -> None:
    """※ **这一条是特性的核心**，而它只有在有对照组时才说明问题 ——
    对照组见下一条。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, TRACKED)])

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert out.forced == 1, "它应当被划进补抽那一趟"
    assert out.tracked_run_id, "补抽那一趟要留下自己的抬头（不然「跑了几趟」查不出来）"
    assert len(out.items) == 1, (
        "被追踪的人说的话没有进条目 —— 它被预筛丢掉了。"
        "这一条是追踪存在的全部意义。"
    )
    assert out.items[0].actor_account == TRACKED


def test_同一条消息没被追踪时仍然被丢掉(app) -> None:
    """※ 对照组。**没有它，上一条证明不了任何东西** ——
    「留下来了」可能只是因为它本来就该留。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, OTHER)])

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert out.forced == 0
    assert out.items == []
    assert out.ledger.count("filtered") == 1


def test_匿名消息不被人物规则命中(app) -> None:
    """※ 与 `search(persons=...)` 同一条判据：**匿名条目 `actor_account` 为
    None**，不允许用 0 冒充 —— 用 0 冒充会让「追踪 0 号账号」意外命中所有
    匿名消息，而那看起来像「这个人今天说了很多」。
    """
    assert track.hit_person(_msg(1, DROPPABLE, None), [_person()]) == []
    assert track.hits_for_item(_item("关于奖学金的通知"), [_person()]) == []
    # 反例：同样的内容、只是**有账号** ⇒ 命中。
    assert track.hit_person(_msg(1, DROPPABLE, TRACKED), [_person()]) != []


def test_纯应答即使被追踪也仍然被丢掉(app) -> None:
    """※ **边界，写下来是因为它是刻意的。**

    硬丢弃（纯应答 / 纯符号）判的是「这条消息**没有信息量**」，与「谁发的」
    无关。把一条「收到」抽成条目只会让日报多一行噪声 —— 而模型对它也只能
    回空。追踪要防的是「这个人的话**因为没命中锚、或者太短**而被默认丢掉」，
    那一条由层级判据挡不住、也正是补抽那一趟解决的。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, "收到", TRACKED)])

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert out.forced == 1, "它确实被划进了补抽那一趟"
    assert out.items == [], "但硬丢弃仍然生效 —— 这是刻意留的边界"


# ── 判据 ③：两趟互不相交 ────────────────────────────────────────────


def test_两趟互不相交_同一条消息不会被抽两遍(app) -> None:
    """※ 重复条目**不报错** —— 它只是让日报里多出一行。

    所以这条判据只能看**结构**：来源集合恰好等于全集，且每条来源只挂
    一个条目。
    """
    app.model = _Model()
    app.store.upsert_messages([
        _msg(1, DROPPABLE, TRACKED),          # 走补抽那一趟
        _msg(2, "10月28日前交材料", OTHER),    # 走常规那一趟（有日期锚）
    ])

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert len(out.items) == 2
    covered = app.reader.covered_msg_ids()
    assert covered == {1, 2}, f"产出条目的来源集合不对：{covered}"

    items = app.reader.all_items()
    assert len(items) == 2, f"库里留下了 {len(items)} 条 —— 有重复条目"
    per_item = {
        it.item_id: {s["msg_id"] for s in app.reader.sources_of(it.item_id)}
        for it in items
    }
    flat = [mid for s in per_item.values() for mid in s]
    assert sorted(flat) == [1, 2], f"同一条消息被挂到了多个条目上：{per_item}"


def test_两趟各留一条抬头(app) -> None:
    """※ 「这一次跑到底跑了几趟」要能查 —— 混成一条就看不出来了。"""
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, TRACKED)])

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert out.run_id and out.tracked_run_id
    assert out.run_id != out.tracked_run_id
    ids = {r.run_id for r in app.reader.recent_runs(limit=10)}
    assert {out.run_id, out.tracked_run_id} <= ids


# ── 判据 ④：名词命中 ────────────────────────────────────────────────


def test_名词命中在标题与详情里都算() -> None:
    rules = [TrackRule(kind="term", value="奖学金")]
    assert [r.key for r in track.hits_for_item(_item("关于奖学金评定的通知"), rules)] == [
        "term:奖学金"
    ]
    assert [
        r.key for r in track.hits_for_item(_item("通知", detail="奖学金名单已公示"), rules)
    ] == ["term:奖学金"]
    # ※ 反例：不做分词 ⇒ 「助学金」不该命中「奖学金」。
    assert track.hits_for_item(_item("助学金申请"), rules) == []


def test_名词命中是子串匹配_不做分词() -> None:
    """※ 判据的形状写下来：`"奖学金" in 文本`。

    子串判据可以被穷举测试（哪条命中、哪条不命中都能逐条钉住），
    而分词要引词典与切分器 —— 那会把「为什么命中」变成一句要再解释一次的话。
    """
    rules = [TrackRule(kind="term", value="奖学")]
    assert track.hit_terms("关于奖学金的通知", rules)
    assert track.hit_terms("关于助学金的通知", rules) == []
    assert track.hit_terms("", rules) == []
    assert track.hit_terms(None, rules) == []


def test_同一条条目可以同时命中人物与名词() -> None:
    rules = [_person(), TrackRule(kind="term", value="奖学金")]
    hits = track.hits_for_item(
        _item("奖学金名单", account=TRACKED, detail=None), rules
    )
    assert {r.key for r in hits} == {f"person:{TRACKED}", "term:奖学金"}


def test_标记里不带规则以外的任何东西() -> None:
    """※ `as_dict` 是 `/api/tracked` 的输出形状 —— 只有 kind 与 value。

    多塞一个字段（比如命中的位置、或者原文片段）会把**内容**带进 API 的
    追踪那一路，而那条路的用途是「谁提到了什么」，不是「他原话怎么说的」。
    """
    r = TrackRule(kind="term", value="奖学金")
    assert r.as_dict() == {"kind": "term", "value": "奖学金"}
    assert r.key == "term:奖学金"


# ── 判据 ⑤：补抽那一趟只改**视图**，不改配置 ────────────────────────


def test_补抽那一趟不改写群配置(app) -> None:
    """※ 「把群层级标成高优」是**这一趟的视图**，不是一次配置更新。

    真写盘的症状很具体：追踪一个人，结果那个群从此变成高优群 ——
    预筛对它再也不起默认丢弃的作用，而用户在配置文件里看到的
    `tier = "normal"` 与**实际行为**不一致。
    """
    app.model = _Model()
    app.store.upsert_messages([_msg(1, DROPPABLE, TRACKED)])
    before = app.config.groups_path.read_text(encoding="utf-8")

    out = track.run(app.extract_deps(), [_person()], budget=200)

    assert out.items, "前提：这一趟真的走了补抽"
    assert app.config.groups_path.read_text(encoding="utf-8") == before
    assert app.config.groups()[0].tier == "normal"


def test_提升视图没有写入口(app) -> None:
    """※ 一个**能被写入**的「临时视图」迟早会被某个调用方真的写进去。

    所以 `_PromotedConfig` 刻意**不转发** `set_*`：调它们会
    `AttributeError`（响的），而不是静默写盘。
    """
    view = track._PromotedConfig(object(), {GROUP})
    for name in ("set_groups", "set_persons", "set_categories", "set_llm"):
        with pytest.raises(AttributeError):
            getattr(view, name)


def test_提升视图会给没配过的群补一项(app) -> None:
    """※ 实测踩到的一条：`prefilter.prescreen` 取层级用的是
    `tiers.get(group_id, "normal")` —— 一个**不在 `groups.toml` 里**的群
    拿到的是 `normal`，于是「只改已有配置」的写法对它是**不生效**的。

    而「消息来自一个没配过的群」真会发生：源读到什么就是什么。

    ※ 合成那一项的 `name` 必须与 `extract._group_name` 的回落值逐字一致
    （`群<id>`）—— 它会被写进条目的 `group_name`。
    """
    from yechao.domain.config_models import GroupConfig

    class _Cfg:
        def groups(self):
            return [GroupConfig(id=1, name="某班", tier="normal")]

    view = track._PromotedConfig(_Cfg(), {1, 999})
    by_id = {g.id: g for g in view.groups()}
    assert by_id[1].tier == "high"
    assert by_id[999].tier == "high", "没配过的群没有被提升 —— 追踪对它不生效"
    assert by_id[999].name == "群999", (
        "合成的群名与 `extract._group_name` 的回落值不一致 —— "
        "它会写进条目的 group_name，而两处不一样不会报错"
    )


def test_提升视图只改指定的那几个群(app) -> None:
    """※ 提升的粒度是**群**，不是「全部」—— 改错范围会让补抽那一趟
    的层级判据对别的群也生效，而那正是它不该有的副作用。"""

    class _Cfg:
        def groups(self):
            from yechao.domain.config_models import GroupConfig

            return [
                GroupConfig(id=1, tier="normal"),
                GroupConfig(id=2, tier="low"),
            ]

    view = track._PromotedConfig(_Cfg(), {1})
    tiers = {g.id: g.tier for g in view.groups()}
    assert tiers == {1: "high", 2: "low"}
    # 原对象没被动过
    assert {g.id: g.tier for g in _Cfg().groups()} == {1: "normal", 2: "low"}


# ── 两个消息视图 ────────────────────────────────────────────────────


def test_两个消息视图按_msg_id_互补(app) -> None:
    """※ 它们必须**互不相交且合起来是全集** —— 那是「不抽两遍」的前提。

    这一条直接钉视图本身（比端到端更早暴露问题）：给一组 id，两个视图
    交起来为空、并起来等于全量。
    """
    app.store.upsert_messages([
        _msg(1, "甲", TRACKED), _msg(2, "乙", OTHER), _msg(3, "丙", OTHER),
    ])
    real = app.store
    only = {m.msg_id for m in track._OnlyMessages(real, {1}).pending_messages()}
    excl = {m.msg_id for m in track._ExcludingMessages(real, {1}).pending_messages()}
    assert only == {1}
    assert excl == {2, 3}
    assert only & excl == set()
    assert only | excl == {1, 2, 3}


def test_视图不改_message_count(app) -> None:
    """※ `message_count()` 报的是「**库里**现在有多少」，不是「这一趟看得到
    多少」。两个数混起来会让报告里那个数变成一句假话。"""
    app.store.upsert_messages([_msg(1, "甲", TRACKED), _msg(2, "乙", OTHER)])
    view = track._ExcludingMessages(app.store, {1})
    assert view.message_count() == 2
