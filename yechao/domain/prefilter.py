"""本地预筛 —— 哪些消息不值得发给模型。

## 要防什么

把消息全发给模型既慢又贵，而其中大部分是「收到」「好的」这类纯应答。

## 判据的形状：从「有没有这个词」换成「填不填得出字段」

**一张平铺的动作词表**配上 `any(词 in 文本)` 这个形状有两个毛病：

  1. **把「出现了一个名词」当成了「有事要办」。** 「讲座」两个字单独发出来
     会命中，于是被留下 —— 可它**填不出条目的任何一格**。词表判据分不清
     「这条消息在讲一件事」和「这条消息提到了一个词」。
  2. 词表是**枚举**：真实语料里没枚举到的说法就是漏检，而唯一的补救是
     「再多加几个词」—— 表只会越长、越不可解释，且每一版都无法单独验证。

这一版换个问法：**这条消息能不能填出条目所需要的至少一个字段？**

条目要填的是 `kind` / `title` / `deadline` / `start` / `place` / `amount`
（见 `domain/models.py` 的 `ExtractionCandidate`）。于是保留侧的判据分成
两类，**都不再是「一张词表」**：

  A. **可核验锚 —— 算出来的，不是查表的。**
     日期 / 时刻复用 `times.find_dates` / `times.find_times`；金额、长数字与
     链接各有一个解析器。它们**正好对应**条目要填的 `deadline` / `start` /
     `amount` 那几格。
     为什么这比词表强：锚是**可计算**的，所以它可以被穷举测试
     （「`10/28` 命中、`10/2` 不命中」这种边界能一条条钉住），而词表只能
     测「我列过的词还在不在表里」。
  B. **祈使结构 —— 句子形状，不是孤立名词。**
     句子在**对读者说话**：祈使标记 / 条件-后果 / 明确受众 / 公开求助。
     这四组各有各的观察与理由，见下面各自的小节，也各有各的判据名。

※ **地点没有锚，这是刻意的。** 「图书馆」三个字不可计算（没有解析器，
硬做就是一串后缀词表 —— 又回到枚举）；而**只出现一个地名**的消息
（「图书馆」）恰恰是最不该发的那种纯名词消息。给地点设锚等于把病灶请回来。
地点该由「有锚的那条消息」顺带带出来（「10月18日在图书馆门口」）。

## 取向：先丢弃、后保留

**硬丢弃一旦命中就出局，保留规则没有翻案机会。**

顺序很重要：如果先判「值得留」，那些明显是噪声（「收到」）的会先被某些规则
捞回来 —— 比如「收到，明天 9 点在体育馆集合」这条**必须**保留（它含时间
地点），而「收到」必须丢。先判保留的话，一条宽松的「含时间词」规则会把
后面的「收到」也捞回来。

所以：

  1. **硬丢弃**：无论出现在哪个群都不值得（纯应答 / 纯符号）
  2. **保留**：高优群 / 有角色的发送者 / 命中字段锚 / 命中祈使结构
  3. 都不命中 ⇒ 走长度判据，再**默认丢弃**（默认不发给模型）

※ 保留侧的判据（锚、祈使结构、群层级、角色）**宽进没有漏检风险**，
只有多花预算的风险。真正的漏检风险在硬丢弃那一侧，而那一侧是
**整体匹配**，很窄 —— 这个不对称是刻意留的。

## 怎么验证

  · 每条判据一个函数，可单独调用、单独断言（`tests/domain/test_prefilter.py`）
  · `prescreen` 对合成语料跑一遍，与真值对照 —— 回归锁在
    `tests/domain/test_prefilter.py::test_合成语料上不漏检也不误留`
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from yechao.domain import times
from yechao.domain.models import PendingMessage

# ══════════════════════════════════════════════════════════════════
# 硬丢弃一：纯应答
# ══════════════════════════════════════════════════════════════════
#
# **观察**：「收到」「好的」「谢谢」这类消息占了群聊的一多半，它们**无论出现
# 在哪个群都不值得**发给模型，所以排在最前。
#
# **为什么判据是「整体匹配」而不是「包含匹配」**：`行，那周四下午三点在实验楼门口等`
# 含「行」，而它**必须**保留（它含时间与地点）。用包含匹配，噪声与真通知
# 会一起被捞掉或一起被留下，怎么调阈值都不对。
# ⇒ 判据是**整条消息剥掉空白与标点后，就是那个应答词本身**（或只由这些词
# 堆成）。「整体」这两个字是这条判据的全部要害。
#
# **怎么验证**：`is_pure_ack("行")` 真、
# `is_pure_ack("行，那周四下午三点在实验楼门口等")` 假
# —— 后者是这条判据的反例，测试里逐字钉住。

#: 确认/应答 —— 对方发了一个指令，我回一句「收到」。
#: 它是**会话的填充物**：确认了「我看见了」，但不携带任何能填进条目的信息。
_ACK_AGREE: tuple[str, ...] = (
    "收到", "好的", "好", "行", "可以", "了解", "明白", "明白啦", "嗯",
    "嗯嗯", "对", "是的", "确实", "没错", "ok", "OK", "okay", "yes", "no",
    "打卡",
)

#: 致谢/附和 —— 「谢谢」「赞」「支持」。
#: 它们与「确认」是两件事（确认是对指令的回应，附和是对人的评价），
#: 但**共同的形状**是：只可能出现在对话流里，且填不出任何一格。
_ACK_THANKS: tuple[str, ...] = (
    "谢谢", "多谢", "感谢", "辛苦了", "赞", "顶", "支持", "同意",
)

#: 起哄 —— 多字、非重复的哄场话。
#: ※ 「哈哈哈」「呵呵呵」「嘿嘿」这类**同一字符重复**刻意**不列在这里** ——
#: 它们由 `is_pure_symbol` 的「同一字符重复 ≥3 次」统一处理（那条本来就在管
#: `+++` / `+++` / `啊啊啊`）。同一件事写两处，改一处就会漏一处。
_ACK_JEER: tuple[str, ...] = ("笑死", "太强了", "牛逼", "绝了", "泪目")

#: 三组合并后的全集。※ **按判据分组**，不是一张平铺表 —— 每组上面都有
#: 各自的理由，测试也按组分别断言。
_ACK_GROUPS: dict[str, tuple[str, ...]] = {
    "agree": _ACK_AGREE,
    "thanks": _ACK_THANKS,
    "jeer": _ACK_JEER,
}

_ACK_WORDS: tuple[str, ...] = tuple(
    w for group in _ACK_GROUPS.values() for w in group
)

#: 逐词剥离时**长词优先**：`明白啦` 必须先于 `明白` 被剥掉，否则会剩下一个
#: 「啦」把整条判成非应答。
_ACK_LONGEST_FIRST: tuple[str, ...] = tuple(
    sorted(set(_ACK_WORDS), key=len, reverse=True)
)

# ※ **数字应答词（`1` / `111` / `666` / `888` …）与「含数字 ⇒ 不是纯应答」
# 这条检查一起被拿掉了，理由值得写下来。**
#
# 原表里既有 `666` 这样的数字应答词，又有一条「剥完含数字 ⇒ 不是纯应答」。
# 这两条**互相抵消**：`666` 剥完就是数字，先被数字检查拦下，永远走不到词表
# 匹配。实测它走的是 `pure_symbol`（同一字符重复 ≥3 次），而 `1` 走的是
# `too_short`。
#
# 一个**命中不了**的判据比没有更糟 —— 读代码的人会以为它在起作用，报告里
# 也永远看不到它。所以两条一起删：删掉数字应答词之后，「含数字」这条检查
# 也就恒不触发了（表里没有任何一个词含数字 ⇒ 任何能被词表拆完的串都不含
# 数字 ⇒ 检查永远为假）。
#
# 行为不变：数字串仍然落在 `pure_symbol` / `too_short` 上，测试逐条钉住。
#
# ※ 同一个病还有一例：`+1`。它**也**命中不了 —— `_strip_noise` 会把 `+`
# 当数学符号剥掉（类别 `Sm`），剥完只剩 `1`，于是永远等不到词表匹配。
# 一起拿掉，同样由 `too_short` 接住。
#
# ⇒ 由此得到一条**可测的不变量**（`test_应答表里没有命中不了的死条目`）：
# 表里任何一个词，剥掉噪声之后必须**等于它自己**，否则它永远匹配不到。

#: 剥掉这些之后看剩下什么。空白、标点、emoji、符号。
_STRIP_CATEGORIES = {"Po", "Ps", "Pe", "Pi", "Pf", "Pd", "Pc", "Sm", "Sc", "Sk", "So", "Zs", "Cc", "Cf"}


def _strip_noise(text: str) -> str:
    """剥掉空白、标点、符号、emoji，只留「实义字符」。"""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch) in _STRIP_CATEGORIES:
            continue
        out.append(ch)
    return "".join(out)


def is_pure_ack(text: str) -> bool:
    """**整体**是纯应答吗。

    ※ 关键在于「整体」：剥掉噪声后剩下的串，必须**只由应答词堆成**。
    多词堆叠（「好的谢谢」「嗯嗯好的」）也算 —— 逐词剥离，剥完必须为空。
    """
    stripped = _strip_noise(text)
    if not stripped:
        return False
    if stripped in _ACK_WORDS:
        return True
    rest = stripped
    # 每轮至少吃掉一个词，所以最多 len(stripped) 轮 —— 有界，且不会因为
    # 堆叠得太长而在中途放弃（「好的」连写九遍仍然是纯应答）。
    for _ in range(len(stripped) + 1):
        if not rest:
            return True
        for w in _ACK_LONGEST_FIRST:
            if rest.startswith(w):
                rest = rest[len(w) :]
                break
        else:
            return False
    return not rest


# ══════════════════════════════════════════════════════════════════
# 硬丢弃二：纯符号 / 无实义
# ══════════════════════════════════════════════════════════════════


def is_pure_symbol(text: str) -> bool:
    """剥掉噪声之后什么都不剩（或者只剩重复的同一个字符）。

    `+++` / `+++` / `～～～` 走这条。
    ※ 「只剩同一个字符重复」这条是为 `哈哈哈哈` / `啊啊啊啊` 这类准备的 ——
    它们不是符号，但同样没有信息量。这条同时替应答表管住了所有
    「同一个字刷一串」的形态。
    """
    stripped = _strip_noise(text)
    if not stripped:
        return True
    return len(set(stripped)) == 1 and len(stripped) >= 3


# ══════════════════════════════════════════════════════════════════
# 保留一：可核验锚（算出来的）
# ══════════════════════════════════════════════════════════════════
#
# **观察**：值得发给模型的消息，几乎都**指着一件具体的事**，而「具体」在
# 文本上留下的痕迹是可计算的那几种 —— 一个日期、一个时刻、一个金额、
# 一串号码或一个链接。
#
# **为什么这些是锚**：它们**正好对应条目要填的格子**。
#   · 日期/时刻 → `deadline` / `start`（哪个是哪个由模型结合语境判，预筛
#     不判 —— 预筛只回答「填得出吗」）
#   · 金额 → `amount`
#   · 长数字串 / 链接 → 这条消息在**指认一个具体对象或联系方式**
#     （手机号、QQ 群号、卡号、订单号、报名表地址）
#
# **为什么可计算比枚举强**：`find_dates` 的边界（`9/3` 不许给 `9/30` 背书）
# 是可穷举测试的，而「动作词表里有没有这个词」只能测「我列过的还在不在」。
#
# **怎么验证**：对每条锚各造正例与**反例**（「人均25元」命中、「10月28日」
# 不命中金额锚），见测试。


@dataclass(frozen=True)
class FieldAnchor:
    """一个**可核验锚** —— 算出来的、且正好对应条目要填的一格。"""

    #: 对应条目里的哪一格：`time` / `amount` / `contact`。
    field: str
    #: 命中的**原文片段** —— 留证据，让「为什么留下它」回答得了。
    raw: str


#: 金额。`30元` / `50元以内` / `¥99` / `人均25元`。
#: ※ 要**单位**，不能只看数字：`20261205` 是日期不是金额，`530001887` 是
#: 群号不是金额。数字本身没有语义，**单位才有**。
_AMOUNT_RE = re.compile(r"[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:元|块钱|块|圆)")

#: 链接 —— 一条消息带链接，说明它指向一个要读者去的地方（报名表、公告页）。
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")

#: 长数字串（≥5 位）—— 手机号 / QQ 号 / 群号 / 卡号 / 学号 / 订单号。
#: ※ 门槛取 5 位：4 位以内的数字太常见（金额、序号、楼层），5 位以上才
#: 谈得上「一个具体的标识」。※ `\d` 在 Python 里匹配 Unicode 十进制数字，
#: 所以全角 `５３０００１８８８` 也算命中 —— 这正是要的（全角是绕过写法）。
_LONG_DIGITS_RE = re.compile(r"\d{5,}")


#: 紧凑日期 `20261205`（YYYYMMDD）。
#: ※ `times.py` 的日期形态都要求**分隔符或「月/日」字**（`10月28日` /
#: `10/28` / `2026-10-28`），紧凑写法它不认。可这一种在本项目的语料里确实
#: 出现，且 `synth.py` 明写了「`20261205` 是日期（8 位），不是号码」。
#: 不认它的话，它会被下面的长数字锚收走、报成 `contact` —— 决定没错
#: （该留），但**证据说错了**，而报告的全部价值就在证据上。
#: 所以这里补一条，并且**照 `times.py` 的取向做同样的合法性校验**
#: （月 1-12、日 1-31），免得把 `12345678` 这种序号也当成日期。
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")


def find_time_anchors(text: str) -> tuple[FieldAnchor, ...]:
    """日期与时刻锚。**复用 `times.py` 的解析器**，不另写一套正则。

    ※ 为什么必须复用：日期/时刻的边界判据（`9/3` 与 `9/30`、`9点5分` 与
    `9点50分`）在 `times.py` 里已经推导并测试过。这里再写一套，两套迟早
    会分叉，而分叉的症状是「预筛留下了一条，闸门却说它没有依据」——
    两边单看都正常。
    """
    out = [FieldAnchor("time", d.raw) for d in times.find_dates(text)]
    out += [FieldAnchor("time", t.raw) for t in times.find_times(text)]
    for m in _COMPACT_DATE_RE.finditer(text):
        month, day = int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.append(FieldAnchor("time", m.group(0)))
    return tuple(out)


def find_amount_anchors(text: str) -> tuple[FieldAnchor, ...]:
    return tuple(FieldAnchor("amount", m.group(0)) for m in _AMOUNT_RE.finditer(text))


def find_contact_anchors(text: str) -> tuple[FieldAnchor, ...]:
    out = [FieldAnchor("contact", m.group(0)) for m in _URL_RE.finditer(text)]
    out += [FieldAnchor("contact", m.group(0)) for m in _LONG_DIGITS_RE.finditer(text)]
    return tuple(out)


#: 锚的解析器，**按固定次序**排 —— 多个锚同时命中时按这个次序取第一个，
#: 只为了让报告稳定可复现（同一段文本每次跑出同一个判据名）。
_ANCHOR_FINDERS = (find_time_anchors, find_amount_anchors, find_contact_anchors)


def find_field_anchors(text: str) -> tuple[FieldAnchor, ...]:
    """全部命中的锚，按 `time → amount → contact` 的固定次序。"""
    out: list[FieldAnchor] = []
    for finder in _ANCHOR_FINDERS:
        out.extend(finder(text))
    return tuple(out)


def find_field_anchor(text: str) -> FieldAnchor | None:
    """第一个命中的锚（判据名由它决定）。没有则 None。"""
    for finder in _ANCHOR_FINDERS:
        hits = finder(text)
        if hits:
            return hits[0]
    return None


# ══════════════════════════════════════════════════════════════════
# 保留二：祈使结构（句子形状）
# ══════════════════════════════════════════════════════════════════
#
# **观察**：一条消息值得看，是因为**它在对读者说话**。而「对读者说话」有
# 可辨认的**句子形状**，与「出现了某个名词」是两件事 ——
# 「讲座」是名词（谁在讲、要不要去、什么时候，全没有），
# 「请各班于10月28日前提交」是形状（有祈使、有受众、有后果）。
#
# 下面四组各自独立：各有各的观察、各有各的判据名、各自可单独测试。
# 它们**不是**一张平铺词表 —— 四组的理由互不相同，合并成一张表就解释不了
# 任何一组了。
#
# ※ 一个反复出现的坑：**「出现在句中小句首」是这四组共同的约束**。
# 「请」在「请假」里不是祈使标记，「记得」在「我记得」里不是祈使副词。
# 判据是「祈使标记**出现在小句开头**」—— 小句首 = 消息开头，或紧跟在
# `。，、；：！？` 与空白之后。这一条把「我请假了」「我记得你」挡在外面，
# 而它们是包含匹配最容易误伤的两类。

#: 小句边界：消息开头，或一个停顿标点/空白之后。
_CLAUSE_START = r"(?:^|[。，、；：！？!?,;:\s])"

#: 组一：**祈使标记** —— 句子在对读者下指令。
#: 这组是**虚词与副词**（请/务必/尽快…），不是内容名词：它们本身没有
#: 语义，它们的语义是「这句话指向你」。
#:
#: ※ `请` 后面挂了一个**复合词例外**：`请假` / `请客` / `请柬` / `请帖`。
#: 这是**一条**观察（「请」被吸进了一个固定复合词，它在这个词里不是祈使
#: 标记），不是一张新词表 —— 例外只对 `请` 生效，且往里加词之前必须先问
#: 「它是不是一个固定复合词」。没有它，`请假`（消息开头）会被判成祈使句。
_COMPOUND_EXCEPTION = r"(?!假|客|柬|帖)"
_IMPERATIVE_RE = re.compile(
    _CLAUSE_START
    + r"(?:请"
    + _COMPOUND_EXCEPTION
    + r"|务必|尽快|抓紧|记得|别忘|切勿|欢迎|注意)"
)

#: 组二：**条件-后果** —— 句子说清了「不办会怎样」。
#: 观察：正式通知与闲聊的分水岭之一，就是通知会写后果（「逾期不予受理」），
#: 而闲聊不会。这条判据抓的是**后果句**这个结构，不是某个名词。
_CONSEQUENCE_RE = re.compile(r"逾期|否则|不予|一律|过期作废|后果自负|过期不候")

#: 组三：**明确受众** —— 句子把「谁该看」指出来了。
#: ※ 要求是「…的**同学** / …的**家长** / …的**同事**」这种**带定语的受众**，
#: 不收光秃秃的「大家」「各位」：光说「大家」可以是任何一句闲聊的开头，
#: 而「…的同学」把受众指出来了（谁是这条消息的对象是明确的）。
_ADDRESSEE_RE = re.compile(r"[一-龥]{1,8}的(?:同学|家长|同事)|有意者|有意的")

#: 组四：**公开求助** —— 疑问词指向**他人**，请求回应。
#: 观察：失物招领、寻人、问路这类消息的形状是「有谁…吗」「谁有…」。
#: ※ 单说「吗」不算 —— 「吃了吗」也是疑问句。判据是**疑问词 + 求助动词**
#: （谁+知道/见过/捡到…），疑问必须指向他人。
_HELP_RE = re.compile(
    r"有谁|谁有|哪位|有没有|"
    r"谁(?:知道|能|可以|帮|见过|捡到|看到)|"
    r"有人(?:知道|见过|捡到|看到)|"
    r"求助|请问"
)

#: 四组：`(判据名, 正则)`。**次序即优先级**，只影响报告里报哪一个名字。
_SHAPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("imperative", _IMPERATIVE_RE),
    ("consequence", _CONSEQUENCE_RE),
    ("addressee", _ADDRESSEE_RE),
    ("help_seeking", _HELP_RE),
)


def find_imperative(text: str) -> tuple[str, str] | None:
    """命中的祈使结构。返回 `(判据名, 命中片段)`，没有则 None。

    ※ 判据名**分组返回**而不是统一叫 `imperative`：报告要能说清
    「这条是因为『逾期』留下的」还是「因为『请』留下的」，那是两条不同的
    证据，混成一个名字就再也分不开了。
    """
    for name, rex in _SHAPE_RULES:
        m = rex.search(text)
        if m:
            return name, m.group(0)
    return None


# ══════════════════════════════════════════════════════════════════
# 硬丢弃三：过短（代理判据）
# ══════════════════════════════════════════════════════════════════
#
# **观察**：想拦住的到底是什么？我的判断：**想拦住的是「信息量少」，不是
# 「字数少」。** 但信息量不可计算，所以用一个**代理判据**，并且明确它
# **不是**同一个东西。
#
# **按字符还是按字节**：用**字符**数（`len()`），不用字节数。
# 中文一个字 3 字节而英文一个字母 1 字节 —— 按字节算，`abc` 是 3 而 `好`
# 也是 3，可 `好` 的信息量远高于 `abc`。按字节算等于**悄悄对中文更宽松**。
#
# **阈值取 4**：`好的` / `收到` / `顶` 都被挡住；`明早集合`（4 字）留下。
#
# ※ **这条判据排在保留之后**（见 `filter_one` 的次序），因为它是代理判据、
# 会误伤。锚与祈使结构都在它前面，所以「10月28日停课」有日期锚，不会被它
# 挡下。真正被它挡下的是「停课」「明早集合」这类**只有名词、或只有相对
# 时间、填不出任何一格**的短消息 —— 那正是本版判据刻意要挡的。
#
# ※ **代价要说清**：一条**孤立**的「停课」会被丢掉。若它旁边有一条真通知，
# `with_context` 会把邻居补进送模型的批次，所以模型看到的上下文不会断。

#: 长度阈值（**字符**数，不是字节 —— 见上面）。
MIN_CONTENT_CHARS = 4


def is_too_short(text: str) -> bool:
    return len(_strip_noise(text)) < MIN_CONTENT_CHARS


# ══════════════════════════════════════════════════════════════════
# 保留三：高优群 / 有角色的发送者
# ══════════════════════════════════════════════════════════════════

#: 群层级 → 是否无条件保留。
#: ※ 这三个键名与 `config/groups.toml` 的 `tier` **必须一致**（对外契约）。
TIER_KEEP: frozenset[str] = frozenset({"high"})

#: 发送者昵称里的**角色词**。有角色的人发的消息**默认可信**——
#: 判断「这条通知是不是官方发的」最可靠的依据。
#:
#: ※ 这是**保留**规则，不是丢弃规则：命中它只是「保住」，不影响别的判断。
#: ※ 它也是**昵称**判据（来源是发送者，不是正文），与正文那几组是两回事。
#: `application/extract.py` 的 `_actor_name` 复用这张表剥昵称前缀 ——
#: 所以它是一份**共享的名单**，改动要同时想清楚那一边。
ROLE_WORDS: tuple[str, ...] = (
    "班主任", "辅导员", "班长", "团支书", "学委", "学习委员", "生活委员",
    "体育委员", "辅导员助理", "助理", "教务", "教务员", "老师", "教授",
    "院长", "处长", "主任", "社长", "会长", "部长", "主席", "管理员",
    "官方", "官方号", "通知号", "管理员", "班助", "导员",
)

_ROLE_RE = re.compile("|".join(re.escape(w) for w in sorted(set(ROLE_WORDS), key=len, reverse=True)))


def has_role(sender: str) -> str | None:
    m = _ROLE_RE.search(sender)
    return m.group(0) if m else None


# ══════════════════════════════════════════════════════════════════
# 结果
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    #: 判据的名字。丢弃侧：`pure_ack` / `pure_symbol` / `too_short` /
    #: `spam` / `no_signal`。保留侧：`high_tier` / `role_sender` /
    #: `anchor_time` / `anchor_amount` / `anchor_contact` /
    #: `imperative` / `consequence` / `addressee` / `help_seeking`。
    #: ※ 这些是**稳定标识**，给测试与日志用；界面文案在
    #: `interfaces/cli.py::_rule_label` 里翻，不在这里改。
    rule: str
    #: 命中的那个词/片段 —— **留证据**，让「为什么丢掉它」回答得了。
    evidence: str = ""
    #: 丢弃 vs 保留的**方向**：`"drop"` / `"keep"` / `"default"`。
    #: ※ `default` 单列，因为「两者都不命中 ⇒ 丢弃」是一个**默认值**，
    #: 不是一条判据。混淆这两者会让覆盖率报告算错分母。
    by: str = "default"


def filter_one(msg: PendingMessage, *, tier: str = "normal") -> FilterDecision:
    """单条消息的判定。**顺序就是判据**（先丢后留）。

    ※ 次序：

      1. 硬丢弃（纯应答 / 纯符号）—— **整体匹配**，很窄，几乎不误伤
      2. 保留（高优群 / 有角色 / 字段锚 / 祈使结构）
      3. 长度判据 —— ※ **排在保留之后**！
         理由：长度是**代理判据**，它会误伤「停课」这类短而重要的消息。
         放在保留之后，凡是命中锚/祈使/角色/高优群的短消息都已经**被保住
         了**，长度判据只对「什么都没有」的消息生效。
      4. 都不命中 ⇒ 丢弃（默认）

    `tier` 只接受 `high` / `normal` / `low`。`low`（如二手群）**不额外丢**，
    只**不额外保留** —— 「二手群里也可能有人发重要通知」这个判断由锚与
    祈使结构兜住（「出二手自行车，200元」有金额锚，照样留）。
    """
    text = msg.content

    if is_pure_ack(text):
        return FilterDecision(False, "pure_ack", _strip_noise(text)[:12], by="drop")

    if is_pure_symbol(text):
        return FilterDecision(False, "pure_symbol", text.strip()[:12], by="drop")

    # ── 以下是保留规则 ──
    if tier in TIER_KEEP:
        return FilterDecision(True, "high_tier", tier, by="keep")

    role = has_role(msg.sender)
    if role:
        return FilterDecision(True, "role_sender", role, by="keep")

    anchor = find_field_anchor(text)
    if anchor:
        return FilterDecision(
            True, f"anchor_{anchor.field}", f"{anchor.field}:{anchor.raw}", by="keep"
        )

    shape = find_imperative(text)
    if shape:
        name, hit = shape
        return FilterDecision(True, name, hit, by="keep")

    # ── 长度判据（故意放在保留之后，见 docstring）──
    if is_too_short(text):
        return FilterDecision(False, "too_short", text.strip()[:12], by="drop")

    return FilterDecision(False, "no_signal", "", by="default")


# ══════════════════════════════════════════════════════════════════
# 刷屏
# ══════════════════════════════════════════════════════════════════
#
# **判据：桶要带哪些维度**
#
# 要害在于：三个不同群转发同一条通知**不是**刷屏 —— 那恰恰是最该保留的。
#
# ⇒ 判「重复」的桶必须含 **(发送者, 群, 内容)** 三个维度。**漏掉「群」这个
# 维度**，跨群转发会被误判成刷屏 —— 而那正是最该留下的东西。
# **漏掉「发送者」**，两个不同的人在同一个群发同一句「收到」会被合并 ——
# 不过那条本来就被纯应答规则挡掉了。
# **漏掉「内容」**，同一个人在同一群发两条不同消息会被当成重复。
#
# ※ 「连续」也有判据：本项目按**窗口内**判，不按「紧邻」。理由：语料里
# 「同一个人发三条一样的」中间常常夹着别人的插话，按紧邻判会漏。

#: 同一 (发送者, 群, 内容) 在窗口内出现这么多次 ⇒ 判刷屏。
#: 3 从「转发通常在 3 个群」推出来，取 ≥3 —— 跨群转发由「群」这一维分开，
#: 所以这个数只对**同一群内**生效。
SPAM_THRESHOLD = 3


def find_spam(
    messages: list[PendingMessage],
) -> dict[int, str]:
    """找出刷屏消息。

    返回 `{msg_id: 保留哪一条的 msg_id}` —— 被标记的那些会被丢弃，映射指向
    留下代表它的那一条。

    ※ **桶是 (发送者, 群, 内容) 三元组** —— 三个维度缺一不可，理由见上面。
    ※ 判据是「≥ 3 次」，**不是**「紧邻的相同」，理由同上。
    """
    buckets: dict[tuple[str, int, str], list[PendingMessage]] = {}
    for m in messages:
        key = (m.sender.strip(), m.group_id, m.content.strip())
        buckets.setdefault(key, []).append(m)

    dup: dict[int, str] = {}
    for (_sender, _group, _content), group in buckets.items():
        if len(group) < SPAM_THRESHOLD:
            continue
        ordered = sorted(group, key=lambda m: (m.ts, m.msg_id))
        keeper = ordered[0]
        for m in ordered[1:]:
            dup[m.msg_id] = keeper.msg_id
    return dup


def prescreen(
    messages: list[PendingMessage],
    *,
    tiers: dict[int, str] | None = None,
) -> tuple[list[PendingMessage], list[tuple[PendingMessage, FilterDecision]]]:
    """跑一遍预筛。

    返回 `(保留的, [(被丢掉的, 为什么)])`。

    ※ **被丢掉的那一份必须带原因返回**，不能只返回保留的：
    「静默跳过一条消息」= 用户以为那天没事。
    调用方拿它记进失败账本（`errors.FailureLedger`），覆盖率的分母就从那儿来。
    """
    tiers = tiers or {}
    spam = find_spam(messages)

    kept: list[PendingMessage] = []
    dropped: list[tuple[PendingMessage, FilterDecision]] = []

    for m in messages:
        if m.msg_id in spam:
            dropped.append(
                (m, FilterDecision(False, "spam", f"重复自 {spam[m.msg_id]}", by="drop"))
            )
            continue
        d = filter_one(m, tier=tiers.get(m.group_id, "normal"))
        if d.keep:
            kept.append(m)
        else:
            dropped.append((m, d))

    return kept, dropped


def with_context(
    kept: list[PendingMessage],
    all_messages: list[PendingMessage],
    *,
    before: int = 2,
    after: int = 1,
) -> list[PendingMessage]:
    """给保留的消息补上**上下文邻居**。

    ※ 为什么需要：一条「好的，那就明天下午三点」单独看没有信息，它的含义
    在**前一条**里。预筛会把它丢掉（没有锚、没有祈使结构），于是模型看到的
    上下文是断的。

    ※ 补进来的邻居**只作上下文**，不产出条目 —— 调用方要能把它们区分开。
    本函数返回的列表是「送进模型的那一批」，与「这一批里哪些该产出条目」
    是两件事，分开由调用方处理。
    """
    if before <= 0 and after <= 0:
        return list(kept)

    by_group: dict[int, list[PendingMessage]] = {}
    for m in sorted(all_messages, key=lambda m: (m.group_id, m.ts, m.msg_id)):
        by_group.setdefault(m.group_id, []).append(m)

    index: dict[int, int] = {}
    for gid, msgs in by_group.items():
        for i, m in enumerate(msgs):
            index[m.msg_id] = i

    chosen: dict[int, PendingMessage] = {m.msg_id: m for m in kept}
    for m in kept:
        gid = m.group_id
        msgs = by_group.get(gid, [])
        i = index.get(m.msg_id)
        if i is None:
            continue
        for j in range(max(0, i - before), min(len(msgs), i + after + 1)):
            nb = msgs[j]
            chosen.setdefault(nb.msg_id, nb)

    return sorted(chosen.values(), key=lambda m: (m.group_id, m.ts, m.msg_id))
