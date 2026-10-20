"""IMAP 入口 —— **只读**地直连信箱收信。

## 为什么要这一条路

让用户先手动导出 `.eml` / `.mbox` 再让程序读，那还是**用户操作** ——
「零操作」要求程序自己去信箱里取。

而一旦程序开始碰用户的邮箱，最大的风险不是读不到，是**写坏了**：
把信标成已读、移进别的文件夹、删掉。用户发现不了（信还在，只是「已读」了），
而那是**用他的账号**做的动作。

## 只读：三条，各自可被机械检查

| 做什么 | 不做什么 | 检查方式 |
|---|---|---|
| `EXAMINE` 打开信箱 | `SELECT` | 假客户端记下发出去的动词 |
| `BODY.PEEK[]` 取正文 | `BODY[]` | 同上（`BODY[]` 会把信标成「已读」） |
| 收完 `LOGOUT` | `STORE` / `COPY` / `EXPUNGE` / `APPEND` / `CLOSE` | 假客户端记下的动词集合里**一个都不许出现** |

※ 为什么把 `CLOSE` 也算进禁用名单：按 RFC 3501，`CLOSE` 在 SELECTED 状态下
**隐含一次 EXPUNGE**。我们从不设 `\\Deleted`，所以它此刻无害 —— 但「此刻无害」
不是判据，**不出现**才是。收尾用 `LOGOUT`。

※ 第 1、2 条管的是「我们主动发的命令」，第 3 条管的是「哪天有人加了一句」。

## 口令只在内存里

`YECHAO_IMAP_PASS` 是口令的**唯一**来源，而且：

  · **不进配置文件**（配置文件会被导出、会被提交 —— 与 `YECHAO_API_KEY` 同理）
  · **不进异常消息**（**结构上不拼**；`_redact` 只是兜底）
  · **不进日志、不进审计**（本模块不打日志、不写文件）

`_Credential.password` 带 `field(repr=False)`，所以 `repr()`、f-string 里的
`!r`、pickle 都不会把它带出来。

## 账号与口令**不写进 URL**

目标串只回答「**去哪儿读**」（host / port / 信箱），账号与口令只从环境变量来。
理由是可观察的：URL 会进 shell 历史、进进程列表、进报错消息 —— 而这三处都
不该有口令。URL 里出现 `user:pass@` 时**直接抛**，不静默忽略（静默忽略会让
用户以为他配的那个账号生效了）。

## 增量：窗口**下推**到服务器，但**精确过滤在本地**

`UID SEARCH SINCE <d1> BEFORE <d2>` 只取窗内的信（不下载整个信箱）。

※ 下推的那一层是**超集**，不是等号：IMAP 的 `SINCE`/`BEFORE` 按
`INTERNALDATE` 的**天**比较，而且那个日期用的是**服务器自己的时区** ——
我们既控制不了它的时区，也拿不到它的日界。所以两端各留一天的余量，
**精确的半开区间由本地再过一次**（`mail_source.read` 用 `_common.in_window`，
与文件那条路同一个函数）。

超集是安全的：多取几封只会被本地滤掉。**漏取**才是不可见的失败。

## 日期格式：月份表**写死**，不用 `strftime("%b")`

IMAP 的日期是 `DD-Mon-YYYY`，月份是**英文三字母缩写**（RFC 3501）。
`strftime("%b")` 跟着 **locale** 走 —— 在中文 Windows 上它给出「10月」，
而那个查询会被服务器拒掉、或者更糟：**静默匹配错**。
所以月份表是写死的英文缩写（`_MONTHS`），`_imap_date()` 自己拼。
"""

from __future__ import annotations

import imaplib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import ConfigError, SourceFormatError, SourceUnavailable

# ── 环境变量 ────────────────────────────────────────────────────────
#
# 五个变量分两类，这个分法就是「凭据不进配置文件」那条判据的落点：
#
#   去哪儿（不机密，可以进 URL / 配置文件）：URL / HOST / PORT
#   是谁  （机密，**只在环境变量里**）      ：USER / PASS

#: 一条完整的连接串。写它就等于「我要走 IMAP」。
IMAP_URL_ENV = "YECHAO_IMAP_URL"
#: 只给主机（不给整条 URL）时用它。信箱取 `DEFAULT_MAILBOX`。
IMAP_HOST_ENV = "YECHAO_IMAP_HOST"
#: 端口。不给取 `DEFAULT_PORT`。
IMAP_PORT_ENV = "YECHAO_IMAP_PORT"
#: 账号。
IMAP_USER_ENV = "YECHAO_IMAP_USER"
#: 口令。**唯一来源**，且只留在内存里（见模块 docstring）。
IMAP_PASS_ENV = "YECHAO_IMAP_PASS"

