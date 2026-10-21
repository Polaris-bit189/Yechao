"""隐私可证伪 + 端到端指标口径。

## 这个文件守的是「证明」本身



> **一个「看起来在保护/在验证」的东西，可能什么都没做，而且不报错。**

所以这里每一条用例都**同时**要正反两面：

  · 必然触发的输入 ⇒ 检查**必须报红**
  · 干净的输入     ⇒ 检查**必须放过**

**两者缺一不可**：只会报红的检查会让红线失去意义（狼来了）；
只会放过的检查什么都没守。
"""

from __future__ import annotations

import pytest

from yechao.application.audit import summarize
from yechao.application.metrics import Counts, prefilter_metrics, scrub_metrics
from yechao.domain.scrub import scrub
from yechao.domain.synth import CORPUS, GROUPS, expectations


# ── 隐私：可证伪 ────────────────────────────────────────────────────


def test_没有任何记录时_不是干净_而_还没跑_() -> None:
    """※ `records: 0` **不是错误**，但它也**不是通过**。

    `hint` 必须给出来 —— 界面据此说「还没有出网记录」，
    而不是说「一切正常」。这两句话的意思完全不同。
    """
    v = summarize([])
    assert v.records == 0
    assert v.hint, "没有记录时必须给引导文案"
    assert v.clean is True  # 没有命中是真的（没有记录），但 hint 把语境说清了


def test_名字名单为空时_必须能分辨没查_与_查了没漏_() -> None:
    """※ ** —— 恒定报绿的那一半。**

    「这一轮没有可比对的名单」时，`known_name_hits: 0` 的含义是
    **「没查」**，不是「通过」。这两件事在数字上长得一模一样 ——
    只有 `names_were_checked` 能把它们分开。
    """
    entries = [
        {"surface": "refine", "messages": 1, "chars": 10, "names_checked": 0,
         "name_hits": 0, "num_hits": 0, "sha256": "x"},
    ]
    v = summarize(entries)
    assert v.known_name_hits == 0
    assert v.names_were_checked is False, (
        "名单为空时 `names_were_checked` 必须是 false —— "
        "否则界面会把「没查」显示成一个让人放心的 0"
    )


def test_真的查过时才说查过() -> None:
    entries = [
        {"surface": "refine", "messages": 1, "chars": 10, "names_checked": 19,
         "name_hits": 0, "num_hits": 0, "sha256": "x"},
    ]
    v = summarize(entries)
    assert v.names_checked == 19
    assert v.names_were_checked is True


def test_命中必须让_clean_变假并进_dirty() -> None:
    """※ **正面：必然触发的输入必须报红。**"""
    entries = [
        {"surface": "refine", "batch": 2, "messages": 1, "chars": 10,
         "names_checked": 5, "name_hits": 1, "num_hits": 0, "sha256": "abc"},
    ]
    v = summarize(entries)
    assert v.clean is False
    assert len(v.dirty) == 1
    assert v.dirty[0]["name_hits"] == 1
    assert v.dirty[0]["batch"] == 2
    assert v.known_name_hits == 1


def test_干净输入必须放过() -> None:
    """※ **反面：干净的输入必须放过。**

    只会报红的检查会让红线失去意义（狼来了）——
    用户看多了「又红了」，就不会再看它。
    """
    entries = [
        {"surface": "refine", "messages": 3, "chars": 100,
         "names_checked": 7, "name_hits": 0, "num_hits": 0, "sha256": "abc"},
    ]
    v = summarize(entries)
    assert v.clean is True
    assert v.dirty == []


# ── 隐私：检查与脱敏复用同一条判据 ──────────────────────────────────


def test_审计读的是脱敏那一刻的报告_不是另扫一遍() -> None:
    """※ ** —— 判据漂移。**

    如果检查用的词表与脱敏用的**不是同一份**，两者迟早漂移。
    漂移后检查报「0 命中」而实际有东西出去了。

    ⇒ 本项目的审计记录里 `num_placeholders` 直接来自 `ScrubReport` ——
    检查与脱敏是**同一次动作**。

    ※ 号码是本文件自己编的（`1770000100X`，与 `domain/synth.py` 同一套编号）
    —— 举例里的号码要自己编，这是本项目的红线之一。
    """
    _, report = scrub("联系17700001006 和 17700001007", ["某人甲"])
    assert report.num_placeholders == 2
    # 这两个数出自同一份报告 —— 不存在「另一个检测器扫出别的结论」
    assert report.names_checked == 1


# ── 隐私：发送者昵称也必须出网脱敏 ──────────────────────────────────


