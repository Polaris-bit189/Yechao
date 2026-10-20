"""出网脱敏的判据。

⚠️ **所有样本都是本文件自己编的。** 它们不必像真的，格式对就够 ——
判据是按格式写的，不是按真实性写的。

每条用例上方写清「这条考的是哪个绕过手法」。**测完要问：还能怎么绕？**
找到新绕法就补一条 —— 这个文件应当随语料增长，而不是一次写完。
"""

from __future__ import annotations

import pytest

from yechao.domain.scrub import (
    NAME_PLACEHOLDER,
    NUM_PLACEHOLDER,
    mask_names,
    scrub,
)
from yechao.domain.textnorm import normalize


# ── 强判据：形态自明，不需要上下文 ────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "联系13700001111",            # 基本手机号
        "我的号是 13700001111。",      # 前后有标点
        "手机13700001111号",           # 前后紧贴汉字
        "（13700001111）",             # 全角括号
    ],
)
def test_手机号必抹(text: str) -> None:
    out, rep = scrub(text)
    assert "13700001111" not in out
    assert NUM_PLACEHOLDER in out
    assert rep.num_placeholders >= 1


@pytest.mark.parametrize(
    "text",
    [
        "20261008",        # 8 位日期
        "当天共 30 人",     # 两位数
        "9/3-9/5",         # 日期区间，数字都短
        "第 3 教学楼",      # 序号
    ],
)
def test_短数字不抹(text: str) -> None:
    """⚠️ 误伤的代价与漏抹不同：**抹掉金额/日期会让模型读不到那一栏**。"""
    out, rep = scrub(text)
    assert NUM_PLACEHOLDER not in out
    assert normalize(text) in out


def test_银行卡整条被吃掉_而不是只抹尾巴() -> None:
    """⚠️ 这是「边界判据」要防的那个事故。

    `62170001370000111` 里**包含着**一个 11 位手机号。如果先抹手机号，
    整条卡号会被从中间切开、剩下两截谁也不匹配 —— 于是真正该抹的卡号
    被留下来，而尾巴上的手机号被抹了。
    """
    out, rep = scrub("收款账号 62170001370000111 请核对")
    assert "62170001370000111" not in out
    assert "13700001111" not in out
    assert NUM_PLACEHOLDER in out


def test_长数字串与手机号同时出现时都抹() -> None:
    out, _ = scrub("卡 62170001370000111 手机 13700001111")
    assert "6217" not in out
    assert "13700001111" not in out


def test_邮箱被抹() -> None:
    out, rep = scrub("发到 student0001@example.test 谢谢")
    assert "student0001@example.test" not in out
    assert rep.num_placeholders == 1


def test_邮箱判据不误伤_at_前后有中文() -> None:
    """⚠️ 用 `\\S+@\\S+` 会把「见@张三」当成邮箱抹掉。"""
    out, _ = scrub("见@张三 拿材料")
    assert "张三" in out


# ── 模糊判据：必须有触发字 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,number",
    [
        ("加群900012345咨询", "900012345"),
        ("QQ群：900012345", "900012345"),
        ("交流群 900012345", "900012345"),
        ("群号900012345", "900012345"),
    ],
)
def test_触发字在场的长数字必抹(text: str, number: str) -> None:
    """同一种东西有很多种写法——
    判据按**字**取，覆盖「群」的各种搭配。"""
    out, _ = scrub(text)
    assert number not in out
    # ⚠️ **触发字本身要保留** —— 它是有效信息，抹了周围的词就断了。
    assert "群" in out


def test_学费里的学不是触发字() -> None:
    """⚠️ 这条是**误伤检查**：按字取触发字，最危险的是「学费」。

    但它后面紧跟的是另一个汉字（`费`）而不是数字，所以不触发。
    """
    out, _ = scrub("本学期学费 4800 元")
    assert "4800" in out, "金额被当成群号抹掉了 —— 模型就读不到金额了"


def test_学院后面的数字不抹() -> None:
    out, _ = scrub("信息学院 2024 级")
    assert "2024" in out


def test_全角字母与全角数字绕过() -> None:
    """⚠️ `ＱＱ ９０００１２３４５` 与半角是同一个号。

    这考的是**规范化在入口收敛**：如果每条规则各自处理全角，落在
    `qq` 那一条上的补丁就漏了别的规则。
    """
    out, _ = scrub("ＱＱ群 ９０００１２３４５ 大家加一下")
    assert "９０００１２３４５" not in out
    assert "900012345" not in out


