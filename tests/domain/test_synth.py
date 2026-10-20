"""合成语料的两条**结构判据** —— 它是评测集，所以它自己也要被检查。

## 要防什么

**一、语料没覆盖全部类目。**
语料是唯一有真值的评测集。少一个类目 ⇒ 那一类的准确率**分母是 0**，
而指标报告里不会显示「这一类没测」—— 它显示的是「这一类没问题」。
界面上也一样：两个类目一条条目都没有，看起来像「这两类没数据」。

**二、真值里留着已经不存在的类目。**
`resolve_kind` 对未知 slug **不报错**（它照原样显示，见 categories.py 的
注释），所以一条写着旧 slug 的真值不会让任何东西变红 ——
它只会让指标永远算不对，且指向一个错误的方向。

**三、文件里那句「号码怎么编的」与文件里真实的号码对不上。**
本文件的 `synth.py` 里写过一句「号码由 `N01`…`N12` 编号拼出」，
而全文件**不存在任何 `N01`**。文实不符的注释比没有注释更坏：它让人
以为自己看懂了编号规则，于是不去核对。
⇒ 那条说明现在写的是可核对的形式，这条用例就照着它核。
"""

from __future__ import annotations

import re

from yechao.domain.categories import DEFAULT_CATEGORIES
from yechao.domain.synth import CORPUS, GROUPS

#: 本项目自定的编号：手机号 `1770000100X`、群号 `82000000X`（9 位）。
OWN_PHONE_PREFIX = "1770000100"
OWN_GROUP_PREFIX = "82000000"


def test_六个默认类目都有语料() -> None:
    """※ 缺一类 = 那一类的指标分母是 0，而报告里看不出来。"""
    wanted = {c.slug for c in DEFAULT_CATEGORIES}
    covered = {c.expect.kind for c in CORPUS if c.expect.kind}
    assert wanted - covered == set(), f"这些类目一条语料都没有：{wanted - covered}"


def test_真值里的类目都是现存的() -> None:
    """※ 未知 slug 不会报错（照原样显示），所以旧 slug 能一直躺着。"""
    known = {c.slug for c in DEFAULT_CATEGORIES}
    unknown = {c.expect.kind for c in CORPUS if c.expect.kind} - known
    assert not unknown, f"真值里出现了不存在的类目：{unknown}"


def test_噪声不该有类目真值() -> None:
    """被预筛丢掉的条目**不该产出条目** —— 给它一个 kind 是自相矛盾的真值。"""
    for c in CORPUS:
        if not c.expect.keep:
            assert c.expect.kind is None, (
                f"msg {c.msg.msg_id} 标了 keep=False 却又有 kind={c.expect.kind}"
            )


def test_语料里的号码都来自本项目自己那一套编号() -> None:
    """※ 这条守的是 `synth.py` 开头那张编号表 —— **文实相符**。

    它同时挡住「从别处抄一个号码进来」：抄来的号码不会符合本项目的编号。

    ※ 扫的是**规范化之后**的正文：有一条语料故意写成全角
    （ＱＱ群 ８２００００００６），而下游（脱敏、审计、真值比对）看到的
    都是规范化后的那一份 —— 不收敛的话，全角那条会被这条判据当成
    「来路不明的号码」，而那不是它的错。
    """
    from yechao.domain.textnorm import normalize

    phone = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    nine = re.compile(r"(?<!\d)\d{9}(?!\d)")
    for c in CORPUS:
        body = normalize(c.msg.content)
        for m in phone.finditer(body):
            assert m.group(0).startswith(OWN_PHONE_PREFIX), (
                f"msg {c.msg.msg_id} 里的手机号 {m.group(0)} 不是本项目编的"
            )
        for m in nine.finditer(body):
            assert m.group(0).startswith(OWN_GROUP_PREFIX), (
                f"msg {c.msg.msg_id} 里的 9 位号 {m.group(0)} 不是本项目编的"
            )


def test_群名都挂在虚构校名下() -> None:
    """※ 群名是最容易把真实来源带进来的地方（截图、演示都会出现它）。"""
    for g in GROUPS:
        assert g.name.startswith("青梧"), f"群 {g.id} 的名字不在虚构校名下：{g.name}"
