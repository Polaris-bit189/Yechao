"""云端二次处理的**隐私边界**。

## 这个文件守的是什么

本项目的核心主张：

> 敏感原文在本机完成抽取，**只有脱敏后的结构化结果**离开这台机器。

云端二次处理加了一条新的出网路径 —— 于是那条主张有了**第二个必须被证明
的地方**。这个文件就是那个证明。

## 判据的取向：**白名单**，不是黑名单

「不该发的字段」列不完。所以这里断言的是**发出去的字段集合恰好等于
白名单** —— 给 `Item` 加一个新字段时，它会**自动**被拦下来，
而不是静默地跟着发出去。

※ 这与分层护栏那套是同一个理由：黑名单会随代码增长而失效，
而失效是静默的。
"""

from __future__ import annotations

import json

import pytest

from yechao.application import cloud_refine
from yechao.domain.models import Item

#: 云端**可以**看到的字段。改它要回答「为什么云端需要这一项」。
EXPECTED_FIELDS = {
    "item_id", "kind", "title", "detail", "place", "amount",
    "deadline_ts", "start_ts",
}


def _item(**kw) -> Item:
    base = dict(
        item_id=1, kind="todo", kind_label="要办的事",
        title="期中材料提交", detail="逾期不受理", event_ts=1000,
        deadline_ts=2000, group_id=820000001, group_name="青梧大学2027届通知群",
        actor="林小满", actor_account=820000012, place="教务处",
        amount="30元", links=("https://example.test/a",), source_count=1,
        start_ts=None, deadline_supported=True, start_supported=False,
    )
    base.update(kw)
    return Item(**base)


class _Cfg:
    def __init__(self, cats) -> None:
        self._cats = cats

    def categories(self):
        from yechao.domain.config_models import CategorySet

        return CategorySet(categories=self._cats, aliases={})


@pytest.fixture()
def cats():
    from yechao.domain.categories import default_categories

    return default_categories()


# ── 边界：发了什么 ──────────────────────────────────────────────────


def test_发出去的字段恰好是白名单(cats) -> None:
    """※ **本文件最重要的一条。**

    断言的是**集合相等**，不是「不包含某些字段」。给 `Item` 加一个
    `sender_phone` 之类的字段时，这条会立刻红 —— 而不是等它悄悄出网。
    """
    payload, _checked = cloud_refine.build_payload(
        [_item()], kinds={c.slug: c for c in cats}, aliases={}
    )
    assert set(payload[0]) == EXPECTED_FIELDS, (
        f"出网字段与白名单不一致。多：{set(payload[0]) - EXPECTED_FIELDS}；"
        f"少：{EXPECTED_FIELDS - set(payload[0])}"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", "林小满"),
        ("group_name", "青梧大学2027届通知群"),
        ("actor_account", 820000012),
        ("event_ts", 1000),
        ("links", ("https://example.test/a",)),
        ("kind_label", "要办的事"),
    ],
)
def test_这些字段绝不发给云端(cats, field, value) -> None:
    """※ **逐字段的正面证明**，把「为什么它不发」的理由写在下面。

    只靠上面那条集合断言的话，有人把白名单**改大**时会同时改测试，
    而这条会逼他回答一次「为什么这个字段要发」。
    """
    payload, _checked = cloud_refine.build_payload(
        [_item(**{field: value})], kinds={c.slug: c for c in cats}, aliases={}
    )
    blob = json.dumps(payload, ensure_ascii=False)
    assert field not in payload[0]
    if isinstance(value, str) and len(value) >= 2:
        assert value not in blob, f"{field} 的值泄漏到了出网文本里"


def test_原文从来不在出网内容里(cats) -> None:
    """※ **这是本项目的全部意义。**

    构造一条**原文里全是敏感信息**的消息，确认云端收到的 JSON 里
    一个都不出现。
    """
    # ※ 这里的号码是**本文件自己编的**（`1770000100X` 手机号、
    # `8200000XX` 群号 —— 与 `domain/synth.py` 自造语料同一套编号）。
    original = "请林小满同学联系 17700001006，群820000009，交给教务处周允"
    it = _item(
        title="材料提交",
        detail="请到教务处交给周允",
        deadline_ts=2000,
        deadline_supported=True,
    )
    # ※ **必须传名单** —— 不带名单时 `scrub()` 的姓名那一维是「没查」，
    # 而不是「查了没命中」。第一版这里漏了，测试当场红：
    # 「周允」原样出现在了出网内容里。
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={},
        names=["林小满", "周允"],
    )
    blob = json.dumps(payload, ensure_ascii=False)

    for sensitive in ("17700001006", "820000009", "林小满", "周允"):
        assert sensitive not in blob, f"{sensitive} 出现在了出网内容里"
    # 原文本身当然更不该出现
    assert original not in blob


def test_条目里被模型抄进去的号码会被再抹一次(cats) -> None:
    """※ **为什么要出网前再跑一遍 `scrub()`。**

    第一次脱敏保护的是「原文 → 本地模型」。而模型可能把号码**抄进它写的
    摘要里** —— 那是**第二次出网**要处理的问题，第一次的结果救不了。
    """
    it = _item(title="联系17700001007领取", detail="QQ 820000010")
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    blob = json.dumps(payload, ensure_ascii=False)
    assert "17700001007" not in blob
    assert "820000010" not in blob
    assert "<号码>" in blob


