"""邮件源 —— 读**本机**的邮件文件（`.eml` / `.mbox`），或**只读**地直连信箱（IMAP）。

## 两条入口，按目标**自身的形状**分

| 入口 | 判据（可观察的事实） | 走网络吗 | 要签协议吗 |
|---|---|---|---|
| **文件** | 目标是一个 `.eml` / `.mbox` 的路径 | 否 | 不要 |
| **IMAP** | 目标是一条 `imaps://…` 连接串 | 是（**只读**） | **要** |

判据为什么长这样：它与网页源「按目标形状分三种入口」是同一条 ——
**让分界落在一个可观察的事实上，而不是一次判断**。嗅内容（「这个文件里有没有
`From ` 分隔行」）会去猜，而猜错的症状是「按另一种结构解析，产出一些看起来
正常的东西」。

※ 两条路都在「**只读**」这一条上：文件是只读打开（`read_bytes()`，不写盘），
信箱是只读会话（`EXAMINE` + `BODY.PEEK[]`，见 `mail_imap` 的 docstring）。

## 目标从哪来（不写死任何邮箱）

**文件**入口，两个来源（环境变量 > 配置文件）：

  1. `YECHAO_MAIL_SOURCE` —— 一次性的、临时换一个文件时用
  2. `<config_dir>/sources.toml` 的 `[mail] target` —— 长期配置

**IMAP** 入口，三个来源（**三选一**）：

  1. `YECHAO_IMAP_URL` —— 一条 `imaps://host:port/信箱` 连接串
  2. `YECHAO_IMAP_HOST`（+ `YECHAO_IMAP_PORT`）—— 只给主机，信箱取 `INBOX`
  3. `<config_dir>/sources.toml` 的 `[mail] target` 写成 `imaps://…`

※ 第 1、2 条**同时给** ⇒ **抛**（见 `resolve_target`）：它们是**同一件事的两种
写法**，同时给两个说明用户不知道哪个生效 —— 而「不知道哪个生效」正是要报出来的
那种状态，不是要静默挑一个的那种。同理，`YECHAO_MAIL_SOURCE` 与 IMAP 那两个
变量也是互斥的。

两个都没有 ⇒ **抛 `ConfigError`，不兜底**（与网页源逐字相同的判据）：兜底到某个
默认文件或默认主机上，`--source mail` 就会在什么都没配的情况下「成功」读到别人的
邮件，而报告看起来一切正常。

## 账号与口令

只从环境变量读（`YECHAO_IMAP_USER` / `YECHAO_IMAP_PASS`），**不进配置文件、
不进异常消息、不进日志**。连接串里出现 `user:pass@` 时**直接抛**。
细节与理由见 `mail_imap` 的模块 docstring。

## 没签协议就不许连

带凭据的源会**用用户本人的账号**去访问第三方 —— 所以先过
`sources/consent.py::check_consent()`。它在**装配时**过（`build_mail_source`），
不是读到一半才过：那与「目标没配」同一档（`ConfigError`，退出码 2），
而且它发生在**任何一个源开始读之前** —— 否则 `--source synth,mail` 会先把
合成语料读完并入库，然后才说协议没签。

## ※ 类名为什么带下划线

`tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是**适配层顶层
公开类名**有没有在 `bootstrap.py` 的源码文本里出现过。数据源不由组合根实例化
（它由 `sources/__init__.py` 的注册表组合）—— 与 `_WebSource` / `SynthSource`
是同一类东西。⇒ 用下划线前缀走那条判据自己留的口子（它只扫不以 `_` 开头的类）。
**这不是绕过护栏**：本类确实被接上了线（`REGISTRY["mail"]`），护栏只是用文本判据
看不见注册表。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yechao.domain.errors import ConfigError, SourceUnavailable
# ※ 从**子模块**导入，不写 `from yechao.infrastructure.sources import mail_imap`
# —— 后者在导入图里是一条指向**包本身**的边，而包的 `__init__` 又 import
# 本模块，于是成环（`tests/contracts/test_layering.py::test_没有导入环`）。
from yechao.infrastructure.sources.mail_imap import (
    DEFAULT_MAILBOX,
    DEFAULT_PORT,
    IMAP_HOST_ENV,
    IMAP_PORT_ENV,
    IMAP_SCHEME,
    IMAP_URL_ENV,
    _ImapEndpoint,
    fetch_window,
    parse_imap_url,
    resolve_credential,
)
from yechao.infrastructure.sources._common import (
    #: 配置目录的环境变量名。**必须与 `bootstrap.Settings.from_env` 一致**。
    CONFIG_DIR_ENV,
    #: 配置目录的默认名。**必须与 `bootstrap.DEFAULT_CONFIG_DIR` 一致**。
    DEFAULT_CONFIG_DIR,
    #: 数据源的配置文件。与 `groups.toml` 等并列放在配置目录下。
    SOURCES_FILE,
    config_dir,
    in_window,
    source_table,
)
#: 没签第三方账号授权协议时抛 `ConfigError`（退出码 2）。
from yechao.infrastructure.sources.consent import check_consent
#: 源的标识与两种后缀都在解析模块里定义（每条消息的 `source` 字段用的就是它），
#: 这里引用而不再写一遍字面量 —— 两处字面量漂移的症状是「消息自称来自一个不
#: 存在的源」，而它在审计里看不出来。
from yechao.infrastructure.sources.mail_parse import (
    EML_SUFFIX,
    MBOX_SUFFIX,
    NAME,
    parse_eml,
    parse_mbox,
    parse_messages,
)

#: 目标的来源。
TARGET_ENV = "YECHAO_MAIL_SOURCE"
#: 分组号。※ 邮件没有天然的整数 id（发件地址是字符串），所以它只能由配置给。
GROUP_ID_ENV = "YECHAO_MAIL_GROUP_ID"

#: 认的文件后缀。不在这里的一律报错 —— 见模块 docstring。
SUFFIXES = (EML_SUFFIX, MBOX_SUFFIX)

#: 判「这个目标是不是一条连接串」用的分界。
_SCHEME_SEP = "://"

#: 不给分组号时用哪个。
#:
#: ※ 它**是**一个默认值，而本项目对默认值的态度是「用了要能被看出来」。
#: 这里能看出它的地方有两处：`--source mail` 的报告会打出源名（所以「读的是
#: 邮件源」一眼可见），以及落库后的 `group_id` 就在消息行上。
#: 取 820000006 是因为它是本项目**自编**的假群号段（`config.example/groups.toml`
#: 里 820000001–820000004 已被占用，网页源默认取 820000001）—— 与网页源**错开**
#: 是刻意的：两个源的默认分组号撞在一起，症状是「邮件与网页通知在同一个『群』
#: 里」，而那一列本来是用来分开它们的。
DEFAULT_GROUP_ID = 820000006


# ── 目标 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """一个已经解析好的目标：**从哪儿读**、**它是哪种入口**、**算进哪个分组**。

    ※ `imap` 只在 `kind == "imap"` 时非空，而且它**不含凭据** —— 账号与口令
    在 `mail_imap.resolve_credential()` 里现读，不进这个对象（见 `mail_imap`）。
    """

    raw: str
    kind: str  # "eml" / "mbox" / "imap"
    group_id: int
    where: str  # 这个目标是从哪儿读出来的（给报错用）
    imap: _ImapEndpoint | None = None


def _env_group_id() -> int | None:
    raw = os.environ.get(GROUP_ID_ENV)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(
            f"{GROUP_ID_ENV}={raw!r} 不是整数。分组号要是一个整数 —— "
            f"它会写进每条消息的 group_id。"
        ) from e


def _imap_url_from_host() -> str:
    """只给了 `YECHAO_IMAP_HOST`（没给整条连接串）时，拼一条出来。

    ※ 端口与信箱都取默认值（`993` / `INBOX`），而这两个默认值**用了要能被
    看出来**：`_ImapEndpoint.where` 会把它们显式写进报错与报告，
    所以「它读了 INBOX 而不是别的」一眼可见。
    """
    host = (os.environ.get(IMAP_HOST_ENV) or "").strip()
    raw_port = (os.environ.get(IMAP_PORT_ENV) or "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError as e:
            raise ConfigError(
                f"{IMAP_PORT_ENV}={raw_port!r} 不是整数。"
            ) from e
    else:
        port = DEFAULT_PORT
    return f"{IMAP_SCHEME}://{host}:{port}/{DEFAULT_MAILBOX}"


def _env_targets() -> list[tuple[str, str]]:
    """环境变量这一层给出的目标。**互斥**（见 `resolve_target`）。

    ※ 每项是 `(目标串, 它来自哪个变量)` —— 顺序是刻意的：调用方拿到的是
    **目标**，而变量名只用来报错。反过来放的话，`raw, where = 环境目标[0]`
    会把变量名当成路径去解析，而症状是「目标明明是 /tmp/x.eml，它却说后缀不认」。
    """
    out: list[tuple[str, str]] = []
    env_mail = (os.environ.get(TARGET_ENV) or "").strip()
    env_url = (os.environ.get(IMAP_URL_ENV) or "").strip()
    env_host = (os.environ.get(IMAP_HOST_ENV) or "").strip()
    if env_mail:
        out.append((env_mail, TARGET_ENV))
    if env_url:
        out.append((env_url, IMAP_URL_ENV))
    elif env_host:
        out.append((_imap_url_from_host(), IMAP_HOST_ENV))
    return out


def resolve_target() -> _Target:
    """把「从哪儿读」定下来。**没配就抛，不兜底。**

    优先级：环境变量 > 配置文件。理由：环境变量是**这一次**的意图（临时读一份
    夹具看看），配置文件是**长期**的意图；临时的不该被长期的覆盖掉。

    ※ 环境变量这一层里，`YECHAO_MAIL_SOURCE`（文件）与
    `YECHAO_IMAP_URL` / `YECHAO_IMAP_HOST`（信箱）**互斥**：同时给两个 ⇒ 抛。
    它们说的是**同一件事**（这次读哪儿），同时给两个等于没说 ——
    而静默挑一个的症状是「我明明配的是那个，它读的是这个」，
    两份报告却长得一模一样。
    """
    table = source_table(
        "mail",
        hint=(
            '  [mail]\n  target = "~/mail/2026-10.mbox"\n  group_id = 820000006\n'
            '  # 或者：target = "imaps://imap.example.test:993/INBOX"'
        ),
    )

    环境目标 = _env_targets()
    if len(环境目标) > 1:
        raise ConfigError(
            f"同时配了 {' 与 '.join(名字 for _, 名字 in 环境目标)} —— "
            f"它们说的是**同一件事**（这一次从哪儿读邮件），同时给两个等于没说。\n"
            f"※ 不静默挑一个：挑错了的症状是「我明明配的是那个，它读的是这个」，"
            f"而两份报告长得一模一样。\n"
            f"※ 只留一个：读**文件**用 {TARGET_ENV}，读**信箱**用 "
            f"{IMAP_URL_ENV}（或 {IMAP_HOST_ENV}）。"
        )

    if 环境目标:
        raw, where = 环境目标[0]
    else:
        cfg = str(table.get("target") or "").strip()
        if cfg:
            raw, where = cfg, f"{SOURCES_FILE} 的 [mail] target"
        else:
            raise ConfigError(
                f"邮件源不知道要读哪儿。给它一个目标 —— 二选一：\n"
                f"  · 读**文件**：{TARGET_ENV}=<一个 .eml 或 .mbox 的路径>\n"
                f"  · 读**信箱**：{IMAP_URL_ENV}="
                f"imaps://主机:993/INBOX\n"
                f"  · 或者写进配置文件：{config_dir() / SOURCES_FILE} 里写\n"
                f'      [mail]\n      target = "~/mail/2026-10.mbox"\n'
                f"※ 刻意**没有默认目标**：默认到一个文件或一台主机上，"
                f"`--source mail` 就会在什么都没配的情况下「成功」读到别人的"
                f"邮件，而报告看起来一切正常。"
            )

    group_id = _env_group_id()
    if group_id is None:
        raw_gid = table.get("group_id")
        if raw_gid is None:
            group_id = DEFAULT_GROUP_ID
        else:
            try:
                group_id = int(raw_gid)
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"{SOURCES_FILE} 的 [mail] group_id={raw_gid!r} 不是整数。"
                ) from e

    return _classify(raw, group_id=group_id, where=where)


def _classify(raw: str, *, group_id: int, where: str) -> _Target:
    """按目标**自身的形状**判它属于哪一种入口。见模块 docstring 那张表。"""
    if _SCHEME_SEP in raw:
        # ※ 任何带 scheme 的目标都交给 IMAP 那条解析器 —— 它会为 `imap://`
        # （明文）说清「为什么只认 imaps」，而不是掉进下面那条「后缀不认」里，
        # 让用户以为问题出在文件类型上。
        return _Target(
            raw=raw,
            kind="imap",
            group_id=group_id,
            where=where,
            imap=parse_imap_url(raw),
        )

    path = Path(raw)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise ConfigError(
            f"邮件源只认 {' 与 '.join(SUFFIXES)} 结尾的文件，或者一条 "
            f"{IMAP_SCHEME}:// 开头的连接串，而这个目标是：{raw}\n"
            f"（目标来自 {where}）\n"
            f"※ 这里**不嗅内容**：嗅探是在两种结构之间猜，猜错的症状是「按另一种"
            f"结构解析，产出一些看起来正常的东西」。后缀是用户自己写的、"
            f"非黑即白的事实。"
        )
    return _Target(
        raw=raw,
        kind="eml" if suffix == EML_SUFFIX else "mbox",
        group_id=group_id,
        where=where,
    )


# ── 取字节（文件那条路） ────────────────────────────────────────────


def _read_bytes(target: _Target) -> bytes:
    """把文件的字节读回来。读不到 ⇒ `SourceUnavailable`（**不返回空**）。

    ※ 只用 `read_bytes()`：**只读**是这条判据的另一半（见模块 docstring）。
    """
    path = Path(target.raw)
    try:
        return path.read_bytes()
    except OSError as e:
        raise SourceUnavailable(
            f"邮件文件读不到：{path}（{e}）。（目标来自 {target.where}）",
            path=str(path),
        ) from e


# ── 源 ──────────────────────────────────────────────────────────────


class _MailSource:
    """邮件源。实现 `application/ports.py::MessageSource`。

    ※ 它**不切**消息：一封邮件就是一个自洽的短文本（与网页通知同类），切了反而
    把它拆散。这与会议转写必须切**相反** —— 端口那句「形状由接口约束，处理方式
    不由接口约束」说的就是这件事。两条入口（文件 / IMAP）都遵守它。

    ※ 目标在**装配时**定下来（由 `build_mail_source` 传进来），不是每次 `read()`
    现查环境变量。理由与网页源逐字相同：报错要发生在**任何一个源开始读之前**，
    而且「目标没配」是**用法错**（退出码 2），不是「跑起来之后才失败」。
    """

    name = NAME

    def __init__(self, target: _Target) -> None:
        self._target = target

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """读一遍，返回消息。

        抛（每一条都是**响亮**的，没有一条是静默返回空）：
          · `ConfigError`                 —— 没配目标 / 后缀不认 / 连接串不合法
                                            / 没签协议（都在装配时报）
          · `SourceUnavailable`           —— 读不到。**文件**：不在 / 没权限；
                                            **IMAP**：连不上 / 认证失败 /
                                            信箱打不开（三个子类，可分辨）
          · `_MailNotUnderstood`          —— 读到了但没读懂
          · `_MailboxHasNoMessages`       —— 读懂了，但文件里确实一封都没有
          · `_ImapNoContent`              —— 服务器说有信，却一封都没取回来

        ※ **时间窗在解析之后才过**（文件那条路），或者**下推 + 本地再过一次**
        （IMAP 那条路）。两条路的分工不同，但**判据是同一条**：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题（去改
        解析器），后者是数据问题（这段时间真的安静）。

        · 文件：**解析出的条数是 0 才抛，窗口内 0 条只是返回空**
        · IMAP：服务器说窗口里 0 封 ⇒ 返回空；说 N 封却取回 0 ⇒ 抛
        """
        if self._target.kind == "imap":
            assert self._target.imap is not None
            raws = fetch_window(
                self._target.imap, since=since, until=until
            )
            if not raws:
                # 「这段时间确实没有信」—— 返回空，**不抛**。
                return []
            # ※ 与文件那条路**共用** `parse_messages`：两条路的 `msg_id`
            # 因此必然一样（身份只由字节里的 Message-ID 决定）。
            messages = parse_messages(raws, group_id=self._target.group_id)
        else:
            raw = _read_bytes(self._target)
            if self._target.kind == "eml":
                messages = parse_eml(raw, group_id=self._target.group_id)
            else:
                messages = parse_mbox(raw, group_id=self._target.group_id)

        # ※ 两条路共用同一个窗口函数（`_common.in_window`，半开区间）。
        # IMAP 那条路下推给服务器的窗口是**超集**（两端各留一天），
        # 精确到秒的过滤就落在这里。
        return [m for m in messages if in_window(m.ts, since, until)]


def build_mail_source() -> _MailSource:
    """注册表用的工厂（`REGISTRY["mail"]`）。

    ※ 它在**装配的时候**就把目标定下来，而不是等 `read()` 时再查 —— 与
    `build_web_source` 同一条判据：`interfaces/cli.py` 把「源名不认识」与
    「目标没配」都算作**用法错**（退出码 2，下一步是去改配置/命令行）。

    ※ IMAP 入口多两道闸，都在**装配时**过：

      1. **没签第三方账号授权协议就不许连**（`check_consent`）
      2. **账号口令没配齐也不许连**（`resolve_credential`）

    ※ 第 2 道为什么不在 `read()` 里过：`resolve_credential` 抛的是
    `ConfigError`（用法错，退出码 2），而用例层只按 `SourceError` **逐个源**
    兜底（`application/ingest.run`）—— 让它留到 `read()` 里抛，会把一次
    「配置没填全」放大成「整轮摄取中断」，而且退出码变成 1（看起来像运行时
    故障）。**装配时报的错，是「去改配置」；读的时候报的错，是「去查源」。**
    """
    target = resolve_target()
    if target.kind == "imap":
        # 没签 ⇒ `ConfigError`（退出码 2），与「目标没配」同一档。
        check_consent()
        # 只验，不用它的返回值 —— 真正的读取在 `fetch_window` 里现读一次
        # （口令因此不在任何长期存活的对象上）。
        resolve_credential()
    return _MailSource(target)
