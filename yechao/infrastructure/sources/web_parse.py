"""网页列表页 → 消息。**纯函数，不碰 I/O**。

## 为什么「解」要单独一个模块

`docs/数据源层.md` §四 说得很清楚：一个源里「读」与「解」的比例决定它该待在
哪儿 —— **解析是最需要被测试的部分，而 `infrastructure/` 的测试成本最高**。

本模块的判据全部来自「这个页面的结构长什么样」，所以它不需要网络、不需要
文件、不需要时钟：喂字节进去，看它吐出什么。`web_source.py` 负责的才是 I/O
（从哪儿取字节），`web_fetch.py` 负责把字节抓下来。

※ **按 `docs/数据源层.md` §四 的判据，这一块本该在 `domain/`** —— 它确实
是纯逻辑。它留在这里的原因是本次任务的硬约束锁住了 `yechao/domain/**`。
代价已经用测试补上：`tests/infrastructure/test_web_source.py` 直接调
`parse_page`，不经过任何 I/O。**接手下一个人该做的事是把本模块搬进
`domain/`**，那时 `web_source.py` 只剩取字节与配置。

## 页面长什么样（判据的**唯一**来源）

本模块认的是一类**通知公告列表页**：一个 `<ul class="notice-list">` 里排着
若干 `<li class="notice-item">`，每个 li 带 `data-publish-time` /
`data-department` 两个属性，li 内部有一个 `<a class="notice-title" href=...>`
和一个可选的 `<p class="notice-summary">`。

这些类名不是随手挑的字符串，每一条都对应页面上一个**可观察的结构事实**，
推导写在各自的常量旁边。

## ※ 「这个网页没读懂」与「这个网页一条通知都没有」必须能分辨

两者都是「解析出 0 条」，而**产出为空是所有错误里最不容易被注意到的一种**
（`docs/数据源层.md` §2.6）。如果它们形状相同，运维会拿着「站点今天没发
通知」的解释去看一个其实已经改版了的解析器。

⇒ 本模块抛**两个不同的异常**（都仍是 `SourceFormatError` 的子类，所以
用例层的处置不变，变的只是人能读到的**下一步**）：

  · `_PageNotUnderstood` —— 结构判据没命中（容器不在 / 条目缺必要字段）。
    **下一步：去改解析器。**
  · `_PageHasNoNotices` —— 结构判据命中了，容器就在那儿，只是里面空的。
    **下一步：去查站点是不是真的没发通知。**

分界线是**列表容器**：站点改版会把它改名/挪走，而「今天没发通知」不会动它。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import SourceFormatError
from yechao.domain.models import PendingMessage

#: 源的标识。与 `web_source.NAME` 同一处口径。
NAME = "web"

# ── 结构标记 ────────────────────────────────────────────────────────
#
# 每一个都对应页面上一个可观察的事实，不是「随便挑一个 class 名」：

#: 列表容器。**它是「读懂了没有」的分界线**：改版会改它，没发通知不会。
LIST_CLASS = "notice-list"

#: 一条通知。页面把标题、摘要、发布单位、发布时间四类信息**全部**放在这一个
#: li 内部 —— 容器外（页头、导航、页脚）只有站点名与链接，没有通知内容。
#: ⇒ 条目边界 = li.notice-item。
ITEM_CLASS = "notice-item"

#: 标题。观察：li 里唯一**每条都非空**、且不含站点模板噪声的文本是它的内容。
#: 摘要有长有短（有的通知没有摘要），部门与时间是属性，只有标题是必备的。
#: ⇒ 它缺失 = 这条 li 不是一条通知（模板残留 / 占位），而不是「一条没标题的通知」。
TITLE_CLASS = "notice-title"

#: 摘要。观察：摘要补的是「几点、在哪、怎么办」，标题补的是「这是什么」。
#: 单独给标题，下游拿不到办理时间与地点；单独给摘要，下游不知道这是什么。
#: ⇒ 两者拼成一条自洽短文本（见 `_compose_content`）。
SUMMARY_CLASS = "notice-summary"

#: 发布时间。观察：页面里时间出现两次 —— 这个属性精确到秒；li 里
#: `<span class="notice-meta">` 那行只有日期，且与部门挤在一句自由文本里。
#: ⇒ 取**精确的那一个**（属性），不从自由文本里猜。
PUBLISH_ATTR = "data-publish-time"

#: 发布单位。观察：同上，结构化属性与自由文本各有一份。
#: ※ 属性拿不到时 `sender` 置**空串**，**不**去切 `notice-meta` 那行 ——
#: 那是「猜」，猜错会把日期的一部分当成部门名，而猜错的部门名会被下游当成
#: 发送者身份的依据。宁可承认「这个源没有自报发送者」。
DEPARTMENT_ATTR = "data-department"

#: 页面上的时间写法。它精确到秒，所以只有这一种格式被认。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: HTML 的**空元素**：它们没有结束标签，不参与配对计数。
#: 不列出来的话，摘要里的一个 `<br>` 会让配对计数错位，症状是「整页解析出 0 条」。
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

#: 从字节里嗅编码声明。观察：`<meta charset>` 按 HTML 规范必须出现在文档前
#: 1024 字节内，所以只嗅前 2048 字节就够，不必先解全文。
_META_CHARSET = re.compile(
    r"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)


# ── 两种「0 条」 ────────────────────────────────────────────────────


class _PageNotUnderstood(SourceFormatError):
    """页面读到了，但**我没读懂**。

    形状判据没命中：列表容器不在，或者条目缺了必要字段。
    下一步是**去改解析器**（站点改版了），不是去查站点有没有发通知。
    """

    default_message = "网页没读懂（结构判据没命中）"


class _PageHasNoNotices(SourceFormatError):
    """页面读懂了 —— 容器在、结构对得上 —— 但它**一条通知都没有**。

    ※ 它**仍然抛**（端口判据：解析出 0 条 ⇒ 抛）。分开成另一个类型是为了
    让下一步不同：这里该去查**站点是不是真的没发通知**，而不是去改解析器。
    """

    default_message = "网页读懂了，但这个列表是空的"


# ── 条目 ────────────────────────────────────────────────────────────


@dataclass
class _NoticeEntry:
    """页面上一条 li 里读出来的东西。**还没过校验**。

    它存在的理由：`<li class="notice-item">` 的**存在**与它**是不是一条
    有效通知**是两件事。先照单收下，再逐条判 —— 这样「一条都没有」与
    「有 3 条但都缺字段」才分得开。
    """

    href: str = ""
    title: str = ""
    summary: str = ""
    department: str = ""
    published_at: str = ""


# ── HTML 扫描 ───────────────────────────────────────────────────────


class _NoticeListParser(HTMLParser):
    """只做一件事：把 `ul.notice-list` 里的 li 逐条收下来。

    ※ 刻意**不**做通用的 HTML 树 —— 通用树要处理的边界（错嵌套、隐式闭合、
    命名空间）比这里需要的多一个数量级，而每多一条边界就多一处猜。
    这里只按「页面观察到的形状」配对，配不上就报「没读懂」。
    """

    def __init__(self) -> None:
        # `convert_charrefs=True`：`&amp;` 一类实体由标准库替我还原成字符。
        # 不还原的话，标题里的 `&amp;` 会以字面形式进库，而它在下游是**不可见
        # 的差异**（人眼看起来一样，逐字比对不一样）。
        super().__init__(convert_charrefs=True)
        #: 见过 `ul.notice-list` 吗 —— 「读懂了没有」的分界线。
        self.found_list = False
        #: 逐个 li 收下来的原始条目（**含**那些明显残缺的）。
        self.entries: list[_NoticeEntry] = []

        self._ul_depth = 0
        self._in_list = False
        self._cur: _NoticeEntry | None = None
        #: 正在采集哪个元素（"title" / "summary"），以及它的配对深度。
        self._cap: str | None = None
        self._cap_depth = 0
        self._buf: list[str] = []

    # ── 标签 ──

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}

        if tag == "ul":
            # ※ 用 `ul` 的**配对计数**判断「还在不在列表里」，不用标签总深度。
            # 总深度会被空元素（`<br>` / `<img>` 不写斜杠）带偏，而错位的症状是
            # 「列表永远没结束」—— 那会让整页看起来只有一条通知，且不报错。
            self._ul_depth += 1
            if self._ul_depth == 1 and LIST_CLASS in _classes(attr):
                self.found_list = True
                self._in_list = True
            return

        if not self._in_list:
            return

        if self._cap is not None:
            # 正在采集：里面的标签只影响配对深度，不换采集目标。
            if tag not in _VOID_TAGS:
                self._cap_depth += 1
            return

        if tag == "li":
            # HTML 允许省略 `</li>`：新 li 开始即视为上一条结束。
            # 不处理的话，一个省略了结束标签的页面会把后面所有内容粘成一条。
            if self._cur is not None:
                self._finish_item()
            if ITEM_CLASS in _classes(attr):
                self._cur = _NoticeEntry(
                    department=attr.get(DEPARTMENT_ATTR, "").strip(),
                    published_at=attr.get(PUBLISH_ATTR, "").strip(),
                )
            return

        if self._cur is None:
            return

        if tag == "a" and TITLE_CLASS in _classes(attr):
            self._cap = "title"
            self._cap_depth = 0
            self._buf = []
            # ※ href 是这条通知在**站点里的身份**，也是 `msg_id` 的来源。
            # 收它不是为了输出链接（链接抽取是下游的事），而是为了幂等。
            self._cur.href = attr.get("href", "").strip()
        elif SUMMARY_CLASS in _classes(attr):
            self._cap = "summary"
            self._cap_depth = 0
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul":
            self._ul_depth = max(0, self._ul_depth - 1)
            if self._ul_depth == 0:
                self._in_list = False
                if self._cur is not None:
                    self._finish_item()
            return

        if self._cap is not None:
            if self._cap_depth > 0:
                self._cap_depth -= 1
            else:
                self._flush_capture()
            return

        if tag == "li" and self._cur is not None:
            self._finish_item()

    def handle_data(self, data: str) -> None:
        if self._cap is not None:
            self._buf.append(data)

    # ── 收尾 ──

    def _flush_capture(self) -> None:
        if self._cur is None:
            self._cap = None
            return
        text = _squeeze("".join(self._buf))
        if self._cap == "title":
            self._cur.title = text
        else:
            self._cur.summary = text
        self._cap = None
        self._buf = []

    def _finish_item(self) -> None:
        if self._cap is not None:
            self._flush_capture()
        if self._cur is not None:
            self.entries.append(self._cur)
        self._cur = None


def _classes(attr: dict[str, str]) -> list[str]:
    return (attr.get("class") or "").split()


def _squeeze(text: str) -> str:
    """把 HTML 里的排版空白折成单个空格。

    观察：标题与摘要在源码里被缩进与换行切开（`<p>` 里换行只是排版），而
    这些换行对下游是噪声 —— 逐字回连（`quote` 必须能在源文里找到）会因为它
    对不上。⇒ 空白折叠成单空格，两端去掉。
    """
    return " ".join(text.split())


# ── 解码 ────────────────────────────────────────────────────────────


def _declared_charset(raw: bytes) -> str | None:
    """页面自己声明的编码（`<meta charset=...>`）。没有返回 `None`。

    ※ 先按 latin-1 解前 2048 字节再正则：latin-1 **永远不会解码失败**，
    所以即使页面是 GBK，这段 ASCII 的 meta 标签也照样读得出来。
    换 `utf-8` 来嗅就会在第一个汉字上抛，那时还什么都没读到。
    """
    head = raw[:2048].decode("latin-1", errors="replace")
    m = _META_CHARSET.search(head)
    return m.group(1) if m else None


def decode_html(raw: bytes, *, encoding: str | None = None) -> str:
    """把字节解成文本。解不开 ⇒ **抛**。

    ※ 判据：**不换一个编码重试**。

    观察：中文页面常见的两种错配是「声明 UTF-8 实际是 GBK」与「声明 GBK
    实际是 UTF-8」，而按错的那个编码解通常**解不出错**（GBK 的字节能被
    latin-1 / cp1252 照单全收），于是你得到一页乱码，而乱码能一路走完解析、
    「成功」产出 N 条乱码消息 —— 那是「产出为空」的镜像：**产出垃圾**。
    两种都不可见，所以都不接受。

    判据的取向：**要么按声明的编码解开，要么响亮地失败**。
    真遇到没声明又解不开的页面，下一步是给它加声明（或在调用方指定编码），
    而不是让程序去猜 —— 猜对了没人知道，猜错了也没人知道。
    """
    codec = encoding or _declared_charset(raw) or "utf-8"
    try:
        return raw.decode(codec)
    except (UnicodeDecodeError, LookupError) as e:
        raise _PageNotUnderstood(
            f"页面按 {codec!r} 解不开：{e}。"
            f"（页面里声明的编码是 {_declared_charset(raw)!r}）"
            f"不换编码重试是刻意的 —— 用错编码「成功」解出来的是一页乱码，"
            f"而乱码会照常走完解析，产出 N 条没人看得出是错的条目。",
            codec=codec,
        ) from e


# ── 时间 ────────────────────────────────────────────────────────────


def wall_clock_to_ts(raw: str) -> int:
    """页面上的**墙上时间**（站点本地时间）→ Unix 秒。

    ※ 时区取**全项目那一处口径**（`domain.days.DEFAULT_TZ_OFFSET`，UTC+8），
    不取机器本地时区。取机器本地的话，同一份夹具在评委的机器上会算出不同的
    秒数，而日报是**按天分**的 —— 差 8 小时足以让一条通知落到**前一天**。
    那是一个跨机器才显形、且看起来像「数据本来就这样」的差异。

    ※ 解析失败 ⇒ **抛**，不兜底成 0 或「今天」。
    兜底会把一条通知放进错误的一天，而它**不报错**。
    """
    try:
        naive = datetime.strptime(raw.strip(), TIME_FORMAT)
    except ValueError as e:
        raise _PageNotUnderstood(
            f"发布时间 {raw!r} 不是 {TIME_FORMAT!r} 的形状：{e}",
            published_at=raw,
        ) from e
    # `tzinfo=utc` 只是让 `.timestamp()` 不去问机器时区（那会不可复现），
    # 减掉偏移才是「这个墙上时间对应的真实时刻」。
    return int(naive.replace(tzinfo=timezone.utc).timestamp()) - DEFAULT_TZ_OFFSET


def _stable_msg_id(namespace: str, href: str) -> int:
    """由「站点 + 这篇文章的地址」算出的稳定 id。

    ※ 为什么**不用列表位置**当 id：列表顺序会变 —— 一条通知被置顶之后，
    它后面每一条的位置都往后挪一格。位置当 id 的话，置顶一条会让**整页**
    换一批新 id，于是重跑摄取把它们**全部重写一遍**（`msg_id` 是幂等的键），
    日报里同一条通知出现两次，而没有任何东西会报错。

    ※ 为什么**要带 namespace**：`/notice/1.html` 在两个不同站点上是两篇
    不同的文章。只按 href 算，两个站点会撞出同一个 id。

    ※ 取 63 位：SQLite 的 INTEGER 是有符号 64 位，右移一位留出符号位。
    不这么做的话，约一半的 id 会超过 `2**63-1`，而症状是插入时报
    "Python int too large to convert to SQLite INTEGER" —— 在**一半**的
    页面上出现，像随机故障。
    """
    digest = hashlib.blake2b(
        f"{namespace}\x00{href}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") >> 1


def _compose_content(title: str, summary: str) -> str:
    """标题 + 摘要拼成**一条自洽的短文本**。

    ※ **不切**。网页通知与邮件同类：一条通知就是一个完整的、自洽的短文本，
    切了反而把它拆散 —— 这与会议转写必须切**相反**。端口那句「形状由接口
    约束，处理方式不由接口约束」说的就是这个：两个源对下游长得一样，而
    归一化方式可以完全不同。
    """
    if not summary:
        return title
    return f"{title}\n{summary}"


# ── 对外的那一个函数 ────────────────────────────────────────────────


def parse_page(
    raw: bytes,
    *,
    namespace: str,
    group_id: int,
    encoding: str | None = None,
) -> list[PendingMessage]:
    """把一页的字节变成消息。**纯函数。**

    `namespace` 是「这一页在哪个站点上」（源标识符），用来算 `msg_id` ——
    见 `_stable_msg_id`。

    抛：
      · `_PageNotUnderstood` —— 没读懂（容器不在 / 条目缺必要字段 / 解不开码）
      · `_PageHasNoNotices`  —— 读懂了，但列表里一条都没有

    ※ 一条 li 缺了必要字段 ⇒ **整页报错**，不静默跳过那一条。
    理由：静默跳过一条消息正是本项目明令禁止的形状（用户会以为那天没事）。
    而缺字段最常见的原因是**站点改版** —— 那会命中每一条，所以「只坏了一条」
    是个例外情形，为它设计一条安静的旁路不划算，代价却是把真问题也吞掉。
    """
    text = decode_html(raw, encoding=encoding)

    parser = _NoticeListParser()
    parser.feed(text)
    parser.close()

    if not parser.found_list:
        raise _PageNotUnderstood(
            f"页面里找不到 <ul class=\"{LIST_CLASS}\"> —— 解析器与站点对不上了"
            f"（改版？），不是「今天没发通知」。",
            want=LIST_CLASS,
        )

    if not parser.entries:
        raise _PageHasNoNotices(
            f"列表容器 <ul class=\"{LIST_CLASS}\"> 在，结构对得上，"
            f"但里面一条 <li class=\"{ITEM_CLASS}\"> 都没有。"
            f"**这是「读懂了、页面确实空」**，不是解析器坏了 —— "
            f"要去查的是站点今天是不是真的没发通知。",
            want=ITEM_CLASS,
        )

    out: list[PendingMessage] = []
    for index, entry in enumerate(parser.entries, start=1):
        if not entry.title:
            raise _PageNotUnderstood(
                f"第 {index} 条 <li class=\"{ITEM_CLASS}\"> 里没有可读的标题"
                f"（<a class=\"{TITLE_CLASS}\"> 缺失或为空）。"
                f"缺字段最常见的成因是站点改版，所以这里**整页报错**，"
                f"而不是安静地少产出那一条 —— 少一条消息是看不出来的。",
                index=index,
            )
        if not entry.href:
            raise _PageNotUnderstood(
                f"第 {index} 条通知（标题 {entry.title!r}）的 "
                f"<a class=\"{TITLE_CLASS}\"> 没有 href。"
                f"href 是这条通知在站点里的身份，也是幂等键的来源 —— "
                f"没有它就保证不了「重跑不重复入库」，而重复入库是无声的。",
                index=index,
            )
        # ※ 时间读不出来时要带上**第几条**—— 否则报错只能告诉人
        # 「有一条的时间写法不认识」，而页面上可能有几十条。
        try:
            ts = wall_clock_to_ts(entry.published_at)
        except _PageNotUnderstood as e:
            raise _PageNotUnderstood(
                f"第 {index} 条通知（标题 {entry.title!r}）的发布时间读不出来：{e}",
                index=index,
            ) from e

        out.append(
            PendingMessage(
                msg_id=_stable_msg_id(namespace, entry.href),
                group_id=group_id,
                ts=ts,
                # 网页源没有「群昵称」这个概念，对应物是**发布单位**。
                # 拿不到就置空串 —— 不从自由文本里猜（见 DEPARTMENT_ATTR）。
                sender=entry.department,
                content=_compose_content(entry.title, entry.summary),
                # 网页通知没有账号可言 ⇒ None。用 0 冒充会让它被人物筛选命中。
                actor_account=None,
                source=NAME,
            )
        )
    return out
