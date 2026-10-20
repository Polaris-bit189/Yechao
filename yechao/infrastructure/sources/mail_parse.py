"""邮件（`.eml` / `.mbox`）→ 消息。**纯函数，不碰 I/O**。

## 为什么「解」要单独一个模块

与 `web_parse.py` 同一条判据（`docs/数据源层.md` §四）：一个源里「读」与「解」的
比例决定它该待在哪儿 —— **解析是最需要被测试的部分，而 `infrastructure/` 的测试
成本最高**。本模块只吃字节：喂一份 `.eml` 的字节进去，看它吐出什么消息，
不需要邮箱、不需要网络、不需要时钟。

取字节那一步在 `mail_source.py`（它才是 I/O）。

## ※ 一封邮件 = 一条消息，**不切**

这是本模块与网页源**共享**的一条判断，也是它与会议转写**相反**的地方：

  · 网页通知：一页多通知，**切**（切错了就少一条 / 多一条）
  · 邮件：一封就是一条自洽的短文本，**不切**（切了反而把它拆散）
  · 会议转写：一份长音视频，**必须切**

端口那句「形状由接口约束，处理方式不由接口约束」说的就是这件事：三个源对下游
长得一样（都是 `PendingMessage`），而归一化方式**必须**各按各的来。

⇒ `parse_eml` 对一份文件**只产出 1 条**消息。这不是实现细节，是可被断言的判据
（`test_一封邮件就是一条消息_不切分`）。

## ※ 「这个文件里没有邮件」与「这个文件我没读懂」必须能分辨

两者都是「解析出 0 条」，而**产出为空是所有错误里最不容易被注意到的一种**
（`docs/数据源层.md` §2.6）。形状相同的话，运维会拿着「这个邮箱今天没来信」的
解释去看一个其实已经坏掉的解析器。

⇒ 抛**两个不同的异常**（都是 `SourceFormatError` 的子类，所以用例层的处置不变，
变的只是人能读到的**下一步**）：

  · `_MailNotUnderstood` —— 读到了，但**我没读懂**：文件里没有邮件头、
    日期读不出来、正文按它自己声明的编码解不开、multipart 里没有可读的文本部分。
    **下一步：去改解析器 / 换一个文件。**
  · `_MailboxHasNoMessages` —— 读懂了 —— 文件在、格式对得上 —— 但**一封都没有**。
    **下一步：去查这份导出是不是空的。**

分界线是**有没有至少一封「像邮件」的东西**：一份 0 字节的导出、一个只有
`From ` 分隔行却没有正文的 mbox，都是「没有邮件」；而一个 `.txt` 改名成
`.eml` 是「没读懂」。

## 编码：用标准库 `email`，**解不出来就抛**

邮件头/正文的编码花样比网页多一个数量级：`=?UTF-8?B?...?=` 这类 encoded-word
（RFC 2047）、GBK 这样的非 UTF-8 字符集、`quoted-printable` 与 `base64` 两种
传输编码（RFC 2045）、`multipart/*` 的多层嵌套。

这些全部交给标准库，理由：**每一样都有规范，自己实现就是自己造一套与规范不同的
东西**。但有一条判据是本模块自己的（与网页源逐字相同）：

> **正文解不出来就抛，不换一个编码重试。**

用错的编码「成功」解出来的是一段乱码，而乱码会照常走完解析、产出 N 条没人看得出
是错的条目 —— 那是「产出为空」的镜像：**产出垃圾**。两种都不可见，所以都不接受。

※ 头部的解码交给 `email.policy.default`（它同时认 encoded-word 与 RFC 6532 的
裸 UTF-8 头）。**已知的边界**：encoded-word 里写了一个不存在的字符集名时，
标准库会退回按 ASCII 解而不报错 —— 这一档本模块**测不出来**（`msg.defects`
里也没有痕迹），所以它写在注释里，而不是假装它被守住了。

## 身份（`msg_id`）用邮件的 `Message-ID`

见 `_identity_of` 与 `_stable_msg_id`。
"""

from __future__ import annotations

