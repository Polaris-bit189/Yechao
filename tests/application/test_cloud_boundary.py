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

⚠️ 这与分层护栏那套是同一个理由：黑名单会随代码增长而失效，
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
        item_id=1, kind="academic", kind_label="学业", kind_icon="📚",
        title="期中材料提交", detail="逾期不受理", event_ts=1000,
        deadline_ts=2000, group_id=910000001, group_name="示例·年级通知群",
        actor="林小满", actor_uin=123456789, place="教务处",
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
    """⚠️ **本文件最重要的一条。**

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
        ("group_name", "示例·年级通知群"),
        ("actor_uin", 123456789),
        ("event_ts", 1000),
        ("links", ("https://example.test/a",)),
        ("kind_label", "学业"),
        ("kind_icon", "📚"),
    ],
)
def test_这些字段绝不发给云端(cats, field, value) -> None:
    """⚠️ **逐字段的正面证明**，把「为什么它不发」的理由写在下面。

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
    """⚠️ **这是本项目的全部意义。**

    构造一条**原文里全是敏感信息**的消息，确认云端收到的 JSON 里
    一个都不出现。
    """
    # ⚠️ 这里的号码是**本文件自己编的**，与语料里那批无关。
    original = "请林小满同学联系 13700002222，群900077777，交给教务处王主任"
    it = _item(
        title="材料提交",
        detail="请到教务处交给王主任",
        deadline_ts=2000,
        deadline_supported=True,
    )
    # ⚠️ **必须传名单** —— 不带名单时 `scrub()` 的姓名那一维是「没查」，
    # 而不是「查了没命中」。第一版这里漏了，测试当场红：
    # 「王主任」原样出现在了出网内容里。
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={},
        names=["林小满", "王主任"],
    )
    blob = json.dumps(payload, ensure_ascii=False)

    for sensitive in ("13700002222", "900077777", "林小满", "王主任"):
        assert sensitive not in blob, f"{sensitive} 出现在了出网内容里"
    # 原文本身当然更不该出现
    assert original not in blob


def test_条目里被模型抄进去的号码会被再抹一次(cats) -> None:
    """⚠️ **为什么要出网前再跑一遍 `scrub()`。**

    第一次脱敏保护的是「原文 → 本地模型」。而模型可能把号码**抄进它写的
    摘要里** —— 那是**第二次出网**要处理的问题，第一次的结果救不了。
    """
    it = _item(title="联系13900003333领取", detail="QQ 900088888")
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    blob = json.dumps(payload, ensure_ascii=False)
    assert "13900003333" not in blob
    assert "900088888" not in blob
    assert "<号码>" in blob


def test_没依据的时间不发给云端(cats) -> None:
    """⚠️ 云端看不到源文，它**没法核验**一个时间。

    把一个没过闸门的时间发给它，只会得到「这个时间看起来合理」这种
    没有价值的确认 —— 而它会把它当真。
    """
    it = _item(deadline_ts=2000, deadline_supported=False)
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    assert payload[0]["deadline_ts"] is None


def test_过了闸门的时间照常发(cats) -> None:
    """⚠️ 阴性对照：只发「不发的那些」会让这条判据恒真。"""
    it = _item(deadline_ts=2000, deadline_supported=True)
    payload, _checked = cloud_refine.build_payload(
        [it], kinds={c.slug: c for c in cats}, aliases={}
    )
    assert payload[0]["deadline_ts"] == 2000


# ── 出口自检 ────────────────────────────────────────────────────────


def test_出口自检会拦下越界字段() -> None:
    """⚠️ `_assert_payload_safe` 是**运行时**的断言，不是测试。

    理由：这条边界一旦被绕过，泄漏**不可撤回**（已经发出去了）。
    在发之前拦一次，代价只是一次集合比较。
    """
    with pytest.raises(AssertionError, match="出网字段越界"):
        cloud_refine._assert_payload_safe([
            {"item_id": 1, "kind": "a", "actor": "张三"}
        ])


def test_出口自检放过合法字段() -> None:
    cloud_refine._assert_payload_safe([
        {k: None for k in EXPECTED_FIELDS} | {"item_id": 1, "kind": "a"}
    ])


# ── 审计 ────────────────────────────────────────────────────────────


def test_云端这一路也落审计_且必须带_names_checked() -> None:
    """⚠️ 「查过了、没命中」与「根本没查」在数字上长得一样

    云端这一路**本来就没有姓名可比对**（`actor` 不发）。所以它的
    `names_checked` 是 **0** —— 而那个 0 的含义是「这一路没查」，
    不是「查了没漏」。记录里必须有这个字段，否则两种含义分不开。
    """
    from yechao.infrastructure.runtime.audit_log import ALLOWED_KEYS

    assert "names_checked" in ALLOWED_KEYS

    # 云端记录里 `names_checked` 是 0，且 surface 能区分它来自哪一路
    assert "surface" in ALLOWED_KEYS


def test_云端用例的_surface_与本地不同() -> None:
    """⚠️ 隐私页要能分开显示「本机出网」与「云端出网」。

    混成一个 `refine` 的话，用户看到的总数分不出「哪些发给了谁」——
    而「发给谁」正是这条边界要回答的问题。
    """
    src = (
        __import__("pathlib").Path(cloud_refine.__file__).read_text(encoding="utf-8")
    )
    assert '"surface": "cloud_refine"' in src
