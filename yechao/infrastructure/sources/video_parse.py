"""字幕 → 消息。**纯函数，不碰 I/O。**

## 为什么「解」要单独一个模块

与 `web_parse.py` 同一条判据（见那个模块的 docstring）：一个源里「读」与
「解」的比例决定它该待在哪儿 —— 解析是最需要被测试的部分，而
`infrastructure/` 的测试成本最高。本模块喂字节进去、看它吐出什么，
不需要网络、不需要文件、不需要时钟。

※ 按那条判据，这一块本该在 `domain/`。它留在这里的原因是本次任务的硬约束
锁住了 `yechao/domain/**`，与 `web_parse.py` 留在同一处是同一个取舍。
**接手的下一个人该做的事是把这两个模块一起搬进 `domain/`。**

## 为什么视频要切、按什么切（本模块的全部判据都在回答这一个问题）

### 现象

一条字幕行没有信息量：真实字幕里大量出现「对」「然后」「这个」「嗯」——
它们是**显示**的切分（一行不超过十几个字、不超过几秒），不是**意思**的切分。
一整段又不堪用：一场七分钟的讲话有四千多字，当成一条消息会撑爆下游，
而端点**会静默截断**（`docs/计划.md` §七 第 1 条：输入超上下文时回 200、
`finish_reason=stop`、`usage` 只报截断后的数，客户端拿不到任何信号）。
⇒ 长度预算**必须由数据源负责**，下游看不见它原来有多长。

### 判据：**语义边界为主，时间上限兜底**

先排除两个极端：

  · **纯时间窗口**（每 60 秒一条）：会切在句子中间。「报名截止到十月二十四号」
    与「晚上十点」若落在两条消息里，两条都不含一个完整的事实 —— 而本项目
    要求条目必须能**逐字回连**到源文（`ExtractionCandidate.quote`）。
    切碎了，回连就失败，整条被丢弃，**而丢弃是无声的**。
  · **纯语义边界**（只在说话人换话题时切）：一个不停顿的讲话人会产出一条
    远超预算的消息，然后被端点静默截断。这正是要防的那件事。

⇒ 采用**两级**：

  1. **句**（原子）：把连续的字幕行合并到「一句话说完」。判据是**句末标点**
     （`。！？…` 与句末的 `.`），或**说话人停了**（行与行之间的间隔 ≥
     `PAUSE_SECONDS`），或这一句本身已经 ≥ `MAX_SEGMENT_SECONDS`（兜底，
     见下）。一句话是**不可再分**的 —— 除非它自己就超了预算。
  2. **段**（消息）：把句攒成段。攒够 `MIN_SEGMENT_SECONDS` 之后**遇到停顿
     就切**；到 `MAX_SEGMENT_SECONDS` 就切；文件末尾切。

于是得到一条可以断言的性质：

  > **一条消息的边界永远落在一句完整的话的末尾**
  > （除非那一句自己就超过了预算上限 —— 那时只能切在句内，而这是
  > 「端点会静默截断」逼出来的下界，不是选择）。

### 为什么「停顿」算语义边界

观察到的现象：说话人在**同一个话题内**换气、断句的间隔通常在 0.3–1.5 秒；
而**换话题**时他会停下来 —— 翻页、换幻灯片、等听众反应 —— 那通常在 2 秒
以上。所以「间隔 ≥ 2 秒」是一个**可观察的事实**（它在字幕文件里就是个数字），
用它代理「这里换话题了」，而不是去猜内容。这是**代理**，不是判据本身，
所以 `PAUSE_SECONDS` 是一个可调的常量：调它不需要改结构。

### 为什么上限是硬的、下限是软的

  · `MAX_SEGMENT_SECONDS` **必须**保证：端点静默截断的代价是「看起来成功」。
  · `MIN_SEGMENT_SECONDS` 只是**不切**的条件（攒不够就不切），不是「切出一段
    来凑够它」。所以最后一段可以短于下限 —— 视频结束了，就该收尾。

## ※ 一条消息的 `ts` 是**这段字幕在视频里的时间**，不是抓取时间

判据：日报**按天分**。视频可能是上周录的，今天才被摄取。用抓取时间会让
它落在**今天**那一天 —— 而「上周三的讲座」出现在今天的日报里，读者不会
觉得哪里不对，只会得到一份错的日报。

⇒ `ts = 视频的绝对起始时刻 + 段首那条字幕的偏移秒数`。

  · 段首而不是段尾：这一条消息说的是「说话人从这一刻开始说的这一段」。
    用段尾会让跨零点的一段落到第二天。
  · **绝对起始时刻必须由外部给**（本地文件的侧车、视频页面的发布时间、
    或配置里的兜底）。拿不到就**抛** —— 见 `video_source.py` 里
    `_anchor_of` 的说明：兜底成 0 或「今天」都是把消息放进错误的一天，
    而它不报错。

## ※ 与网页源的关系：两者都切，切法不同

网页源是「一页多通知」：它把**页面**切成通知（一条 `<li>` = 一条消息），
每条通知的正文**不再切** —— 一条通知本来就是一个自洽的短文本。
本模块是「一段连续讲话」：**没有**天然的条目边界，边界只能从时间轴上推。

⇒ 两者的产物对下游**长得一样**（都是 `PendingMessage`），归一化方式却完全
不同。`ports.py` 那句「形状由接口约束，处理方式不由接口约束」说的就是这件事：
如果两个源为了迎合同一个下游而做同样的处理，其中一个必然是错的。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from yechao.domain.errors import SourceFormatError
from yechao.domain.models import PendingMessage

#: 源的标识。与 `video_source.NAME` 同一处口径（这里定义、那里引用）。
NAME = "video"

# ── 切分常量 ────────────────────────────────────────────────────────
#
# 三个都是**可调**的。它们不是「随便挑的数字」，每个都对应一个可观察的
# 事实或一条硬约束，理由写在各自的注释里 —— 调它的人要知道自己在动什么。

#: 说话人停下来的门槛（秒）。见模块 docstring「为什么停顿算语义边界」。
#: 调大 ⇒ 段变长（切得少）；调小 ⇒ 一段话被切成更多条。
PAUSE_SECONDS = 2.0

#: 一段的时长下限（秒）。攒不够就不切。
#: 判据：一条字幕行没有信息量（「对」「然后」），太短的段同样是噪声 ——
#: 45 秒的中文口语约两百字，够放下一个完整的表述（谁、什么时候、在哪、怎么办）。
MIN_SEGMENT_SECONDS = 45.0

#: 一段的时长上限（秒）。**硬约束**，理由见模块 docstring。
#: 180 秒的中文口语约八百字（≈ 六百 token），远小于常见上下文预算，
#: 但足够容纳一段完整表述 —— 于是正常情况下是「停顿」在切、上限只是保险。
MAX_SEGMENT_SECONDS = 180.0

#: 句末标点。※ `；`（分号）**不在**里面 —— 它分开的是同一句话里的两个分句，
#: 在它上面切会切出「虽然但是」的半个句子。`、`同理。
SENTENCE_END = "。！？!?…"

#: 句末标点后面可能还跟着的收尾符号（引号、括号）。
#: 不剥掉它们的话，`「……就这样。」` 这类行会被判成「没说完」。
_CLOSERS = "」』”’\"')）】》〉"

#: 时间戳：`00:01:02,500` / `00:01:02.500` / `01:02.500`。
#: 三种写法都要认：SRT 用逗号，VTT 用点，而 VTT 允许省略小时位。
_TIMESTAMP = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")

#: 时间行：`<起点> --> <终点>[ 设置…]`。终点后面可能跟 VTT 的 cue settings
#: （`align:start position:0%`），所以终点只取第一个空白分隔的 token。
_ARROW = "-->"

#: VTT 的**说话人标记**。它是标准里就有的东西（`<v 名字>`），所以用它不算
#: 「猜」—— 见 `_speaker_of`。SRT 的标准里**没有**这个东西，所以 SRT 字幕
#: 里的说话人一律取不到（那就置空，不猜）。
_VOICE_TAG = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]*)>", re.IGNORECASE)

#: 其余的 VTT 内联标签（`<i>` `<b>` `<c.foo>` `<00:00:01.000>`）—— 一律剥掉。
#: 它们是**排版**，不是内容。
_ANY_TAG = re.compile(r"<[^>]*>")


# ── 两种「0 条」 ────────────────────────────────────────────────────
#
# 与 `web_parse.py` 同一套设计（见那个模块的 docstring）：两者都是「解析出
# 0 条」，而**产出为空是所有错误里最不容易被注意到的一种**。形状相同会让
# 运维拿着「这场讲座没内容」的解释去看一个其实已经坏了的解析器。
#
# ⇒ 抛两个不同的异常（都仍是 `SourceFormatError` 的子类，所以用例层的处置
# 不变，变的只是人能读到的**下一步**）。


class _SubtitleNotUnderstood(SourceFormatError):
    """字幕素材读到了，但**一条字幕行都没解析出来**。

    下一步：**去改解析器**（格式不认识 / 文件是空的 / 编码解错了），
    不是去查这场讲座有没有内容。
    """


class _VideoHasNoSubtitles(SourceFormatError):
    """视频页读懂了，但**这个视频没有字幕轨**（也没有可读的简介）。

    下一步：**去查这个视频本身**（它确实没配字幕），不是去改解析器。
    """


# ── 形状 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Cue:
    """一条字幕行。`start` / `end` 是**视频内的偏移秒数**，不是绝对时刻。

    ※ 偏移在解析层，绝对时刻在组装层（`build_messages`）—— 两者分开，
    是因为偏移是字幕文件里**原样写着**的东西（可测、可复现），而绝对时刻
    依赖外部给的锚点（可能拿不到）。混在一起会让「文件没问题、只是锚点没配」
    看起来像「文件坏了」。
    """

    start: float
    end: float
    text: str
    #: 这一段话的说话人（VTT 的 `<v 名字>` 声明）。取不到就是空串。
    voice: str = ""


# ── 解字幕 ──────────────────────────────────────────────────────────


def _decode(raw: bytes, *, encoding: str | None = None) -> str:
    """字节 → 文本。解不开 ⇒ **抛**。

    ※ 判据与 `web_parse.decode_html` 一致：**不换一个编码重试**。

    观察：字幕文件里**没有**编码声明（SRT / VTT 的标准都不带）。所以
    「按 utf-8 解不开」是唯一能拿到的信号，而拿 GBK 的字节去按 utf-8 解
    通常会**解出错**（不像 latin-1 那样照单全收）—— 那就响亮地失败。

    要读一份非 utf-8 的字幕，就给它的**侧车**写一个 `encoding` —— 那是
    「这份素材用什么编码」的**声明**，与「程序猜一个」是两件事。
    BOM 不是猜：它是文件自己带的前三个字节。
    """
    if encoding:
        codec = encoding
    elif raw.startswith(b"\xef\xbb\xbf"):
        # UTF-8 BOM。**它不是猜**：文件自己带的前三个字节。
        codec = "utf-8-sig"
    else:
        codec = "utf-8"
    try:
        return raw.decode(codec)
    except (UnicodeDecodeError, LookupError) as e:
        raise _SubtitleNotUnderstood(
            f"字幕按 {codec!r} 解不开：{e}。"
            f"字幕文件里没有编码声明，所以只有两个办法：把它转成 utf-8，"
            f"或者在侧车里写一行 \"encoding\" 声明它实际用的是什么。"
            f"（不自动换编码重试是刻意的 —— 用错编码「成功」解出来的是一份"
            f"乱码，而乱码会照常走完切分，产出 N 条没人看得出是错的条目。）",
            codec=codec,
        ) from e


def _parse_timestamp(raw: str) -> float:
    """`00:01:02,500` → 62.5（秒）。认不出来 ⇒ **抛**。"""
    m = _TIMESTAMP.fullmatch(raw.strip())
    if m is None:
        raise _SubtitleNotUnderstood(
            f"时间戳 {raw!r} 认不出来。认的写法是 `时:分:秒,毫秒`（SRT）、"
            f"`时:分:秒.毫秒` 或 `分:秒.毫秒`（VTT）。",
            timestamp=raw,
        )
    hours, minutes, seconds, millis = m.groups()
    return (
        int(hours or 0) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def _strip_tags(raw: str) -> tuple[str, str]:
    """剥掉 VTT 的内联标签，返回 `(正文, 说话人)`。

    ※ 说话人只在**标准声明了它**的时候才取（VTT 的 `<v 名字>`）。SRT 的标准
    里没有这个东西，所以 SRT 字幕的 `voice` 一律是空串 —— **不猜**。
    猜的症状是：把正文的一部分当成名字，而那个「名字」还会被下游当成人名
    抹掉，正文被咬掉一块（`docs/数据源层.md` §3.1）。
    """
    voices = _VOICE_TAG.findall(raw)
    text = _ANY_TAG.sub("", raw)
    # VTT 的正文里可以有 `&amp;` 这类实体，它是**文字**，要还原。
    text = html.unescape(text)
    # 一条 cue 的正文可能是多行（SRT / VTT 都允许）。多行是**排版**换行，
    # 不是内容里的换行 —— 用 `_join_texts` 的同一套规矩拼回一行。
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    voice = ""
    if voices:
        # 同一个 cue 里出现多个 `<v>` ⇒ 这一行里就换了人。取不到「单一说话人」，
        # 那就置空 —— 见 `_speaker_of`。
        distinct = {v.strip() for v in voices if v.strip()}
        voice = distinct.pop() if len(distinct) == 1 else ""
    return _join_texts(lines), voice


def _join_texts(parts: list[str]) -> str:
    """把若干段文字拼成一行。**按需要补空格**。

    ※ 为什么要这条规矩：字幕的换行是**显示**的切分，中文里它落在词的中间
    （「今天我们要讲」+「的是动态规划」），拼回去时**不能**补空格，否则
    「报名截止到 十月二十四号」这种带空格的正文会让下游的**逐字回连**失败
    （条目必须能逐字在源文里找到，见 `ExtractionCandidate.quote`）。
    而英文里正好相反：不补空格会把两个单词粘成一个。

    ⇒ 判据只看两个字符本身：**两边都是 ASCII 的字母或数字才补空格**。
    中文、标点、中英交界一律直接拼。
    """
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out and _needs_space(out[-1], part[0]):
            out += " "
        out += part
    return out


def _needs_space(left: str, right: str) -> bool:
    return left.isascii() and left.isalnum() and right.isascii() and right.isalnum()


def _scan_cues(text: str) -> list[_Cue]:
    """文本 → 字幕行。**按空行分块**，块里有 `-->` 的才是字幕行。

    ※ 这条规矩同时认了两种格式，而且是**故意的**：SRT 与 VTT 的差别只在
    时间戳的分隔符、序号行、和 `WEBVTT` / `NOTE` / `STYLE` 这几个头块 ——
    而它们**全都不含 `-->`**，所以「含 `-->` 的块才是字幕」一句话就分开了。
    分别写两个解析器会让「同一个文件按两种格式解析出不同的条数」变成一个
    无人察觉的可能，而它正是本模块最该防的那种错。

    ※ 一块里有时间行但**格式不认识** ⇒ **整份报错**，不跳过那一块。
    理由与 `web_parse` 同：静默跳过一条字幕 = 用户以为视频里没说过那句话。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    cues: list[_Cue] = []
    for index, block in enumerate(blocks):
        arrow_at = next(
            (i for i, ln in enumerate(block) if _ARROW in ln), None
        )
        if arrow_at is None:
            # `WEBVTT` / `NOTE …` / `STYLE …` 这些头块。它们不含 `-->`。
            continue
        left, _, right = block[arrow_at].partition(_ARROW)
        # 终点后面可能跟着 VTT 的 cue settings，只取第一个 token。
        right = right.strip().split(" ")[0] if right.strip() else ""
        try:
            start = _parse_timestamp(left)
            end = _parse_timestamp(right)
        except _SubtitleNotUnderstood as e:
            raise _SubtitleNotUnderstood(
                f"第 {index} 块的时间行 {block[arrow_at]!r} 读不出来：{e}",
                block=index,
            ) from e

        body, voice = _strip_tags("\n".join(block[arrow_at + 1 :]))
        if not body:
            # 只带标签、没有文字的行（`<i></i>` 之类的排版残留）。
            # 跳过它**不是**「静默丢一条消息」—— 它本来就没有文字。
            continue
        cues.append(_Cue(start=start, end=end, text=body, voice=voice))
    return cues