import hashlib
from datetime import timezone
from email import message_from_bytes, policy
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import SourceFormatError
from yechao.domain.models import PendingMessage

#: 源的标识。与 `mail_source.NAME` 同一处口径 —— 这里定义，那边引用而不再写
#: 一遍字面量。两处字面量漂移的症状是「消息自称来自一个不存在的源」，
#: 而它在审计里看不出来。
NAME = "mail"

#: 认的两种后缀。**它同时是「这份文件该怎么读」的判据**（见 `mail_source._classify`）。
EML_SUFFIX = ".eml"
MBOX_SUFFIX = ".mbox"

#: mbox 的分隔行前缀。mbox 的格式定义是「每封邮件前面一行 `From `」（RFC 4155）。
_MBOX_FROM = b"From "

#: 认的文本类型。优先纯文本，纯文本没有才退回 HTML。
_TEXT_PLAIN = "text/plain"
_TEXT_HTML = "text/html"

#: 部分**没有声明字符集**时按什么解。
#:
#: ※ RFC 2045 说默认是 `us-ascii`，但那个默认值在今天的邮件上几乎总是错的
#: （ASCII 是 UTF-8 的子集，所以按 UTF-8 解 ASCII 一定也对）。取 UTF-8 是
#: 「尽量对，错就响亮地错」：真按 us-ascii 解，一封带汉字的邮件会当场抛，
#: 而那本来是一封读得懂的邮件。
DEFAULT_CHARSET = "utf-8"

#: `msg_id` 哈希的**域标签**。
#:
#: ※ 网页源的 id 是 `blake2b("站点\x00文章地址")`，邮件的是
#: `blake2b("mail\x00邮件身份")`。两个哈希值本来就不会撞（63 位、不同输入），
#: 加这个标签是为了**让输入自解释**：谁把 id 打印出来，都能看出它是按哪条
#: 规则算的，而不是一个来路不明的数。
_ID_DOMAIN = "mail"

#: 邮件身份内部各段之间的分隔符（ASCII 单元分隔符）。
#: 不用 `\x00` 是为了与网页源那个命名空间的拼法**分开**：两处的输入形状不同，
#: 拼接规则也就不该长得一样。
_FIELD_SEP = "\x1f"


# ── 两种「0 条」 ────────────────────────────────────────────────────


class _MailNotUnderstood(SourceFormatError):
    """读到了，但**我没读懂**。

    文件里没有邮件头 / 日期读不出来 / 正文按它自己声明的编码解不开 /
    multipart 里没有可读的文本部分。下一步是**去改解析器或换一个文件**，
    不是去查这份导出有没有内容。
    """

    default_message = "邮件没读懂"


class _MailboxHasNoMessages(SourceFormatError):
    """读懂了 —— 文件在、格式对得上 —— 但它**一封邮件都没有**。

    ※ 它**仍然抛**（端口判据：解析出 0 条 ⇒ 抛）。分开成另一个类型是为了
    让下一步不同：这里该去查**这份导出是不是空的**，而不是去改解析器。
    """

    default_message = "这份邮件文件里一封邮件都没有"


# ── 对外的那两个函数 ────────────────────────────────────────────────


def parse_eml(raw: bytes, *, group_id: int) -> list[PendingMessage]:
    """一份 `.eml`（**单封**）→ 消息。**纯函数。**

    ※ 一份 `.eml` 只产出 1 条（或抛）。「一封邮件就是一条消息」在这里是**结构
    保证的**，不是靠别处不去切它。
    """
    if not raw.strip():
        raise _MailboxHasNoMessages(
            "这个 .eml 文件是 0 字节（或只有空白）—— 里面没有邮件。"
            "**这是「读懂了、文件确实是空的」**，不是解析器坏了："
            "要去查的是这份导出为什么是空的。",
            bytes=len(raw),
        )
    return parse_messages([raw], group_id=group_id)