#: 只认这一种 scheme。
#:
#: ※ `imap://`（明文 143）**不认** —— 它会把口令原样发到网络上，而这条路上
#: 没有「降级」这个选项：认它等于把一个明文口令的路径做成默认可达的。
#: 报错里说清这一点，而不是静默当成 `imaps://`。
IMAP_SCHEME = "imaps"
#: 默认端口。993 是 IMAPS 的注册端口。
DEFAULT_PORT = 993
#: 不给信箱名时读哪个。INBOX 是 RFC 3501 规定的、**大小写不敏感**的收件箱名。
DEFAULT_MAILBOX = "INBOX"

#: 单次连接超时（秒）。不设的话一个卡住的连接会挂住整个摄取。
DEFAULT_TIMEOUT = 15.0

#: IMAP 日期的月份缩写。**写死的**，理由见模块 docstring（locale）。
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

#: 一天的秒数。窗口下推时两端的余量用它。
_DAY = 86400

#: 会改邮箱状态的动词 —— 本模块**一个都不发**。见模块 docstring 那张表。
#:
#: ※ 它在**测试**里被用来断言「发出去的动词集合与它不相交」，所以这份名单
#: 是判据的一部分，不是文档。改小它等于放宽那条判据。
MUTATING_COMMANDS = frozenset(
    {
        "STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "CREATE",
        "DELETE", "RENAME", "SUBSCRIBE", "UNSUBSCRIBE", "CLOSE",
        "SETACL", "DELETEACL", "SETQUOTA", "SETANNOTATION",
    }
)

#: 口令短于这个长度就不做替换（替换会把消息里每个该字符都换成 `***`）。
#: 真正的保证在**结构**上：口令从不被拼进任何消息。
MIN_REDACT_CHARS = 4


# ── 三种「读不到」 ──────────────────────────────────────────────────


class _ImapUnavailable(SourceUnavailable):
    """连不上（DNS 失败、连接被拒、TLS 握手失败、超时、服务器中途断开）。

    ※ 与「认证失败」分开成两个类型，因为**下一步不同**：
    这一个去查网络与主机名，那一个去查账号口令。
    """

    default_message = "连不上 IMAP 服务器"


class _ImapAuthFailed(SourceUnavailable):
    """连上了，但**认证没过**。

    ※ 它**不是** `ConfigError`：账号口令来自环境变量，而「改环境变量重跑」
    与「改配置文件重跑」是两件事 —— 后者是用法错（退出码 2），前者是这次
    运行没成（退出码 1）。用 `ConfigError` 会让 CLI 报「去改配置」，
    而配置里根本没有口令。
    """

    default_message = "IMAP 认证失败"


class _ImapMailboxUnavailable(SourceUnavailable):
    """认证过了，但**这个信箱打不开**（名字写错 / 没有权限）。

    ※ 与「认证失败」分开：那一个错在**账号**，这一个错在**信箱名**。
    """

    default_message = "IMAP 信箱打不开"


class _ImapNoContent(SourceFormatError):
    """服务器说这个窗口里有 N 封，但**一封都没取回来**。

    ※ 这是端口那条「**解析出 0 条就抛**」在 IMAP 这条路上的落点，而且它与
    「这段时间确实没有信」**形状不同**：

      · 服务器说 0 封        ⇒ 返回空（那是「这段时间确实没有信」）
      · 服务器说 N 封、取回 0 ⇒ **抛**（那是我读到了却读不出东西）

    两者混成一个空列表的话，一个坏掉的 FETCH 会显示成「今天邮箱很安静」。
    """

    default_message = "服务器说有信，但一封都没取回来"


# ── 端点与凭据 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ImapEndpoint:
    """**去哪儿读**。不含任何凭据 —— 见模块 docstring。"""

    host: str
    port: int
    mailbox: str
    #: 用户写的那个串（报错时原样回显）。它**保证不含账号口令** ——
    #: 带 userinfo 的 URL 在 `parse_imap_url` 里就被拒了。
    raw: str

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}/{self.mailbox}"


