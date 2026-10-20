"""数据源实现。

※ **接口只约束形状，不约束处理方式**：
一份会议转写是「长音视频」必须切，一封邮件是「自洽的短文本」切了反而把它
拆散。两个源为了迎合同一个下游而做同样的处理，其中一个必然是错的。

**归一化的责任在数据源**，不在接口。

## 这个文件是「有哪些源」的**唯一**答案

观察到的现象：源写出来了、端口的 docstring 写得很清楚，但**没有任何地方
能指定用哪一个** —— 装配处写死了 `[SynthSource()]`，命令行没有 `ingest`。
于是数据源层是一段**死代码**：跑得通，但没人跑它。再写三个源出来，也只是
多三段死代码。

⇒ 所以这里放一张**名字 → 工厂**的表，让「用哪个源」变成一个**能被指定**的
参数（命令行 `--source`）。加一个真源只需要在这里加一行 —— 而这是可以被
机械检查的：表里每多一项，就多一个能从命令行选中的源。

### 为什么值是**工厂**而不是实例

源是有状态的（打开的连接、游标位置、上次读到哪）。一个被两次摄取共用的
实例会把状态串起来，而症状是「第二次读到的比第一次少」—— 看起来像
「今天没消息」。每次装配现造一个，状态的生命周期就与那一次摄取对齐。

### 为什么键是**字符串**

因为这个名字要出现在命令行上（`--source meeting`），而**入口层不许 import
适配层**（`tests/contracts/test_layering.py::test_入口层不直接摸适配层`
钉着这条）。于是入口层只说名字，由这里把名字翻成实例。

### ※ 名字说的是「这个源**是什么**」，不是「这个产品是什么」

`MessageSource` 是抽象的：源可以是群聊、会议转写、邮件、合成语料。
所以表里的键也应当是**中性**的（`chat` / `meeting` / `mail` / `synth`）——
一个源的名字回答的是「它从哪儿读」，不是「这个产品只吃哪家的数据」。

反过来说：**如果某个源真的只读某个平台的本地库**，那是**它自己的性质**，
不是产品的定位 —— 那时它叫那个平台的名字就是如实的，不算把产品锚在平台上。

※ 这两句话的区别是判据的全部：看名字描述的是**源的实现**还是**产品的
适用范围**。前者可以具体到平台，后者必须中性。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from yechao.domain.errors import ConfigError
from yechao.infrastructure.sources.mail_source import build_mail_source
from yechao.infrastructure.sources.meeting_source import build_meeting_source
from yechao.infrastructure.sources.qq_source import build_qq_source
from yechao.infrastructure.sources.subscription_source import build_subscription_source
from yechao.infrastructure.sources.synth_source import SynthSource
from yechao.infrastructure.sources.video_source import build_video_source
from yechao.infrastructure.sources.web_source import build_web_source

#: 源名 → 工厂。
#:
#: ※ 键是**对外的**（会出现在命令行与文档里），改名等于换接口。
REGISTRY: dict[str, Callable[[], Any]] = {
    "synth": SynthSource,
    #: 网页通知源。※ 名字说的是「它从哪儿读」（一个网页），不是「这个产品
    #: 是干什么的」—— 见本模块 docstring 里「名字说的是这个源**是什么**」。
    #:
    #: ※ 它**不在 `DEFAULT_SOURCES` 里**，这是判据不是疏忽：默认源要在
    #: **离线、无配置**的情况下可用（`demo` 那条路的地基），而网页源需要
    #: 一个目标、还可能要走网络。默认带上它会让「什么都没配」变成
    #: 「报错」或者更糟 ——「静默读了别的东西」。
    #:
    #: ※ 它是一个**会报错的**工厂：没配目标时它抛 `ConfigError`，
    #: 而 `cli.py` 把它归在「用法错」那一档（退出码 2）。见 `build_web_source`。
    "web": build_web_source,
    #: 邮件源。※ 名字说的是「它从哪儿读」（本机的邮件文件），不是「这个产品
    #: 是干什么的」—— 见本模块 docstring 里「名字说的是这个源**是什么**」。
    #:
    #: ※ 它**不在 `DEFAULT_SOURCES` 里**，理由与网页源逐字相同：默认源要在
    #: **离线、无配置**的情况下可用（`demo` 那条路的地基），而邮件源需要一个
    #: 目标文件。默认带上它会让「什么都没配」变成「报错」。
    #:
    #: ※ 它是一个**会报错的**工厂：没配目标（或目标后缀不认）时它抛
    #: `ConfigError`，而 `cli.py` 把它归在「用法错」那一档（退出码 2）。
    #: 见 `build_mail_source`。
    "mail": build_mail_source,
    #: 会议转写源。目标是一个**目录**（转写文件）或本机客户端留下的会议历史库 ——
    #: 没配时自动探测本机落盘位置。※ 不在 `DEFAULT_SOURCES` 里，理由同上面几个。
    "meeting": build_meeting_source,
    #: 视频源。目标是本地字幕文件或一个视频页链接（含 B 站）。
    #: ※ 字幕在接口域名里，而那个域名的 robots 是 Disallow: / ——
    #: 默认完全遵守；越过要用户在配置里点名，而且越权会先落一行记录。见 `video_source`。
    "video": build_video_source,
    #: 订阅源（小红书 / 抖音 / 快手 / 微博 / 知网）。要一个目标，可能还要登录态与协议。
    #: QQ 源。读**本机 QQ 的群消息** —— 零操作那条路（QQ 开着就不用做任何事），
    #: 也认一份同形的 `.db` 夹具（评委机器上没有 QQ 时走这条）。
    #:
    #: ※ 名字叫 `qq` 是**如实的**：这个源真的只读 QQ 本地库，那是**它自己的
    #: 性质**，不是把产品锚在 QQ 上 —— 判据见本模块 docstring 里
    #: 「名字说的是这个源**是什么**」那一段。
    #:
    #: ※ 它**不在 `DEFAULT_SOURCES` 里**，理由与别的源逐字相同：默认源要在
    #: 离线、无配置的情况下可用（`demo` 那条路的地基）。而 QQ 源要么需要本机
    #: 装着 QQ，要么需要一个目标。
    "qq": build_qq_source,
    "subscription": build_subscription_source,
}

#: 不给 `--source` 时用哪些。
#:
#: 观察到的现象：`ingest` 如果**必须**显式给源，第一次跑它的人会先撞一个
#: 报错再去查有哪些源 —— 而 `demo` 那条路已经证明合成源是可用的。
#:
#: ⇒ 默认给合成源。它**不是**静默兜底：`ingest` 的每一份报告都会把实际用到
#: 的源名列出来，所以「我明明想读会议转写，怎么读的是合成语料」一眼可见。
DEFAULT_SOURCES: tuple[str, ...] = ("synth",)


def known_names() -> tuple[str, ...]:
    """全部已注册的源名（排序后）。给报错文案与文档用。"""
    return tuple(sorted(REGISTRY))


def build(names: Iterable[str]) -> list[Any]:
    """把源名翻成实例。名字不认识 ⇒ `ConfigError`，**不回落**。

    ※ **不回落**是刻意的。回落到默认源会让 `--source meeting`（还没实现）
    变成「静默读了合成语料」—— 用户拿到一份看起来成功的报告，而里面一条真
    消息都没有。这类失败比报错危险得多。
    """
    out: list[Any] = []
    for name in names:
        factory = REGISTRY.get(name)
        if factory is None:
            raise ConfigError(
                f"不认识的数据源 {name!r}；可用的源：{'、'.join(known_names())}"
            )
        out.append(factory())
    return out