def parse_mbox(raw: bytes, *, group_id: int) -> list[PendingMessage]:
    """一份 `.mbox`（**多封**）→ 消息。**纯函数。**

    ※ 多封 ≠ 切分：每一封**邮件**本来就一条消息，mbox 只是把它们装在一个文件里。
    这与「把一封长邮件切成几条」是两件完全不同的事 —— 后者本模块不做。
    """
    if not raw.strip():
        raise _MailboxHasNoMessages(
            "这个 .mbox 文件是 0 字节（或只有空白）—— 里面没有邮件。",
            bytes=len(raw),
        )
    segments = _split_mbox(raw)
    if not segments:
        raise _MailboxHasNoMessages(
            "这份 .mbox 的结构对得上（每一封前面都有 `From ` 分隔行），"
            "但它一封邮件都没有。**这是「读懂了、文件确实是空的」**，"
            "不是解析器坏了：要去查的是这份导出为什么是空的。",
            bytes=len(raw),
        )
    return parse_messages(segments, group_id=group_id)


def parse_messages(raws: list[bytes], *, group_id: int) -> list[PendingMessage]:
    """一批**已经从别处取回来的**原始邮件字节 → 消息。**纯函数。**

    ※ 这是「取字节」与「解」之间的那道缝：文件那条路（`parse_eml` /
    `parse_mbox`）与 IMAP 那条路（`mail_imap.fetch_window`）都从这儿进。
    两条路算出的 `msg_id` 因此**必然**一样 —— 身份只由字节里的 `Message-ID`
    决定，与「这些字节是从文件读的还是从服务器取的」无关。这是判据，有测试钉住
    （`test_同一封信从文件读和从_IMAP_读算出同一个_msg_id`）。

    ※ `raws` 为空时**返回空**，不抛：那是调用方的事。IMAP 那条路上「服务器说
    这个窗口里 0 封」是一个**数据事实**（这段时间确实没有信），不是格式错；
    而「取回来 N 封却读不出东西」由调用方在更外层抛（`_ImapNoContent`）。
    """
    return _convert_all(
        [message_from_bytes(r, policy=policy.default) for r in raws],
        group_id=group_id,
    )


# ── mbox 的分隔 ─────────────────────────────────────────────────────


def _split_mbox(raw: bytes) -> list[bytes]:
    """把一份 mbox 的字节切成「每封邮件一段」（不含 `From ` 分隔行）。

    ※ 判据来自 RFC 4155：mbox 里每一封邮件前面有一行 `From `，而它**必然出现在
    文件开头或一个空行之后** —— 正文里以 `From ` 开头的行按规范会被转义成
    `>From `，所以「前一行是空行」这条限制不会漏掉真正的分隔行，却能挡掉
    正文里的裸 `From `。

    ※ 开头就不是 `From ` 行 ⇒ **抛**（没读懂）。文件后缀说它是 mbox，而它长得
    不像 mbox —— 那多半是把别的文件改了名字，不是「一份空的 mbox」。
    """
    segments: list[list[bytes]] = []
    cur: list[bytes] | None = None
    prev_blank = True  # 文件开头等价于「前面是一个空行」
    for line in raw.split(b"\n"):
        if line.endswith(b"\r"):
            line = line[:-1]
        if line.startswith(_MBOX_FROM) and prev_blank:
            if cur is not None:
                segments.append(cur)
            cur = []
        elif cur is None:
            if line.strip():
                raise _MailNotUnderstood(
                    "这份 .mbox 的第一行不是 `From ` 分隔行 —— 它不像一份 mbox。"
                    "（mbox 的每一封邮件前面都有一行 `From `；把别的文件改名成 "
                    ".mbox 会落到这里。）",
                    first_line=line[:60].decode("latin-1"),
                )
        else:
            cur.append(line)
        prev_blank = not line.strip()
    if cur is not None:
        segments.append(cur)

    # ※ **全是分隔行、一封都没有** ⇒ 那是「没有邮件」，不是「没读懂」：
    # 文件的结构完全正确，只是它装着 0 封。
    bodies = [b"\n".join(seg) for seg in segments]
    return [b for b in bodies if b.strip()]


# ── 一封邮件 → 一条消息 ─────────────────────────────────────────────