@dataclass(frozen=True)
class _Credential:
    """**是谁**。口令带 `repr=False`，所以 `repr()` 与 f-string 的 `!r` 都不带它。

    ※ 这只是第一道；**主要防线是结构**：口令从不被拼进任何消息。
    `field(repr=False)` 防的是「某天有人把凭据对象塞进了异常的 context」。
    """

    user: str
    password: str = field(default="", repr=False)

    def __reduce__(self) -> tuple[Any, tuple[str, str]]:
        """pickle / copy 时不带口令。

        ※ 一个把口令序列化进去的对象，会在**进程外**留下它的副本 ——
        崩溃转储、消息队列、缓存，都算。而那些地方没人会去看第二眼。
        """
        return (_Credential, (self.user, ""))


def _redact(text: str, secrets: Iterable[str]) -> str:
    """把秘密从一段文本里抹掉。**兜底**，不是主要防线。

    ※ 主要防线是**结构**：口令从不被拼进任何消息。这个函数防的是
    「某天有人加了一句 `f"... {password}"`」—— 那种改动看起来无害，
    而它的后果是凭据进了日志。
    """
    out = text
    for s in secrets:
        if s and len(s) >= MIN_REDACT_CHARS:
            out = out.replace(s, "***")
    return out


def parse_imap_url(raw: str) -> _ImapEndpoint:
    """连接串 → 端点。**不解析凭据** —— 带了就直接抛。

    ※ 判据是「URL 里出现的账号口令会被复制到 shell 历史、进程列表与报错
    消息里」，而那三处都不该有口令。所以这里不是「忽略它」，是**拒掉它**：
    忽略会让用户以为他写进 URL 的那个账号生效了。
    """
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme != IMAP_SCHEME:
        raise ConfigError(
            f"邮件源的 IMAP 目标只认 {IMAP_SCHEME}:// 开头的连接串，"
            f"而这个是 {raw!r}（scheme={scheme or '（没有）'!r}）。\n"
            f"※ `imap://`（明文）**不认**：它会把口令原样发到网络上，"
            f"而这条路上没有「降级」这个选项。"
        )
    if parts.username or parts.password:
        # ※ **不回显**那一段 userinfo：它正是口令所在的地方，而报错会进
        # 日志与终端回滚。只报主机名，够用户认出是哪一条配置了。
        raise ConfigError(
            f"IMAP 连接串里**不许写账号或口令**"
            f"（{parts.scheme}://{parts.hostname or '（没有主机名）'} 那一条）。\n"
            f"※ 连接串只回答「去哪儿读」（host / port / 信箱）。账号与口令只从"
            f"环境变量来 —— {IMAP_USER_ENV} 与 {IMAP_PASS_ENV}。\n"
            f"※ 原因不是洁癖：写在 URL 里的口令会进 shell 历史、进进程列表、"
            f"进报错消息，而这三处都会被别人看到。"
        )
    host = (parts.hostname or "").strip()
    if not host:
        raise ConfigError(f"IMAP 连接串里没有主机名：{raw!r}")
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as e:
        raise ConfigError(f"IMAP 连接串里的端口不是数字：{raw!r}（{e}）") from e
    mailbox = unquote(parts.path.lstrip("/")).strip() or DEFAULT_MAILBOX
    return _ImapEndpoint(host=host, port=port, mailbox=mailbox, raw=raw)


def resolve_credential() -> _Credential:
    """从环境变量读账号与口令。**缺一个就抛，不兜底。**

    ※ 口令**不 strip**：口令两端可以有空格，而 strip 掉的症状是
    「同一个口令在我这儿能登、在你那儿登不上」—— 一句让人去查错方向的话。
    """
    user = (os.environ.get(IMAP_USER_ENV) or "").strip()
    password = os.environ.get(IMAP_PASS_ENV) or ""
    if not user or not password:
        missing = []
        if not user:
            missing.append(IMAP_USER_ENV)
        if not password:
            missing.append(IMAP_PASS_ENV)
        raise ConfigError(
            f"IMAP 要账号与口令，而 {'、'.join(missing)} 没配。\n"
            f"※ 它们**只从环境变量读**，不写进配置文件：配置文件会被导出、"
            f"会被提交，而口令放进去就是泄漏。\n"
            f"    {IMAP_USER_ENV}=<账号>\n    {IMAP_PASS_ENV}=<口令>"
        )
    return _Credential(user=user, password=password)


# ── 日期 ────────────────────────────────────────────────────────────


