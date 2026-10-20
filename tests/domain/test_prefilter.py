"""本地预筛 —— 判据的每一条都单独钉住。

## 这个文件为什么非有不可

预筛原先**只有间接覆盖**（从 `demo` / `metrics` 那条路顺手跑过），于是有两件
事没人发现：

  1. 应答表里的数字应答词（`1` / `111` / `666` / `888`）**永远命中不了** ——
     它们先被「含数字 ⇒ 不是纯应答」那条检查拦下，实际走的是别的规则。
     **一条死判据**，报告里看不见，读代码的人以为它在起作用。
  2. 判据的形状是「一张平铺词表 + `any(词 in 文本)`」—— 这个形状的病
     （把「出现了一个名词」当成「有事要办」）测不出来，因为它**没有反例**：
     表里有的词命中，表里没有的词不命中，而「该不该命中」这个问题，
     一张表回答不了。

所以这里按**判据分组**写：每组先钉住正例，再钉住**反例**（这一组最容易被
误伤的那一类输入）。没有反例的判据，等于没测。

※ 假号码自己编（`1770000100X`），假群号 `820000001` 起 —— 与 `synth.py`
同一批编号，不是从任何地方抄的。
"""

from __future__ import annotations

import pytest

from yechao.domain import prefilter as P
from yechao.domain.models import PendingMessage
from yechao.domain.synth import CORPUS, GROUPS, expectations

# ── 夹具 ────────────────────────────────────────────────────────────

#: 一个**不高优、发送者也没有角色**的群 —— 内容判据要单独成立，就得挑一个
#: 别的保留规则都不命中的地方。否则「留下了」可能是高优群或角色兜的。
PLAIN_GROUP = 820000002
PLAIN_SENDER = "路人甲"


def _msg(content: str, *, msg_id: int = 1, group_id: int = PLAIN_GROUP,
         sender: str = PLAIN_SENDER, ts: int = 0) -> PendingMessage:
    return PendingMessage(
        msg_id=msg_id, group_id=group_id, ts=ts, sender=sender, content=content
    )


def _decide(content: str, *, tier: str = "normal", sender: str = PLAIN_SENDER):
    return P.filter_one(_msg(content, sender=sender), tier=tier)


# ══════════════════════════════════════════════════════════════════
# 硬丢弃一：纯应答（**整体匹配**，不是包含匹配）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text", ["收到", "好的", "好的谢谢", "嗯嗯好的", "谢谢", "顶", "打卡", "ok"]
)
def test_纯应答被丢(text: str) -> None:
    d = _decide(text)
    assert not d.keep
    assert d.rule == "pure_ack"


def test_应答表里没有命中不了的死条目() -> None:
    """**这条测试防的是审计指出的那个病本身。**

    一个**命中不了**的判据比没有更糟：读代码的人以为它在起作用，报告里也
    永远看不到它。原表里有两例 —— `666` 这类数字应答词（被「含数字」检查
    拦下）与 `+1`（`+` 被当数学符号剥掉）。

    ⇒ 不变量：表里任何一个词，剥掉噪声之后必须**等于它自己**。
    不等于自己的，永远等不到匹配，因为匹配只发生在**剥完噪声之后**的串上。
    """
    for word in P._ACK_WORDS:
        assert P._strip_noise(word) == word, f"{word!r} 剥噪声后变形，永远匹配不到"
        assert P.is_pure_ack(word), f"{word!r} 单独一条都判不成纯应答"


def test_纯应答是整体匹配而不是包含匹配() -> None:
    """**这条是整个硬丢弃侧的要害。**

    「收到，下午3点图书馆门口集合」含「收到」，可它是一条真通知 ——
    它必须留下。如果判据写成 `"收到" in text`，噪声与真通知会一起被
    捞掉或一起被留下，怎么调阈值都不对。
    """
    assert not P.is_pure_ack("收到，下午3点图书馆门口集合")
    d = _decide("收到，下午3点图书馆门口集合")
    assert d.keep
    assert d.rule == "anchor_time"  # 靠 9点 这个时刻锚留下的


def test_含数字的应答不是纯应答() -> None:
    """「收到 3 份」含数字 ⇒ 有信息，不是纯应答。

    ※ 注意这条**不靠**一条专门的「含数字就否决」检查 —— 那条检查在数字
    应答词被拿掉之后恒不触发（见下一条测试）。这里是**结构**保证的：
    表里没有任何一个词含数字，所以任何能被词表拆完的串都不含数字。
    """
    assert not P.is_pure_ack("收到3份")
    assert not P.is_pure_ack("收到 3 份")


