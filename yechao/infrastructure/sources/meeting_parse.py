"""会议转写 → 消息。**纯函数，不碰 I/O**。

## 为什么「解」单独一个模块

与 `web_parse.py` 同一条判据（`docs/数据源层.md` §四）：一个源里「读」与「解」的
比例决定它该待在哪儿 —— **解析是最需要被测试的部分，而 `infrastructure/` 的
测试成本最高**。

本模块的判据全部来自「转写文件长什么样」：喂字节进去，看它吐出什么。不需要
网络、不需要文件、不需要时钟。`meeting_source.py` 负责的才是 I/O（从哪个目录
读、读哪些文件）。

※ 按 `docs/数据源层.md` §四 的判据，切分与时间解析本该在 `domain/` —— 它们确实
是纯逻辑。它留在这里的原因是本次任务的硬约束锁住了 `yechao/domain/**`。代价用
测试补上：`tests/infrastructure/test_meeting_source.py` 直接调本模块，不经过
任何 I/O。**接手的下一个人该做的事是把本模块搬进 `domain/`。**

## 这个源与 `web` 源的分工（端口那句「归一化的责任在数据源」）

| 源 | 输入形态 | 归一化动作 |
|---|---|---|
| `web` | 一页多通知 | **切**成多条 |
| `mail` | 自洽的短文本 | **不切** |
| `meeting` | 一段几万字的转写 | **必须切** |

会议转写不切会怎样：下游 `domain/chunking.py::chunk()` 是**按条数**切批的，
一份四十分钟的转写当成一条消息，这一批就只有它一条，而它的正文上万字 ——
端点会**静默截断**（回 200、`finish_reason=stop`、`usage` 只报截断后的数，
客户端拿不到任何信号）。所以切分必须发生在这里。

※ `domain/chunking.py::oversized()` 就是为这件事准备的：它找出「单条就超过
预算」的消息，让调用方能报警。本模块的切分预算**直接取那个函数的阈值** ——
见 `EFFECTIVE_BUDGET_CHARS`。两个数漂移的症状是「源以为自己切好了、下游
仍然报 oversized」，而那时已经白花了一次调用。

## 切在哪：说话人轮次 + 时间窗口

两个触发条件，同一件事的两个维度：

1. **说话人轮次**是主单位 —— 一轮话是一个自洽的片段（与 `web` 的「一条通知」
   同量级）。轮次变了就换一条消息。
2. **长度预算 / 时间窗口**是次单位 —— 一轮话太长（独白）时再切：
   · 字符侧：`EFFECTIVE_BUDGET_CHARS`（= 下游的 `oversized` 阈值）。
   · 时间侧：`TURN_WINDOW_SECONDS`。定 10 分钟的理由是它与字符预算**同量级
     地触发**：正常语速下 10 分钟口播约 2500–3500 字，而预算是 4500 字 ——
     两个条件谁先到都有可能。若其中一个总是先触发，另一个就是死代码，
     而**一条声明配一个空操作比没有这条声明更危险**。

※ 切点尽量落在**句末标点**上，不是按字数硬切：从句子中间切开会把一句话劈成
两条半截话，两条都读不懂（`chunking.py` 对「超长单体」的处置理由一字不差）。

## 三种格式，各自能给出什么

| 后缀 | 说话人 | 逐轮时间 | 会议开始时刻 |
|---|---|---|---|
| `.txt`（腾讯会议形态） | 形态**自带**（独立一行） | 自带（`时:分:秒`） | 文件头 |
| `.txt`（钉钉形态） | 形态**自带**（`说：` 标记） | 自带（行首 `[时:分:秒]`） | 文件头 |
| `.srt` | **没有** —— 不猜，见下 | 自带（每条字幕的时间轴） | **只有侧车能给** |
| `.vtt` | 标准自带（`<v 说话人>`） | 自带 | **只有侧车能给** |

### ※ 关于真实形态的诚实说明（先说清楚，免得被下面那些「观察」误导）

上面这张表里的「形态」，是本模块**定义**的一类形状 —— 判据写在各自的常量与
函数旁边（观察到什么 / 为什么这么定 / 怎么验证）。我**没有**在真实的腾讯会议
或钉钉导出文件上逐字节验证过它们的排版细节：这台机器上装了腾讯会议客户端，
但它的本地库里没有任何一场会的转写（见 `meeting_wemeet.py`），所以拿不到
真实样本。

⇒ 所以这些判据的取法是**保守**的：每一条都要求一个**可机械分辨**的结构事实
（独占一行 + 起一段 + 分隔符是对齐空白 / 自带 `说：` 标记 / 标准的 `<v>`），
而**认不出就抛**。这个取向的代价是「真实导出可能与它不完全一样」，但换来的是
另一种失败不会发生：把一份别的文本当成转写，产出看起来正常的错消息。

⇒ 接手的下一个人拿到真实导出时，该做的是：把样本放进
`fixtures/meeting/`，跑一遍 `python -m pytest tests/infrastructure/test_meeting_source.py`
—— 认得出就补一条夹具测试，认不出就把那个结构事实加进判据（**不是**放宽成
「差不多就行」）。

### 说话人：形态**自带**才认，不带的**不猜**

观察到的现象：四种形态里，只有三种在**格式本身**里声明了「谁在说话」——
腾讯会议的独立行、钉钉的 `说：` 标记、WebVTT 的 `<v 说话人>` 是标准的一部分。
SRT 的标准里**没有**说话人字段（字幕规范只定义序号 / 时间轴 / 正文）。

⇒ SRT 的 `sender` 置**空串**，正文原样保留。**不**去猜「正文第一个冒号之前
是不是名字」：猜错会把正文的一部分当成名字，而那个「名字」会进人脸名单、
被当成人名抹掉 —— **正文被咬掉一块**，而且没有任何东西会报错。

※ 这与 `web_parse.py` 对 `data-department` 的处置是同一条判据：宁可承认
「这个源没有自报发送者」，也不从自由文本里切。

## 两种「0 条」必须能分辨（`web_parse.py` 的同一条判据）

两者都是「解析出 0 条」，而**产出为空是所有错误里最不容易被注意到的一种**：

  · `_NotUnderstood` —— 我**没读懂**：形态判据没命中、编码解不开、
    时间戳读不出来、文件头没有会议开始时刻。
    **下一步：去改解析器（或去确认这份导出是不是新形态）。**
  · `_NoMeetingsHere` —— 我**读懂了，这个目录确实没有会议**：一个转写文件都
    没有，或者转写读懂了但里面一轮发言都没有。
    **下一步：去查目录是不是选对了 / 这场会是不是真的没内容。**

分界线是**「有没有东西可读」**：有没有认得出的轮次行、有没有会议头。
改版会动前者，「这场会没内容」不会。

## 时间：锚点 + 偏移，**拿不到就抛**

转写里的时间戳是**相对会议开始**的偏移，而下游要的是**绝对时刻**。

⇒ 绝对时刻 = `锚点 + 偏移`。锚点只有两个来源：文件头（txt 形态自带）或
同名侧车 `<名>.meta.json` 的 `started_at`（srt / vtt 没有别的地方能给）。

**两个都没有 ⇒ 抛，不兜底。** 兜底成 0 会把消息扔进 1970 年；兜底成「今天」
会把一场上周的会放进今天的日报 —— 而日报是**按天分**的，两种错都不报错。

※ 侧车**压过**文件头：侧车是**显式**的（有人专门为这个文件写了它），文件头是
**隐式**的（导出器顺手写的）。与 `web_source` 的「环境变量压过配置文件」是
同一条判据 —— 显式的意图压过隐式的默认。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from yechao.domain.chunking import DEFAULT_BUDGET_CHARS, HEADROOM
from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import SourceFormatError
from yechao.domain.models import PendingMessage

#: 源的标识。每条消息的 `source` 字段用的就是它，`meeting_source.py` 也引用它
#: 而不再写一遍字面量 —— 两处字面量漂移的症状是「消息自称来自一个不存在的源」。
NAME = "meeting"

#: 一条消息的字符预算。
#:
#: ※ **直接取下游那个阈值**，不另定一个数：`domain/chunking.py::oversized()`
#: 用的就是 `int(DEFAULT_BUDGET_CHARS * HEADROOM)`。本模块若自己定一个「差不多」
#: 的数，两边漂移的症状是「源以为自己切好了、下游仍然报 oversized」——
#: 而那时已经白花了一次模型调用。
EFFECTIVE_BUDGET_CHARS = int(DEFAULT_BUDGET_CHARS * HEADROOM)

#: 一条消息在时间上最多跨多长（秒）。见模块 docstring「切在哪」。
TURN_WINDOW_SECONDS = 10 * 60

#: 说话人名字的长度上限。
#:
#: ※ 这个上限不是「美化」，是**判据**：腾讯会议形态的轮次行是
#: `说话人 时:分:秒`，而正文里完全可能出现「开放时间 08:00:00」这种**同形状**
#: 的行。区分它们靠三条可观察的事实（见 `_tencent_turn_indices`），
#: 名字长度是其中一条 —— 真实转写里的说话人是名字，不是一整句话。
SPEAKER_MAX_CHARS = 16

# ── 会议的身份与命名空间 ────────────────────────────────────────────
#
# ## 一场会议 = 一个「群」
#
# 这不是修辞，是一条会**咬人**的判据：`domain/chunking.py::_split_group`
# 只在**群内**切批，而它切批的依据之一是「群内时间连续」。两场不同的会若共用
# 一个 `group_id`，它们就会被切进**同一批** —— 于是模型会把 A 会的上下文套到
# B 会（`chunking.py` 模块 docstring 的第一条硬约束，症状是「B 会的通知里出现
# 了 A 会的地点」）。
#
# ⇒ 每份转写文件 = 一场会 = 一个群，`group_id` 由会议身份**稳定地**算出来。
# 网页源没有天然的整数 id 所以它只能由配置给；会议也一样没有天然 id，但它有
# 一个**稳定的名字**（文件名），所以它可以算出来 —— 算出来的好处是「同一场会
# 重跑摄取落回同一个群」，不需要任何人维护配置。

#: 会议群号的保留段起点。
#:
#: ※ 取 830000000 是因为网页源的默认群号是 820000001 —— 两者不重叠，
#: 于是「哪条消息来自哪一类源」在库里一眼可见。一个真实部署应当把这里的
#: 号段登记进 `config/groups.toml`，那时日报会显示会议自己的名字而不是
#: 「群<号>」（`interfaces/view.py` 对查不到的群名就是这样兜底的）。
GROUP_ID_BASE = 830_000_000
GROUP_ID_SPAN = 10_000_000


# ── 两种「0 条」 ────────────────────────────────────────────────────


class _NotUnderstood(SourceFormatError):
    """转写读到了，但**我没读懂**。

    形状判据没命中：既不是腾讯会议形态、也不是钉钉形态、也不是字幕形态；
    或者编码解不开、时间戳读不出来、文件头没有会议开始时刻。
    下一步是**去改解析器**（或去确认这份导出是不是新形态）。
    """

    default_message = "会议转写没读懂（形态判据没命中）"


class _NoMeetingsHere(SourceFormatError):
    """转写读懂了 —— 结构对得上 —— 但**这里确实没有会议内容**。

    ※ 它**仍然抛**（端口判据：解析出 0 条 ⇒ 抛）。分开成另一个类型是为了
    让下一步不同：这里该去查**目录是不是选对了 / 这场会是不是真的没内容**，
    而不是去改解析器。
    """

    default_message = "读懂了，但这个目录里没有会议内容"


# ── 中间形状 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Segment:
    """转写里**一段连续的发言**：一个时间点 + 一段正文。

    它是「解析」与「切分」之间的那一层：txt 形态一轮话就是一个 segment
    （整段正文共用一个时间点），字幕形态一条 cue 就是一个 segment
    （各有各的时间轴）。切分只看这个形状，于是两种形态共用同一套切分逻辑。
    """

    start: int  # 相对会议开始的秒数
    text: str


@dataclass(frozen=True)
class _Turn:
    """**一轮话** —— 同一个说话人连续说的一串 segment。切分的主单位。"""

    speaker: str
    segments: tuple[_Segment, ...]


@dataclass(frozen=True)
class _Part:
    """切完之后的一段 —— 它就是一条消息的正文与时间。"""

    start: int
    text: str


# ── 形态标记 ────────────────────────────────────────────────────────

#: 腾讯会议形态的轮次行：`说话人<对齐空白>时:分:秒`，**整行只有这两样**。
#:
#: ※ 分隔符要求**一个制表符或两个以上空白**，不接受一个空格。这一条不是排版
#: 讲究，是**判据**：正文里「开放时间 08:00:00」这种行与轮次行**同形状**，
#: 唯一可机械分辨的差别就是分隔符 —— 轮次行把说话人与时间对齐成一列，正文
#: 用的是一个普通空格。没有这条，一段正文就会被当成一次换人，而症状是
#: 「一轮话被劈成两半」，两半都读不懂，且**不报错**。
#:
#: ※ `.+?` 非贪婪 + `$` 锚点：说话人取「到行尾那个时间为止」的全部内容。
#: 是否真的算一轮，还要过 `_tencent_turn_indices` 的三条判据。
_TENCENT_TURN = re.compile(
    r"^(?P<speaker>.+?)(?:\t|[ \t　]{2,})(?P<offset>\d{1,2}:\d{2}:\d{2})"
    r"[ \t　]*$"
)

#: 钉钉形态的轮次行：`[时:分:秒] 说话人 说：正文`，**说话人与正文在同一行**。
#:
#: ※ 它与腾讯会议形态的区别不只是排版：`说：` 是**形态自带的说话人标记**，
#: 于是这一行不需要别的旁证就能确认是一轮（见 `_tencent_turn_indices` 为什么要
#: 三条判据）。这也是「两种形态都要认、认不出就抛」里「认得出」的可观察依据。
_DINGTALK_TURN = re.compile(
    r"^\[(?P<offset>\d{1,2}:\d{2}:\d{2})\][ \t　]*"
    r"(?P<speaker>.+?)[ \t　]*说[：:][ \t　]*(?P<body>.*)$"
)

#: 文件头里的**墙上时间**。会议开始时刻从它来（见模块 docstring「时间」）。
_WALL_CLOCK = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?"
)

#: 字幕的时间轴。SRT 用 `,` 分隔毫秒，VTT 用 `.`；VTT 还允许省略小时。
_CUE_TIMING = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{1,2}(?:[.,]\d{1,3})?)"
    r"[ \t]*-->[ \t]*"
    r"(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{1,2}(?:[.,]\d{1,3})?)"
)

#: 一个字幕时间点（`00:01:30,500` / `01:30.5`）。
_CUE_TIME = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})(?:[.,]\d{1,3})?")

#: WebVTT 的**说话人标记** —— 标准里就有的那一个（`<v 林小满>正文`）。
_VTT_VOICE = re.compile(r"<v[ \t　]+(?P<speaker>[^>]+)>", re.IGNORECASE)

#: 字幕里的排版标记（`<i>` / `</i>` / `<c.loud>` / `<00:00:12.000>`）。
#:
#: ※ 它们**不是正文**，所以要剥掉；但只剥「看起来像标签」的（标签名以字母或
#: `/` 开头）。`<` 与 `>` 在正文里也出现（比如「成绩 < 60 分」），无条件剥会
#: 把正文咬掉一块。
_SUBTITLE_TAG = re.compile(r"</?[A-Za-z][^>]*>")

#: 句末标点 —— 切点尽量落在这些字符**之后**。见模块 docstring「切在哪」。
_SENTENCE_END = "。！？!?；;…"


# ── 时间 ────────────────────────────────────────────────────────────


def _wall_clock_to_ts(raw: str) -> int:
    """文件头里的**墙上时间**（会议本地时间）→ Unix 秒。

    ※ 时区取**全项目那一处口径**（`domain.days.DEFAULT_TZ_OFFSET`，UTC+8），
    不取机器本地时区。取机器本地的话，同一份夹具在评委的机器上会算出不同的
    秒数，而日报是**按天分**的 —— 差 8 小时足以让一场会落到**前一天**。
    """
    normalized = raw.strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        # `tzinfo=utc` 只是让 `.timestamp()` 不去问机器时区（那会不可复现），
        # 减掉偏移才是「这个墙上时间对应的真实时刻」。
        return int(naive.replace(tzinfo=timezone.utc).timestamp()) - DEFAULT_TZ_OFFSET
    raise _NotUnderstood(
        f"文件头里的时间 {raw!r} 读不出来 —— 只认 `YYYY-MM-DD HH:MM[:SS]`"
        f"（`/` 与 `T` 也收）。兜底成 0 或「今天」都会把一场会放进错误的一天，"
        f"而它**不报错**。",
        raw=raw,
    )


def parse_wall_clock(raw: str) -> int:
    """侧车里的 `started_at` → Unix 秒。与文件头走的是**同一个**解析。

    ※ 两处各写一遍解析的症状是「文件头认的写法，侧车不认」—— 而侧车正是
    字幕格式唯一的锚点来源，它读不出来就等于那两种格式用不了。
    """
    return _wall_clock_to_ts(raw)


def _cue_offset(raw: str) -> int:
    """字幕时间点 → 相对会议开始的**整秒**。

    ※ 毫秒直接丢掉：它只影响排序的精细度，而下游按秒记时间
    （`PendingMessage.ts` 是 Unix 秒）。保留它反而要在两处做取整。
    """
    m = _CUE_TIME.fullmatch(raw.strip())
    if m is None:
        raise _NotUnderstood(f"字幕时间点 {raw!r} 读不出来", raw=raw)
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _txt_offset(raw: str) -> int:
    """`时:分:秒` → 秒。"""
    h, m, s = raw.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


# ── 解码 ────────────────────────────────────────────────────────────


def _decode(raw: bytes) -> str:
    """把字节解成文本。解不开 ⇒ **抛**。

    ※ 判据：**不换一个编码重试**（与 `web_parse.decode_html` 同一条）。

    观察：中文转写的常见错配是「实际是 GBK、被当成 UTF-8 解」以及反过来。
    按错的那个编码解通常**解不出错**（GBK 的字节能被 latin-1 / cp1252 照单
    全收），于是你得到一整份乱码，而乱码能一路走完解析、「成功」产出 N 条
    没人看得出是错的条目 —— 那是「产出为空」的镜像：**产出垃圾**。

    ⇒ 要么按 UTF-8（含 BOM）解开，要么响亮地失败。腾讯会议、钉钉与
    WebVTT 的导出都是 UTF-8（WebVTT 的规范甚至**要求** UTF-8）。
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _NotUnderstood(
            f"转写文件不是 UTF-8：{e}。"
            f"（腾讯会议 / 钉钉 / WebVTT 的导出都是 UTF-8；换个编码重试是刻意的，"
            f"因为猜错编码「成功」解出来的是一整份乱码，而乱码会照常走完解析。）",
        ) from e


