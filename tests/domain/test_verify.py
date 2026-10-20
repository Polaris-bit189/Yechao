"""核验闸门 —— 「源文里查得到吗」。

※ 每一条结论都要能**指着源文说**「这里查得到 / 查不到」。所以这里的用例
不只断言布尔，还断言 `Verdict.evidence` —— 那一段必须是源文里的原文。

## 三种结论，不是两种

`SUPPORTED` / `UNSUPPORTED` / `NOT_APPLICABLE`。第三态存在的理由是：
返回「没依据」会**清掉真信息**，返回「有依据」会**放行一个没验过的值**
—— 两个都不能选。

## 四个字段，四种「最小可信单元」

日期与时刻是 token（两侧要边界）；地点是一段够长的连续片段；
金额是一个个完整的数字。各自的最小单元不同，判据也就不同 —— 见
`verify.py` 的模块 docstring。
"""

from __future__ import annotations

import datetime as dt

import pytest

from yechao.domain.verify import (
    Support,
    gate,
    verify_amount,
    verify_deadline,
    verify_place,
    verify_start,
)

#: 测试用的时区偏移。※ **与生产同一口径**（东八区）。
#:
#: 闸门按**本地日**拆模型填的时间戳，所以夹具必须用同一个口径造时间戳。
#: 用 UTC 造、闸门按本地拆，测试自己就会造出一个「11月1日 00:00 UTC」，
#: 闸门拆出来是 11月1日（测试通过），而生产里存的是「11月1日 00:00 本地」，
#: 闸门拆出来是 **10月31日** —— 判据失效，而测试全绿。
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