@pytest.mark.parametrize(
    "text,rule",
    [
        ("666", "pure_symbol"),
        ("111", "pure_symbol"),
        ("888", "pure_symbol"),
        ("777", "pure_symbol"),
        ("1", "too_short"),
        ("6", "too_short"),
        ("+1", "too_short"),
    ],
)
def test_数字与符号应答词现在真的可达(text: str, rule: str) -> None:
    """**审计指出的那条死判据，这里逐条钉住它现在落在哪。**

    原表里 `1` / `111` / `666` / `888` / `+1` 与一条「含数字 ⇒ 不是纯应答」
    互相抵消：它们永远走不到词表匹配。现在这些条目与那条检查一起拿掉了，
    它们由**本来就在管这件事**的规则接住：

      · 同一个字符重复 ≥3 次（`666` / `111` / `888`）⇒ `pure_symbol`
      · 单个数字或剥完只剩一个数字（`1` / `6` / `+1`）⇒ `too_short`

    ※ 行为**不变**（都还是丢），变的是「为什么丢」现在说得清、也测得着。
    """
    assert not P.is_pure_ack(text)
    d = _decide(text)
    assert not d.keep
    assert d.rule == rule


# ══════════════════════════════════════════════════════════════════
# 硬丢弃二：纯符号 / 无实义
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("text", ["+++", "***", "～～～", "。。。", "！！！"])
def test_纯符号被丢(text: str) -> None:
    d = _decide(text)
    assert not d.keep
    assert d.rule == "pure_symbol"


@pytest.mark.parametrize("text", ["哈哈哈哈哈", "啊啊啊啊", "哦哦哦"])
def test_同一字符重复也算无实义(text: str) -> None:
    """`哈哈哈哈哈` 不是符号，但它和 `+++` 一样没有信息量。

    ※ 这条同时**替应答表管住了**所有「同一个字刷一串」的形态 ——
    所以应答表里刻意不再重复列 `哈哈哈` / `呵呵呵`：同一件事写两处，
    改一处就会漏一处。
    """
    assert _decide(text).rule == "pure_symbol"


# ══════════════════════════════════════════════════════════════════
# 硬丢弃三：过短（**字符**，不是字节）
# ══════════════════════════════════════════════════════════════════


def test_过短按字符不按字节() -> None:
    """中文一个字 3 字节、英文一个字母 1 字节 —— 按字节算等于**悄悄对中文
    更宽松**（`abc` 是 3、`好` 也是 3，可 `好` 的信息量远高于 `abc`）。"""
    assert len("好".encode("utf-8")) == 3
    assert P.is_too_short("好")
    assert P.is_too_short("abc")
    assert not P.is_too_short("abcd")
    assert not P.is_too_short("明早集合")


def test_过短排在保留之后() -> None:
    """长度是**代理判据**，会误伤短而重要的消息。

    ⇒ 它排在锚与祈使结构**之后**：短消息只要命中了任何一条保留规则，
    就已经被保住了，长度判据只对「什么都没有」的消息生效。
    """
    # 「停课」只有两个字，但有日期锚 ⇒ 留下（若长度判据在前，它会被误伤）
    assert _decide("10月28日停课").rule == "anchor_time"
    # 光秃秃的「停课」填不出任何一格 ⇒ 走长度判据
    assert _decide("停课").rule == "too_short"


def test_只出现名词的消息不值得发给模型() -> None:
    """**这是本版判据要治的病。**

    旧判据是「文本里有没有表里的词」，于是「讲座」「图书馆」这种**只出现
    一个名词**的消息会命中、被留下 —— 可它填不出 `kind` / `title` /
    `deadline` / `start` / `place` / `amount` 里的任何一格。
    """
    for text in ("讲座", "图书馆", "停课"):
        assert not _decide(text).keep, text


# ══════════════════════════════════════════════════════════════════
# 保留一：可核验锚（算出来的）
# ══════════════════════════════════════════════════════════════════