def test_发送者昵称里的姓名不许出网_且审计要覆盖它() -> None:
    """※ **这条守的是一个真洞：昵称没过 `scrub()`。**

    **观察到什么现象**：`sender` 是群昵称，真实语料里长成
    「22-3班学习委员李青萝」—— 人名就在里面。原来只对 `m.content` 调了
    `scrub()`，昵称是直接拼进提示词的。后果是同一行里正文的姓名被抹成
    `<姓名>`，消息头上的「学习委员李青萝」原样出网：一个**看起来已经脱敏**
    的提示词，漏了最直接的那一处人名。

    **为什么这么定**：出网的每一段文本都必须是 `scrub()` 的产物，且两次
    `scrub()`（正文、昵称）的报告要落进**同一条**审计记录 —— 否则
    「审计记了 1 处、实际出去 2 处」这种偏差没有任何地方能发现。

    **怎么验证**：跑真的 `_run_batch`（只用桩替掉模型与审计），
    ① 截住真正发出去的 user 文本，断言里面没有裸名；
    ② 断言审计记录的 `name_placeholders` 把**昵称那一处**也算进去了
    （正文 1 处 + 昵称 1 处 = 2），`names_checked` 不是 0（不是「没查」）。
    """
    from yechao.application import extract
    from yechao.application.prompts import build_extract_user
    from yechao.application.prompts import EXTRACT_MAX_TOKENS as _MAX_TOKENS
    from yechao.application.prompts import EXTRACT_SYSTEM as _SYSTEM
    from yechao.domain.chunking import Batch
    from yechao.domain.models import PendingMessage

    captured: dict[str, str] = {}

    class _Model:
        """桩：**不回任何条目** —— 于是 `_run_batch` 走完审计那一步就返回，
        不需要 clock / config 这些只有解析成功后才用到的东西。"""

        def complete(self, *, system: str, user: str, max_tokens: int = 2048, response_schema: dict | None = None) -> str:
            captured["user"] = user
            return ""

    class _Audit:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def record(self, entry, *, ts=None) -> None:
            self.rows.append(entry)

    class _Prompt:
        # ※ 类体看不到函数的局部名，所以上面 import 时改过名。
        EXTRACT_SYSTEM = _SYSTEM
        EXTRACT_MAX_TOKENS = _MAX_TOKENS

        @staticmethod
        def build_extract_user(messages, scrubbed):
            return build_extract_user(messages, scrubbed)

    audit = _Audit()
    # ※ `config` 不能是 None：抽取要问它「当前的类目有哪些」（结构化输出的
    # 枚举来自那里）。这条用例只关心脱敏，但依赖必须是真的 —— 拿 None 顶替，
    # 一旦某处真的用到它就会炸在一个与脱敏无关的地方。
    from yechao.domain.categories import default_categories
    from yechao.domain.config_models import CategorySet

    class _Cfg:
        def categories(self):
            return CategorySet(categories=default_categories(), aliases={})

    deps = extract.ExtractDeps(
        messages=None, items=None, model=_Model(), audit=audit,
        clock=None, config=_Cfg(), prompt=_Prompt(),
    )
    batch = Batch(
        group_id=820000001,
        messages=(
            PendingMessage(
                msg_id=42, group_id=820000001, ts=0,
                sender="22-3班学习委员李青萝",
                content="请李青萝同学明天把材料交到教务处",
            ),
        ),
    )

    extract._run_batch(
        batch, deps, extract.ExtractOutcome(),
        names=["李青萝"], known_accounts=[], kinds_map={},
    )

    user = captured["user"]
    assert user, "提示词没被拼出来 —— 这条用例成了空转"
    assert "李青萝" not in user, "发送者昵称里的姓名原样出网了"
    assert "<姓名>" in user

    # ※ 审计必须覆盖新增的脱敏面：只记正文那一份，就成了一条
    # 「看起来记了、实际漏了昵称」的记录。
    row = audit.rows[0]
    assert row["name_placeholders"] == 2, "昵称那一处没被计入审计"
    assert row["names_checked"] == 1, "名单非空却报「没查」"
    # ※ 指纹与字符数按**整段出网文本**算（正文 + 昵称）——
    # 只算正文的话，昵称那一处改了它也看不出来。
    assert row["chars"] > len(batch.messages[0].content)


# ── 指标：口径 ──────────────────────────────────────────────────────


def test_分母为零时是无法计算_而不是_0() -> None:
    """※ 这是本文件最重要的一条设计判据。

    `0/0` 返回 `0.0` 会被读成「一次都没命中」，而真相是**「没测」**。
    与隐私页的 `names_were_checked` 是同一条判据：
    **别让「没测到」冒充「没问题」。**
    """
    c = Counts(hit=0, total=0)
    assert c.rate is None
    assert "无法计算" in str(c)


def test_预筛指标的分母是该保留_该丢的条数_不是语料总数() -> None:
    """※ 用总条数当分母会把两个不相干的量混起来。"""
    expects = expectations()
    # 只挑两条：一条该留、一条该丢
    keep_id = next(mid for mid, e in expects.items() if e.keep)
    drop_id = next(mid for mid, e in expects.items() if not e.keep)

    recall, false_keep = prefilter_metrics(
        {keep_id: True, drop_id: False}, expects
    )
    assert recall.total == 1 and recall.hit == 1
    assert false_keep.total == 1 and false_keep.hit == 0