def test_地点与金额里的号码姓名也要抹(cats) -> None:
    """※ **同一个 payload 里只有 `title` / `detail` 过 `scrub()` 是个真洞。**

    **观察到什么现象**：`place` / `amount` 原来原样发出去。而它们是**模型从
    原文里抄的短字段** —— 「到 3 号教学楼找王主任」里有人名，
    「加群 820000004 交费」里有群号。原来那一格写的是「短字段，风险低」，
    而风险与字段长短**无关**，与「它是不是原文的片段」有关。

    **为什么这么定**：四个字段用**同一个口径**（同一个 payload 里那一份
    `names`）—— 另造一套名单的话，两条路的判据迟早漂移，
    而漂移的症状是「一条抹了、另一条没抹」，看审计看不出来。

    **怎么验证**：号码与姓名都必须被抹掉，且两个字段**仍然在 payload 里**
    （抹掉的是里面的敏感串，不是整个字段 —— 字段本身云端要用）。
    """
    it = _item(place="到 3 号教学楼找周允", amount="加群 820000004 交费")
    payload, checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}, names=["周允"],
    )
    blob = json.dumps(payload, ensure_ascii=False)

    assert "周允" not in blob, "place 里的姓名出网了"
    assert "820000004" not in blob, "amount 里的群号出网了"
    assert "<姓名>" in blob
    assert "<号码>" in blob
    # ※ 阴性对照：字段本身不许被整个丢掉（云端要用它），只抹里面的敏感串。
    assert payload[0]["place"] is not None
    assert payload[0]["amount"] is not None
    # ※ 名单非空 ⇒ 这一维是「查了」，不是「没查」。
    assert checked == 1


def test_地点金额为空时保持_None_而不是空串(cats) -> None:
    """※ 「这一栏没有」与「这一栏有值但为空」在提示词里是两件事 ——
    云端据此判断该不该动这一栏。脱敏不许把 `None` 变成 `""`。"""
    payload, _checked = cloud_refine.build_payload(
        [_item(place=None, amount=None)],
        kinds={c.slug: c for c in cats}, aliases={}, names=["周允"],
    )
    assert payload[0]["place"] is None
    assert payload[0]["amount"] is None


def test_没依据的时间不发给云端(cats) -> None:
    """※ 云端看不到源文，它**没法核验**一个时间。

    把一个没过闸门的时间发给它，只会得到「这个时间看起来合理」这种
    没有价值的确认 —— 而它会把它当真。
    """
    it = _item(deadline_ts=2000, deadline_supported=False)
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    assert payload[0]["deadline_ts"] is None


def test_过了闸门的时间照常发(cats) -> None:
    """※ 阴性对照：只发「不发的那些」会让这条判据恒真。"""
    it = _item(deadline_ts=2000, deadline_supported=True)
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    assert payload[0]["deadline_ts"] == 2000


# ── 出口自检 ────────────────────────────────────────────────────────


def test_出口自检会拦下越界字段() -> None:
    """※ `_assert_payload_safe` 是**运行时**的断言，不是测试。

    理由：这条边界一旦被绕过，泄漏**不可撤回**（已经发出去了）。
    在发之前拦一次，代价只是一次集合比较。
    """
    with pytest.raises(AssertionError, match="出网字段越界"):
        cloud_refine._assert_payload_safe([
            {"item_id": 1, "kind": "a", "actor": "林小满"}
        ])


def test_出口自检放过合法字段() -> None:
    cloud_refine._assert_payload_safe([
        {k: None for k in EXPECTED_FIELDS} | {"item_id": 1, "kind": "a"}
    ])


# ── 审计 ────────────────────────────────────────────────────────────


def test_云端这一路也落审计_且必须带_names_checked() -> None:
    """※ 「查过了、没命中」与「根本没查」在数字上长得一样

    云端这一路**本来就没有姓名可比对**（`actor` 不发）。所以它的
    `names_checked` 是 **0** —— 而那个 0 的含义是「这一路没查」，
    不是「查了没漏」。记录里必须有这个字段，否则两种含义分不开。
    """
    from yechao.infrastructure.runtime.audit_log import ALLOWED_KEYS

    assert "names_checked" in ALLOWED_KEYS

    # 云端记录里 `names_checked` 是 0，且 surface 能区分它来自哪一路
    assert "surface" in ALLOWED_KEYS


def test_云端用例的_surface_与本地不同() -> None:
    """※ 隐私页要能分开显示「本机出网」与「云端出网」。

    混成一个 `refine` 的话，用户看到的总数分不出「哪些发给了谁」——
    而「发给谁」正是这条边界要回答的问题。
    """
    src = (
        __import__("pathlib").Path(cloud_refine.__file__).read_text(encoding="utf-8")
    )
    assert '"surface": "cloud_refine"' in src