def test_时间锚复用times的边界判据() -> None:
    """※ 锚**必须复用 `times.py`**，不能另写一套正则。

    日期/时刻的边界判据在那边已经推导并测试过。这里再写一套，两套迟早分叉，
    而分叉的症状是「预筛留下了一条，闸门却说它没有依据」—— 两边单看都正常。

    ⇒ 这条测试直接拿 `times.py` 的边界当预期：`9/3` 与 `9/30` 是同一个
    数字串的两种读法，但它们是**不同的日子**，都要能被认出来。
    """
    assert P.find_field_anchors("10月28日前提交")
    assert P.find_field_anchors("10/28 截止")
    assert P.find_field_anchors("14:30 开会")
    assert P.find_field_anchors("下午两点集合")
    # `9/3-9/5` 是日期区间，两端都算 —— 「短」不是拒绝的理由
    assert len(P.find_time_anchors("9/3-9/5")) == 2


def test_金额锚要单位不要数字() -> None:
    """数字本身没有语义，**单位才有**。

    ※ 反例比正例重要：`20261205` 是日期、`530001887` 是群号 —— 它们都是
    数字，但都不是金额。只按「有没有数字」判金额，会把它们收进来。
    """
    assert [a.raw for a in P.find_amount_anchors("出一台台灯，30元")] == ["30元"]
    assert [a.raw for a in P.find_amount_anchors("预算50元以内")] == ["50元"]
    assert [a.raw for a in P.find_amount_anchors("￥99")] == ["￥99"]
    assert P.find_amount_anchors("20261205 起执行") == ()
    assert P.find_amount_anchors("加群530001887") == ()
    assert P.find_amount_anchors("10月28日") == ()


def test_联系锚是长数字与链接() -> None:
    """长数字串（≥5 位）与链接说明这条消息在**指认一个具体对象或联系方式**。"""
    assert [a.raw for a in P.find_contact_anchors("联系17700001005")] == ["17700001005"]
    assert [a.raw for a in P.find_contact_anchors("加群530001887咨询")] == ["530001887"]
    assert P.find_contact_anchors("报名表 https://example-notice.test/form 请填写")
    # 全角数字也算 —— 全角是绕过写法，不是另一回事
    assert P.find_contact_anchors("ＱＱ群 ５３０００１８８８ 大家加一下")


def test_紧凑日期被认成时间而不是联系() -> None:
    """`20261205` 是日期（8 位），不是号码。

    `times.py` 的日期形态都要求分隔符或「月/日」字，紧凑写法它不认；
    不补这条的话，`20261205` 会被长数字锚收走、报成 `contact` ——
    **决定没错（该留），但证据说错了**，而报告的全部价值就在证据上。
    """
    assert [a.field for a in P.find_time_anchors("20261205 起执行新作息")] == ["time"]
    d = _decide("春季学期校历已发布，20261205 起执行新作息。")
    assert d.rule == "anchor_time"


def test_紧凑日期的合法性校验() -> None:
    """※ 校验是照 `times.py` 的取向做的（月 1-12、日 1-31）。

    少了它，`12345678` 这种序号也会被当成日期 —— 那是**给一个不是日期的
    东西背书**，正是 `times.py` 反复在防的形状。
    """
    assert not P.find_time_anchors("12345678")
    assert not P.find_time_anchors("20261340")
    # 但它们仍然是「一串具体的数字」⇒ 走联系锚，而不是被完全忽略
    assert P.find_contact_anchors("12345678")


def test_锚正例在普通群里也留得下() -> None:
    """锚必须**单独成立** —— 不靠高优群、也不靠发送者角色。

    挑的是一个既不高优、发送者也没有角色的群：如果锚不成立，这几条会掉。
    """
    assert _decide("出一台二手台灯，30元，宿舍楼下自取。").rule == "anchor_amount"
    assert _decide("收款账号 62170017700001003 请核对。").rule == "anchor_contact"


# ══════════════════════════════════════════════════════════════════
# 保留二：祈使结构（四组，各自独立）
# ══════════════════════════════════════════════════════════════════


def test_祈使标记要出现在小句开头() -> None:
    """**这一组最常见的误伤是「包含匹配」。**

    「请」在「请假」里不是祈使标记，「记得」在「我记得」里不是祈使副词。
    判据是「祈使标记出现在**小句开头**」（消息开头，或紧跟一个停顿标点）。
    """
    assert P.find_imperative("请各班于10月28日前提交") == ("imperative", "请")
    assert P.find_imperative("请于月底前缴清") == ("imperative", "请")
    assert P.find_imperative("记得带材料") == ("imperative", "记得")
    # 反例：同样含这些字，但它们不在小句开头
    assert P.find_imperative("我请假了") is None
    assert P.find_imperative("我记得你") is None
    assert P.find_imperative("请假") is None
    assert P.find_imperative("请客") is None


