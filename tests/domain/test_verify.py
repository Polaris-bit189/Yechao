"""核验闸门 —— 「源文里查得到吗」。

⚠️ 每一条结论都要能**指着源文说**「这里查得到 / 查不到」。所以这里的用例
不只断言布尔，还断言 `Verdict.evidence` —— 那一段是源文里的原文。

## 三种结论，不是两种

`SUPPORTED` / `UNSUPPORTED` / `NOT_APPLICABLE`。第三态存在的理由是：
返回「没依据」会**清掉真信息**，
返回「有依据」会**放行一个没验过的值** —— 两个都不能选。
"""

from __future__ import annotations

import datetime as dt

import pytest

from yechao.domain.verify import Support, gate, verify_amount, verify_deadline, verify_place, verify_start


#: 测试用的时区偏移。⚠️ **与生产同一口径**（东八区）。
#:
#: 第一版这里用 UTC 造时间戳，而闸门按**本地日**拆 —— 于是测试自己
#: 造出了一个「11月1日 00:00 UTC」，闸门拆出来是 11月1日，测试通过；
#: 而生产里 `_resolve_ts` 存的是「11月1日 00:00 本地」，闸门拆出来是
#: **10月31日**，判据失效。
#:
#: ⇒ 夹具必须与生产**同口径**。这不是「测试写错了」，是测试**默认了一个
#: 与生产不同的时区** —— 而那正是生产 bug 能在测试里静默通过的原因。
TZ = 8 * 3600


def _ts(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> int:
    """构造一个「本地时刻」的 Unix 秒 —— 与 `_resolve_ts` 存进去的形状相同。"""
    dt_utc = dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)
    return int(dt_utc.timestamp()) - TZ


# ── 日期 ────────────────────────────────────────────────────────────


def test_日期字面命中() -> None:
    v = verify_deadline("请于10月28日前提交材料", _ts(2026, 10, 28), local_year=2026, tz_offset=TZ)
    assert v.support is Support.SUPPORTED
    assert v.evidence and "10月28" in v.evidence


