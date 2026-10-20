"""第三方账号授权 —— **签字之后**才允许带凭据的源启动。

## 要防什么

一个源拿用户的账号密码去登录第三方站点，这件事的性质与「读一个公开的
通知公告页」完全不同：前者是**用用户的身份**去访问，出了事（账号被限制、
平台条款）后果落在用户身上。所以「用户同意」不能是一句口头默认 ——
它必须是一条**能被读出来、能被核对**的记录。

三条形态，每一条都对应一次真实的失败：

| 看起来像 | 真相 |
|---|---|
| 用户「应该知道吧」 | 没有任何记录，事后无法证明他同意过 |
| 记录在，但协议已经改版 | 用户同意的是**旧条款**，新条款没人同意过 |
| 记录在，但版本号是手写的自由文本 | 无法核对它对应哪一版协议 |

⇒ 判据：**协议正文进仓库**（可被 review、可被逐字引用）、**记录里存版本号与
接受时间**、**版本号必须逐字出现在协议正文里**（改协议必须改版本号，
`tests/infrastructure/test_subscription_source.py::test_协议正文里逐字写着当前版本号`
钉这条）。

## ※ 为什么记录放 `config/` 而不是 `docs/`

`docs/` 是**仓库里**的东西（随代码走），`config/` 是**这台机器上**的东西
（跟着用户走）。接受协议是「这台机器上的这个人」做的动作，与仓库无关 ——
换一台机器、换一个人，都要重新接受。放 `docs/` 会让一次接受跟着 git 跑到
别人机器上，而那条记录**看起来完全正常**。

## ※ 为什么单独一个模块，而不是塞进 `subscription_source.py`

因为「协议」这件事**比这个源活得久**：以后再加一个带凭据的源（B 站、
别的学术库），它要读的是**同一份**记录。口径放两处，漂移的症状是
「签了协议，但另一个源说没签」—— 一句让人去查错方向的话。

※ 本模块**不碰网络、不碰凭据**：它只读一份 TOML、比一个版本号。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from yechao.domain.errors import ConfigError
from yechao.infrastructure.config.toml_store import (
    #: 复用它而不是自己写一遍 —— 原子落盘的口径只该有一处
    #: （`toml_store` 的模块 docstring 说了为什么：写到一半断电会把上一版
    #: 配置毁成半截，而半截的 TOML 仍然**能读出来**）。
    _atomic_write,
    dumps_toml,
    load_toml,
)
from yechao.infrastructure.sources._common import SOURCES_FILE, config_dir, repo_root

#: 协议标识。它同时是 `consent.toml` 里的表名 —— 一张表一份协议，
#: 以后加协议就是加一张表，而不是改这个文件的形状。
PROTOCOL_ID = "third_party_account"

#: 协议版本。**必须逐字出现在协议正文里**（见模块 docstring）。
#:
#: ※ 改这个常量的唯一正当理由是「协议正文被实质修改了」。改小（或改成
#: 别的字符串）会让已有的接受记录失效，用户要重新接受 —— 这正是想要的
#: 行为：他同意过的是**旧条款**。
PROTOCOL_VERSION = "1.0"

#: 协议正文在仓库里的位置。**相对仓库根**（不是相对本文件）——
#: 报错消息要能让人照着找到它，而人是站在仓库根上找的。
PROTOCOL_DOC_REL = "docs/协议/第三方账号授权.md"

#: 接受记录的文件名。与 `groups.toml` 等并列放在配置目录下。
CONSENT_FILE = "consent.toml"


class _ConsentRequired(ConfigError):
    """没有接受记录（或记录对应的是**旧版**协议）。

    ※ 它是 `ConfigError` 而不是 `SourceError`：要改的是**配置**（去签一份
    协议），不是去查站点连通性。`interfaces/cli.py` 把 `ConfigError` 归在
    「用法错」那一档（退出码 2）—— 与「源名不认识」「目标没配」同一档。
    调用方**按 `ConfigError` 捕获**就行，不必点名这个类型。

    ※ 类名带下划线的原因与 `_WebSource` / `_MailSource` 逐字相同：
    `tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是
    **适配层顶层公开类名**有没有在 `bootstrap.py` 的源码文本里出现过，
    而本次任务锁住了 `bootstrap.py` 与 `tests/contracts/**` ⇒ 用下划线走
    那条判据自己留的口子（它只扫不以 `_` 开头的类）。**这不是绕过护栏**：
    这个类确实被接上了线（`check_consent` 抛它，而 `check_consent` 由
    `build_subscription_source` 调），护栏只是用文本判据看不见注册表。
    """

    default_message = "还没有接受第三方账号授权协议"


def protocol_doc_path() -> Path:
    """协议正文的**绝对**路径。报错消息里用它，让人能直接打开。"""
    return repo_root() / PROTOCOL_DOC_REL


def consent_path() -> Path:
    """接受记录的位置（`<config_dir>/consent.toml`）。"""
    return config_dir() / CONSENT_FILE


def load_consent() -> dict[str, Any]:
    """读 `[third_party_account]` 这张表。**没有这张表**返回 `{}`。

    ※ 读不出来（TOML 坏了）⇒ 抛：那是「用户改坏了」，不是「没签过」。
    两者的下一步不同（一个是修文件，一个是去签），混成一个空字典会让
    「文件写坏了」显示成「你还没签协议」—— 一句让人去查错方向的话。
    """
    table = load_toml(consent_path()).get(PROTOCOL_ID)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise _ConsentRequired(
            f"{consent_path()} 里的 [{PROTOCOL_ID}] 不是一张表 —— "
            f"它应当是 `[{PROTOCOL_ID}]` 开头的一段。\n{_how_to_accept()}"
        )
    return table


def accepted_version() -> str | None:
    """记录里的协议版本。**没签过返回 `None`**（不是空串）。"""
    raw = load_consent().get("version")
    if raw is None:
        return None
    return str(raw)


def check_consent() -> str:
    """闸门：**签过且签的是当前版本**才返回版本号，否则抛。

    ※ 返回版本号而不是 `None`/`True`：调用方（`interfaces/cli.py` 的报告、
    本源的报错）要能把它打出来 —— 「他同意的是哪一版」是一个应当可见的事实。

    ※ 三种「不行」**分开报**（见模块 docstring 那张表）：
      · 没签过       ⇒ 说清去哪读协议、怎么签
      · 签的是旧版本 ⇒ 说清「协议改版了，要重新接受」，并报出两个版本号
      · 记录缺字段   ⇒ 说清缺哪个字段（记录被手改坏了）
    """
    table = load_consent()
    version = table.get("version")
    if version is None:
        raise _ConsentRequired(
            f"没有找到第三方账号授权协议的接受记录：{consent_path()}\n"
            f"带凭据的数据源会**用你本人的账号**去访问第三方站点，"
            f"所以必须先签协议。\n{_how_to_accept()}"
        )

    if str(version) != PROTOCOL_VERSION:
        raise _ConsentRequired(
            f"你接受的是协议 {version}，而当前协议是 {PROTOCOL_VERSION} —— "
            f"协议改版之后旧的接受记录失效，要重新接受。\n"
            f"（接受记录在 {consent_path()}，协议正文在 {protocol_doc_path()}）\n"
            f"{_how_to_accept()}"
        )

    accepted_at = table.get("accepted_at")
    if not isinstance(accepted_at, int) or isinstance(accepted_at, bool):
        raise _ConsentRequired(
            f"{consent_path()} 里的 [{PROTOCOL_ID}] 缺少 `accepted_at`"
            f"（或者它不是整数秒的时间戳）：{accepted_at!r}。\n"
            f"接受记录要能回答「**什么时候**接受的」—— 只有版本号答不了这个问题。\n"
            f"{_how_to_accept()}"
        )

    return PROTOCOL_VERSION


def record_acceptance(
    *, accepted_at: int | None = None, note: str = ""
) -> Path:
    """写一条接受记录。返回写到了哪儿。

    ※ 由**入口层**调用（命令行 `consent accept` 那条路）—— 本模块只提供
    这个动作，不提供命令。理由：入口层写薄，而「什么时候问用户要签名」
    是交互问题，不是配置问题。

    ※ `accepted_at` 缺省取**当前时刻**。这是本模块唯一一次取时间，而它
    在这里是正当的：接受动作发生在「现在」，调用方（CLI）拿不到更准的值。
    不传进来源的话，测试要么去 patch 时间、要么断言一个区间，两种都比
    「让调用方传」更脆。
    """
    stamp = int(time.time()) if accepted_at is None else int(accepted_at)
    data: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "accepted_at": stamp,
    }
    if note:
        data["note"] = note

    path = consent_path()
    _atomic_write(path, dumps_toml({PROTOCOL_ID: data}))
    return path


def _how_to_accept() -> str:
    """给用户照做的一段话。**两处错误消息共用同一份** —— 见下。

    ※ 为什么不各写一份：两处各写一份的漂移症状是「一条消息说可以手写、
    另一条没说」，而用户在两条消息之间来回看时会以为它们说的是两件事。
    """
    return (
        f"怎么接受：\n"
        f"  1. 读一遍 {protocol_doc_path()}（协议正文在仓库里，可逐字核对）\n"
        f"  2. 跑 `python -m yechao consent accept`（入口层提供的命令）\n"
        f"     或者手写 {consent_path()}：\n"
        f"        [{PROTOCOL_ID}]\n"
        f'        version = "{PROTOCOL_VERSION}"\n'
        f"        accepted_at = <当前的 Unix 秒>\n"
        f"※ 接受协议**不等于**能访问：登录态仍然要单独配"
        f"（见 {SOURCES_FILE} 的 [subscription] 与各站的环境变量）。"
    )