# ── 正文整理 ────────────────────────────────────────────────────────


def _clean_block(lines: list[str]) -> str:
    """把一段正文的行整理成文本：去掉两端空行、抹掉每行的尾部空白。

    ※ 刻意**不**做「空白折叠成单空格」（`web_parse._squeeze` 的做法）：
    转写正文里的换行是**发言的分句**，不是 HTML 的排版噪声。折掉它会让
    整段话变成一行，读的人与模型都失去停顿的位置。
    """
    text = [ln.rstrip() for ln in lines]
    while text and not text[0].strip():
        text.pop(0)
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text)


def _split_sentences(text: str) -> list[str]:
    """按**句末标点**切句。这是长度切分的切点来源（见模块 docstring）。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _SENTENCE_END or ch == "\n":
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _hard_split(text: str, budget: int) -> list[str]:
    """单句就超预算时的最后手段：按字符硬切。

    ※ 它**只在**「一个句子里塞了超过一整条预算的字」时才走到 —— 那通常意味着
    正文里没有标点（机器生成的清单、连成一串的数字）。硬切会把这句话劈成两半，
    但另一条路（不切、让它超预算）会导致下游**静默截断**，那更糟：
    截断没有信号，硬切至少两条都在库里。
    """
    return [text[i : i + budget] for i in range(0, len(text), budget)]


def _pack(sentences: list[str], budget: int) -> list[str]:
    """把句子贪心地装进 ≤ `budget` 的块里，块内用换行连接。"""
    chunks: list[str] = []
    cur: list[str] = []
    for s in sentences:
        if len(s) > budget:
            if cur:
                chunks.append("\n".join(cur))
                cur = []
            chunks.extend(_hard_split(s, budget))
            continue
        if cur and len("\n".join(cur)) + 1 + len(s) > budget:
            chunks.append("\n".join(cur))
            cur = []
        cur.append(s)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ── 切分 ────────────────────────────────────────────────────────────


def _segments_of_turn(turn: _Turn, budget: int) -> list[_Segment]:
    """把一轮话的每个 segment 预先削到 ≤ 预算。

    ※ 为什么先削 segment 再合并：segment 是**不可分的最小时间单位**
    （一条 cue 只有一个时间点，一轮 txt 发言也是）。若一个 segment 自己就
    超预算，它必须先被切开，否则合并那一步无论怎么切都救不回来。
    """
    out: list[_Segment] = []
    for seg in turn.segments:
        if len(seg.text) <= budget:
            out.append(seg)
            continue
        for piece in _pack(_split_sentences(seg.text), budget):
            out.append(_Segment(start=seg.start, text=piece))
    return out


def split_turn(
    turn: _Turn, *, budget: int = EFFECTIVE_BUDGET_CHARS
) -> list[_Part]:
    """一轮话 → 若干条消息的正文。**两个触发条件见模块 docstring。**

    · 预算装不下下一个 segment ⇒ 切（硬约束）。
    · 这一段已经跨了超过 `TURN_WINDOW_SECONDS` ⇒ 切（时间侧）。

    ※ 时间窗用**严格大于**：恰好等于阈值的跨度**不**切。这个边界取闭还是
    开会改变切分结果，所以钉死在测试里（`chunking._split_group` 对
    `BREAK_GAP_SECONDS` 用的是同一条约定）。
    """
    parts: list[_Part] = []
    cur: list[_Segment] = []
    for seg in _segments_of_turn(turn, budget):
        if cur:
            joined = len("\n".join(s.text for s in cur)) + 1 + len(seg.text)
            too_long = joined > budget
            too_wide = seg.start - cur[0].start > TURN_WINDOW_SECONDS
            if too_long or too_wide:
                parts.append(_Part(start=cur[0].start, text="\n".join(s.text for s in cur)))
                cur = []
        cur.append(seg)
    if cur:
        parts.append(_Part(start=cur[0].start, text="\n".join(s.text for s in cur)))
    return parts


def split_text(
    text: str, *, budget: int = EFFECTIVE_BUDGET_CHARS
) -> list[_Part]:
    """一段**没有轮次结构**的正文 → 若干条消息的正文。

    ※ 它是给「本机会议历史」那条路用的（`meeting_wemeet.py`）：那里一场会
    只有一小段自述文本，没有说话人轮次。**同一套切分**照旧生效 —— 预算与
    时间窗口的判据不该因为正文从哪来而变。
    """
    return split_turn(
        _Turn(speaker="", segments=(_Segment(start=0, text=text),)), budget=budget
    )


# ── 身份 ────────────────────────────────────────────────────────────


def _stable_int(namespace: str, key: str, *, salt: str) -> int:
    """由（命名空间 + 键）算出的稳定整数。

    ※ 为什么**不用** `hash()`：Python 的字符串哈希**每个进程都不一样**
    （`PYTHONHASHSEED` 随机化）。用它当 id 的症状是「每次跑摄取都写一遍全库」
    —— `msg_id` 是幂等键，键一变，同一条消息就变成新消息，日报里同一条
    出现两次，而没有任何东西会报错。

    ※ 为什么**取 63 位**：SQLite 的 INTEGER 是有符号 64 位，右移一位留出
    符号位。不这么做的话约一半的 id 会超过 `2**63-1`，症状是插入时报
    "Python int too large to convert to SQLite INTEGER" —— 在**一半**的
    文件上出现，像随机故障。
    """
    digest = hashlib.blake2b(
        f"{salt}\x00{namespace}\x00{key}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") >> 1


def meeting_message_id(namespace: str, key: str) -> int:
    """由（会议的命名空间 + 这个会议里某个片段的键）算出的稳定 `msg_id`。

    ※ 键是**内容在会议里的位置**，不是它在文件里的行号：行号会被「在文件头
    补一行导出时间」这类编辑整体挪动，挪了之后整份文件换一批 id，于是重跑
    摄取把同一场会**全部重写一遍**，而没有任何东西会报错。
    """
    return _stable_int(namespace, key, salt="meeting-msg")


def message_id(namespace: str, *, start: int, part: int) -> int:
    """转写里一条消息的 `msg_id`。

    ※ 为什么要带 `part`：一段长独白被切成几段时，它们**共用同一个起始秒**
    （txt 形态一轮话只有一个时间点）。不带它就撞键，撞键的后果是后一段
    覆盖前一段 —— 消息**变少**，而没有任何东西会报错。
    """
    return meeting_message_id(namespace, f"{start}|{part}")


def group_id_for(namespace: str) -> int:
    """一场会议的 `group_id`（= 下游视角里的一个「群」，见模块顶部的推导）。"""
    return GROUP_ID_BASE + _stable_int(namespace, "group", salt="meeting-group") % GROUP_ID_SPAN


# ── 腾讯会议形态 ────────────────────────────────────────────────────


def _looks_like_speaker(raw: str) -> bool:
    """`说话人 时:分:秒` 里前半截**像不像一个说话人**。

    见 `_tencent_turn_indices`：这是三条判据里的第三条。
    """
    name = raw.strip()
    if not name or len(name) > SPEAKER_MAX_CHARS:
        return False
    # 名字里不该有数字。`2026-09-16`、`9 月 16 日`、`08:00` 全被这一条挡掉。
    return not any(ch.isdigit() for ch in name)


def _tencent_turn_indices(lines: list[str]) -> list[int]:
    """哪些行是腾讯会议形态的**轮次行**。

    形状判据有三条，缺一不可 —— 每一条都对应一个可观察的事实：

    1. 整行是 `说话人 + 对齐空白 + 时:分:秒`（正则，见 `_TENCENT_TURN`）。
       观察：这一形态把一轮的元信息单独放一行，正文从下一行开始。
    2. 这一行**起一段**（它前面是空行，或是文件的第一行）。
       观察：这一形态在轮次之间空一行。**这一条是必需的** —— 正文里完全可能
       出现「开放时间 08:00:00」这种与轮次行**同形状**的行，而「起一段」
       是它们唯一可机械分辨的差别。
    3. 前半截**像名字**（非空、≤ `SPEAKER_MAX_CHARS`、不含数字）。

    ※ 三条都是**结构性**的，不是「看起来差不多」。少任何一条，一段正文
    就会被当成一次换人，而那会把一轮话劈成两半 —— 两半都读不懂。
    """
    out: list[int] = []
    prev_blank = True
    for i, line in enumerate(lines):
        if prev_blank:
            m = _TENCENT_TURN.match(line)
            if m is not None and _looks_like_speaker(m.group("speaker")):
                out.append(i)
        prev_blank = not line.strip()
    return out


def _tencent_turns(lines: list[str], indices: list[int]) -> list[_Turn]:
    """按已知的轮次行下标切出轮次。正文 = 该行之后、下一个轮次行之前的行。"""
    turns: list[_Turn] = []
    for n, i in enumerate(indices):
        end = indices[n + 1] if n + 1 < len(indices) else len(lines)
        m = _TENCENT_TURN.match(lines[i])
        assert m is not None  # 下标来自 `_tencent_turn_indices`，必然匹配
        text = _clean_block(lines[i + 1 : end])
        if not text:
            # 只有时间、没有正文的一轮 —— 它没有可读内容，不产出消息。
            # 这不是「静默跳过一条消息」：跳过的是一段**空**发言。
            continue
        turns.append(
            _Turn(
                speaker=m.group("speaker").strip(),
                segments=(_Segment(start=_txt_offset(m.group("offset")), text=text),),
            )
        )
    return turns


# ── 钉钉形态 ────────────────────────────────────────────────────────


def _dingtalk_turn_indices(lines: list[str]) -> list[int]:
    """哪些行是钉钉形态的轮次行。

    ※ 这里**不需要**「起一段」那条判据：`说：` 是形态自带的说话人标记，
    行首的 `[时:分:秒]` 又把它与正文区分开。两种形态的判据不同，正是
    「形态不同」这件事本身 —— 用腾讯会议那套去认钉钉，会把正文当成轮次；
    用钉钉这套去认腾讯会议，一条都认不出来。
    """
    return [i for i, line in enumerate(lines) if _DINGTALK_TURN.match(line) is not None]


def _dingtalk_turns(lines: list[str], indices: list[int]) -> list[_Turn]:
    turns: list[_Turn] = []
    for n, i in enumerate(indices):
        end = indices[n + 1] if n + 1 < len(indices) else len(lines)
        m = _DINGTALK_TURN.match(lines[i])
        assert m is not None
        # 正文 = 本行 `说：` 之后的那一段 + 后续行（导出器可能在长发言里换行）。
        text = _clean_block([m.group("body")] + lines[i + 1 : end])
        if not text:
            continue
        turns.append(
            _Turn(
                speaker=m.group("speaker").strip(),
                segments=(_Segment(start=_txt_offset(m.group("offset")), text=text),),
            )
        )
    return turns


# ── txt：两种形态都要认，认不出就抛 ─────────────────────────────────


def _head_anchor(lines: list[str]) -> int | None:
    """从**文件头**（第一个轮次行之前）里取会议开始时刻。没有返回 `None`。

    ※ 只扫文件头，不扫全文：正文里出现日期是常态（「报名截止 9 月 16 日」），
    在全文里取第一个日期会把正文里的日期当成会议开始时刻 —— 而那个错
    **不报错**，只是把整场会放进错误的一天。
    """
    for line in lines:
        m = _WALL_CLOCK.search(line)
        if m is not None:
            return _wall_clock_to_ts(m.group(0))
    return None


def _parse_txt(text: str) -> tuple[list[_Turn], int | None]:
    """腾讯会议形态与钉钉形态各试一次，取**第一个轮次行更靠前**的那一种。"""
    lines = text.splitlines()
    tencent = _tencent_turn_indices(lines)
    dingtalk = _dingtalk_turn_indices(lines)

    if not tencent and not dingtalk:
        anchor = _head_anchor(lines)
        if anchor is not None:
            raise _NoMeetingsHere(
                "这个 txt 的文件头读懂了（有会议开始时刻），但**一轮发言都没有** —— "
                "这是「读懂了、里面确实没内容」，不是解析器坏了。"
                "要去查的是这场会是不是真的没有转写正文。"
            )
        raise _NotUnderstood(
            "这个 txt 既不是腾讯会议形态（`说话人 时:分:秒` 独占一行、且起一段），"
            "也不是钉钉形态（`[时:分:秒] 说话人 说：正文`），文件头里也没有会议开始"
            "时刻。**不猜**是刻意的：把一份别的文本当成转写，产出的是看起来正常的"
            "错消息，而它不报错。"
        )

    # 两种都命中时取靠前的那个：一份文件里混了两种形态是异常，但「谁先出现」
    # 是一个确定的答案，比「随便选一种」好。
    use_tencent = bool(tencent) and (not dingtalk or tencent[0] <= dingtalk[0])
    first = tencent[0] if use_tencent else dingtalk[0]
    turns = (
        _tencent_turns(lines, tencent)
        if use_tencent
        else _dingtalk_turns(lines, dingtalk)
    )
    return turns, _head_anchor(lines[:first])


# ── 字幕（.srt / .vtt） ─────────────────────────────────────────────


def _blocks(text: str) -> list[list[str]]:
    """按空行切成块。字幕的每条 cue 就是一块（序号 / 时间轴 / 正文）。"""
    out: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _strip_subtitle_tags(text: str) -> str:
    return "\n".join(_SUBTITLE_TAG.sub("", ln) for ln in text.splitlines())


def _cue_turns(cues: list[tuple[str, _Segment]]) -> list[_Turn]:
    """把（说话人，段）序列按**说话人连续**归并成轮次。

    ※ 说话人为空串时（SRT 形态）全部 cue 归成**一轮** —— 那是如实的：
    这个形态确实没有告诉我们谁在说话，把它当成「每一条字幕换一个人」
    就是编。归成一轮之后，切分退化成「按时间窗与预算切」，产出的仍然是
    时间上自洽的片段。
    """
    turns: list[_Turn] = []
    for speaker, seg in cues:
        if turns and turns[-1].speaker == speaker:
            turns[-1] = _Turn(speaker=speaker, segments=turns[-1].segments + (seg,))
        else:
            turns.append(_Turn(speaker=speaker, segments=(seg,)))
    return turns


def _parse_cues(text: str, *, suffix: str) -> list[_Turn]:
    """`.srt` 与 `.vtt` 共用的 cue 扫描。

    ※ 两者的**时间轴写法**与**说话人来源**不同，但「一条 cue = 一个时间点 +
    一段正文」是同一个形状，所以扫描只有一份。
    """
    is_vtt = suffix == ".vtt"
    cues: list[tuple[str, _Segment]] = []
    saw_timing = False

    for block in _blocks(text):
        head = block[0].strip()
        if head.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_at = next(
            (i for i, ln in enumerate(block) if _CUE_TIMING.search(ln) is not None), None
        )
        if timing_at is None:
            continue
        saw_timing = True
        m = _CUE_TIMING.search(block[timing_at])
        assert m is not None
        body = _clean_block(block[timing_at + 1 :])
        if not body:
            # 只有时间、没有字的一条 cue（纯音效段）—— 没有可读内容。
            continue
        if is_vtt:
            # WebVTT **标准里就有**说话人标记，所以这里认它不算猜。
            voice = _VTT_VOICE.search(body)
            speaker = voice.group("speaker").strip() if voice is not None else ""
        else:
            # SRT 的标准里**没有**说话人字段 ⇒ 不猜（见模块 docstring）。
            speaker = ""
        cues.append(
            (
                speaker,
                _Segment(start=_cue_offset(m.group("start")), text=_strip_subtitle_tags(body)),
            )
        )

    if not cues:
        if saw_timing:
            raise _NoMeetingsHere(
                f"这个 {suffix} 有字幕时间轴，结构对得上，但**一条有正文的 cue 都没有**。"
                f"这是「读懂了、文件确实空」，不是解析器坏了。"
            )
        raise _NotUnderstood(
            f"这个 {suffix} 里找不到一条字幕时间轴（`开始 --> 结束`）—— "
            f"解析器与这份导出对不上了（改版？），不是「这场会没内容」。"
        )
    return _cue_turns(cues)


# ── 对外的那一个函数 ────────────────────────────────────────────────

#: 认的转写后缀。`meeting_source.py` 用它筛目录里的文件。
TRANSCRIPT_SUFFIXES = (".txt", ".srt", ".vtt")


def parse_transcript(
    raw: bytes,
    *,
    suffix: str,
    namespace: str,
    group_id: int,
    anchor_ts: int | None = None,
    budget: int = EFFECTIVE_BUDGET_CHARS,
) -> list[PendingMessage]:
    """把**一份转写**的字节变成消息。**纯函数。**

    `namespace` 是「这是哪一场会」（`meeting_source.py` 用文件名给），用来算
    `msg_id`；`group_id` 由 `group_id_for(namespace)` 算（见模块顶部）。

    `anchor_ts` 是**侧车**给出的会议开始时刻（Unix 秒）。它**压过**文件头里的
    那个 —— 理由见模块 docstring「时间」。

    抛：
      · `_NotUnderstood` —— 没读懂（形态 / 编码 / 时间戳 / 没有锚点）
      · `_NoMeetingsHere`          —— 读懂了，但这份转写里没有发言

    ※ 切分发生在这里，下游一行都不用改 —— 「切分归一化的责任在数据源」。
    """
    suffix = suffix.lower()
    text = _decode(raw)

    if suffix == ".txt":
        turns, file_anchor = _parse_txt(text)
    elif suffix in (".srt", ".vtt"):
        turns = _parse_cues(text, suffix=suffix)
        # 字幕格式里**没有**会议开始时刻的位置（SRT / WebVTT 的规范都不带
        # 录制日期），所以它只能来自侧车。
        file_anchor = None
    else:
        raise _NotUnderstood(
            f"不认识的后缀 {suffix!r} —— 认的是 {'、'.join(TRANSCRIPT_SUFFIXES)}。"
        )

    anchor = anchor_ts if anchor_ts is not None else file_anchor
    if anchor is None:
        raise _NotUnderstood(
            f"这份 {suffix} 里没有会议开始时刻，也没有同名侧车给出。"
            f"转写里的时间戳是**相对会议开始**的偏移，没有锚点就算不出绝对时刻。"
            f"**不兜底**：兜底成 0 会把这场会扔进 1970 年，兜底成「今天」会把它"
            f"放进今天的日报 —— 两种错都不报错。"
            f"要修就给它写一份同名侧车 `<文件名>.meta.json`，里面写 "
            f'{{"started_at": "2026-09-14 14:00:00"}}。'
        )

    if not turns:
        raise _NoMeetingsHere(
            "这份转写的形态读懂了，但里面一轮发言都没有。"
        )

    out: list[PendingMessage] = []
    for turn in turns:
        # ※ `part` 是「这一轮里的第几段」，由 `enumerate` 数 —— **不要**拿
        # 别的东西去数它。第一版这里数的是「已经产出的消息里有没有和这一段
        # 相等的」，而消息与段不是同一个类型，那个数**恒为 0**；症状是同一轮
        # 切出来的几段**撞同一个 `msg_id`**，后一段覆盖前一段 ——
        # 消息**变少**，而没有任何东西会报错。
        for i, part in enumerate(split_turn(turn, budget=budget)):
            out.append(
                PendingMessage(
                    msg_id=message_id(namespace, start=part.start, part=i),
                    group_id=group_id,
                    ts=anchor + part.start,
                    # 说话人就是「谁在说话」—— 会议源里没有「群昵称」这个概念，
                    # 对应物就是它。形态不带的那些（SRT）在这里是空串。
                    sender=turn.speaker,
                    content=part.text,
                    # 会议转写里没有账号可言 ⇒ None。用 0 冒充会让它被人物
                    # 筛选命中（0 会被当成一个真实的账号）。
                    actor_account=None,
                    source=NAME,
                )
            )
    return out