def test_零宽字符绕过姓名() -> None:
    """⚠️ `张<U+200B>三` 在模型眼里就是 `张三`。"""
    names = ["赵小满"]
    out, _, checked = mask_names("请赵​小满同学到场", names)
    assert checked == 1
    assert "赵​小满" not in out
    assert NAME_PLACEHOLDER in out


# ── URL ─────────────────────────────────────────────────────────────


def test_URL_里的手机号照样抹() -> None:
    """⚠️ **URL 豁免的是模糊判据，不是强判据。**

    查询参数里的手机号仍然指得到人 —— 与它出现在哪里无关。
    """
    out, rep = scrub("报名 https://example.test/f?phone=13700001111 请填")
    assert "13700001111" not in out
    assert "example.test" in out, "URL 本身不该被打烂"
    assert rep.num_placeholders == 1


def test_URL_豁免不会吞掉后面的正文() -> None:
    """⚠️ 豁免的右边界如果写成「从 http 到空白」，它会一路吞掉后面的正文
    —— 而手机号就藏在正文里。"""
    out, _ = scrub("看 https://example.test/a 然后联系13700001111")
    assert "13700001111" not in out


def test_短链不被抹() -> None:
    out, _ = scrub("https://t.example/AbC123")
    assert "t.example/AbC123" in out


# ── 姓名 ─────────────────────────────────────────────────────────────


def test_姓名按精确子串抹() -> None:
    out, _, _ = mask_names("请张三到教务处", ["张三"])
    assert "张三" not in out
    assert NAME_PLACEHOLDER in out


def test_长名字先替换_不会留下断尾() -> None:
    """⚠️ 名单里同时有「张三」与「张三丰」时，先替换短的会把长的切成
    `<姓名>丰`。从长到短保证最长的先被吃掉。"""
    out, _, _ = mask_names("张三丰来了", ["张三", "张三丰"])
    assert "丰" not in out
    assert out.count(NAME_PLACEHOLDER) == 1


def test_单字姓名不抹() -> None:
    """⚠️ 单字姓名在中文里满地都是（「王」「李」），抹它会把正文咬得稀烂。"""
    out, _, checked = mask_names("王老师说", ["王"])
    assert "王老师" in out
    assert checked == 0


def test_名单为空时_checked_为零而不是通过() -> None:
    """⚠️ **这是本文件最重要的一条。**

    「查过了、没命中」与「**根本没查**」在结果里长得一模一样
    只有 `names_checked` 能把它们分开。
    """
    _, rep = scrub("一段没有姓名的文本", [])
    assert rep.names_checked == 0
    assert rep.name_placeholders == 0
    assert rep.names_checked == 0, "名单为空 ⇒ 这一维**没查**"


def test_有名单时_checked_是名单里的人数() -> None:
    _, rep = scrub("完全干净的文本", ["甲小明", "乙小红"])
    assert rep.names_checked == 2
    assert rep.name_placeholders == 0


# ── 报告 ─────────────────────────────────────────────────────────────


def test_审计归档不存内容_内存里的命中可以带原文() -> None:
    """⚠️ **这条判据的落点被修正过一次，值得记。**

    原来它断言「`ScrubHit` 没有 `text` 字段」—— 那是把**内存里的对象**
    当成了**磁盘上的记录**。而这两件事的需求正好相反：

      · 内存里：调用方（指标脚本、自检）**需要知道抹掉了什么**，
        否则它只能拿坐标去切原文 —— 而坐标是替换过程中的，切出来是垃圾。
        （真的发生过：指标报了 5/7 召回 + 100% 误伤，两个错得合理的数。）
      · 磁盘上：**绝不能存内容** —— 那会新造一个泄漏面，
        而它恰好会存下最该保护的那些文本。

    ⇒ 判据改成断言**落盘那一侧**：`ALLOWED_KEYS` 白名单里没有 `text`，
    且 `record()` 写出去的行里不含它。白名单而不是黑名单 ——
    黑名单会在有人加新字段时静默放行。
    """
    from yechao.infrastructure.runtime.audit_log import ALLOWED_KEYS, JsonlAuditLog

    _, rep = scrub("联系13700001111", ["赵小满"])
    # 内存里：有 text（调用方需要它）
    assert any(h.text == "13700001111" for h in rep.hits)

    # 磁盘上：不许有
    assert "text" not in ALLOWED_KEYS
    assert "content" not in ALLOWED_KEYS
    assert "raw" not in ALLOWED_KEYS

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        log = JsonlAuditLog(d)
        log.record({"surface": "refine", "text": "13700001111", "messages": 1}, ts=1700000000)
        row = log.read_all()[0]
    assert "text" not in row, "被抹掉的原文写进了审计归档"
    assert "13700001111" not in json.dumps(row)