def test_条件后果是一种结构不是名词() -> None:
    """通知与闲聊的分水岭之一：通知会写「不办会怎样」。"""
    assert P.find_imperative("逾期不予受理") == ("consequence", "逾期")
    assert P.find_imperative("过期作废，后果自负") == ("consequence", "过期作废")
    assert P.find_imperative("大家记得吃饭") is None


def test_明确受众要带定语() -> None:
    """※ 不收光秃秃的「大家」「各位」：光说「大家」可以是任何一句闲聊的
    开头，而「…的同学」把**受众**指出来了（谁该看这条是明确的）。"""
    assert P.find_imperative("还没交的同学抓紧") == ("addressee", "还没交的同学")
    assert P.find_imperative("有意者加群咨询") == ("addressee", "有意者")
    assert P.find_imperative("大家加一下") is None
    assert P.find_imperative("同学们好") is None


def test_公开求助的疑问词要指向他人() -> None:
    """失物招领/寻人这类消息的形状是「有谁…吗」「谁有…」。

    ※ 单说「吗」不算 —— 「吃了吗」也是疑问句。判据是**疑问词 + 求助动词**。
    """
    assert P.find_imperative("有谁捡到一把黑色雨伞吗") == ("help_seeking", "有谁")
    assert P.find_imperative("谁有闲置的台灯") == ("help_seeking", "谁有")
    assert P.find_imperative("有人知道这事吗") == ("help_seeking", "有人知道")
    # 反例：疑问指向的**不是他人**
    assert P.find_imperative("吃了吗") is None
    assert P.find_imperative("有人吗") is None


def test_四组祈使结构各报各的名字() -> None:
    """※ 分组返回而不是统一叫 `imperative`：报告要能说清「这条是因为
    『逾期』留下的」还是「因为『请』留下的」—— 那是两条不同的证据。"""
    names = {
        P.find_imperative("请各班注意")[0],
        P.find_imperative("逾期不予受理")[0],
        P.find_imperative("有意者加群")[0],
        P.find_imperative("有谁捡到吗")[0],
    }
    assert names == {"imperative", "consequence", "addressee", "help_seeking"}


# ══════════════════════════════════════════════════════════════════
# 保留三：群层级 / 发送者角色
# ══════════════════════════════════════════════════════════════════


def test_高优群无条件保留() -> None:
    assert _decide("今天天气不错", tier="high").rule == "high_tier"


def test_有角色的发送者默认可信() -> None:
    assert _decide("今天天气不错", sender="22-1班班主任助理林小满").rule == "role_sender"
    assert _decide("今天天气不错", sender="教务员周允").rule == "role_sender"
    # 反例：昵称里没有角色词
    assert not _decide("今天天气不错", sender="路人甲").keep


def test_低优群不额外丢只不额外保留() -> None:
    """「二手群里也可能有人发重要通知」——这个判断由锚与祈使结构兜住。"""
    assert _decide("出二手自行车，200元。", tier="low").keep


# ══════════════════════════════════════════════════════════════════
# 顺序：先丢弃、后保留
# ══════════════════════════════════════════════════════════════════


def test_硬丢弃优先于一切保留规则() -> None:
    """**顺序就是判据。**

    如果先判「值得留」，一条宽松的规则会把「收到」也捞回来。
    这条测试把「收到」放进一个**最容易被保留规则捞走**的位置：
    高优群 + 有角色的发送者 —— 它仍然必须被丢。
    """
    for tier in ("high", "normal", "low"):
        for sender in ("路人甲", "班长孙予棠"):
            d = P.filter_one(_msg("收到", sender=sender), tier=tier)
            assert not d.keep, (tier, sender)
            assert d.rule == "pure_ack"
            assert d.by == "drop"


def test_都不命中时是默认丢弃而不是某条判据() -> None:
    """※ `by="default"` 单列：混淆「默认值」与「判据」会让覆盖率算错分母。"""
    d = _decide("abcd")
    assert not d.keep
    assert d.rule == "no_signal"
    assert d.by == "default"