def _convert_all(messages: list[Message], *, group_id: int) -> list[PendingMessage]:
    """逐封转换。**任一封读不懂就整批抛**，不静默跳过那一封。

    ※ 静默跳过一封正是本项目明令禁止的形状（用户会以为那天没有那封）。
    而「只坏了一封」是例外情形，为它设计一条安静的旁路不划算，代价却是把
    真问题也吞掉 —— 与 `web_parse.parse_page` 逐条报错同一条判据。
    """
    return [_convert(m, index=i, group_id=group_id) for i, m in enumerate(messages, start=1)]


def _convert(msg: Message, *, index: int, group_id: int) -> PendingMessage:
    if not msg.items():
        raise _MailNotUnderstood(
            f"第 {index} 封没有任何邮件头 —— 这不是一封邮件。"
            f"（多半是把别的文件当成 .eml / .mbox 读了）",
            index=index,
        )

    date_raw = _header(msg, "Date")
    if not date_raw:
        # ※ 不兜底成「今天」。日期决定这封邮件落在哪一天，而日报是**按天分**的
        # —— 兜底会把它放进错误的一天，而且不报错。
        raise _MailNotUnderstood(
            f"第 {index} 封没有 Date 头。日期决定它落在哪一天，而日报是**按天分**的"
            f"—— 没有它就没法把这封邮件放进任何一天，所以这里抛，而不是兜底成"
            f"「今天」。",
            index=index,
        )
    ts = _parse_date(date_raw, index=index)

    subject = _header(msg, "Subject")
    body = _extract_body(msg, index=index)
    content = _compose_content(subject, body)
    if not content.strip():
        raise _MailNotUnderstood(
            f"第 {index} 封既没有主题也没有正文 —— 产出一条空内容的消息比不产出"
            f"更糟：它会在日报里占一行，却什么都没说。",
            index=index,
        )

    return PendingMessage(
        msg_id=_stable_msg_id(_identity_of(msg, date_raw=date_raw, subject=subject)),
        group_id=group_id,
        ts=ts,
        sender=_sender_of(msg),
        content=content,
        # ※ 发件地址是**字符串**，而 `actor_account` 现在是整数契约
        # （见 `domain/models.py`）。把它哈希成一个整数就是**编一个不存在的账号**
        # —— 那个数字会被下游当成一个真实的人。⇒ 如实置 None：
        # 「这个源没有可比的账号」。
        actor_account=None,
        source=NAME,
    )


# ── 头 ──────────────────────────────────────────────────────────────


def _header(msg: Message, name: str) -> str:
    """读一个头的**已解码**值。没有 ⇒ 空串。

    ※ 解码交给 `email.policy.default`（`parse_*` 传进去的那个 policy）：
    它同时认 encoded-word（RFC 2047）与裸 UTF-8 头（RFC 6532），
    而自己拿 `decode_header` 拼一遍会在裸 UTF-8 那一档上解出乱码。
    """
    value = msg.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _sender_of(msg: Message) -> str:
    """发件人 —— 取**显示名**，没有显示名才退回地址。

    ※ 为什么只取一个而不是拼成 `名字 <地址>`：下游拿 `sender` 当「这是谁发的」
    这一信号用（判断「这条是不是官方发的」），而两个源都只给它**一个**身份串。
    拼成复合形状会造出一种别的源不产出的形状，而下游对它的处置是未定义的。
    """
    raw = _header(msg, "From")
    if not raw:
        return ""
    name, addr = parseaddr(raw)
    return name or addr


# ── 日期 ────────────────────────────────────────────────────────────


def _parse_date(raw: str, *, index: int) -> int:
    """`Date` 头 → Unix 秒。

    ※ 时区：头里带偏移时按那个偏移（那是发信机器报的真实时刻）；**没带**时按
    全项目那一处口径（`domain.days.DEFAULT_TZ_OFFSET`，UTC+8）解释墙上时间 ——
    与网页源同一个做法。不取机器本地时区：那样同一份夹具在评委的机器上会算出
    不同的秒数，而日报是按天分的，差 8 小时足以让一封邮件落到前一天。

    ※ 读不出来 ⇒ **抛**，不兜底成 0 或「今天」。
    """
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError) as e:
        raise _MailNotUnderstood(
            f"第 {index} 封的 Date 头 {raw!r} 读不出来：{e}",
            index=index,
        ) from e
    if dt.tzinfo is None:
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) - DEFAULT_TZ_OFFSET
    return int(dt.timestamp())


