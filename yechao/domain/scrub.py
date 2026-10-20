"""出网脱敏 —— 抹掉能指到具体人的东西。

## 要防什么

把群聊原文发给模型之前，抹掉能指到具体人的东西：号码、账号、邮箱、姓名。

**为什么不能一刀切、在入口就抹掉姓名**：

姓名在**本机内部**是重要信号 —— 群昵称常带「22-1班班主任助理张三」这类角色与
班级，是判断「这条通知是不是官方发的」最可靠的依据。在入口就抹掉，等于把最有
用的信号先废掉。

所以边界画在**出网那一刻**，不是入口。

## 号码：判据不该只有长度，还要有两侧的上下文

观察到的事故形状：

| 输入 | 陷阱 |
|---|---|
| `20261008` | 是日期。但 `2026100812` 可能是一个 QQ 号 |
| `62170013800138000` | 是银行卡号 —— 它**包含着**一个 11 位手机号 |
| `9/3-9/5` | 日期区间，里面的数字都短于任何号码阈值 |

⇒ **一个数字串的左边和右边紧挨着什么，决定了它是什么。**
所以本模块的每条号码判据都带**边界断言**：一个号码必须**自成一段** ——
它的左邻与右邻都不是数字。没有这条，`62170013800138000` 里那个手机号会被
当成手机号抹掉，而真正该抹的整条卡号被留下来。

上表第三条由长度判据自然排除（单数字不可能过任何阈值），前两条由边界
判据排除（它们两侧都是数字）。

## 号码判据分两档：强判据与模糊判据

这个分档是本模块的核心判断，理由在「URL 豁免」那一节会用到。

**强判据** —— 数字串的形态本身就足以说明它是号码，不需要任何上下文：
  · 手机号：`1[3-9]` 开头 + 9 位，两侧非数字。11 位、1 开头，这个形状
    在校园语料里不会被别的东西撞上（日期是 8 位，金额一般 ≤ 4 位）。
  · 长数字串：≥ 12 位连续数字，两侧非数字。银行卡（16-19 位）、统一社会
    信用代码等。12 这个阈值从「不比它更长的东西是什么」推出来：8 位日期、
    10 位 Unix 时间戳、11 位手机号 —— 都短于 12。
  · 邮箱：`local@domain.tld`，形态自明。

**模糊判据** —— 数字串本身**不足以**说明它是号码，必须靠邻近的触发字：
  · QQ 号：5-11 位纯数字。⚠️ 这个区间与「金额」「房间号」「课程序号」
    「座位号」严重重叠 —— `4800` 是学费还是群号？纯看数字分不出来。
    所以它**必须**有触发字（`群` / `QQ` / `扣扣`）在场才抹。

这一档的取舍：
**误伤和漏抹，哪个代价大？**

我的判断：**分档处理，而不是统一取向**。
  · 强判据误伤极少（11 位 1 开头的数字串几乎只有手机号一种可能）⇒ 一律抹。
  · 模糊判据误伤代价高（抹掉「学费 4800 元」里的 4800，模型就读不到金额了，
    而金额是条目要填的字段）⇒ 只在有触发字时抹。
  · 漏抹的代价也不小（真号码出网）⇒ 所以触发字表要**尽量宽**，
    宁可多触发几次。触发字表的取向是**宽进**，因为它代价低（多抹一个
    看起来像号码的数字）。

## 「触发字」为什么按字而不是按词

真实语料里「群号」的写法：`群号90000`、`加群 90000`、`QQ群：90000`、
`交流群90000`、`校友群 90000`……

枚举写法枚举不完。取它们的**共同点**：都有
「群」这个**字**。按字处理，覆盖面大得多。

**误伤检查**：按字处理最危险的是「学费」「学院」里的「学」。但它们后面
紧跟的是另一个汉字而不是数字 ⇒ 不触发。判据是「触发字 + 可分隔符 + 数字」，
**中间不许夹别的汉字**。

## URL 豁免：豁免的是模糊规则，不是强规则

链接里的数字不该抹，否则短链就废了。但**豁免范围**很关键：
太宽（「从 http 一直到空白」）会一路吞掉后面的正文 —— 而手机号就藏在正文里。

**右边界从哪里来**：URL 里能出现什么字符？RFC 3986 的 unreserved + reserved
全是 **ASCII**。中文**不能**直接出现在 URL 里，必须被百分号编码
（`%E4%B8%AD`）。⇒ **URL 的右边界就是第一个非 URL 字符**，而在中文语料里
那通常是第一个汉字。这个答案直接给出了豁免的右边界。

**豁免到什么程度**：这是本模块的第二个关键判断 ——
**URL 里豁免「模糊判据」，但不豁免「强判据」。**

理由：`?phone=13800138000` 里的那个号码**仍然是手机号** —— 它指得到人，
与它出现在哪里无关。而 5-11 位的模糊数字在 URL 里没有中文触发字在场，
本来就不会触发。所以：
  · 手机号 / 长数字串 / 邮箱 → URL 里**照样抹**（形态自明，不依赖上下文）
  · 触发字规则 → URL 里自然不适用（没有汉字），无需特殊处理

这样既不会把短链打烂（短链的码是字母数字混合，不匹配任何号码形态），
也不会漏掉「夹在查询参数里的手机号」。

## 姓名：保留角色、抹掉人名本体

群昵称 `22-1班班主任助理张三` 里，`22-1班班主任助理` 是**要保留的信号**，
`张三` 才是要抹的**个人标识符**。

判据的来源是**本机已有的名单**（群成员昵称表）—— 从昵称里解析出人名集合，
然后对出网文本做**精确子串替换**。

⚠️ 这条边界**证明不了**「不含任何姓名」：模型自己补出一个不在名单上的名字，
精确子串替换拦不住。这是本架构**已知的**边界，不许在界面上简化成
「无隐私风险」。

## 为什么要返回 `ScrubReport` 而不是一个字符串

出网审计（主叙事「可证伪的隐私」）要能回答「那一刻送出去的到底是什么」。
只返回字符串的话，调用方没有任何东西可记。

⚠️ 而且报告里必须带 **`names_checked`（本轮实际比对了几个姓名）**：
「查过了、没命中」与「**根本没查**」在结果里长得一模一样
输入是空的名单时，「命中 0」的含义是
**「没查」**，不是「通过」。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from yechao.domain.textnorm import normalize

#: 号码被遮蔽成这个。前端 `num_placeholders` 数它的处数。
NUM_PLACEHOLDER = "<号码>"
#: 姓名被抹成这个。前端 `name_placeholders` 数它的处数。
NAME_PLACEHOLDER = "<姓名>"

# ── 边界 ────────────────────────────────────────────────────────────

#: 「不是数字」的判据。⚠️ 用 `\d` 会漏掉全角数字与其它 Unicode 数字 ——
#: 那些在 `normalize()` 之后不该出现，但这里再防一层：
#: 一个 Unicode 数字邻接的串，同样不是「自成一段」的号码。
_NOT_DIGIT = r"(?<!\d)"
_NOT_DIGIT_AFTER = r"(?!\d)"


def _is_number_char(ch: str) -> bool:
    """这个字符算不算「数字」—— 用于边界判断。"""
    return ch.isdigit() or unicodedata.category(ch) == "Nd"


# ── 强判据 ──────────────────────────────────────────────────────────

#: 手机号：`1[3-9]` + 9 位，两侧非数字。
#: ⚠️ 第二个数字 `.` 用 `[3-9]` 而不是 `\d`：`10`/`11`/`12` 开头的 11 位串
#: 不是手机号（那是座机区号 / 短号 / 其它编号）。收紧这里的收益是**降低误伤**
#: —— 而 11 位这个形状本身就很少被别的东西撞上。
_PHONE = re.compile(_NOT_DIGIT + r"(1[3-9]\d{9})" + _NOT_DIGIT_AFTER)

#: 长数字串：≥ 12 位，两侧非数字。
#: 12 从「不比它更长的东西是什么」推出来 —— 见模块 docstring。
#: ⚠️ **贪婪**匹配（`\d{12,}`）：`62170013800138000` 要整条被吃掉，
#: 而不是只吃掉尾巴上的 11 位（那正是「边界判据」要防的那个事故）。
_LONG_DIGITS = re.compile(_NOT_DIGIT + r"(\d{12,})" + _NOT_DIGIT_AFTER)

#: 邮箱。⚠️ 刻意**不用**过于宽松的 `\S+@\S+`：那会把 `见@张三` 这类误伤。
#: 要求 local 与 domain 都只含邮箱合法字符，且 domain 至少有一个点。
_EMAIL = re.compile(
    r"(?<![\w.+-])"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r"(?![\w-])"
)

# ── 模糊判据：触发字 + 数字 ──────────────────────────────────────────

#: QQ 号的触发字。按**字**取，不按词 —— 写法枚举不完，取共同点。
#: 取向是**宽进**：多触发几次的代价（多抹一个像号码的数字）低于漏抹。
_QQ_TRIGGER_CHARS = "群"

#: 显式的 QQ 写法（大小写不敏感，`normalize()` 已折叠全角）。
_QQ_TRIGGER_WORDS = ("qq", "扣扣", "q群", "🐧")

#: 触发字与数字之间允许的分隔符。⚠️ **不许夹别的汉字** ——
#: 这是「学费 4800」不触发的原因（`学` 后面是 `费`，不是分隔符也不是数字）。
#:
#: ⚠️ 但 `号` 必须放行：`群号900012345` 是最常见的写法之一，而
#: 「号」不是「别的汉字」，它是这个说法的一部分（`群号` / `QQ群号`）。
#: 第一次写这条正则时漏了它，测试当场红 —— 那正是这条用例存在的理由。
#: 允许：空白、冒号、`#`、`-`、**以及 `号`**。
_SEP = r"(?:[\s:：#\-#]|号)*"

#: 触发字在场时，抹 5-11 位数字。
#: 下界 5：QQ 号最短 5 位（4 位的是老号，早已回收）。
#: 上界 11：与手机号同宽 —— 再长就走长数字串那条判据了。
_QQ_AFTER_TRIGGER = re.compile(
    r"(?:"
    r"(?:[" + _QQ_TRIGGER_CHARS + r"]号?)"          # 群 / 群号
    r"|(?:"
    + "|".join(_QQ_TRIGGER_WORDS)
    + r")"                                          # qq / 扣扣 / q群
    r")"
    r"(" + _SEP + r")"
    r"(" + _NOT_DIGIT + r"(\d{5,11})" + _NOT_DIGIT_AFTER + r")",
    re.IGNORECASE,
)

# ── URL ─────────────────────────────────────────────────────────────

#: URL 的**字符集**。RFC 3986 的 unreserved + reserved + `%`。
#: ⚠️ 这里**不含**任何中文字符 —— 中文不能直接出现在 URL 里，必须被编码成
#: `%XX`。所以扫描到第一个非此集合的字符就是 URL 的右边界。
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"

#: 整条 URL。
_URL = re.compile(r"(?:https?://|www\.)" + _URL_CHARS + r"+", re.IGNORECASE)


@dataclass
class ScrubHit:
    """一处命中。

    ## ⚠️ `text` 字段是**内存里的临时值**，审计绝不存它

    它存在是因为**调用方需要知道抹掉的是什么**：指标脚本要拿它跟真值比，
    自检要拿它断言「该抹的抹了」。

    ⚠️ 而 `start` / `end` 是在**替换过程中**的坐标 —— 替换从右往左做，
    但多个模式串行执行，所以那两个值指向的是**当时那一段文本**的位置，
    而**不是原文**的坐标。第一版没有 `text` 字段，指标脚本拿 `h.start`
    去切原文，切出来的是 `<号码>认领。` 这种垃圾 ——
    于是它报了 5/7 的召回与 100% 的误伤，**两个错得看起来合理的数**。

    ⇒ 现在把**被抹掉的原文**直接带上（`text`），调用方不用猜坐标。

    ⚠️ **但审计归档只允许白名单里的键**（`audit_log.ALLOWED_KEYS`），
    里面没有 `text` —— 所以「记录里不存内容」这条仍然成立。
    这两件事不矛盾：**内存里需要它，磁盘上不需要**。
    """

    kind: str  # "phone" | "long_digits" | "email" | "qq" | "name"
    start: int
    end: int
    #: 被抹掉的那段原文。⚠️ 只活在内存里，不进审计归档。
    text: str = ""


@dataclass
class ScrubReport:
    """出网脱敏的**可证伪记录**。

    它是「那一刻送出去的到底是什么」这句承诺的**机械证据**

    ⚠️ 它**不存内容** —— 见 `ScrubHit`。
    """

    hits: list[ScrubHit] = field(default_factory=list)
    #: 本轮实际比对了几个姓名。
    #: ⚠️ **这个字段是「查了没命中」与「根本没查」的唯一区分点**
    #: 没有它，名单为空时的「命中 0」会被
    #: 读成「通过」，而真相是「没查」。
    names_checked: int = 0
    #: 被遮蔽的号码处数。
    num_placeholders: int = 0
    #: 被抹掉的姓名处数。
    name_placeholders: int = 0

    @property
    def ok(self) -> bool:
        return not self.hits

    def __bool__(self) -> bool:
        return bool(self.hits)


# ── 号码遮蔽 ────────────────────────────────────────────────────────


def _mask_numbers(text: str) -> tuple[str, list[ScrubHit]]:
    """抹号码。**强判据与模糊判据都在这条流水线上**，顺序有讲究：

    1. 长数字串**先**抹。`62170013800138000` 必须整条被吃掉 —— 如果先抹手机号，
       它会被从中间切开，剩下的半截（`6217001` / `38000`）再也匹配不上长数字串。
    2. 然后是手机号。
    3. 最后是触发字规则（它依赖上下文，被前面替换掉的文本会改变上下文，
       所以放最后 —— 前面抹出的 `<号码>` 里没有数字，不会误触发）。

    替换是**从右往左**做的：这样命中位置在替换过程中不会失效
    （从左往右替换会让后面的位置整体偏移）。
    """
    hits: list[ScrubHit] = []

    def _apply(pattern: re.Pattern[str], kind: str, group: int, cur: str) -> str:
        matches = list(pattern.finditer(cur))
        for m in reversed(matches):
            s, e = m.span(group)
            # ⚠️ 在**替换之前**取被抹掉的那段 —— 这是 `text` 的唯一来源。
            hits.append(ScrubHit(kind=kind, start=s, end=e, text=cur[s:e]))
            cur = cur[:s] + NUM_PLACEHOLDER + cur[e:]
        return cur

    out = _apply(_LONG_DIGITS, "long_digits", 1, text)
    out = _apply(_PHONE, "phone", 1, out)
    out = _apply(_EMAIL, "email", 1, out)
    out, n_qq = _mask_qq(out)
    hits.extend(n_qq)
    return out, hits


def _mask_qq(text: str) -> tuple[str, list[ScrubHit]]:
    """触发字 + 数字 → 抹。

    ⚠️ 这条与上面几条不同：它要**保留触发字本身**（「群」这个字是有效信息，
    抹了周围的词就断了），只抹数字。

    实现是逐字符扫而不是 `re.sub` —— 因为要保留捕获组 1（分隔符）与
    触发字，而 `re.sub` 的回调里拿不到「匹配起点之前的那个字」。
    """
    hits: list[ScrubHit] = []
    out: list[str] = []
    i = 0
    while i < len(text):
        m = _QQ_AFTER_TRIGGER.match(text, i)
        if m:
            # m.group(0) = 触发字 + 分隔符 + 数字
            digit_start = m.start(2)
            digit_end = m.end(2)
            out.append(text[i:digit_start])
            hits.append(ScrubHit(
                kind="qq", start=digit_start, end=digit_end,
                text=text[digit_start:digit_end],
            ))
            out.append(NUM_PLACEHOLDER)
            i = digit_end
        else:
            out.append(text[i])
            i += 1
    return "".join(out), hits


# ── 姓名遮蔽 ────────────────────────────────────────────────────────


def mask_names(text: str, names: list[str] | tuple[str, ...]) -> tuple[str, list[ScrubHit], int]:
    """把名单里的姓名精确子串替换成 `<姓名>`。

    返回 `(抹过的文本, 命中列表, 实际比对了几个姓名)`。

    ⚠️ **按长度从长到短替换**：名单里同时有「张三」与「张三丰」时，
    先替换短的会把「张三丰」切成「<姓名>丰」。从长到短保证最长的那个先被
    吃掉。

    ⚠️ **返回 `names_checked` 而不是「命中数」**：调用方要能把「查过了、
    没命中」与「根本没查」分开。传进来空名单时，本函数做的事是**没查**，
    而它返回的命中数同样是 0 —— 两个 0 长得一模一样，只有比对过的对象数
    能把它们分开。
    """
    # ⚠️ **先规范化再比**：`张<U+200B>三` 在模型眼里就是 `张三`，
    # 而调用方可能直接调本函数（不经 `scrub()`）。不在这里收敛的话，
    # 绕过手法只在「恰好走了 `scrub()` 那条路」时被挡住 ——
    # 那是一个**取决于调用点**的判据，而那种判据最容易在新增调用点时失效。
    text = normalize(text)
    names = [normalize(n) for n in names]

    # 去重 + 丢掉过短的（单字姓名误伤极大：「王」「李」在中文里满地都是）
    unique = {n for n in names if n and len(n) >= 2}
    ordered = sorted(unique, key=len, reverse=True)

    hits: list[ScrubHit] = []
    out = text
    for name in ordered:
        if name not in out:
            continue
        # 收集所有出现位置（在**已替换过的**串上找，所以位置是当前串的）
        idx = 0
        while True:
            found = out.find(name, idx)
            if found < 0:
                break
            start, end = found, found + len(name)
            hits.append(ScrubHit(kind="name", start=start, end=end, text=out[start:end]))
            out = out[:start] + NAME_PLACEHOLDER + out[end:]
            idx = start + len(NAME_PLACEHOLDER)

    return out, hits, len(unique)



#: 昵称里常见的角色/班级前缀词。⚠️ 拆的是**前缀**，不是所有位置 ——
#: 名字里也可能含这些字（「王老师」是名字的一部分吗？不一定）。
#: 取「从头剥」这个保守取向：剥不到就保留原名，不猜。
_NICK_PREFIX_WORDS = (
    "班主任助理", "班主任", "辅导员助理", "辅导员", "学习委员", "生活委员",
    "体育委员", "团支书", "班长", "学委", "社长", "会长", "部长", "主席",
    "教务员", "助理", "主任", "处长", "院长", "老师", "教授", "管理员",
    "班助", "导员", "官方号", "官方", "通知号",
)


def _strip_nick_prefix(nick: str) -> str | None:
    """从群昵称里剥出**人名本体**。剥不出来就返回 None（不猜）。

    ⚠️ 顺序：先剥**班级前缀**（`22-1班`、`2022级`），再从**头**剥角色词。
    这个顺序是问题域决定的：角色词总是紧贴人名的左边，而班级在最外面。
    """
    import re as _re

    s = nick.strip()
    if not s:
        return None
    # 班级前缀：`22-1班` / `2022级` / `高一(3)班`
    s = _re.sub(r"^[\d\s\-]*(?:级|班|届)[\s\-]*", "", s)
    s = _re.sub(r"^\d{2,4}[-级]?\d*\s*", "", s)
    # 角色词：从长到短试，剥掉一个就停（不连续剥，避免把名字本身剥掉）
    for w in sorted(_NICK_PREFIX_WORDS, key=len, reverse=True):
        if s.startswith(w):
            s = s[len(w):].strip()
            break
    # ⚠️ 剥完还太短 ⇒ **不猜**。一个字的「名字」在中文里满地都是。
    return s if len(s) >= 2 else None


def _expand_names(names: list[str]) -> list[str]:
    """名单 = 完整昵称 ∪ 从昵称里剥出的人名本体。

    ⚠️ **两样都留**：完整昵称也可能出现在正文里（比如引用了一条消息头），
    而人名本体是正文里的常态。只留一样都会漏。
    """
    seen: set[str] = set()
    for n in names:
        seen.add(n)
        body = _strip_nick_prefix(n)
        if body:
            seen.add(body)
    return sorted(seen)



# ── 对外入口 ────────────────────────────────────────────────────────


def scrub(
    text: str,
    names: list[str] | tuple[str, ...] = (),
    *,
    known_uins: list[int] | tuple[int, ...] = (),
) -> tuple[str, ScrubReport]:
    """**出网脱敏的唯一入口。**

    ⚠️ 全项目只有这一个函数被允许在「发出去之前」被调用。检查与脱敏必须
    复用**同一条判据** —— 另写一套的话两者迟早漂移，漂移后检查报「0 命中」
    而实际有东西出去了。
    所以审计读的是本函数返回的 `ScrubReport`，而不是自己再扫一遍。

    `known_uins`：本机已知的 QQ 号。它们按**强判据**处理 —— 无论出现在哪里
    都抹（包括 URL 里的）。
    """
    out = normalize(text)

    # 顺序：先抹号码，再抹姓名。
    # 为什么这个顺序：姓名可能**包含**数字（少见），而号码不会包含姓名。
    # 先抹号码，就不会出现「姓名被替换成 <姓名> 之后，里面的数字逃过检查」
    # 这种形状（虽然 `<姓名>` 里没有数字，但保持一个方向总是安全的）。
    out, num_hits = _mask_numbers(out)

    name_hits: list[ScrubHit] = []
    checked = 0
    all_names = list(names)

    # 已知 uin 进姓名名单 —— 它们同样是「指得到具体人」的东西。
    # ⚠️ 只管 ≥ 5 位的（与 QQ 号判据一致）：短数字（如 uin=7）满地都是，
    # 抹了会把正文咬得稀烂。
    for uin in known_uins:
        s = str(uin)
        if len(s) >= 5:
            all_names.append(s)

    if all_names:
        # ⚠️ **昵称是「全称」，正文是「裸名」** —— 两者必须能对应得上：
        # 拿全称去匹配正文，一个字都命中不了。
        #
        # 名单里给的是 `22-1班班主任助理林小满`，而正文写的是 `林小满`。
        # 只拿全称做精确子串匹配，`林小满` **永远匹配不上** ——
        # 于是姓名脱敏在真实语料上等于没开，而它看起来是「配置了名单」。
        #
        # ⇒ 从每个昵称里**再拆出人名本体**，两样都进名单。
        out, name_hits, checked = mask_names(out, _expand_names(all_names))
    else:
        # ⚠️ 名单为空时**也要有东西可查**：`checked = 0` 本身就是结果。
        # 别在这里短路返回一个「干净」的报告 —— 那正是「恒定报绿」。
        out, name_hits, checked = mask_names(out, [])

    report = ScrubReport(
        hits=num_hits + name_hits,
        names_checked=checked,
        num_placeholders=sum(
            1 for h in num_hits if h.kind in {"phone", "long_digits", "email", "qq"}
        ),
        name_placeholders=len(name_hits),
    )
    return out, report


def strip_urls(text: str) -> tuple[str, int]:
    """把 URL 换成 `<链接>`。**仅供某些调用点使用**（如提示词的引用区）。

    ⚠️ 这**不是**脱敏的一部分，所以它不参与审计。它存在是因为有些出网点
    只需要文本的语义，不需要链接（链接会白占上下文预算）。
    URL 内的号码已由 `scrub()` 按强判据抹掉了。
    """
    n = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return "<链接>"

    return _URL.sub(_sub, text), n