def parse_subtitles(raw: bytes, *, encoding: str | None = None) -> list[_Cue]:
    """字幕文件的字节 → 字幕行。**纯函数。**

    抛 `_SubtitleNotUnderstood`：解不开码 / 时间行认不出来 / **一条都没有**。
    最后一种最要紧 —— 见 `_SubtitleNotUnderstood` 的说明。
    """
    cues = _scan_cues(_decode(raw, encoding=encoding))
    if not cues:
        raise _SubtitleNotUnderstood(
            "这份字幕素材里一条字幕行都没有。"
            "（文件是空的、格式不认识、或者时间行全都不含 `-->`。）"
            "这不是「这场讲座没内容」—— 那是另一个判断，见 "
            "_VideoHasNoSubtitles。下一步是去看这份素材本身。"
        )
    return cues


# ── 切 ──────────────────────────────────────────────────────────────


def _ends_sentence(text: str) -> bool:
    """这句话说完了吗。判据：末尾是句末标点（剥掉收尾的引号括号之后）。"""
    stripped = text.rstrip().rstrip(_CLOSERS).rstrip()
    if not stripped:
        return False
    return stripped[-1] in SENTENCE_END or stripped.endswith(".")


def _split_sentences(
    cues: list[_Cue], *, pause_seconds: float, max_seconds: float
) -> list[list[_Cue]]:
    """字幕行 → 句。一句话是**不可再分**的原子（除非它自己超了上限）。

    一句话结束于三种情形之一：
      · 句末标点 —— 说话人自己说完了；
      · 下一行之前有 ≥ `pause_seconds` 的空白 —— 说话人停了（见模块 docstring）；
      · 这一句自己已经 ≥ `max_seconds` —— **兜底**。没有标点、也不停顿的
        讲话（自动生成的字幕常常如此）会走到这里，那时只能切在句内。
        这是「端点会静默截断」逼出来的下界，不是选择。
    """
    out: list[list[_Cue]] = []
    current: list[_Cue] = []
    for i, cue in enumerate(cues):
        current.append(cue)
        span = current[-1].end - current[0].start
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        gap = (nxt.start - cue.end) if nxt is not None else 0.0
        if _ends_sentence(cue.text) or gap >= pause_seconds or span >= max_seconds:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _group_segments(
    sentences: list[list[_Cue]],
    *,
    pause_seconds: float,
    min_seconds: float,
    max_seconds: float,
) -> list[list[_Cue]]:
    """句 → 段（= 一条消息）。**下限是软的，上限是硬的。**

    · 攒够 `min_seconds` 之后遇到停顿 ⇒ 切（这是常态）；
    · 到 `max_seconds` ⇒ 切（保险）；
    · 最后一句 ⇒ 切（视频结束了，就该收尾 —— 哪怕这一段很短）。
    """
    out: list[list[_Cue]] = []
    current: list[list[_Cue]] = []
    for i, sentence in enumerate(sentences):
        current.append(sentence)
        first = current[0][0]
        last = current[-1][-1]
        span = last.end - first.start
        nxt = sentences[i + 1] if i + 1 < len(sentences) else None
        gap = (nxt[0].start - last.end) if nxt is not None else 0.0
        if (
            nxt is None
            or span >= max_seconds
            or (gap >= pause_seconds and span >= min_seconds)
        ):
            out.append([cue for sent in current for cue in sent])
            current = []
    if current:
        out.append([cue for sent in current for cue in sent])
    return out