def _imap_date(ts: int) -> str:
    """Unix 秒 → IMAP 的 `DD-Mon-YYYY`（**UTC+8 的那一天**）。

    ※ 不用 `strftime("%b")`：它跟着 locale 走，在中文 Windows 上给出「10月」。
    那个查询要么被服务器拒掉，要么**静默匹配错** —— 后者不可见。

    ※ 不用 `fromtimestamp()`：它跟着**机器时区**走，而同一份查询在评委的
    机器上会变成另一天。从 1970-01-01 起算 `timedelta` 不碰任何本地时区。
    """
    day = (ts + DEFAULT_TZ_OFFSET) // _DAY
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year:04d}"


def _search_criteria(since: int | None, until: int | None) -> list[str]:
    """时间窗 → `SEARCH` 的判据。**两端各留一天余量**（见模块 docstring）。

    ※ 余量是「超集」的实现，不是保守：本地还会用 `in_window` 精确过一次。
    少留这一天，一封在服务器时区的日界附近、而落在我们窗口里的信会被
    **静默漏掉** —— 而漏掉一封信是看不出来的。
    """
    criteria: list[str] = []
    if since is not None:
        criteria += ["SINCE", _imap_date(since - _DAY)]
    if until is not None:
        # `BEFORE d` = 内部日期 **早于** d 那一天。要覆盖 `until` 那一整天，
        # 得取到 `until` 的次日；再加一天的余量。
        criteria += ["BEFORE", _imap_date(until + 2 * _DAY)]
    if not criteria:
        criteria = ["ALL"]
    return criteria


# ── 连接与收信 ──────────────────────────────────────────────────────


def _connect(endpoint: _ImapEndpoint, credential: _Credential, *, timeout: float) -> Any:
    """连上并登录。**失败分两种**（见那两个异常类）。"""
    try:
        conn = imaplib.IMAP4_SSL(endpoint.host, endpoint.port, timeout=timeout)
    except Exception as e:  # noqa: BLE001 —— 见下
        # ※ 这里**故意宽**：构造函数里会发生的事横跨 socket（DNS / 拒绝 /
        # 超时）、ssl（握手 / 证书）、imaplib（问候语不是 OK）三族，而它们
        # 的公共父类只有 `Exception`。窄捕获的后果是漏出一种，而那种失败会
        # 以 traceback 的样子出现 —— 看起来像「程序坏了」，不是「连不上」。
        # 消息**不含**凭据：这一步还没登录。
        raise _ImapUnavailable(
            f"连不上 IMAP 服务器 {endpoint.host}:{endpoint.port}：{e}",
            host=endpoint.host, port=endpoint.port,
        ) from e

    try:
        conn.login(credential.user, credential.password)
    except imaplib.IMAP4.error as e:
        # ※ 服务器给的这段文本可能回显请求（正常不回显口令，但**不由我们
        # 决定**）。所以它先过 `_redact` 再进消息 —— 兜底，不是主防线。
        raise _ImapAuthFailed(
            f"IMAP 认证失败（{endpoint.host}:{endpoint.port}，账号 "
            f"{credential.user}）：{_redact(str(e), [credential.password])}",
            host=endpoint.host, port=endpoint.port,
        ) from e
    except Exception as e:  # noqa: BLE001 —— 登录途中掉线 / 超时：那是「连不上」
        raise _ImapUnavailable(
            f"登录 IMAP 服务器 {endpoint.host}:{endpoint.port} 时连接断了："
            f"{_redact(str(e), [credential.password])}",
            host=endpoint.host, port=endpoint.port,
        ) from e
    return conn


def _examine(conn: Any, endpoint: _ImapEndpoint) -> None:
    """**只读**打开信箱。

    ※ `readonly=True` 是 `imaplib` 把命令翻成 `EXAMINE` 的开关
    （`select()` 的源码里就是这一句）。用 `SELECT` 会让服务端认为我们要写，
    而「认为要写」本身就够让某些服务器改状态。
    """
    try:
        typ, dat = conn.select(endpoint.mailbox, readonly=True)
    except Exception as e:  # noqa: BLE001 —— 断线 / 服务器拒绝
        raise _ImapMailboxUnavailable(
            f"信箱 {endpoint.mailbox!r} 打不开：{e}",
            mailbox=endpoint.mailbox,
        ) from e
    if typ != "OK":
        raise _ImapMailboxUnavailable(
            f"信箱 {endpoint.mailbox!r} 打不开（EXAMINE 回了 {typ}）："
            f"{_first_text(dat)}",
            mailbox=endpoint.mailbox,
        )