# ── 正文 ────────────────────────────────────────────────────────────


def _extract_body(msg: Message, *, index: int) -> str:
    """抠出可读正文：**优先纯文本**，没有才退回 HTML（剥标签）。

    ※ 为什么优先纯文本：`multipart/alternative` 的两部分是**同一段话的两种写法**，
    不是两段内容。把两份都收下会让正文里每句话出现两次 —— 而重复的内容会被
    下游当成「这条通知强调了两遍」。
    """
    plain = _collect_text(msg, _TEXT_PLAIN, index=index)
    if plain and plain.strip():
        return plain
    html = _collect_text(msg, _TEXT_HTML, index=index)
    if html and html.strip():
        return _strip_html(html)
    if plain is None and html is None:
        raise _MailNotUnderstood(
            f"第 {index} 封里没有可读的文本部分（既没有 text/plain 也没有 text/html）"
            f"—— 只有附件、或者是一个本模块不认的 multipart 结构。",
            index=index,
        )
    return ""


def _collect_text(msg: Message, ctype: str, *, index: int) -> str | None:
    """把（可能是多层的）结构里所有 `ctype` 的文本部分拼起来。

    ※ 返回 `None` = **这个结构里根本没有这种部分**；返回 `""` = 有，但它是空的。
    两者的区别见 `_extract_body` —— 合成一个值，一封「只写了主题」的通知就会
    被当成格式错误丢掉。
    """
    if msg.is_multipart():
        found = [_collect_text(part, ctype, index=index) for part in msg.get_payload()]
        parts = [p for p in found if p is not None]
        if not parts:
            return None
        return "\n".join(p for p in parts if p.strip())
    if msg.get_content_type() != ctype:
        return None
    if msg.get_filename():
        # ※ 附件**不是正文**。一个 .txt 附件与正文长得一样（都是 text/plain），
        # 只有 `get_filename()` 能把两者分开。
        return None
    return _decode_part(msg, index=index)