def _speaker_of(segment: list[_Cue], *, default: str) -> str:
    """这一段的说话人。

    ※ 只在**标准声明了说话人**、且这一段里**只有一个**说话人时才用它；
    否则退到视频级的 `default`（侧车 / 页面声明的讲者）；再没有就置空串。

    **不猜**。观察：一段里出现两个 `<v>` 标签，说明这段跨了两个人的发言 ——
    那它就没有单一说话人，硬取一个会让「谁说的」这条信息变成错的，
    而下游用它判断「是不是官方发的」。
    """
    voices = {cue.voice for cue in segment if cue.voice}
    if len(voices) == 1:
        return voices.pop()
    return default


# ── 组装 ────────────────────────────────────────────────────────────


def build_messages(
    cues: list[_Cue],
    *,
    namespace: str,
    group_id: int,
    anchor_ts: int,
    speaker: str = "",
    pause_seconds: float = PAUSE_SECONDS,
    min_seconds: float = MIN_SEGMENT_SECONDS,
    max_seconds: float = MAX_SEGMENT_SECONDS,
) -> list[PendingMessage]:
    """字幕行 → 消息。**纯函数。**

    `anchor_ts` 是视频的**绝对起始时刻**（Unix 秒）。每条消息的 `ts` 是
    「锚点 + 段首偏移」—— 见模块 docstring：`ts` 是这段字幕**在视频里的时间**，
    不是抓取时间。

    `namespace` 是「这段视频在哪儿」（视频页面的地址，或夹具侧车里记的地址），
    用来算 `msg_id` —— 见 `_msg_id`。
    """
    sentences = _split_sentences(
        cues, pause_seconds=pause_seconds, max_seconds=max_seconds
    )
    segments = _group_segments(
        sentences,
        pause_seconds=pause_seconds,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )

    out: list[PendingMessage] = []
    for segment in segments:
        offset = segment[0].start
        out.append(
            PendingMessage(
                # 段在视频里的位置取整到秒之后当身份 —— 见 `_msg_id`。
                msg_id=_msg_id(namespace, f"@{int(offset)}"),
                group_id=group_id,
                # ※ 段首，不是段尾。见模块 docstring。
                ts=anchor_ts + int(offset),
                sender=_speaker_of(segment, default=speaker),
                content=_join_texts([cue.text for cue in segment]),
                # 视频里的讲话没有「账号」可言 ⇒ None。
                # 用 0 冒充会让它被人物筛选命中。
                actor_account=None,
                source=NAME,
            )
        )
    return out