def test_判据名不出文档化的那一个集合() -> None:
    """判据名是给测试与日志用的**稳定标识**（界面文案在 `cli._rule_label`）。

    跑一批形态各异的输入，断言产出的名字都在文档化的集合里 —— 防的是
    改名之后报告里悄悄多出一个没人认识的键。
    """
    documented = {
        "pure_ack", "pure_symbol", "too_short", "no_signal",  # 丢弃
        "high_tier", "role_sender",  # 群 / 人
        "anchor_time", "anchor_amount", "anchor_contact",  # 锚
        "imperative", "consequence", "addressee", "help_seeking",  # 祈使结构
    }
    samples = [
        "收到", "+++", "停课", "abcd", "今天天气不错",
        "10月28日前提交材料", "人均25元", "联系17700001005",
        "请各班注意", "逾期不予受理", "有意者加群", "有谁捡到吗",
    ]
    seen = {_decide(s).rule for s in samples}
    seen |= {_decide(s, tier="high").rule for s in samples}
    seen |= {_decide(s, sender="班长孙予棠").rule for s in samples}
    assert seen <= documented, seen - documented


# ══════════════════════════════════════════════════════════════════
# 刷屏：桶的三个维度
# ══════════════════════════════════════════════════════════════════


def test_同人同群同内容连发三条判刷屏() -> None:
    msgs = [_msg("出二手自行车，200元。", msg_id=i, ts=i) for i in (1, 2, 3)]
    dup = P.find_spam(msgs)
    assert set(dup) == {2, 3}
    assert dup[2] == 1  # 留最早的那一条


def test_跨群转发不是刷屏() -> None:
    """**这条是刷屏判据的全部要害。**

    三个不同群转发同一条通知**不是**刷屏 —— 那恰恰是最该保留、最该合并的
    信号。判「重复」的桶漏掉「群」这一维，它就会被误判。
    """
    msgs = [
        _msg("转：期中活动报名截止 11月1日，请相关同学注意。", msg_id=1, group_id=820000001, ts=1),
        _msg("转：期中活动报名截止 11月1日，请相关同学注意。", msg_id=2, group_id=820000002, ts=2),
        _msg("转：期中活动报名截止 11月1日，请相关同学注意。", msg_id=3, group_id=820000003, ts=3),
    ]
    assert P.find_spam(msgs) == {}


def test_不同的人发同一句不合并() -> None:
    """桶缺「发送者」这一维，两个人发的同一句会被合并成一个桶。"""
    msgs = [
        _msg("收到", msg_id=1, sender="甲", ts=1),
        _msg("收到", msg_id=2, sender="乙", ts=2),
        _msg("收到", msg_id=3, sender="丙", ts=3),
    ]
    assert P.find_spam(msgs) == {}


def test_同人同群但内容不同不判刷屏() -> None:
    """桶缺「内容」这一维，同一个人发的两条不同消息会被当成重复。"""
    msgs = [
        _msg("出二手自行车，200元。", msg_id=1, ts=1),
        _msg("出二手台灯，30元。", msg_id=2, ts=2),
        _msg("出二手键盘，50元。", msg_id=3, ts=3),
    ]
    assert P.find_spam(msgs) == {}


def test_刷屏按窗口判不按紧邻判() -> None:
    """语料里同一个人发三条一样的，中间常常夹着别人的插话 —— 按紧邻判会漏。"""
    msgs = [
        _msg("出二手自行车，200元。", msg_id=1, sender="甲", ts=1),
        _msg("插一句", msg_id=2, sender="乙", ts=2),
        _msg("出二手自行车，200元。", msg_id=3, sender="甲", ts=3),
        _msg("再插一句", msg_id=4, sender="乙", ts=4),
        _msg("出二手自行车，200元。", msg_id=5, sender="甲", ts=5),
    ]
    assert set(P.find_spam(msgs)) == {3, 5}


# ══════════════════════════════════════════════════════════════════
# prescreen：被丢掉的那一份必须带原因返回
# ══════════════════════════════════════════════════════════════════


def test_prescreen把丢弃原因一起返回() -> None:
    """※ **静默跳过一条消息 = 用户以为那天没事。**

    所以被丢掉的那一份必须带原因返回，调用方拿它记进失败账本，
    覆盖率的分母就从那儿来。
    """
    msgs = [_msg("收到", msg_id=1), _msg("10月28日前提交材料", msg_id=2)]
    kept, dropped = P.prescreen(msgs, tiers={})
    assert [m.msg_id for m in kept] == [2]
    assert [(m.msg_id, d.rule) for m, d in dropped] == [(1, "pure_ack")]