def test_日期边界_长月日不给短月日背书() -> None:
    """※ **这条是日期判据的核心。**

    `11/25` 里含 `11/2`。判据如果只按「子串在不在」写，源文写着 11/25 就会给
    模型填的 11/2 背书 —— 而 11 月 2 日与 11 月 25 日是**两个不同的截止日**。
    用户按错的那个去办，会错过期限。
    """
    v = verify_deadline("11/25 截止", _ts(2026, 11, 2), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_日期边界_长月日确实能给自己背书() -> None:
    v = verify_deadline("11/25 截止", _ts(2026, 11, 25), local_year=2026, tz_offset=TZ)
    assert v.support is Support.SUPPORTED


def test_裸日不算依据_月底推不出具体日期() -> None:
    """※ 源文写「月底」，模型填一个具体日期 —— 那**不是**源文的依据。

    判据必须是字面匹配。但也不该说成「源文完全没提时间」——
    所以 evidence 里留着那个相对词。
    """
    v = verify_deadline("请于月底前缴清", _ts(2026, 10, 31), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED
    assert v.evidence == "月底"


def test_裸日不算依据_只写日不写月() -> None:
    """源文只写「28日」，模型填 `10月28日` —— **不算**。

    月份不在源文里，闸门也看不到消息的发出时刻，所以它核对不了那个月份。
    见 `times.py` 的「裸日不算依据」一节。
    """
    v = verify_deadline("28日前提交", _ts(2026, 10, 28), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_源文完全没时间时_无依据() -> None:
    v = verify_deadline("明天考试", _ts(2026, 10, 28), local_year=2026, tz_offset=TZ)
    # 「明天」是相对日 —— 有相对词，但推不出月日
    assert v.support is Support.UNSUPPORTED


def test_源文写明了别的年份_不给这一年背书() -> None:
    """※ 源文写 `2027年10月28日`，模型填 `2026-10-28` —— 两个不同的日子。"""
    v = verify_deadline("2027年10月28日截止", _ts(2026, 10, 28), local_year=2026, tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED
    v2 = verify_deadline("2027年10月28日截止", _ts(2027, 10, 28), local_year=2026, tz_offset=TZ)
    assert v2.support is Support.SUPPORTED


def test_模型没填时是判据不适用() -> None:
    v = verify_deadline("随便一段话", None, local_year=2026, tz_offset=TZ)
    assert v.support is Support.NOT_APPLICABLE


# ── 时刻 ────────────────────────────────────────────────────────────


def test_时刻必须同时有日期与时刻() -> None:
    src = "10月18日 14:00 在图书馆门口"
    v = verify_start(src, _ts(2026, 10, 18, 14, 0), tz_offset=TZ)
    assert v.support is Support.SUPPORTED
    assert v.evidence and "14:00" in v.evidence


def test_时刻的分钟边界_长分钟不给短分钟背书() -> None:
    """※ `7点4分` 与 `7点40分` 只差一个字符 —— 与日期那个坑同形，
    但更隐蔽（「分」这个字把边界藏了起来）。"""
    v = verify_start("7点40分集合", _ts(2026, 10, 18, 7, 4), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_时刻对但日期不对_无依据() -> None:
    src = "10月18日 14:00 在图书馆"
    # 时刻在源文里，但它配的日期是 10月19日
    v = verify_start(src, _ts(2026, 10, 19, 14, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


def test_相对时间推不出确定时刻() -> None:
    """※ 「明早集合」推不出 08:00。模型填了具体时刻**就是编的**。"""
    v = verify_start("明早集合，别迟到", _ts(2026, 10, 19, 8, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED
    assert v.evidence == "明早"


def test_源文只给了日期没给时刻_模型填不进这一格() -> None:
    """※ **结构下限**：`start_ts` 要求精确到分钟。

    源文只写「10月18日在图书馆摆摊」，这一格**本来就不该有值**
    模型硬填一个时刻，闸门要拦。
    """
    v = verify_start("10月18日在图书馆摆摊", _ts(2026, 10, 18, 9, 0), tz_offset=TZ)
    assert v.support is Support.UNSUPPORTED


# ── 地点：共同片段的长度下限是 3 ────────────────────────────────────


def test_地点_核心词重合就算有依据() -> None:
    """误杀那一侧：源文写「西太湖连隔板」（转写错误），模型写「西太湖校区」
    —— 它们是同一个地方。三字核心词 `西太湖` 重合 ⇒ 有依据。"""
    v = verify_place("在西太湖连隔板集合", "西太湖校区")
    assert v.support is Support.SUPPORTED
    assert v.evidence == "西太湖"


def test_地点_模型多写了细节仍算有依据() -> None:
    """`图书馆三楼` vs 源文 `图书馆` —— 模型补了楼层，地点本身在源文里。"""
    v = verify_place("在图书馆集合", "图书馆三楼")
    assert v.support is Support.SUPPORTED


def test_地点_共享通用后缀不算同一个地方() -> None:
    """※ **这条是地点判据的核心，也是它与「任一 2 字子串命中」的分界。**

    源文写「西校区」，模型填「东校区」—— 它们是**两个**地方。
    放行它，用户会**跑错校区**：这正是判据想避免的那种代价，
    只是方向相反（误杀 vs 放行错的）。

    它们共享的 `校区` 只有 2 字，而 2 字**不够** —— `校区`/`门口`/`食堂`
    这类通用后缀正好都是 2 字，共享它们不代表是同一个地方。
    """
    v = verify_place("在西校区集合", "东校区")
    assert v.support is Support.UNSUPPORTED


def test_地点_完全对不上时无依据() -> None:
    v = verify_place("在图书馆集合", "食堂二楼")
    assert v.support is Support.UNSUPPORTED


def test_地点_两字地点要求整串命中() -> None:
    """`东门`、`食堂` 这类两字地点没有三字可要求 ⇒ 退而要求**整串**。

    两字全中比「共享一个通用后缀」强得多，是这两字地点上唯一还成立的判据。
    """
    assert verify_place("东门集合", "东门").support is Support.SUPPORTED
    # 反例：只共享一个字不算
    assert verify_place("西门集合", "东门").support is Support.UNSUPPORTED


def test_单字地点判据没有分辨力_不适用() -> None:
    """※ 单字命中恒真（「楼」「厅」「馆」满地都是），判据在这个输入下
    根本没在判。返回「没依据」会**清掉真信息** ⇒ 第三态，保留原值 + 标记。"""
    v = verify_place("在图书馆集合", "楼")
    assert v.support is Support.NOT_APPLICABLE


# ── 金额 ────────────────────────────────────────────────────────────


def test_金额每个数字都要找到() -> None:
    v = verify_amount("押金100，租金200", "100")
    assert v.support is Support.SUPPORTED


def test_金额里有一个数找不到就无依据() -> None:
    """※ 「每个数字都要单独找到依据」—— 只验其中一个会给另一个背书。"""
    v = verify_amount("押金100", "100 元 + 押金 500")
    assert v.support is Support.UNSUPPORTED


def test_金额最小单元是完整数字_不是子串() -> None:
    """※ `200` 与 `1200` 不是同一个数 —— 判据是**完整数字**的相等。"""
    v = verify_amount("总价1200元", "200元")
    assert v.support is Support.UNSUPPORTED


def test_金额容忍千分位() -> None:
    v = verify_amount("总计 1,200 元", "1200元")
    assert v.support is Support.SUPPORTED


def test_金额容忍补零() -> None:
    v = verify_amount("租金 1200 元", "1200.00元")
    assert v.support is Support.SUPPORTED


def test_面议没有数字_判据不适用() -> None:
    """※ 返回「没依据」会把「面议」这个词清掉 —— 而那是一条真信息。"""
    v = verify_amount("押金面议", "面议")
    assert v.support is Support.NOT_APPLICABLE


def test_免费同理() -> None:
    v = verify_amount("活动免费", "免费")
    assert v.support is Support.NOT_APPLICABLE


# ── 闸门的处置 ──────────────────────────────────────────────────────


def _item(**kw):
    from yechao.domain.models import Item

    base = dict(
        item_id=1, kind="todo", kind_label="要办的事",
        title="测试", detail=None, event_ts=0, deadline_ts=None,
        group_id=1, group_name="群1", actor=None, actor_account=None,
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


def test_闸门不清金额_只标记() -> None:
    """※ **金额的处置与其他字段不同**，理由写在 `verify_amount` 的 docstring 里：

    清掉金额之后用户看到「这条没有金额」，而**他不会去别处找** ——
    那是一个假话。保留它、让它被上下文提醒，代价更低。
    """
    it = _item(amount="999元")
    r = gate(it, "活动免费", local_year=2026, tz_offset=TZ)
    assert r.item.amount == "999元", "金额不该被清掉"
    assert "amount" in r.flagged


def test_不变量_deadline_ts_为空时_supported_恒为假() -> None:
    """※ 这两个字段必须同步 —— 只清一个有症状：
    前端看到 `deadline_ts is None` 不显示，而库里 `supported` 还是真，
    那会让「日报该不该把它发给模型」判断错。"""
    for case in (
        _item(deadline_ts=None, deadline_supported=True),
        _item(deadline_ts=_ts(2026, 12, 1), deadline_supported=False),
    ):
        r = gate(case, "没有日期的一段话", local_year=2026, tz_offset=TZ)
        assert r.item.deadline_ts is None
        assert r.item.deadline_supported is False


def test_闸门对四个字段各出一条结论_不是空转() -> None:
    """※ 分母自检：`gate` 必须对**四个**字段各给一条 `Verdict`。

    一个只查日期的实现能让上面大多数用例通过（它们大多只看一两个字段）。
    这里钉住分母，顺便钉住 `cleared` 与 `flagged` 两个集合的分工：
    `cleared` 是**清掉**的（截止日 / 时刻 / 地点），
    `flagged` 是**留着但没验过**的（金额）。
    """
    it = _item(
        deadline_ts=_ts(2026, 12, 1), start_ts=_ts(2026, 12, 1, 9, 0),
        place="食堂二楼", amount="999元",
    )
    r = gate(it, "源文里什么时间都没有", local_year=2026, tz_offset=TZ)
    assert {v.field for v in r.verdicts} == {"deadline_ts", "start_ts", "place", "amount"}
    assert len(r.verdicts) == 4
    assert set(r.cleared) == {"deadline_ts", "start_ts", "place"}
    assert "amount" in r.flagged
    # 两个集合不重叠：一个字段不可能既被清掉又被标记
    assert not (set(r.cleared) & set(r.flagged))


@pytest.mark.parametrize("field", ["deadline_ts", "start_ts", "place", "amount"])
def test_每个字段都有它自己的结论(field: str) -> None:
    """参数化的分母：四个字段名逐个点名，少一个就少一条用例。"""
    it = _item(**{field: None})
    r = gate(it, "一段没有信息的话", local_year=2026, tz_offset=TZ)
    assert any(v.field == field for v in r.verdicts)