def _msg_id(namespace: str, key: str) -> int:
    """由「哪段视频 + 这一段在视频里的位置」算出的稳定 id。

    ※ 为什么**不用段的序号**：序号取决于切分常量。改一次
    `MIN_SEGMENT_SECONDS`，后面每一段都换一个号 —— 于是重跑摄取把它们
    **全部重写一遍**（`msg_id` 是幂等的键），日报里同一条出现两次，
    而没有任何东西会报错。用**偏移**则只有被挪动的边界附近会变。

    ※ 身份串由调用方给（`key`）。字幕那一路给的是 `@<段首偏移取整到秒>`：
    `00:01:02,500` 与 `00:01:02,499` 在两次抓取之间抖动不到一秒，取整能
    吸收掉它；抖动**超过**一秒时仍然会换 id —— 那时素材真的变了，换 id 是对的。
    简介那一路给 `@简介`，B 站投稿那一路给 `@<bvid>`：它们是**另一类东西**，
    用同一个命名空间但不共用身份串。

    ※ 幂等键的算法**只有一处**（`web_parse._stable_msg_id`）：两处实现漂移的
    症状是「同一个源换一批 id」，而它在审计里看不出来。本模块 import 它而
    不是抄一遍。**该做的修法**是把它提到 `_common.py`（与时间窗的口径并列），
    但本次任务锁住了那个文件 —— 与 `_WebSource` 用下划线前缀走护栏口子是
    同一类取舍。
    """
    from yechao.infrastructure.sources.web_parse import _stable_msg_id

    return _stable_msg_id(namespace, key)
