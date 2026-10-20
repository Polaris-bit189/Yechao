"""提示词渲染 与 模型输出解析。

⚠️ **这个文件里的每一条用例都对应一个真踩过的坑。** 它们不是设想的边界，
是实际跑出来、看实际产出才发现的失败。

而这些失败有一个共同形状：**它们看起来像成功**。

  · 提示词里的 `{categories}` 没渲染 ⇒ 模型照样吐出形状完美的 JSON
  · 解析器把自己的合法输出咬坏 ⇒ 「解析出 0 条」，与「模型没找到条目」
    在报告里长得一样
  · 思考型模型把预算烧光 ⇒ `finish_reason=length` 且正文为空，
    看起来像「模型不会干活」

⇒ 所以它们必须被用例钉住 —— 而**只有跑一遍真流程**才找得到它们。
「测试绿 ≠ 对」这条在本项目上是实测过的。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from yechao.application.extract import parse_candidates
from yechao.application.prompts import (
    EXTRACT_SYSTEM,
    build_extract_system,
    build_extract_user,
    parse_digest_lines,
)


@dataclass
class _Cat:
    slug: str
    label: str
    desc: str


# ── 提示词渲染 ──────────────────────────────────────────────────────


def test_类目判据必须真的渲染进提示词() -> None:
    """⚠️ **本项目最隐蔽的一个 bug，就是这条用例抓的。**

    第一版 `_PromptAdapter` 直接取了模块常量 `EXTRACT_SYSTEM` ——
    而它是**带 `{categories}` 占位符的模板**。

    发出去的提示词里，类目表那一整块是一个字面量 `{categories}`。
    模型**照样**回了形状完全正确的 JSON：字段齐全、摘录能回连、语法合法。
    唯一的区别是：**类目判据完全没有依据，它在猜。**

    靠读输出发现不了它，靠断言形状也发现不了 —— 只有**看发出去的文本**。
    """
    cats = [
        _Cat("alpha", "甲类", "要读者做甲这件事"),
        _Cat("beta", "乙类", "要读者做乙这件事"),
    ]
    prompt = build_extract_system(cats)

    assert "{categories}" not in prompt, "占位符没被渲染 —— 类目判据整块丢了"
    assert "`alpha`（甲类）：要读者做甲这件事" in prompt
    assert "`beta`（乙类）：要读者做乙这件事" in prompt


def test_加一个类目_提示词里就多一条判据() -> None:
    """⚠️ 类目判据是**用户可编辑的数据**（`CategoryDetail.desc` 的注释逐字：
    「会逐字进提示词。编辑器必须能改它。」）——
    所以提示词不能把七条判据写死在里面。"""
    base = build_extract_system([_Cat("a", "甲", "甲判据")])
    extended = build_extract_system(
        [_Cat("a", "甲", "甲判据"), _Cat("b", "乙", "乙判据")]
    )
    assert "`b`" not in base
    assert "`b`" in extended


def test_改类目判据_提示词跟着变() -> None:
    before = build_extract_system([_Cat("a", "甲", "旧判据")])
    after = build_extract_system([_Cat("a", "甲", "新判据")])
    assert "旧判据" in before
    assert "新判据" in after
    assert "旧判据" not in after


def test_提示词模板里必须有占位符_否则上面三条是空转() -> None:
    """⚠️ **空转自检**。

    如果 `EXTRACT_SYSTEM` 里根本没有 `{categories}`，那么
    `build_extract_system` 就是一个恒等函数 —— 上面三条用例全都会以
    某种方式通过，而它们守的是一个**没在渲染**的东西。
    """
    assert "{categories}" in EXTRACT_SYSTEM


def test_user_部分带上编号与发送者() -> None:
    from yechao.domain.models import PendingMessage

    msgs = [
        PendingMessage(msg_id=7, group_id=1, ts=0, sender="某同学", content="原文"),
    ]
    user = build_extract_user(msgs, ["脱敏后的原文"])
    assert "[7]" in user
    assert "某同学" in user
    assert "脱敏后的原文" in user


def test_user_部分用的是脱敏后的文本而不是原文() -> None:
    """⚠️ 这个函数**不自己脱敏** —— 它拿到的必须已经是脱敏后的。
    传错了（把原文递进来）就是直接把号码发出去。这条用例钉的是**调用约定**。"""
    from yechao.domain.models import PendingMessage

    msgs = [PendingMessage(msg_id=1, group_id=1, ts=0, sender="s", content="联系13700001111")]
    user = build_extract_user(msgs, ["联系<号码>"])
    assert "13700001111" not in user
    assert "<号码>" in user


# ── 解析 ────────────────────────────────────────────────────────────


BARE = '[{"kind":"academic","title":"交材料","quote":"10月28日前提交"}]'
FENCED = '```json\n' + BARE + '\n```'
PREFIXED = "好的，以下是结果：\n" + BARE
SUFFIXED = BARE + "\n\n以上。"
DICT_WRAPPED = '{"items": ' + BARE + "}"


@pytest.mark.parametrize(
    "name,raw",
    [
        ("裸数组", BARE),
        ("Markdown 围栏", FENCED),
        ("前面有一句话", PREFIXED),
        ("后面有一句话", SUFFIXED),
        ("包在对象里", DICT_WRAPPED),
    ],
)
def test_各种包装都能解析出来(name: str, raw: str) -> None:
    """⚠️ 模型不是每次都把 JSON 裸着给 —— 而且**不是每次都包**。

    不处理的话症状是「有时成功有时整批失败」，而那种间歇性失败最难查，
    因为它看起来像「模型今天心情不好」。
    """
    out = parse_candidates(raw)
    assert len(out) == 1, f"{name} 解析失败"
    assert out[0].title == "交材料"


def test_解析器不许把自己的合法输出咬坏() -> None:
    """⚠️ **这条是那个真 bug 的回归用例。**

    原来的写法是「头切第一个 `[`、尾截最后一个 `]` 或 `}`」。
    对 `[{...}]` 来说：先对 `]` 截一次（无事），**再对 `}` 截一次** ——
    而 `}` 在数组内部，于是 `]` 被切掉了。

    症状：**模型完全正常、输出完全合法，但「条目 0 条」**。
    而「解析出 0 条」与「模型没找到条目」在报告里长得一样。

    ⇒ 现在的实现让 JSON 解析器自己告诉我们值到哪儿结束（`raw_decode`），
    而不是猜边界。
    """
    out = parse_candidates(BARE)
    assert len(out) == 1
    # 多条时同样不许被咬坏
    two = '[{"kind":"a","title":"t1","quote":"q1"},{"kind":"b","title":"t2","quote":"q2"}]'
    assert len(parse_candidates(two)) == 2


def test_没有摘录的条目被丢掉() -> None:
    """⚠️ 没有摘录就没有回连的可能 —— 整条丢弃，**不猜**。"""
    out = parse_candidates('[{"kind":"a","title":"t","quote":""}]')
    assert out == []


def test_完全不是_JSON_时返回空而不是抛() -> None:
    """⚠️ 「解析不出来」与「网络错误」处置不同：前者丢这批，后者要重试。
    在这里抛会让调用方分不清它们。"""
    assert parse_candidates("抱歉，我无法处理这段内容") == []


# ── 日报解析 ────────────────────────────────────────────────────────


def test_日报解析同样复用同一条实现() -> None:
    """⚠️ 日报与抽取两路的模型输出形状相同。

    原来两边各写了一份「头切第一个 `[`、尾切最后一个 `]`」，而**两份都有
    同一个 bug** —— 其中一份修了另一份没修，正是「两边各写一份必漂移」。
    """
    raw = '```json\n[{"item_id": 12, "line": "10月28日前交材料"}]\n```'
    out = parse_digest_lines(raw)
    assert out == {12: "10月28日前交材料"}


def test_日报解析_编号对不上的行被丢掉() -> None:
    raw = '[{"item_id": "abc", "line": "x"}, {"item_id": 3, "line": ""}]'
    assert parse_digest_lines(raw) == {}