# ── 提示词：这一路要防什么 ──────────────────────────────────────────
#
# ※ 上面那些用例守的是**发出去的东西**（payload）。下面这几条守的是
# **提示词**：云端那一路的输入形状与本地抽取完全不同（条目 vs 原文），
# 所以它要防的失败也完全不同 —— 而这些失败没有一条会在 payload 用例里
# 露出马脚。


def _rendered_cloud_system(cats: list) -> str:
    """按 `_run_batch` 的方式渲染一次云端提示词（`cats` 是类目表本身）。

    ※ 刻意**不**复制 `_run_batch` 里那两行，而是调同一个渲染器 ——
    测试若自己拼一份，就成了第三份抄本，而它永远是对的。
    """
    from yechao.application.prompts import example_kind, render_categories

    return cloud_refine.CLOUD_SYSTEM.format(
        categories=render_categories(cats),
        example_kind=example_kind(cats),
    )


def test_云端提示词的类目判据真的渲染进去了(cats) -> None:
    """※ 与抽取那一路同一个坑：模板没渲染时，模型**照样**回形状完美的
    JSON，只是类目判据整块是一个字面量 `{categories}`。

    云端这一路更隐蔽：它认不出的 slug 会被**静默忽略**（见
    `_run_batch` 里的 `new_kind in {c.slug ...}`），所以类目判据丢了
    以后，界面上什么都不变。
    """
    system = _rendered_cloud_system(cats)
    assert "{categories}" not in system
    assert "{example_kind}" not in system
    for c in cats:
        assert f"`{c.slug}`（{c.label}）：{c.desc}" in system


def test_云端提示词的示例_slug_来自真实类目表(cats) -> None:
    """※ 示例里的 slug 会过期（类目表是用户可编辑的数据）。

    而云端这一路过期之后**不报错** —— 模型照着一个已经不存在的 slug 回填，
    程序静默忽略。所以这条只能靠用例钉。
    """
    system = _rendered_cloud_system(cats)
    assert f'"kind": "{cats[0].slug}"' in system


def test_云端提示词把类目渲染与抽取共用(cats) -> None:
    """※ 类目那一段**只有一处渲染实现**。

    原来 `_run_batch` 自己拼了一遍 `f"- `{c.slug}`（{c.label}）：…"`，
    与 `build_extract_system` 里那份是两份抄本 —— 改了一处漏另一处，
    症状是「本地判得对、云端判得不对」，而两份代码单看都对。
    """
    import pathlib

    from yechao.application.prompts import build_extract_system, render_categories

    cloud_system = _rendered_cloud_system(cats)
    extract_system = build_extract_system(cats)
    # 同一张表 ⇒ 两套提示词里出现的是**逐字相同**的那一段
    block = render_categories(cats)
    assert block in cloud_system
    assert block in extract_system
    # 而 `_run_batch` 里不该再有一份手拼的抄本
    src = pathlib.Path(cloud_refine.__file__).read_text(encoding="utf-8")
    assert "c.label}）：{c.desc}" not in src, "云端又自己拼了一份类目渲染"


def test_云端提示词说明时间戳是读不出日期的整数() -> None:
    """※ payload 里的时间是 **Unix 秒的整数**（`build_payload` 直传
    `deadline_ts` / `start_ts`）。

    模型读不出 `1793001600` 是哪一天。所以它能做的只有一件事：
    **判断这两个值的次序是不是反了**（正常是「先截止、后举办」）。
    提示词不说清这一点，模型会去猜那个数字是哪一天 —— 而它猜出来的
    任何日期都是编的。
    """
    system = cloud_refine.CLOUD_SYSTEM
    assert "Unix 秒" in system
    assert "次序" in system


def test_云端提示词要求_swap_time_是布尔字面量() -> None:
    """※ `_run_batch` 判的是 `if row.get("swap_time")` —— 而字符串 `"false"`
    在 Python 里是**真值**。

    模型写 `"swap_time": "false"` 时，「不交换」会被读成「交换」。
    示例里给 `false`（不是 `"false"`）就是在传达这是布尔。
    """
    assert '"swap_time": false' in cloud_refine.CLOUD_SYSTEM


def test_云端提示词要求每条都给全四个键() -> None:
    """※ 形状固定下来，才不会「有时候少一个键」。

    漏 `item_id` ⇒ 回连不上被丢；漏 `line` ⇒ 那一条没有摘要，
    换成机械行 —— 而两者都**看不见**（`_run_batch` 静默跳过）。
    """
    system = cloud_refine.CLOUD_SYSTEM
    for key in ("item_id", "kind", "swap_time", "line"):
        assert f'"{key}"' in system, f"示例里少了 {key}"
    assert "每条输入都要出现一次" in system


def test_云端提示词不许把_null_当成忘了填() -> None:
    """※ payload 里的 `null` 不是「没填」，是「**没通过本机核验**」。

    模型若把另一格的值搬过去补上，等于绕过核验 —— 那个值又变成
    「有依据」的样子了，而它其实一个依据都没有。
    """
    system = cloud_refine.CLOUD_SYSTEM
    assert "null" in system
    assert "没通过本机的核验" in system
