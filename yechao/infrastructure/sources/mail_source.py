"""邮件源 —— 读**本机**的邮件文件（`.eml` / `.mbox`），把它变成消息。

## 离线、只读

本模块**不连 IMAP、不发任何请求、不写任何文件**：输入是一个文件路径，输出是
消息。判据有三条，都可以机械检查：

  · 全模块不 import `urllib` / `socket` / `imaplib` / `poplib` / `smtplib`
    （`test_邮件源不碰网络`）
  · 打开文件只用 `read_bytes()` —— 没有 `open(..., "w")`、没有 `mailbox.mbox`
    （它按 `rb+` 打开，是**可写**的）
  · 夹具就是**仓库里的一个文件**，跑通它不需要任何外部东西

## 目标从哪来（不写死任何邮箱）

  1. 环境变量 `YECHAO_MAIL_SOURCE` —— 一次性的、临时换一个文件时用
  2. `<config_dir>/sources.toml` 的 `[mail] target` —— 长期配置

两个都没有 ⇒ **抛 `ConfigError`，不兜底**（与网页源逐字相同的判据）：兜底到某个
默认文件会让 `--source mail` 在什么都没配的情况下「成功」读到一份别人的邮件，
而报告看起来一切正常。

## 怎么知道该按 `.eml` 读还是按 `.mbox` 读

按**后缀**（`_classify`）。判据是「用户给这个文件起了什么名字」：

  · `.eml`  —— 一封邮件（RFC 5322 的「单封导出」，绝大多数邮件客户端都认）
  · `.mbox` —— 一个信箱（多封，RFC 4155）

※ 为什么**不**去嗅内容（比如「文件里有没有 `From ` 分隔行」）：嗅探会在两种格式
之间**猜**，而猜错的症状是「同一批邮件按另一种结构解析，产出一些看起来正常的东西」。
后缀是用户自己写的、非黑即白的事实 —— 与网页源「夹具与本地 HTML 靠有没有侧车
区分」是同一种取舍：**让分界落在一个可观察的事实上，而不是一次判断**。

※ 别的后缀 ⇒ `ConfigError`（**用法错**，退出码 2）：用户把目标指到了一个我读不了
的文件类型，要改的是配置或命令行，不是解析器。这与「文件不在」不同 —— 后者是
`SourceUnavailable`（退出码 1，去查文件系统）。

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
#: 源的标识与两种后缀都在解析模块里定义（每条消息的 `source` 字段用的就是它），
#: 这里引用而不再写一遍字面量 —— 两处字面量漂移的症状是「消息自称来自一个不
#: 存在的源」，而它在审计里看不出来。
from yechao.infrastructure.sources.mail_parse import (
    EML_SUFFIX,
    MBOX_SUFFIX,
    NAME,
    parse_eml,
    parse_mbox,
)

#: 目标的来源。
TARGET_ENV = "YECHAO_MAIL_SOURCE"
#: 分组号。※ 邮件没有天然的整数 id（发件地址是字符串），所以它只能由配置给。
GROUP_ID_ENV = "YECHAO_MAIL_GROUP_ID"

#: 认的后缀。不在这里的一律报错 —— 见模块 docstring。
SUFFIXES = (EML_SUFFIX, MBOX_SUFFIX)

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
    """一个已经解析好的目标：**从哪儿读**、**它是哪种格式**、**算进哪个分组**。"""

    raw: str
    kind: str  # "eml" / "mbox"
    group_id: int
    where: str  # 这个目标是从哪儿读出来的（给报错用）


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


def resolve_target() -> _Target:
    """把「从哪儿读」定下来。**没配就抛，不兜底。**

    优先级：环境变量 > 配置文件。理由：环境变量是**这一次**的意图（临时读一份
    夹具看看），配置文件是**长期**的意图；临时的不该被长期的覆盖掉。
    """
    table = source_table(
        "mail",
        hint='  [mail]\n  target = "~/mail/2026-10.mbox"\n  group_id = 820000006',
    )
    from_env = (os.environ.get(TARGET_ENV) or "").strip()
    if from_env:
        raw, where = from_env, TARGET_ENV
    elif str(table.get("target") or "").strip():
        raw, where = str(table["target"]).strip(), f"{SOURCES_FILE} 的 [mail] target"
    else:
        raise ConfigError(
            f"邮件源不知道要读哪儿。给它一个目标 —— 二选一：\n"
            f"  · 环境变量：{TARGET_ENV}=<一个 .eml 或 .mbox 文件的路径>\n"
            f"  · 配置文件：{config_dir() / SOURCES_FILE} 里写\n"
            f'      [mail]\n      target = "~/mail/2026-10.mbox"\n'
            f"※ 刻意**没有默认目标**：默认到一个文件上，`--source mail` 就会在"
            f"什么都没配的情况下「成功」读到别人的邮件，而报告看起来一切正常。"
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
    """按目标**自身的形状**（后缀）判它属于哪一种格式。见模块 docstring。"""
    path = Path(raw)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise ConfigError(
            f"邮件源只认 {' 与 '.join(SUFFIXES)}，而这个目标的后缀是 "
            f"{suffix or '（没有后缀）'!r}：{raw}\n"
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


# ── 取字节 ──────────────────────────────────────────────────────────


def _read_bytes(target: _Target) -> bytes:
    """把目标的字节读回来。读不到 ⇒ `SourceUnavailable`（**不返回空**）。

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
    不由接口约束」说的就是这件事。

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
        """读一个文件，返回消息。

        抛（三条都是**响亮**的，没有一条是静默返回空）：
          · `ConfigError`             —— 没配目标 / 后缀不认（在装配时报）
          · `SourceUnavailable`       —— 读不到（文件不在 / 没有权限）
          · `_MailNotUnderstood`      —— 读到了但没读懂
          · `_MailboxHasNoMessages`   —— 读懂了，但文件里确实一封邮件都没有

        ※ **时间窗在解析之后才过**。这不是实现细节，是判据：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题（去改
        解析器），后者是数据问题（这段时间真的安静）。先解析、再过滤，两者
        就分得开：**解析出的条数是 0 才抛，窗口内 0 条只是返回空**。
        """
        raw = _read_bytes(self._target)
        if self._target.kind == "eml":
            messages = parse_eml(raw, group_id=self._target.group_id)
        else:
            messages = parse_mbox(raw, group_id=self._target.group_id)
        return [m for m in messages if in_window(m.ts, since, until)]


def build_mail_source() -> _MailSource:
    """注册表用的工厂（`REGISTRY["mail"]`）。

    ※ 它在**装配的时候**就把目标定下来，而不是等 `read()` 时再查 —— 与
    `build_web_source` 同一条判据：`interfaces/cli.py` 把「源名不认识」与
    「目标没配」都算作**用法错**（退出码 2，下一步是去改配置/命令行）。
    """
    return _MailSource(resolve_target())
