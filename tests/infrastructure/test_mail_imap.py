"""IMAP 入口 —— **只读**的机械判据、凭据的边界、以及增量窗口。

## 这个文件**不连任何服务器**

真 IMAP 没有账号可配，所以全程用一个**假连接**：它记录每一条收到的命令、
回放预设的响应。`mail_imap.imaplib` 被换成一个只有 `IMAP4_SSL` 与 `error`
两样东西的假模块 —— 换掉整个模块而不是 `imaplib.IMAP4_SSL` 一个属性，
是为了**不污染全局的 `imaplib`**（别的用例、别的模块可能正在用它）。

※ 假连接**显式实现了**我们该发的那些方法，剩下的走 `__getattr__` 记账。
所以「哪天有人加了一句 `conn.store(...)`」不会 `AttributeError` 被吞掉，
而是**留在命令表里**等着被断言 —— 那条断言是白名单（见
`test_只读会话里只出现这几个动词`）。

## 样本全部自己编

假域名 `example.test`（RFC 2606 保留）、假账号、假口令。
"""

from __future__ import annotations

import ast
import imaplib
import types
from pathlib import Path

import pytest

from yechao.domain.errors import (
    ConfigError,
    SourceFormatError,
    SourceUnavailable,
)
from yechao.infrastructure.sources import consent, mail_imap, mail_parse, mail_source

夹具_eml = Path(mail_source.__file__).resolve().parent / "fixtures" / "mail-notices.eml"

#: 假口令。**它绝不该出现在任何消息里** —— 几条测试专门找它。
假口令 = "yechao-fake-pass-1770000100X"
假账号 = "notice@example.test"

#: 一个自己编的连接串。`example.test` 是保留域名，永远不会解析。
假URL = "imaps://imap.example.test:993/INBOX"


# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture()
def 干净的配置(tmp_path, monkeypatch):
    """把配置目录与**全部**相关环境变量都指到干净的地方。

    ※ 不这么做的话，用例会读到开发机上的 `sources.toml` 或某个残留的
    `YECHAO_IMAP_*` —— 于是「没配目标时抛」这条在别人机器上会因为
    「他恰好配了」而失败，而那种失败看起来像代码坏了。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(mail_source.CONFIG_DIR_ENV, str(cfg))
    for 变量 in (
        mail_source.TARGET_ENV,
        mail_source.GROUP_ID_ENV,
        mail_imap.IMAP_URL_ENV,
        mail_imap.IMAP_HOST_ENV,
        mail_imap.IMAP_PORT_ENV,
        mail_imap.IMAP_USER_ENV,
        mail_imap.IMAP_PASS_ENV,
    ):
        monkeypatch.delenv(变量, raising=False)
    return cfg


@pytest.fixture()
def 签了协议(干净的配置):
    """写一份**当前版本**的接受记录。"""
    (干净的配置 / consent.CONSENT_FILE).write_text(
        f"[{consent.PROTOCOL_ID}]\n"
        f'version = "{consent.PROTOCOL_VERSION}"\n'
        f"accepted_at = 1789348800\n",
        encoding="utf-8",
    )
    return 干净的配置


@pytest.fixture()
def 配好信箱(签了协议, monkeypatch):
    """一个完整可用的 IMAP 目标 + 凭据（**不连**）。"""
    monkeypatch.setenv(mail_imap.IMAP_URL_ENV, 假URL)
    monkeypatch.setenv(mail_imap.IMAP_USER_ENV, 假账号)
    monkeypatch.setenv(mail_imap.IMAP_PASS_ENV, 假口令)
    return 假URL


# ── 假连接 ──────────────────────────────────────────────────────────


class _假连接:
    """记录每一条收到的命令，回放预设的响应。

    ※ `select` 里那个 `EXAMINE / SELECT` 的映射**与标准库逐字相同**
    （`imaplib.IMAP4.select` 里就是 `name = 'EXAMINE' if readonly else 'SELECT'`），
    所以「我们记下的动词」就是「真的会发出去的动词」。这条映射本身另有一条
    测试从标准库源码里读一遍（`test_只读打开的_EXAMINE_映射来自标准库`）。
    """

    def __init__(
        self,
        *,
        uids: bytes = b"",
        每封: bytes = b"",
        信箱响应: tuple = ("OK", [b"1"]),
        登录时抛: Exception | None = None,
    ) -> None:
        self.命令: list[tuple] = []
        self.已登出 = False
        self._uids = uids
        self._每封 = 每封
        self._信箱响应 = 信箱响应
        self._登录时抛 = 登录时抛

    # ── 我们该发的那些 ──

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple:
        self.命令.append(("EXAMINE" if readonly else "SELECT", mailbox))
        return self._信箱响应

    def login(self, user: str, password: str) -> tuple:
        self.命令.append(("LOGIN", user))
        if self._登录时抛 is not None:
            raise self._登录时抛
        return "OK", [b"LOGIN completed"]

    def uid(self, command: str, *args: object) -> tuple:
        self.命令.append((f"UID {command.upper()}", *args))
        if command.upper() == "SEARCH":
            return "OK", [self._uids]
        if command.upper() == "FETCH":
            uid = args[0]
            return "OK", [
                (b"%s (UID %s BODY[] {%d}" % (uid, uid, len(self._每封)), self._每封),
                b")",
            ]
        raise AssertionError(f"意外的 UID 子命令：{command}")

    def logout(self) -> tuple:
        self.命令.append(("LOGOUT",))
        self.已登出 = True
        return "BYE", [b"bye"]

    # ── 我们**不该**发的那些：记账，然后交给断言 ──

    def __getattr__(self, name: str):
        def _记账(*args: object, **kwargs: object) -> tuple:
            self.命令.append((name.upper(), args))
            return "OK", [b""]

        return _记账


def _装上(monkeypatch, *, 连接=None, 连接时抛: Exception | None = None) -> dict:
    """把 `mail_imap.imaplib` 换成一个假模块。返回一个记录 host/port 的字典。"""
    记录: dict = {"host": None, "port": None, "timeout": None}

    def _开(host: str, port: int, timeout: float | None = None):
        记录["host"], 记录["port"], 记录["timeout"] = host, port, timeout
        if 连接时抛 is not None:
            raise 连接时抛
        return 连接

    monkeypatch.setattr(
        mail_imap,
        "imaplib",
        # ※ `IMAP4` 也要给：`mail_imap` 里写的是 `except imaplib.IMAP4.error`，
        # 读的是**模块的 `IMAP4` 属性** —— 只给 `error` 会让那一句当场
        # `AttributeError`，而症状看起来像「代码坏了」，不是「假模块缺东西」。
        types.SimpleNamespace(
            IMAP4_SSL=_开, IMAP4=imaplib.IMAP4, error=imaplib.IMAP4.error
        ),
    )
    return 记录


# ── 只读：三条硬判据 ────────────────────────────────────────────────


def test_只读会话里只出现这几个动词(monkeypatch, 配好信箱) -> None:
    """**这是本模块最重要的一条判据。**

    我们允许自己发的命令，**列得出来**：登录、只读打开、按 UID 找、按 UID 取、
    登出。这是一张**白名单**，不是黑名单 —— 黑名单会随「哪天有人加了一个
    新动词」而静默失效，而白名单不会：新动词一出现，这条就红。

    ※ 用它挡的是**写**：`STORE`（打标）、`COPY` / `MOVE`（搬信）、
    `EXPUNGE`（删）、`APPEND`（投信）、`CLOSE`（隐含 EXPUNGE）。
    用户看不见这些动作发生 —— 信还在，只是「已读」了，或者已经不在原来那个
    文件夹里了。
    """
    连接 = _假连接(uids=b"1 2", 每封=b"From: a@example.test\n\nhi\n")
    _装上(monkeypatch, 连接=连接)

    mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    动词 = {c[0].removeprefix("UID ") for c in 连接.命令}
    assert 动词 == {"LOGIN", "EXAMINE", "SEARCH", "FETCH", "LOGOUT"}, (
        f"只读会话里出现了计划外的动词：{sorted(动词)}"
    )
    assert not (动词 & mail_imap.MUTATING_COMMANDS)


def test_只读打开走的是_EXAMINE_而不是_SELECT(monkeypatch, 配好信箱) -> None:
    """`select(..., readonly=True)` —— 不是 `SELECT`。"""
    连接 = _假连接(uids=b"1", 每封=b"x")
    _装上(monkeypatch, 连接=连接)

    mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    assert ("EXAMINE", "INBOX") in 连接.命令
    assert not [c for c in 连接.命令 if c[0] == "SELECT"], "用了 SELECT（那会以可写方式打开）"


def test_只读打开的_EXAMINE_映射来自标准库() -> None:
    """※ 上一条测的是**我们**记下的动词，而那个动词是假连接按标准库的映射算的。

    这一条把那个映射本身钉住：它从**标准库的源码**里读，所以哪天 CPython 把
    `readonly=True` 的翻法改了（或有人把 `readonly` 去掉），这里会红 ——
    而不是等到某台真服务器上用户的信被标成已读。

    ※ 它是**关于标准库**的判据，不是关于本模块的。红了的下一步是去读
    `imaplib.IMAP4.select` 的源码，不是去改这里的断言。
    """
    import inspect

    源码 = inspect.getsource(imaplib.IMAP4.select)
    assert "readonly" in 源码, "标准库的 select 不再认 readonly 了"
    assert "'EXAMINE'" in 源码, "标准库不再把 readonly=True 翻成 EXAMINE 了"
    assert "'SELECT'" in 源码


def test_取正文用_BODY_PEEK_而不是_BODY(monkeypatch, 配好信箱) -> None:
    """`BODY[]` 的副作用是把信标成「已读」—— 那是**对用户邮箱的写**，
    而用户看不见它发生。`PEEK` 就是为这件事存在的。"""
    连接 = _假连接(uids=b"7 8", 每封=b"x")
    _装上(monkeypatch, 连接=连接)

    mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    取信 = [c for c in 连接.命令 if c[0] == "UID FETCH"]
    assert [c[1] for c in 取信] == [b"7", b"8"], "没有按 UID 逐封取"
    assert all(c[2] == "(BODY.PEEK[])" for c in 取信), (
        f"取正文时用的不是 BODY.PEEK[]：{[c[2] for c in 取信]}"
    )
    assert not any("BODY[]" in str(c[2]) for c in 取信 if len(c) > 2), (
        "出现了 BODY[]（不带 PEEK）—— 那会把信标成已读"
    )


def test_收尾用_LOGOUT_而不是_CLOSE(monkeypatch, 配好信箱) -> None:
    """※ 按 RFC 3501，`CLOSE` 在 SELECTED 状态下**隐含一次 EXPUNGE**。

    我们从不设 `\\Deleted`，所以它此刻无害 —— 但「此刻无害」不是判据。
    """
    连接 = _假连接(uids=b"1", 每封=b"x")
    _装上(monkeypatch, 连接=连接)

    mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    assert 连接.已登出
    assert "CLOSE" not in {c[0] for c in 连接.命令}


def test_源码里不出现会改状态的方法名() -> None:
    """※ 上面几条测的是**这一次**发的命令。这一条管的是「哪天有人加了一句」。

    它扫的是**属性名**（`conn.store(...)` 里的 `store`），所以文档字符串里
    提到这些词不会误伤 —— 而一次真实调用会。
    """
    属性 = {
        node.attr.lower()
        for node in ast.walk(
            ast.parse(Path(mail_imap.__file__).read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.Attribute)
    }
    # ※ 只列**不可能出现在别处**的那些名字：`append` / `copy` / `close`
    # 是 list 与 str 的常用方法（`out.append(...)`），列进来这条判据就恒红，
    # 而一条恒红的判据会被人加进豁免名单 —— 那时它就真的死了。
    # 那几个由**白名单**那条判据守（发出去的动词集合），白名单不会漏。
    危险 = {
        "store", "expunge", "setacl", "deleteacl", "setquota",
        "setannotation", "subscribe", "unsubscribe", "proxyauth",
    }
    碰了 = 属性 & 危险
    assert not 碰了, f"mail_imap.py 里出现了会改邮箱状态的调用：{sorted(碰了)}"


# ── 连不上 / 认证失败：两种，能分辨 ─────────────────────────────────


def test_连不上时抛_SourceUnavailable_而不是裸异常(monkeypatch, 配好信箱) -> None:
    """`IMAP4_SSL(...)` 的构造函数里会发生的事横跨 socket / ssl / imaplib
    三族，而它们的公共父类只有 `Exception`。窄捕获的后果是漏出一种，
    而那种失败会以 traceback 的样子出现 —— 看起来像「程序坏了」。"""
    _装上(monkeypatch, 连接时抛=ConnectionRefusedError("连接被拒"))

    with pytest.raises(SourceUnavailable) as ei:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    assert "连不上" in str(ei.value)
    assert "imap.example.test" in str(ei.value)


def test_认证失败与连不上是两种错误(monkeypatch, 配好信箱) -> None:
    """※ 两者的**下一步不同**：一个去查网络与主机名，一个去查账号口令。

    形状相同的话，运维会拿着「网络不通」的结论去看一个口令写错的配置。
    """
    连接 = _假连接(登录时抛=imaplib.IMAP4.error("NO [AUTHENTICATIONFAILED] 认证失败"))
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_imap._ImapAuthFailed) as 认证:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    with pytest.raises(SourceUnavailable) as 连不上:
        _装上(monkeypatch, 连接时抛=OSError("dns 挂了"))
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    assert type(认证.value) is not type(连不上.value)
    # 两者都仍是 SourceUnavailable —— 用例层的处置不变（记账、非零退出），
    # 变的只是人能读到的**下一步**。
    assert isinstance(认证.value, SourceUnavailable)
    assert isinstance(连不上.value, SourceUnavailable)
    assert "认证" in str(认证.value)
    assert "认证" not in str(连不上.value)


def test_信箱打不开与认证失败是两种错误(monkeypatch, 配好信箱) -> None:
    """一个错在**账号**，一个错在**信箱名**。"""
    连接 = _假连接(信箱响应=("NO", [b"Mailbox does not exist"]))
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_imap._ImapMailboxUnavailable) as ei:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    assert "信箱" in str(ei.value)
    assert isinstance(ei.value, SourceUnavailable)


def test_登录途中掉线算连不上_不算认证失败(monkeypatch, 配好信箱) -> None:
    """`login()` 里抛的 OSError 是**网络**问题，不是口令问题。

    ※ `imaplib.IMAP4.error` 与 `OSError` 是两个不相干的族（前者只继承
    `Exception`），所以这两支能分开捕获 —— 而不是靠「看消息里有没有某个词」。
    """
    连接 = _假连接(登录时抛=OSError("连接被重置"))
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_imap._ImapUnavailable) as ei:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    assert not isinstance(ei.value, mail_imap._ImapAuthFailed)


# ── 两种「0 条」 ────────────────────────────────────────────────────


def test_窗口里没有信时返回空_不抛(monkeypatch, 配好信箱) -> None:
    """「这段时间确实没有信」是一个**数据事实**，不是失败。

    它与文件那条路「窗口内 0 条只是返回空」是同一条判据。
    """
    连接 = _假连接(uids=b"")
    _装上(monkeypatch, 连接=连接)

    assert mail_imap.fetch_window(mail_imap.parse_imap_url(假URL)) == []


def test_服务器说有信却一封都没取回来时抛(monkeypatch, 配好信箱) -> None:
    """※ 这是端口那条「**解析出 0 条就抛**」在 IMAP 上的落点。

    它与上一条**形状不同**：上一条是「本来就没有」，这一条是「我读到了却读不出
    东西」。两者混成一个空列表的话，一个坏掉的 FETCH 会显示成「今天邮箱很安静」。
    """
    连接 = _假连接(uids=b"1 2")
    # 让 FETCH 回一个**没有字面量**的响应（只有响应头）。
    连接.uid = lambda command, *args: (
        ("OK", [b"1 (UID 1 BODY[] {0}"]) if command.upper() == "FETCH"
        else ("OK", [b"1 2"])
    )
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_imap._ImapNoContent) as ei:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    assert isinstance(ei.value, SourceFormatError)
    assert "一封都没取回来" in str(ei.value)


def test_取回来的信读不懂时抛(monkeypatch, 配好信箱) -> None:
    """取回来的字节交给**同一个**解析器 —— 坏字节在那里就会响。"""
    连接 = _假连接(uids=b"1", 每封="这不是一封邮件。\n".encode("utf-8"))
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_parse._MailNotUnderstood):
        mail_source._MailSource(mail_source.resolve_target()).read()


# ── 增量：窗口下推 ──────────────────────────────────────────────────


def test_窗口下推给服务器_是超集不是等号() -> None:
    """※ IMAP 的 `SINCE` / `BEFORE` 按 `INTERNALDATE` 的**天**比较，而那个日期
    用的是**服务器自己的时区** —— 我们既控制不了它，也拿不到它的日界。

    所以两端各留**一天**余量：多取几封只会被本地滤掉，而**漏取**是看不出来的。

    锚点：2026-09-14 09:20:00 +0800（UTC+8）→ `14-Sep-2026`。
    """
    起点 = 1789348800  # 2026-09-14 09:20 +0800
    判据 = mail_imap._search_criteria(起点, 起点 + 1)
    assert 判据 == ["SINCE", "13-Sep-2026", "BEFORE", "16-Sep-2026"]


def test_不给窗口时搜全部() -> None:
    assert mail_imap._search_criteria(None, None) == ["ALL"]


def test_日期是英文月份缩写_不随_locale() -> None:
    """※ `strftime("%b")` 跟着 **locale** 走 —— 在中文 Windows 上它给出「10月」，
    而那个查询会被服务器拒掉、或者更糟：**静默匹配错**。

    ⇒ 月份表是写死的英文缩写。这条断言的是**形状**（`DD-Mon-YYYY`，月份只能是
    那十二个缩写之一），所以它不依赖跑测试的那台机器的 locale。
    """
    assert mail_imap._imap_date(0) == "01-Jan-1970"
    assert mail_imap._imap_date(1789348800) == "14-Sep-2026"
    assert mail_imap._imap_date(1789348800 - 86400) == "13-Sep-2026"

    所有 = [mail_imap._imap_date(1789348800 + i * 86400 * 30) for i in range(12)]
    for 串 in 所有:
        assert 串[3:6] in mail_imap._MONTHS, f"月份不是英文缩写：{串}"
        assert 串.isascii(), f"日期里出现了非 ASCII（locale 泄漏）：{串}"


def test_时间窗真的发到了_SEARCH_上(monkeypatch, 配好信箱) -> None:
    """下推不是「文档里说下推了」—— 它是**发出去的参数**。"""
    连接 = _假连接(uids=b"1", 每封=b"x")
    _装上(monkeypatch, 连接=连接)

    mail_imap.fetch_window(
        mail_imap.parse_imap_url(假URL), since=1789348800, until=1789348801
    )

    检索 = [c for c in 连接.命令 if c[0] == "UID SEARCH"]
    assert len(检索) == 1
    assert 检索[0][1:] == ("SINCE", "13-Sep-2026", "BEFORE", "16-Sep-2026")


def test_超集由本地再过一次精确窗口(monkeypatch, 配好信箱) -> None:
    """※ 服务器给的窗口是**天**粒度，而我们的窗口是**秒**粒度。

    多取回来的那几封必须被本地滤掉 —— 否则「增量」会变成「多算几封」，
    而日报里会多出几条不属于这一天的通知。
    """
    早一封 = (
        "Date: Mon, 14 Sep 2026 08:00:00 +0800\nFrom: a@example.test\n"
        "Subject: 早一封\n\nbody\n"
    ).encode("utf-8")
    窗内 = (
        "Date: Mon, 14 Sep 2026 09:20:00 +0800\nFrom: a@example.test\n"
        "Subject: 窗内\n\nbody\n"
    ).encode("utf-8")

    连接 = _假连接(uids=b"1 2", 每封=b"x")
    连接.uid = lambda command, *args: (
        ("OK", [(b"hdr", 早一封 if args[0] == b"1" else 窗内), b")"])
        if command.upper() == "FETCH"
        else ("OK", [b"1 2"])
    )
    _装上(monkeypatch, 连接=连接)

    src = mail_source._MailSource(mail_source.resolve_target())
    读到的 = src.read(since=1789348800, until=1789348801)

    assert [m.content.split("\n")[0] for m in 读到的] == ["窗内"], (
        "窗口外的信没被本地滤掉 —— 超集的那一天余量会变成多算几条"
    )


# ── msg_id：两条来路一致 ────────────────────────────────────────────


def test_同一封信从文件读和从_IMAP_读算出同一个_msg_id(monkeypatch, 配好信箱) -> None:
    """※ 这是「两条入口」这条设计的**核心判据**。

    身份只由字节里的 `Message-ID` 决定，与「这些字节是从文件读的还是从服务器
    取的」无关。不成立的话，同一封信会**入两次库**（今天导出读一遍、明天直连
    读一遍），而重复条目是无声的 —— 日报里同一封出现两次，没有任何东西报错。
    """
    字节 = 夹具_eml.read_bytes()
    从文件 = mail_parse.parse_eml(字节, group_id=1)[0].msg_id

    连接 = _假连接(uids=b"1", 每封=字节)
    _装上(monkeypatch, 连接=连接)
    从信箱 = mail_source._MailSource(mail_source.resolve_target()).read()

    assert [m.msg_id for m in 从信箱] == [从文件]
    # 连内容都对得上 —— 同一封信，不是「碰巧 id 一样」。
    assert 从信箱[0].content == mail_parse.parse_eml(字节, group_id=1)[0].content


def test_两条路走的是同一个解析函数() -> None:
    """`parse_messages` 是「取字节」与「解」之间那道缝 —— 两条路都从这儿进。

    ※ 这是**结构**上的保证，不是靠两条路各写一遍「碰巧一样」。
    """
    字节 = 夹具_eml.read_bytes()
    assert (
        mail_parse.parse_messages([字节], group_id=1)[0].msg_id
        == mail_parse.parse_eml(字节, group_id=1)[0].msg_id
    )


# ── 凭据 ────────────────────────────────────────────────────────────


def test_口令绝不出现在任何错误里(monkeypatch, 配好信箱) -> None:
    """※ 两道防线，这条测的是**结果**。

    主防线是结构（口令从不被拼进任何消息）；`_redact` 只是兜底。
    这里让服务器**故意把口令回显在错误响应里** —— 那是真实存在的一种服务器
    行为，而它正是「结构防线」挡不住的那一档。
    """
    连接 = _假连接(
        登录时抛=imaplib.IMAP4.error(f"NO 认证失败，你给的是 {假口令}")
    )
    _装上(monkeypatch, 连接=连接)

    with pytest.raises(mail_imap._ImapAuthFailed) as ei:
        邮件 = mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))

    assert 假口令 not in str(ei.value)
    assert 假口令 not in repr(ei.value)
    assert 假口令 not in repr(ei.value.context)
    assert "***" in str(ei.value), "兜底没生效 —— 口令没被替换掉"


def test_连不上的错误里也没有口令(monkeypatch, 配好信箱) -> None:
    """连接失败发生在登录**之前**，所以那条路上口令连碰都没碰过。

    这条断言的是「没碰」这个事实 —— 而不是「碰了但被替换了」。
    """
    _装上(monkeypatch, 连接时抛=OSError("连不上"))

    with pytest.raises(SourceUnavailable) as ei:
        mail_imap.fetch_window(mail_imap.parse_imap_url(假URL))
    assert 假口令 not in str(ei.value)


def test_凭据对象的_repr_不含口令() -> None:
    """`field(repr=False)` 防的是「哪天有人把凭据对象塞进了异常的 context」——
    而 `YechaoError.__str__` 会对 context 里的每个值做 `!r`。"""
    凭据 = mail_imap._Credential(user=假账号, password=假口令)
    assert 假口令 not in repr(凭据)
    assert 假口令 not in f"{凭据!r}"
    assert 假账号 in repr(凭据), "账号不是秘密，该看得见"


def test_凭据被_pickle_时不带口令() -> None:
    """一个把口令序列化进去的对象，会在**进程外**留下它的副本 ——
    崩溃转储、消息队列、缓存，都算。而那些地方没人会去看第二眼。"""
    import pickle

    凭据 = mail_imap._Credential(user=假账号, password=假口令)
    还原 = pickle.loads(pickle.dumps(凭据))
    assert 还原.user == 假账号
    assert 还原.password == ""


def test_缺账号或口令时抛_且说清从哪配(干净的配置, monkeypatch) -> None:
    monkeypatch.setenv(mail_imap.IMAP_USER_ENV, 假账号)
    with pytest.raises(ConfigError) as ei:
        mail_imap.resolve_credential()
    assert mail_imap.IMAP_PASS_ENV in str(ei.value)
    assert "只从环境变量读" in str(ei.value)

    monkeypatch.delenv(mail_imap.IMAP_USER_ENV)
    monkeypatch.setenv(mail_imap.IMAP_PASS_ENV, 假口令)
    with pytest.raises(ConfigError) as ei2:
        mail_imap.resolve_credential()
    assert mail_imap.IMAP_USER_ENV in str(ei2.value)


def test_口令两端不_strip(干净的配置, monkeypatch) -> None:
    """※ 口令两端可以有空格，而 strip 掉的症状是「同一个口令在我这儿能登、
    在你那儿登不上」—— 一句让人去查错方向的话。"""
    monkeypatch.setenv(mail_imap.IMAP_USER_ENV, 假账号)
    monkeypatch.setenv(mail_imap.IMAP_PASS_ENV, "  pass with spaces  ")
    assert mail_imap.resolve_credential().password == "  pass with spaces  "


def test_连接串里不许写账号口令() -> None:
    """※ 不是「忽略它」，是**拒掉它**：忽略会让用户以为他写进 URL 的那个账号
    生效了。写在 URL 里的口令会进 shell 历史、进进程列表、进报错消息。"""
    with pytest.raises(ConfigError) as ei:
        mail_imap.parse_imap_url("imaps://user:secret@imap.example.test/INBOX")
    assert "不许写账号或口令" in str(ei.value)
    assert "secret" not in str(ei.value), "报错里回显了口令"


def test_明文_imap_不认() -> None:
    """`imap://`（明文 143）会把口令原样发到网络上，而这条路上没有「降级」
    这个选项：认它等于把一个明文口令的路径做成默认可达的。"""
    with pytest.raises(ConfigError) as ei:
        mail_imap.parse_imap_url("imap://imap.example.test/INBOX")
    assert "只认 imaps://" in str(ei.value)
    assert "明文" in str(ei.value)


def test_连接串的默认端口与信箱() -> None:
    assert mail_imap.parse_imap_url("imaps://imap.example.test").port == 993
    assert mail_imap.parse_imap_url("imaps://imap.example.test").mailbox == "INBOX"
    端点 = mail_imap.parse_imap_url("imaps://imap.example.test:1993/收件箱")
    assert (端点.port, 端点.mailbox) == (1993, "收件箱")
    assert 端点.where == "imap.example.test:1993/收件箱"


def test_连接串里没有主机名时抛() -> None:
    with pytest.raises(ConfigError):
        mail_imap.parse_imap_url("imaps:///INBOX")


# ── 协议闸门 ────────────────────────────────────────────────────────


def test_没签协议就不许连_而且是在装配时拒绝(干净的配置, monkeypatch) -> None:
    """※ 带凭据的源会**用用户本人的账号**去访问第三方 —— 所以先过协议闸门。

    它在**装配时**过：那与「目标没配」同一档（`ConfigError`，退出码 2），
    而且发生在**任何一个源开始读之前**。等 `read()` 才拒绝的话，
    `--source synth,mail` 会先把合成语料读完并入库，然后才说协议没签。
    """
    monkeypatch.setenv(mail_imap.IMAP_URL_ENV, 假URL)
    monkeypatch.setenv(mail_imap.IMAP_USER_ENV, 假账号)
    monkeypatch.setenv(mail_imap.IMAP_PASS_ENV, 假口令)

    with pytest.raises(ConfigError) as ei:
        mail_source.build_mail_source()
    assert "协议" in str(ei.value)


def test_签了协议就能造出源_而且造的时候不连(配好信箱, monkeypatch) -> None:
    """※ 「造出来」与「连上去」是两件事：连接发生在 `read()` 里。

    这条顺带钉住「装配不碰网络」—— 它把一个**会抛的**假连接装上，
    而 `build_mail_source()` 照样成功。
    """
    _装上(monkeypatch, 连接时抛=AssertionError("装配阶段不该连网络"))

    src = mail_source.build_mail_source()
    assert src.name == "mail"


def test_账号口令没配齐时_装配就拒绝(签了协议, monkeypatch) -> None:
    """※ 这一道闸与协议那道一样，必须在**装配时**过。

    留到 `read()` 里抛的话，`ConfigError` 会**穿过**用例层逐个源兜底的那个
    `except SourceError`，把一次「配置没填全」放大成「整轮摄取中断」——
    而且退出码变成 1，看起来像运行时故障，而不是「去改配置」。
    """
    monkeypatch.setenv(mail_imap.IMAP_URL_ENV, 假URL)
    # 账号口令一个都不给。

    with pytest.raises(ConfigError) as ei:
        mail_source.build_mail_source()
    assert mail_imap.IMAP_PASS_ENV in str(ei.value)


def test_文件入口不需要签协议(干净的配置, monkeypatch) -> None:
    """文件是**本机的东西**，不涉及第三方账号 —— 所以那道闸门对它不成立。"""
    monkeypatch.setenv(mail_source.TARGET_ENV, str(夹具_eml))
    assert mail_source.build_mail_source().read() != []


# ── 目标从哪来 ──────────────────────────────────────────────────────


def test_两个环境变量同时配时抛_不静默挑一个(干净的配置, monkeypatch) -> None:
    """※ 它们说的是**同一件事**（这一次从哪儿读邮件）。

    静默挑一个的症状是「我明明配的是那个，它读的是这个」，而两份报告长得
    一模一样 —— 用户没有任何线索。
    """
    monkeypatch.setenv(mail_source.TARGET_ENV, str(夹具_eml))
    monkeypatch.setenv(mail_imap.IMAP_URL_ENV, 假URL)

    with pytest.raises(ConfigError) as ei:
        mail_source.resolve_target()
    assert "同时配了" in str(ei.value)
    assert mail_source.TARGET_ENV in str(ei.value)
    assert mail_imap.IMAP_URL_ENV in str(ei.value)


def test_只给_HOST_时拼出一条连接串_并取默认端口与信箱(干净的配置, monkeypatch) -> None:
    monkeypatch.setenv(mail_imap.IMAP_HOST_ENV, "imap.example.test")
    t = mail_source.resolve_target()
    assert t.kind == "imap"
    assert (t.imap.host, t.imap.port, t.imap.mailbox) == ("imap.example.test", 993, "INBOX")

    monkeypatch.setenv(mail_imap.IMAP_PORT_ENV, "1993")
    assert mail_source.resolve_target().imap.port == 1993


def test_端口不是整数时抛(干净的配置, monkeypatch) -> None:
    monkeypatch.setenv(mail_imap.IMAP_HOST_ENV, "imap.example.test")
    monkeypatch.setenv(mail_imap.IMAP_PORT_ENV, "九三")
    with pytest.raises(ConfigError):
        mail_source.resolve_target()


def test_配置文件里也能写连接串(干净的配置) -> None:
    (干净的配置 / mail_source.SOURCES_FILE).write_text(
        f'[mail]\ntarget = "{假URL}"\ngroup_id = 820000042\n', encoding="utf-8"
    )
    t = mail_source.resolve_target()
    assert t.kind == "imap"
    assert t.group_id == 820000042
    assert t.imap.mailbox == "INBOX"


def test_目标带_scheme_时走_IMAP_解析器而不是报后缀(干净的配置) -> None:
    """※ `imap://` 要落到「只认 imaps」那句话上，而不是掉进「后缀不认」里 ——
    后者会让用户以为问题出在文件类型上。"""
    with pytest.raises(ConfigError) as ei:
        mail_source._classify("imap://h/INBOX", group_id=1, where="t")
    assert "只认 imaps://" in str(ei.value)
    assert "后缀" not in str(ei.value)


def test_端点对象里没有口令(配好信箱) -> None:
    """※ 目标是**去哪儿读**，凭据是**是谁** —— 两者不混在一个对象里。

    混在一起的症状是「某天有人把目标打进日志，而日志里出现了口令」。
    """
    t = mail_source.resolve_target()
    字段 = set(vars(t.imap))
    assert not {f for f in 字段 if "pass" in f.lower() or "secret" in f.lower()}
    assert 假口令 not in repr(t.imap)


# ── 机械判据：网络面 ────────────────────────────────────────────────


def test_IMAP_这条路的网络面只有_imaplib() -> None:
    """※ 「只走标准库的 IMAP 客户端」这条要求，机械可检查的那一半在这里。

    `urllib.parse` 是**字符串解析**（拆一条 URL），不发任何请求 ——
    它与 `urllib.request` 是同一个包里的两样东西，所以这里点名放行的是
    `urllib.parse` 而不是整个 `urllib`。
    """
    顶层: set[str] = set()
    for node in ast.walk(
        ast.parse(Path(mail_imap.__file__).read_text(encoding="utf-8"))
    ):
        if isinstance(node, ast.Import):
            顶层.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            顶层.add(node.module)

    不许 = {
        "socket", "ssl", "requests", "httpx", "aiohttp",
        "urllib.request", "http.client", "poplib", "smtplib", "ftplib",
    }
    碰了 = 顶层 & 不许
    assert not 碰了, f"mail_imap.py 碰了别的网络库：{sorted(碰了)}"
    assert "imaplib" in 顶层
    assert "urllib.parse" in 顶层