def test_日期边界_9月3日不给9月30日背书() -> None:
    """⚠️ **这条是日期判据的核心。**

    `9/30` 里含 `9/3`。判据如果只按「子串在不在」写，源文写着 9/30 就会给
    模型填的 9/3 背书 —— 而 9 月 3 日与 9 月 30 日是**两个不同的截止日**。
    用户按错的那个去办，会错过期限。
    """
    v = verify_deadline("9/30 截止", _ts(2026, 9, 3), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_日期边界_9月30日确实能背书9月30日() -> None:
    v = verify_deadline("9/30 截止", _ts(2026, 9, 30), local_year=2026, tz_offset=TZ)
    assert v.support is Support.SUPPORTED


def test_裸日不算依据_月底推不出具体日期() -> None:
    """⚠️ 源文写「月底」，模型填一个具体日期 —— 那**不是**源文的依据。

    判据必须是字面匹配。
    但也不该说成「源文完全没提时间」—— 所以 evidence 里留着那个相对词。
    """
    v = verify_deadline("请于月底前缴清", _ts(2026, 10, 31), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED
    assert v.evidence == "月底"


def test_源文完全没有时间时_无依据() -> None:
    v = verify_deadline("明天考试", _ts(2026, 10, 28), local_year=2026, tz_offset=TZ)
    # 「明天」是相对日 —— 有相对词，但推不出月日
    assert v.support is Support.UNSUPPORTED


def test_模型没填时是判据不适用() -> None:
    v = verify_deadline("随便一段话", None, local_year=2026, tz_offset=TZ)
    assert v.support is Support.NOT_APPLICABLE


# ── 时刻 ────────────────────────────────────────────────────────────


def test_时刻必须同时有日期与时刻() -> None:
    src = "10月18日 14:00 在图书馆门口"
    v = verify_start(src, _ts(2026, 10, 18, 14, 0), tz_offset=TZ)
    assert v.support is Support.SUPPORTED
    assert v.evidence and "14:00" in v.evidence


def test_时刻的分钟边界_9点5分不给9点50分背书() -> None:
    """⚠️ `9点5分` 与 `9点50分` 只差一个字符 —— 与日期那个坑同形，
    但更隐蔽。"""
    v = verify_start("9点50分集合", _ts(2026, 10, 18, 9, 5), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_时刻对但日期不对_无依据() -> None:
    src = "10月18日 14:00 在图书馆"
    # 时刻在源文里，但它配的日期是 10月19日
    v = verify_start(src, _ts(2026, 10, 19, 14, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_相对时间推不出确定时刻() -> None:
    """⚠️ 「明早集合」推不出 08:00。模型填了具体时刻**就是编的**。"""
    v = verify_start("明早集合，别迟到", _ts(2026, 10, 19, 8, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED
    assert v.evidence == "明早"


def test_源文只给了日期没给时刻_模型填不进这一格() -> None:
    """⚠️ **结构下限**：`start_ts` 要求精确到分钟。

    源文只写「10月18日在图书馆摆摊」，这一格**本来就不该有值**
    模型硬填一个时刻，闸门要拦。
    """
    v = verify_start("10月18日在图书馆摆摊", _ts(2026, 10, 18, 9, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


# ── 地点 ────────────────────────────────────────────────────────────


def test_地点按两字片段命中() -> None:
    """地点名往往不精确 —— 「西太湖校区」vs 源文的「西太湖连隔板」。"""
    v = verify_place("在西太湖连隔板集合", "西太湖校区")
    assert v.support is Support.SUPPORTED


def test_地点完全对不上时无依据() -> None:
    v = verify_place("在图书馆集合", "食堂二楼")
    assert v.support is Support.UNSUPPORTED


def test_单字地点判据不适用() -> None:
    """⚠️ 单字命中率太高（「楼」「厅」「馆」满地都是），判据在这个输入下
    根本没在判。返回「没依据」会**清掉真信息**。"""
    v = verify_place("在图书馆集合", "楼")
    assert v.support is Support.NOT_APPLICABLE


# ── 金额 ────────────────────────────────────────────────────────────


def test_金额每个数字都要找到() -> None:
    v = verify_amount("押金100，租金200", "100")
    assert v.support is Support.SUPPORTED


def test_金额里有一个数找不到就无依据() -> None:
    """⚠️ 「每个数字都要单独找到依据」—— 只验其中一个会给另一个背书。"""
    v = verify_amount("押金100", "100 元 + 押金 500")
    assert v.support is Support.UNSUPPORTED


def test_金额200不给1200背书() -> None:
    """⚠️ **最小可信单元是一个完整数字**，不是子串。"""
    v = verify_amount("总价1200元", "200元")
    assert v.support is Support.UNSUPPORTED


def test_金额容忍千分位() -> None:
    v = verify_amount("总计 1,200 元", "1200元")
    assert v.support is Support.SUPPORTED


def test_面议没有数字_判据不适用() -> None:
    """⚠️ 返回「没依据」会把「面议」这个词清掉 —— 而那是一条真信息。"""
    v = verify_amount("押金面议", "面议")
    assert v.support is Support.NOT_APPLICABLE


def test_免费同理() -> None:
    v = verify_amount("活动免费", "免费")
    assert v.support is Support.NOT_APPLICABLE


# ── 闸门的处置 ──────────────────────────────────────────────────────


def _item(**kw):
    from yechao.domain.models import Item

    base = dict(
        item_id=1, kind="activity", kind_label="活动", kind_icon="",
        title="测试", detail=None, event_ts=0, deadline_ts=None,
        group_id=1, group_name="群1", actor=None, actor_uin=None,
        place=None, amount=None, links=(), source_count=1, start_ts=None,
    )
    base.update(kw)
    return Item(**base)


def test_闸门清掉无依据的截止日与时刻() -> None:
    it = _item(deadline_ts=_ts(2026, 12, 1), start_ts=_ts(2026, 12, 1, 9, 0))
    r = gate(it, "源文里什么时间都没有", local_year=2026, tz_offset=TZ)
    assert r.item.deadline_ts is None
    assert r.item.start_ts is None
    assert set(r.cleared) == {"deadline_ts", "start_ts"}


def test_闸门保留有依据的() -> None:
    it = _item(deadline_ts=_ts(2026, 10, 28))
    r = gate(it, "10月28日前提交", local_year=2026, tz_offset=TZ)
    assert r.item.deadline_ts == _ts(2026, 10, 28)
    assert r.item.deadline_supported is True
    assert r.cleared == ()


def test_闸门清掉无依据的地点() -> None:
    """地点与时间同类：错了会让用户**白跑一趟**。"""
    it = _item(place="食堂二楼")
    r = gate(it, "在图书馆集合", local_year=2026, tz_offset=TZ)
    assert r.item.place is None


def test_两字子串判据会放过部分重合的地点() -> None:
    """⚠️ **一条如实记录的判据弱点，不是缺陷修复。**

    地点判据故意放宽到「任一 2 字子串命中」，是为了不误杀
    「西太湖校区」vs「西太湖连隔板」这种写法差异。
    它的代价是：任何共享两个字的两个地点都会通过 ——
    「东校区」与「西校区」共享 `校区`。

    ⇒ 我**不**在这里收紧判据：收紧会退回「整串一致才通过」，
    而那会误杀真实的写法差异，误杀的代价是**清掉一个真地点**。
    这里只把取舍钉下来 —— 如果将来有人想改成更严的判据，
    这条用例会告诉他代价是什么。
    """
    v = verify_place("在西校区集合", "东校区")
    assert v.support is Support.SUPPORTED, (
        "判据放宽了，所以共享「校区」两字会通过 —— 这是知情取舍"
    )


def test_闸门不清金额_只标记() -> None:
    """⚠️ **金额的处置与其他字段不同**，理由写在 `verify_amount` 的 docstring 里：

    清掉金额之后用户看到「这条没有金额」，而**他不会去别处找** ——
    那是一个假话。保留它、让它被上下文提醒，代价更低。
    """
    it = _item(amount="999元")
    r = gate(it, "活动免费", local_year=2026, tz_offset=TZ)
    assert r.item.amount == "999元", "金额不该被清掉"
    assert "amount" in r.flagged


def test_不变量_deadline_ts_为空时_supported_恒为假() -> None:
    """⚠️ 这两个字段必须同步 —— 只清一个有症状：
    前端看到 `deadline_ts is None` 不显示，而库里 `supported` 还是真，
    那会让「日报该不该把它发给模型」判断错。"""
    for case in (
        _item(deadline_ts=None, deadline_supported=True),
        _item(deadline_ts=_ts(2026, 12, 1), deadline_supported=False),
    ):
        r = gate(case, "没有日期的一段话", local_year=2026, tz_offset=TZ)
        assert r.item.deadline_ts is None
        assert r.item.deadline_supported is False