def test_prescreen按群层级取tier() -> None:
    msgs = [_msg("今天天气不错", msg_id=1, group_id=820000001)]
    kept, dropped = P.prescreen(msgs, tiers={820000001: "high"})
    assert [m.msg_id for m in kept] == [1]
    assert dropped == []


# ══════════════════════════════════════════════════════════════════
# with_context：补邻居，但邻居只作上下文
# ══════════════════════════════════════════════════════════════════


def test_上下文邻居被补进送模型的批次() -> None:
    """一条「好的，那就这么定了」单独看没有信息，它的含义在**前一条**里。

    预筛会把它丢掉（没有锚、没有祈使结构），于是模型看到的上下文是断的 ——
    所以保留消息的前后邻居要补进来。
    """
    msgs = [
        _msg("好的，那就这么定了", msg_id=1, ts=1),
        _msg("行，听你的", msg_id=2, ts=2),
        _msg("嗯，明白了", msg_id=3, ts=3),
        _msg("社团招新 10月18日 14:00 图书馆门口", msg_id=4, ts=4),
        _msg("收到", msg_id=5, ts=5),
    ]
    kept, _ = P.prescreen(msgs, tiers={})
    assert [m.msg_id for m in kept] == [4]
    batch = P.with_context(kept, msgs)
    # 默认 before=2 / after=1：前两条与后一条补进来，更远的不补
    assert {m.msg_id for m in batch} == {2, 3, 4, 5}


def test_上下文邻居只按群补不跨群() -> None:
    msgs = [
        _msg("10月18日摆摊招新", msg_id=1, group_id=820000003, ts=1),
        _msg("另一条闲聊", msg_id=2, group_id=820000004, ts=2),
    ]
    kept, _ = P.prescreen(msgs, tiers={})
    batch = P.with_context(kept, msgs)
    assert {m.msg_id for m in batch} == {1}


# ══════════════════════════════════════════════════════════════════
# 端到端回归：对合成语料的真值跑一遍
# ══════════════════════════════════════════════════════════════════


def test_合成语料上不漏检也不误留() -> None:
    """**这一条是重设计的回归锁。**

    `synth.py` 的 `expect.keep` 是**标准答案**（每条语料自己带着真值）。
    改判据结构时最怕的是「测试绿、覆盖率悄悄掉」—— 所以这里直接拿真值
    算两个数：

      · 漏检（真值说该留、实际被丢）⇒ 真通知没进模型，用户以为那天没事
      · 误留（真值说该丢、实际留下）⇒ 白花预算

    ※ 这两个数**只对合成语料成立**。真实数据没有真值（隐私原因下永远
    无法人工标注），指标的意义是证明判据**可被度量**，不是声称普适准确率。
    """
    tiers = {g.id: g.tier for g in GROUPS}
    msgs = [c.msg for c in CORPUS]
    kept, dropped = P.prescreen(msgs, tiers=tiers)
    kept_ids = {m.msg_id for m in kept}
    expect = expectations()

    missed = sorted(i for i, e in expect.items() if e.keep and i not in kept_ids)
    false_keep = sorted(i for i, e in expect.items() if not e.keep and i in kept_ids)
    assert missed == []
    assert false_keep == []
    # 丢弃的那一份必须**带原因**（不静默跳过）
    assert all(d.rule for _, d in dropped)


def test_合成语料里跨群转发的那三条都留下来了() -> None:
    """跨群转发是最该保留、最该合并的信号 —— 它**不能**被刷屏判据吃掉。"""
    tiers = {g.id: g.tier for g in GROUPS}
    msgs = [c.msg for c in CORPUS]
    kept, dropped = P.prescreen(msgs, tiers=tiers)
    kept_ids = {m.msg_id for m in kept}
    assert {22, 23, 24} <= kept_ids
    assert not any(m.msg_id in {22, 23, 24} for m, _ in dropped)


def test_合成语料里刷屏的那两条确实被刷屏判据丢掉() -> None:
    tiers = {g.id: g.tier for g in GROUPS}
    msgs = [c.msg for c in CORPUS]
    _kept, dropped = P.prescreen(msgs, tiers=tiers)
    spam_ids = {m.msg_id for m, d in dropped if d.rule == "spam"}
    assert spam_ids == {33, 34}