def _search(conn: Any, criteria: list[str]) -> list[bytes]:
    """`UID SEARCH` —— 只拿 **UID**，不拿序号。

    ※ UID 在信箱里是**稳定**的，而序号会随着别人删信而整体前移。用序号的话，
    「先 SEARCH 再 FETCH」之间的一次并发删除，会让取回来的信**错位** ——
    而这不会报错，只是内容对不上。
    """
    try:
        typ, dat = conn.uid("SEARCH", *criteria)
    except Exception as e:  # noqa: BLE001
        raise _ImapUnavailable(f"在信箱里检索失败：{e}") from e
    if typ != "OK":
        raise _ImapUnavailable(
            f"在信箱里检索失败（SEARCH 回了 {typ}）：{_first_text(dat)}"
        )
    uids: list[bytes] = []
    for chunk in dat:
        if isinstance(chunk, bytes):
            uids.extend(chunk.split())
    return uids


def _fetch(conn: Any, uids: list[bytes]) -> list[bytes]:
    """逐封取回**原始字节**。用 `BODY.PEEK[]`，**不是** `BODY[]`。

    ※ `BODY[]` 的副作用是把这封信标成「已读」—— 那是**对用户邮箱的写**，
    而用户看不见它发生。`PEEK` 就是为这件事存在的。
    """
    out: list[bytes] = []
    for uid in uids:
        try:
            typ, dat = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        except Exception as e:  # noqa: BLE001
            raise _ImapUnavailable(f"取信失败（UID {uid!r}）：{e}") from e
        if typ != "OK":
            raise _ImapUnavailable(
                f"取信失败（UID {uid!r}，FETCH 回了 {typ}）：{_first_text(dat)}"
            )
        out.extend(_literals(dat))
    return out


def _literals(dat: Any) -> list[bytes]:
    """从 FETCH 的响应里抠出**字面量**（就是那封信的原始字节）。

    ※ imaplib 的形状是 `[(b'1 (UID 1 BODY[] {123}', b'<123 字节>'), b')']` ——
    只有元组的第二项是信本身。第一项是响应头（含长度），把它当正文会得到
    一封「开头是 IMAP 协议文本」的邮件。
    """
    out: list[bytes] = []
    for item in dat or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            out.append(item[1])
    return out


def _first_text(dat: Any) -> str:
    """响应里第一段可读文本，给报错用。"""
    for item in dat or []:
        if isinstance(item, bytes):
            return item.decode("utf-8", "replace")[:200]
        if isinstance(item, tuple) and item and isinstance(item[0], bytes):
            return item[0].decode("utf-8", "replace")[:200]
    return ""


def _logout(conn: Any) -> None:
    """收尾。**不用 `CLOSE`** —— 它在 SELECTED 状态下隐含一次 EXPUNGE。"""
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        # ※ 收尾失败**不覆盖**已经拿到的结果，也不改变退出码：这是一次
        # 只读会话，没关干净不留任何状态（我们没设过任何 flag）。
        pass


def fetch_window(
    endpoint: _ImapEndpoint,
    *,
    since: int | None = None,
    until: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[bytes]:
    """取回窗口内的信的**原始字节**。**只读**。

    返回 `[]` 的含义是「**服务器说这个窗口里没有信**」—— 那是数据事实，
    不是失败。服务器说有几封、却一封都没取回来时**抛** `_ImapNoContent`。

    ※ 返回的是**超集**：两端各留了一天余量（见模块 docstring），精确的
    半开区间由调用方再滤一次。这与文件那条路「先解析、再过滤」的分工一致。
    """
    credential = resolve_credential()
    conn = _connect(endpoint, credential, timeout=timeout)
    try:
        _examine(conn, endpoint)
        uids = _search(conn, _search_criteria(since, until))
        if not uids:
            # 「这段时间确实没有信」—— **返回空，不抛**。与文件那条路
            # 「窗口内 0 条只是返回空」是同一条判据。
            return []
        raws = _fetch(conn, uids)
    finally:
        _logout(conn)

    if not raws:
        raise _ImapNoContent(
            f"服务器说 {endpoint.where} 在窗口内有 {len(uids)} 封，"
            f"但一封都没取回来 —— 这是「读到了却读不出东西」，"
            f"不是「这段时间没有信」。",
            uids=len(uids),
        )
    return raws