def _decode_part(part: Message, *, index: int) -> str:
    """把一部分的字节按**它自己声明的字符集**解成文本。解不开 ⇒ **抛**。

    ※ 判据：**不换一个编码重试**。用错的编码「成功」解出来的是一段乱码，而乱码
    能一路走完解析、「成功」产出 N 条乱码消息。它与「产出为空」是同一个病的两面：
    两种都不可见。
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        # 走不到：multipart 已在 `_collect_text` 里分流。留着是为了**响亮**，
        # 而不是让 `None` 漏下去变成一句 `AttributeError`。
        raise _MailNotUnderstood(f"第 {index} 封的正文部分不是一段可解码的字节")
    charset = part.get_content_charset() or DEFAULT_CHARSET
    try:
        return payload.decode(charset)
    except (UnicodeDecodeError, LookupError) as e:
        raise _MailNotUnderstood(
            f"第 {index} 封的正文按 {charset!r} 解不开：{e}。"
            f"**不换一个编码重试**是刻意的 —— 用错的编码「成功」解出来的是一段"
            f"乱码，而乱码会照常走完解析，产出 N 条没人看得出是错的条目。",
            index=index,
            charset=charset,
        ) from e


class _HtmlToText(HTMLParser):
    """从 HTML 里抠出可读文本。**刻意最小**。

    ※ 与 `web_parse` 一样不建通用 HTML 树：通用树要处理的边界（错嵌套、隐式闭合、
    命名空间）比这里需要的多一个数量级，而每多一条边界就多一处猜。
    这里只做两件事：**跳过脚本与样式**、把标签之间的文本收下来。
    """

    #: 内容不是给人读的标签。
    _SKIP = frozenset({"script", "style"})

    def __init__(self) -> None:
        # `convert_charrefs=True`：`&amp;` 一类实体由标准库替我还原成字符 ——
        # 不还原的话它在下游是**不可见的差异**（人眼看起来一样，逐字比对不一样）。
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._buf.append(data)

    def text(self) -> str:
        return "".join(self._buf)


def _strip_html(html: str) -> str:
    """HTML → 一行文本（空白折成单空格）。

    ※ 折成单空格是**与网页源同一口径**的取舍（`web_parse._squeeze`）：模型给出的
    `quote` 必须能在源文里**逐字找到**，而 HTML 源码里的缩进与换行只是排版。
    不折的话，一段在源码里被换行切开的正文，它的摘录会「看起来一模一样但对不上」。
    """
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return " ".join(parser.text().split())


def _compose_content(subject: str, body: str) -> str:
    """主题 + 正文拼成**一条自洽的短文本**。

    ※ 主题回答「这是什么」，正文回答「具体怎么回事」—— 与网页源的
    「标题 + 摘要」是同一组角色。**不切**：一封邮件就是一个完整的短文本。
    """
    s, b = subject.strip(), body.strip()
    if not s:
        return b
    if not b:
        return s
    return f"{s}\n{b}"


# ── 身份 ────────────────────────────────────────────────────────────


def _identity_of(msg: Message, *, date_raw: str, subject: str) -> str:
    """这封邮件的身份串。

    ※ **首选 `Message-ID`**：它是邮件**自己声明的**身份（RFC 5322 要求发信的
    MTA 生成一个全局唯一的），也是邮件世界里「同一封信」的公认判据。

    ※ 与网页源的取舍对比（这一段是判据，不是背景）：
      网页源用「站点 + 文章地址」，是因为网页通知**没有**一个自报的 id ——
      最接近的东西是它的 URL，而 URL 只在**那个站点里**唯一，所以要带站点。
      邮件**有**自报的 id，所以它不需要站点那一层命名空间。反过来，如果邮件
      也去用「文件路径 + 第几封」当身份，同一封信被导出成两份（一次 .eml、
      一次 .mbox）就会算出两个 id，于是重复入库 —— 而重复条目是无声的。
      ⇒ 身份要取**邮件自己的**，不是「它在哪个文件里」。

    ※ 没有 `Message-ID` 时**回落**到「发件人 + 日期 + 主题」的指纹。
    回落是必要的：手写的邮件与脚本发的通知经常没有这个头，而缺它就直接抛会让
    这个源对一整类正常输入不可用。代价写在明处：这三样都一样的**两封不同的邮件**
    会被认成同一封（现实中几乎不存在，而且那种情况下它们本来就分不出来）。
    不把正文算进指纹，是因为正文的换行与传输编码会被重写，而主题/日期/发件人
    不会 —— 指纹要的是**重读同一封信时不变**。
    """
    message_id = _header(msg, "Message-ID")
    if message_id:
        return message_id
    return _FIELD_SEP.join((_header(msg, "From"), date_raw, subject))


def _stable_msg_id(identity: str) -> int:
    """由邮件的身份算出的稳定 id。

    ※ 为什么**不用文件里的位置**（第几封）当 id：位置会变 —— 往 mbox 前面插一封，
    它后面每一封的位置都往后挪一格。位置当 id 的话，插一封会让**整个文件**换一批
    新 id，于是重跑摄取把它们全部重写一遍（`msg_id` 是幂等的键），日报里同一封
    邮件出现两次，而没有任何东西会报错。这与网页源「不用列表位置」是同一条判据。

    ※ 取 63 位：SQLite 的 INTEGER 是有符号 64 位，右移一位留出符号位。
    不这么做的话，约一半的 id 会超过 `2**63-1`，而症状是插入时报
    "Python int too large to convert to SQLite INTEGER" —— 在**一半**的邮件上
    出现，像随机故障。
    """
    digest = hashlib.blake2b(
        f"{_ID_DOMAIN}\x00{identity}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") >> 1
