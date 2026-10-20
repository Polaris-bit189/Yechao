"""出网脱敏的判据。

※ **所有样本都是本文件自己编的。** 它们不必像真的，格式对就够 ——
判据是按格式写的，不是按真实性写的。
（假手机号用 `1770000100X` 段，假群号用 `820000001` 起的 9 位数字，
假域名用 RFC 2606 保留域 `example.test`。）

每条用例上方写清「这条考的是哪个绕过手法 / 哪个信号」。**测完要问：还能怎么绕？**
找到新绕法就补一条 —— 这个文件应当随语料增长，而不是一次写完。

## 分档

判据分两档，依据不是「严格程度」，而是**信号自己够不够**（见 `scrub.py`）：
  · 强判据（信号 A 形态 / 信号 B 长度 / 邮箱）⇒ 一律抹，不看上下文；
  · 模糊判据（信号 C 触发词）⇒ 只在触发词在场时抹。
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


# ── 强判据：信号 A —— 形态符合号段编制 ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "联系17700001001",            # 基本手机号
        "我的号是 17700001001。",      # 前后有标点
        "手机17700001001号",           # 前后紧贴汉字
        "（17700001001）",             # 全角括号
        "手机 177 0000 1001 打这个",   # ※ 分组写法（空格）
        "手机 177-0000-1001 打这个",   # ※ 分组写法（连字符）
    ],
)
def test_手机号必抹(text: str) -> None:
    """※ 最后两条是**同一个号码的另一种写法**。

    漏掉分组写法，等于让一个手机号原样出网 —— 而它是强判据，
    「形态自明」这句话必须覆盖它的全部常见写法。
    """
    out, rep = scrub(text)
    assert "17700001001" not in out
    # 分组写法里数字被拆开了，所以按「去掉分隔符之后也找不到」再查一遍
    assert "17700001001" not in out.replace(" ", "").replace("-", "")
    assert NUM_PLACEHOLDER in out
    assert rep.num_placeholders >= 1


# ── 强判据：信号 B —— 长度超出本语料一切非标识符数字 ────────────────


def test_长数字串整条被吃掉() -> None:
    """※ **掩码单元取最大的那个 token。**

    `820000177000010019`（18 位）里**含**一个 11 位的手机号形状
    （下标 6 起是 `17700001001`）。判据必须把**整条**吃掉，
    而不是只抹中间那 11 位 —— 那会把长串切成两截残渣。

    实测：两侧的边界断言让手机号规则在长串里**根本匹配不上**
    （两侧都是数字），所以长串是被**信号 B** 整条接住的，
    结果只有一个占位符。
    """
    out, rep = scrub("收款账号 820000177000010019 请核对")
    assert "820000177000010019" not in out
    assert "17700001001" not in out
    assert out.count(NUM_PLACEHOLDER) == 1


def test_长数字串与手机号同时出现时都抹() -> None:
    out, rep = scrub("账号 820000177000010019 手机 17700001001")
    assert "820000177000010019" not in out
    assert "17700001001" not in out
    assert rep.num_placeholders == 2


# ── 强判据：邮箱 ────────────────────────────────────────────────────


def test_邮箱被抹() -> None:
    out, rep = scrub("发到 student0001@example.test 谢谢")
    assert "student0001@example.test" not in out
    assert rep.num_placeholders == 1


def test_邮箱先于号码抹_不会只抹掉中间那一段() -> None:
    """※ **掩码单元取最大的 token**：邮箱**整体**才是指得到人的标识符。

    若先抹手机号，`student17700001001@example.test` 会被从中间切开 ——
    抹掉中间 11 位、域名留在原地（`student<号码>@example.test`）。
    症状是「抹了，但没抹干净」，而报告里的计数还是 1。
    """
    out, rep = scrub("发到 student17700001001@example.test 谢谢")
    assert "example.test" not in out, "邮箱只被抹掉了一段，域名留在原地"
    assert "17700001001" not in out
    assert out.count(NUM_PLACEHOLDER) == 1


def test_邮箱判据不误伤_at_前后有中文() -> None:
    """※ 用 `\\S+@\\S+` 会把「见@某人」当成邮箱抹掉。"""
    out, _ = scrub("见@某人 拿材料")
    assert "某人" in out


# ── 模糊判据：信号 C —— 触发词命名了一个标识符 ──────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "加群820000001咨询",
        "QQ群：820000001",
        "交流群 820000001",
        "群号820000001",
    ],
)
def test_触发词在场的数字必抹(text: str) -> None:
    """同一种东西有很多种写法 —— 判据取它们的**共同点**：
    数字左边紧挨着一个命名标识符的词。

    ※ `群号820000001` 考的是「`号` 是**词的一部分**」这条：
    若把 `号` 当成分隔符而不是词的一部分，这一条就匹配不上。
    """
    out, _ = scrub(text)
    assert "820000001" not in out
    # ※ **触发词本身要保留** —— 它是有效信息，抹了周围的词就断了。
    assert "群" in out


def test_触发词后面紧跟汉字时不触发() -> None:
    """※ 误伤检查：触发词是一个**词**，不是「见到那个字就开火」。

    `群力 660 元` 里 `群` 后面紧跟的是汉字 `力` ⇒ 与数字之间隔着词字符
    ⇒ **不触发**。这是「按词取」最容易误伤的形状（`群力`/`群主`/`群规`…）。
    """
    out, _ = scrub("群力 660 元")
    assert "660" in out, "触发词后面跟着汉字，不该开火"


def test_触发词与数字之间夹了别的汉字不触发() -> None:
    """`群公告 820000001` 是一条**公告**，不是群号。

    判据是「触发词 + 非词字符 + 数字」——「公告」是词字符，
    把触发词与数字隔开了。
    """
    out, _ = scrub("群公告 820000001 已发布")
    assert "820000001" in out


def test_没有触发词的短数字不抹() -> None:
    """※ **分档的另一半**：5–11 位数字**单独**不足以说明它是标识符。

    它与金额、房号、课程序号、订单号严重重叠 ⇒ 没有触发词就不动它。
    （误伤代价：抹掉一条真金额，模型就读不到金额了，而金额是条目要填的字段。）
    """
    out, _ = scrub("订单 501234567 已发货")
    assert "501234567" in out


@pytest.mark.parametrize(
    "text",
    [
        "第 12 教室",
        "每班 5 人",
        "20270315",          # 8 位日期
        "9:00-11:30 开会",    # 时刻区间，数字都短
    ],
)
def test_短数字不抹(text: str) -> None:
    out, rep = scrub(text)
    assert NUM_PLACEHOLDER not in out
    assert normalize(text) in out
    assert rep.num_placeholders == 0


# ── URL ─────────────────────────────────────────────────────────────


def test_URL_里的手机号照样抹() -> None:
    """※ **URL 豁免的是模糊判据，不是强判据。**

    查询参数里的手机号仍然指得到人 —— 与它出现在哪里无关。
    """
    out, rep = scrub("报名 https://example.test/f?phone=17700001001 请填")
    assert "17700001001" not in out
    assert "example.test" in out, "URL 本身不该被打烂"
    assert rep.num_placeholders == 1


def test_URL_里的模糊判据被显式豁免() -> None:
    """※ 触发词规则在 URL 区间内**显式跳过**。

    `https://example.test/qq820000001` 里，触发词与数字**都在 URL 里** ——
    所以「URL 里没有汉字所以不会触发」那句推论**不成立**（`qq` 是 ASCII）。
    豁免必须是**代码**，不能是一条推论：否则链接会被打烂。
    """
    out, _ = scrub("https://example.test/qq820000001")
    assert "820000001" in out, "URL 里的定位符不该被抹 —— 那会把链接打烂"


def test_URL_豁免不会吞掉后面的正文() -> None:
    """※ 豁免的右边界如果写成「从 http 到空白」，它会一路吞掉后面的正文
    —— 而手机号就藏在正文里。"""
    out, _ = scrub("看 https://example.test/a 然后联系17700001001")
    assert "17700001001" not in out


def test_短链不被抹() -> None:
    out, _ = scrub("https://t.example/AbC123")
    assert "t.example/AbC123" in out


# ── 规范化：绕过手法在这里收敛（入口） ──────────────────────────────


def test_全角字母与全角数字绕过() -> None:
    """※ `ＱＱ群 ８２００００００１` 与半角是同一个号。

    这考的是**规范化在入口收敛**：如果每条规则各自处理全角，落在
    `qq` 那一条上的补丁就漏了别的规则。
    """
    out, _ = scrub("ＱＱ群 ８２００００００１ 大家加一下")
    assert "８２００００００１" not in out
    assert "820000001" not in out


def test_变体选择符绕过() -> None:
    """※ **变体选择符是 `Mn`，不是 `Cf`。**

    早先 `textnorm` 的注释写着「删 Cf：零宽、软连字符、**变体选择符**」——
    那句话是错的，按类别删 Cf **删不掉**它。症状是一个手机号里插一个
    U+FE0F 就整条逃过判据（实测：改前这条用例红）。

    ※ 输入用**显式转义**拼（`\\ufe0f`），不写字面量 ——
    字面量在编辑器里看不见，一旦被谁顺手删掉，这条用例就变成空转
    （一个普通手机号照样会被抹）。
    """
    text = "手机1" + "️" + "7700001001打这个"
    assert "️" in text  # 前提自检：绕过字符确实在输入里
    out, _ = scrub(text)
    assert "17700001001" not in out
    assert NUM_PLACEHOLDER in out


def test_零宽字符绕过姓名() -> None:
    """※ `赵<U+200B>小满` 在模型眼里就是 `赵小满`。

    ※ 同样用**显式转义**拼输入，理由见上一条。
    """
    names = ["赵小满"]
    text = "请赵" + "​" + "小满同学到场"
    assert "​" in text
    out, _, checked = mask_names(text, names)
    assert checked == 1
    assert "赵小满" not in out
    assert NAME_PLACEHOLDER in out


# ── 姓名 ─────────────────────────────────────────────────────────────


def test_姓名按精确子串抹() -> None:
    out, _, _ = mask_names("请林小满到教务处", ["林小满"])
    assert "林小满" not in out
    assert NAME_PLACEHOLDER in out


def test_长名字先替换_不会留下断尾() -> None:
    """※ 名单里同时有 `欧阳长` 与 `欧阳长风` 时，先替换短的会把长的切成
    `<姓名>风`。从长到短保证最长的先被吃掉。"""
    out, _, _ = mask_names("欧阳长风来了", ["欧阳长", "欧阳长风"])
    assert "风" not in out
    assert out.count(NAME_PLACEHOLDER) == 1


def test_单字姓名不抹() -> None:
    """※ 单字姓名在中文里满地都是（「陈」「李」），抹它会把正文咬得稀烂。"""
    out, _, checked = mask_names("陈老师说", ["陈"])
    assert "陈老师" in out
    assert checked == 0


def test_名单为空时_checked_为零而不是通过() -> None:
    """※ **这是本文件最重要的一条。**

    「查过了、没命中」与「**根本没查**」在结果里长得一模一样 ——
    只有 `names_checked` 能把它们分开。
    """
    _, rep = scrub("一段没有姓名的文本", [])
    assert rep.names_checked == 0
    assert rep.name_placeholders == 0


def test_有名单时_checked_是名单里的人数() -> None:
    """分母自检：名单非空时，`checked` 必须**等于比对过的姓名个数**，
    不是一个恒定的 0。"""
    _, rep = scrub("完全干净的文本", ["丙小禾", "丁小舟"])
    assert rep.names_checked == 2
    assert rep.name_placeholders == 0


def test_昵称的全称与裸名都能对上() -> None:
    """※ **昵称是全称、正文是裸名**。

    名单里给的是 `22-3班学习委员林小满`，而正文写的是 `林小满`。
    只拿全称做精确子串匹配，`林小满` **永远匹配不上** ——
    于是姓名脱敏在真实语料上等于没开，而它看起来是「配置了名单」。

    `scrub()` 会把每个昵称拆出人名本体，两样都进名单。
    """
    out, rep = scrub("请林小满同学到教务处", ["22-3班学习委员林小满"])
    assert "林小满" not in out
    assert rep.name_placeholders == 1


# ── 报告与审计 ──────────────────────────────────────────────────────


def test_报告分得清号码与姓名() -> None:
    """两个计数各数各的：号码占位符与姓名占位符不混。"""
    out, rep = scrub("联系17700001001 找林小满", ["林小满"])
    assert NUM_PLACEHOLDER in out and NAME_PLACEHOLDER in out
    assert rep.num_placeholders == 1
    assert rep.name_placeholders == 1


def test_审计归档不存内容_内存里的命中可以带原文() -> None:
    """※ **这条判据的落点被修正过一次，值得记。**

    原来它断言「`ScrubHit` 没有 `text` 字段」—— 那是把**内存里的对象**
    当成了**磁盘上的记录**。而这两件事的需求正好相反：

      · 内存里：调用方（指标脚本、自检）**需要知道抹掉了什么**，
        否则它只能拿坐标去切原文 —— 而坐标是替换过程中的，切出来是垃圾。
      · 磁盘上：**绝不能存内容** —— 那会新造一个泄漏面。

    ⇒ 判据断言**落盘那一侧**：`ALLOWED_KEYS` 白名单里没有 `text`，
    且 `record()` 写出去的行里不含它。白名单而不是黑名单 ——
    黑名单会在有人加新字段时静默放行。
    """
    from yechao.infrastructure.runtime.audit_log import ALLOWED_KEYS, JsonlAuditLog

    _, rep = scrub("联系17700001001", ["赵小满"])
    # 内存里：有 text（调用方需要它）
    assert any(h.text == "17700001001" for h in rep.hits)

    # 磁盘上：不许有
    assert "text" not in ALLOWED_KEYS
    assert "content" not in ALLOWED_KEYS
    assert "raw" not in ALLOWED_KEYS

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        log = JsonlAuditLog(d)
        log.record({"surface": "refine", "text": "17700001001", "messages": 1}, ts=1700000000)
        row = log.read_all()[0]
    assert "text" not in row, "被抹掉的原文写进了审计归档"
    assert "17700001001" not in json.dumps(row)


# ── 判据表本身的自检 ────────────────────────────────────────────────


def test_触发词表非空且每个词都能真的触发() -> None:
    """※ **空转自检：表里的词不能有「命中不了的死条目」。**

    一个永远匹配不到的词比没有更糟 —— 读代码的人会以为它在起作用。
    这里对表里**每一个**词都造一条「词 + 数字」的输入，逐个断言它真的开火。
    """
    from yechao.domain.scrub import _ACCOUNT_TRIGGER_WORDS

    assert _ACCOUNT_TRIGGER_WORDS, "触发词表是空的 —— 这一维根本没在查"
    for word in _ACCOUNT_TRIGGER_WORDS:
        out, _ = scrub(f"{word}820000001")
        assert "820000001" not in out, f"触发词 {word!r} 匹配不到自己的数字"


def test_强判据与模糊判据的边界是同一个数() -> None:
    """11 位是信号 A（手机号）与信号 C（触发词）的**共同上界**，
    12 位是信号 B（长数字串）的下界 —— 三条判据在这一点上接住，不留缝。

    这里钉住「接缝」：11 位的触发词数字仍然靠触发词抹，12 位的不用触发词也抹。
    """
    out, _ = scrub("群 820000001")       # 9 位：靠触发词
    assert "820000001" not in out
    out2, _ = scrub("820000001234")      # 12 位：不用触发词也抹（信号 B）
    assert "820000001234" not in out2
    out3, _ = scrub("12345678901")       # 11 位、不是 1[3-9] 开头：无触发词 ⇒ 不抹
    assert "12345678901" in out3