def test_预筛漏掉一条重要的_召回率必须下降() -> None:
    """※ **正面：真的漏了要能看出来。**

    这是产品的核心痛点 ——「怕漏掉重要的通知」。如果这个指标在漏了的时候
    不动，那它就不是指标。
    """
    expects = expectations()
    keep_ids = [mid for mid, e in expects.items() if e.keep][:3]
    decisions = {mid: True for mid in keep_ids}
    decisions[keep_ids[0]] = False  # 漏了一条

    recall, _ = prefilter_metrics(decisions, expects)
    assert recall.total == 3
    assert recall.hit == 2


def test_脱敏指标_漏抹会掉召回_多抹会记误伤() -> None:
    """※ 两个方向都要测 —— 只测召回的话，一个「把全文都抹掉」的实现
    会得到 100% 的召回。"""
    expects = expectations()
    want_nums = {n for e in expects.values() for n in e.numbers}
    assert want_nums, "语料里必须有标了号码的条目，否则这条是空转"

    # ※ **分母是「号码 + 姓名」**，不只是号码 —— 这一点我第一版写错了。
    # 指标把两维合成一个召回率是有意的：它们都是「不该出网的东西」，
    # 分成两个数会让「总共漏了几处」没有一处能回答。
    want_names = {n for e in expects.values() for n in e.names}
    assert want_names, "语料里必须有标了姓名的条目"

    # 全部抹掉 ⇒ 召回满分、误伤为零
    full = scrub_metrics(
        flagged_numbers=set(want_nums), flagged_names=set(want_names), expects=expects
    )
    assert full[0].rate == 1.0, f"该为 1.0，实得 {full[0]}"
    assert full[1].total == 0

    # 全都不抹 ⇒ 召回为零
    none = scrub_metrics(flagged_numbers=set(), flagged_names=set(), expects=expects)
    assert none[0].rate == 0.0
    assert none[0].total == len(want_nums) + len(want_names)

    # 抹了一个不是号码的 ⇒ 记误伤
    over = scrub_metrics(
        flagged_numbers={"不是号码"}, flagged_names=set(), expects=expects
    )
    assert over[1].total == 1
    assert over[1].hit == 1


# ── 指标：真值本身 ──────────────────────────────────────────────────


def test_真值分得清截止日与开始时刻() -> None:
    """※  2：同一个标签下混着两种语义，
    模型做对了也会被判错。

    ⇒ 真值里 `deadline_text` 与 `start_text` 是**两个字段**。
    """
    expects = expectations()
    has_deadline = [e for e in expects.values() if e.deadline_text]
    has_start = [e for e in expects.values() if e.start_text]
    assert has_deadline, "语料里要有带截止日的条目"
    assert has_start, "语料里要有带开始时刻的条目"
    # 至少有一条**两者都有** —— 那正是模型最容易混的地方
    assert any(e.deadline_text and e.start_text for e in expects.values())


def test_真值标出了推不出绝对时刻的那些() -> None:
    """※  3：拿一个结构性达不到的标准去衡量模型，
    等于给自己造一个假缺陷。

    「明早集合」推不出确定时刻 —— 那条**不进**时刻抽取的分母。
    """
    expects = expectations()
    not_inferable = [e for e in expects.values() if e.start_not_inferable]
    assert not_inferable, "语料里必须有「推不出时刻」的样本，否则坑 3 没法被验证"
    for e in not_inferable:
        assert e.start_text is None or e.start_not_inferable


def test_真值里的号码与姓名都能在语料里找到() -> None:
    """※ 真值如果指向语料里不存在的东西，指标会永远显示「漏抹」。"""
    from yechao.domain.textnorm import normalize

    for c in CORPUS:
        # ※ **号码也要规范化之后再比。** 有一条语料故意写成全角
        # （`ＱＱ群 ８２００００００６`）—— 而真值里记的是半角。
        # 直接用 `in` 比会红，而那不是「真值错了」，是**比较前该收敛**。
        # 判据是「规范化之后的文本里有没有这个号码」——
        # 因为下游（脱敏、审计）看到的都是规范化后的那一份。
        for n in c.expect.numbers:
            assert normalize(n) in normalize(c.msg.content), (
                f"{c.msg.msg_id} 的真值号码 {n} 不在正文里（规范化之后）"
            )
        for n in c.expect.names:
            # 名字可能被零宽字符插过，规范化之后再比
            from yechao.domain.textnorm import normalize

            assert normalize(n) in normalize(c.msg.content), (
                f"{c.msg.msg_id} 的真值姓名 {n} 不在正文里"
            )


def test_语料里的群都在群配置里() -> None:
    """※ 未知群会让抽取拿不到 tier —— 而 `tier` 影响预筛的保留判据。"""
    gids = {g.id for g in GROUPS}
    for c in CORPUS:
        assert c.msg.group_id in gids, f"消息 {c.msg.msg_id} 属于一个不在配置里的群"
